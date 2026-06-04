"""
georeferencer — Lightweight georeferencing tool

Usage:
    georef mymap.pdf
    georef mymap.jpg
    georef mymap.tif
    georef mymap.png --port 8080 --epsg 4326

For PDF input (optional):
    Install pdf2image extra:  pip install georeferencer[pdf]
    Or install poppler:
        macOS:  brew install poppler
        Ubuntu: apt install poppler-utils

Output:
    <input_name>_georef.tif written next to the input file
"""

import os
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import click
import json
import mimetypes
import tempfile
from flask import Flask, Response, jsonify, request, send_file
from PIL import Image
import rasterio
from rasterio.transform import from_gcps
from rasterio.control import GroundControlPoint
from rasterio.crs import CRS


# ---------------------------------------------------------------------------
# Image conversion helpers
# ---------------------------------------------------------------------------

def convert_pdf(pdf_path: Path, out_dir: Path) -> Path:
    """Convert first page of PDF to PNG. Tries pdftoppm, then pdf2image."""
    out_png = out_dir / "source.png"

    if shutil.which("pdftoppm"):
        prefix = str(out_dir / "page")
        result = subprocess.run(
            ["pdftoppm", "-r", "150", "-l", "1", "-png", str(pdf_path), prefix],
            capture_output=True,
        )
        if result.returncode == 0:
            candidates = sorted(out_dir.glob("page*.png"))
            if candidates:
                candidates[0].rename(out_png)
                return out_png
        print("pdftoppm failed:", result.stderr.decode(), file=sys.stderr)

    try:
        from pdf2image import convert_from_path
        pages = convert_from_path(str(pdf_path), dpi=150, first_page=1, last_page=1)
        if pages:
            pages[0].save(str(out_png), "PNG")
            return out_png
    except ImportError:
        pass

    raise RuntimeError(
        "Cannot convert PDF. Install poppler or pdf2image:\n"
        "  macOS:  brew install poppler\n"
        "  Ubuntu: apt install poppler-utils\n"
        "  pip:    pip install 'georeferencer[pdf]'"
    )


def prepare_image(input_path: Path, work_dir: Path) -> Path:
    """Convert any supported image to a working PNG in work_dir."""
    suffix = input_path.suffix.lower()
    out_png = work_dir / "source.png"

    if suffix == ".pdf":
        return convert_pdf(input_path, work_dir)

    img = Image.open(input_path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    elif img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    img.save(str(out_png), "PNG", optimize=False)
    return out_png


# ---------------------------------------------------------------------------
# Georeferencing
# ---------------------------------------------------------------------------

def run_georef(src_png: Path, gcps_data: list, epsg: int, output_path: Path):
    """Write a GeoTIFF from src_png using the provided GCP list."""
    with rasterio.open(str(src_png)) as src:
        width = src.width
        height = src.height
        data = src.read()
        count = src.count

    crs = CRS.from_epsg(epsg)
    gcps = [
        GroundControlPoint(
            row=g["py"],
            col=g["px"],
            x=g["lon"],
            y=g["lat"],
        )
        for g in gcps_data
    ]

    transform = from_gcps(gcps)

    profile = {
        "driver": "GTiff",
        "dtype": data.dtype,
        "width": width,
        "height": height,
        "count": count,
        "crs": crs,
        "transform": transform,
    }

    with rasterio.open(str(output_path), "w", **profile) as dst:
        dst.write(data)
        dst.update_tags(GEOREF_TOOL="georeferencer")


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>georef</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0f1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e8e8e8;
    --muted: #8b949e;
    --teal: #4ecdc4;
    --teal-dim: rgba(78,205,196,0.15);
    --danger: #f85149;
    --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", "Consolas", monospace;
  }

  html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--font-mono); font-size: 13px; }

  /* ── header ── */
  #header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 16px; height: 40px;
    background: var(--surface); border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  #header h1 { font-size: 13px; font-weight: 600; letter-spacing: 0.05em; color: var(--teal); }
  #header-right { display: flex; align-items: center; gap: 12px; }
  #gcp-count { color: var(--muted); font-size: 12px; }

  /* ── export button ── */
  #btn-export {
    padding: 5px 14px; font-size: 12px; font-family: var(--font-mono);
    border: 1px solid var(--border); border-radius: 4px;
    background: transparent; color: var(--muted); cursor: not-allowed;
    transition: all 0.15s;
  }
  #btn-export.ready {
    border-color: var(--teal); background: var(--teal); color: #0f1117;
    cursor: pointer; font-weight: 600;
  }
  #btn-export.ready:hover { background: #3bbdb5; }

  /* ── main split ── */
  #main {
    display: flex; flex: 1; overflow: hidden;
    height: calc(100vh - 40px - 140px);
  }

  .pane {
    flex: 1; display: flex; flex-direction: column;
    border-right: 1px solid var(--border); overflow: hidden;
    position: relative;
  }
  .pane:last-child { border-right: none; }

  .pane-header {
    height: 28px; padding: 0 12px;
    display: flex; align-items: center; justify-content: space-between;
    background: var(--surface); border-bottom: 1px solid var(--border);
    font-size: 11px; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase;
    flex-shrink: 0;
  }
  .pane-hint { font-size: 10px; }
  .pane-hint.active { color: var(--teal); }

  /* ── image pane ── */
  #img-viewport {
    flex: 1; overflow: hidden; cursor: crosshair; position: relative;
    background:
      linear-gradient(rgba(48,54,61,0.4) 1px, transparent 1px),
      linear-gradient(90deg, rgba(48,54,61,0.4) 1px, transparent 1px);
    background-size: 32px 32px;
    background-color: #0a0e13;
  }
  #img-stage {
    position: absolute; top: 0; left: 0;
    transform-origin: 0 0;
    will-change: transform;
  }
  #img-stage img { display: block; max-width: none; user-select: none; }
  #img-markers { position: absolute; top: 0; left: 0; pointer-events: none; }

  /* ── map pane ── */
  #map-pane { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  #map { flex: 1; }

  /* ── GCP panel ── */
  #gcp-panel {
    height: 140px; flex-shrink: 0;
    border-top: 1px solid var(--border);
    background: var(--surface);
    display: flex; flex-direction: column;
  }
  #gcp-panel-header {
    height: 24px; padding: 0 12px;
    display: flex; align-items: center;
    font-size: 10px; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase;
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  #gcp-list {
    flex: 1; overflow-y: auto; padding: 4px 0;
  }
  .gcp-row {
    display: flex; align-items: center; padding: 3px 12px; gap: 10px;
    transition: background 0.1s;
  }
  .gcp-row:hover { background: rgba(255,255,255,0.03); }
  .gcp-dot {
    width: 18px; height: 18px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 10px; font-weight: 700; color: #fff; flex-shrink: 0;
  }
  .gcp-coords { flex: 1; color: var(--muted); font-size: 11px; }
  .gcp-coords span { color: var(--text); }
  .gcp-del {
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 14px; padding: 0 2px; line-height: 1;
    transition: color 0.1s;
  }
  .gcp-del:hover { color: var(--danger); }

  /* pending row (half-pair) */
  .gcp-row.pending { opacity: 0.55; }
  .gcp-pending-label { color: var(--teal); font-size: 11px; font-style: italic; }

  /* ── SVG markers on image ── */
  .img-marker {
    position: absolute; transform: translate(-50%, -50%);
    pointer-events: none;
  }

  /* ── toast ── */
  #toast {
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(80px);
    background: #1a2b2a; border: 1px solid var(--teal); border-radius: 6px;
    padding: 10px 20px; font-size: 13px; color: var(--teal);
    display: flex; align-items: center; gap: 14px;
    transition: transform 0.3s ease;
    z-index: 9999;
  }
  #toast.show { transform: translateX(-50%) translateY(0); }
  #btn-close {
    background: none; border: 1px solid var(--teal); border-radius: 4px;
    color: var(--teal); padding: 3px 10px; cursor: pointer; font-family: var(--font-mono); font-size: 12px;
  }
  #btn-close:hover { background: var(--teal-dim); }

  /* scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<div id="header">
  <h1>georef</h1>
  <div id="header-right">
    <span id="gcp-count">0 GCPs</span>
    <button id="btn-export" disabled>Export GeoTIFF</button>
  </div>
</div>

<div id="main">
  <!-- Image pane -->
  <div class="pane" id="img-pane">
    <div class="pane-header">
      <span>Source Image</span>
      <span class="pane-hint" id="img-hint">click to place point</span>
    </div>
    <div id="img-viewport">
      <div id="img-stage">
        <img id="source-img" src="/image" draggable="false" alt="source map"/>
        <svg id="img-markers" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
    </div>
  </div>

  <!-- Map pane -->
  <div class="pane" id="map-pane">
    <div class="pane-header">
      <span>OpenStreetMap</span>
      <span class="pane-hint" id="map-hint">click to place matching point</span>
    </div>
    <div id="map"></div>
  </div>
</div>

<!-- GCP panel -->
<div id="gcp-panel">
  <div id="gcp-panel-header">Ground Control Points</div>
  <div id="gcp-list"></div>
</div>

<!-- Toast -->
<div id="toast">
  <span id="toast-msg"></span>
  <button id="btn-close">Close &amp; quit</button>
</div>

<script>
// ─────────────────────────────────────────────
//  State
// ─────────────────────────────────────────────
const GCP_COLORS = [
  '#e05c5c','#e0965c','#d4c44a','#6abf69','#4ecdc4',
  '#5c9ee0','#9b7de0','#e05cb8','#80c7a0','#c0a080',
];

let gcps = [];          // [{px,py,lon,lat}]
let pending = null;     // {source:'img'|'map', px?,py?,lon?,lat?}
let nextId = 0;

// ─────────────────────────────────────────────
//  Leaflet map
// ─────────────────────────────────────────────
const map = L.map('map', { zoomControl: true }).setView([20, 0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://openstreetmap.org">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

const mapMarkers = {};   // id → L.marker
let pendingMapMarker = null;

function makeLeafletIcon(color, label) {
  return L.divIcon({
    className: '',
    html: `<div style="
      width:22px;height:22px;border-radius:50%;
      background:${color};border:2px solid #fff;
      display:flex;align-items:center;justify-content:center;
      font-size:11px;font-weight:700;color:#fff;
      font-family:ui-monospace,monospace;
      box-shadow:0 1px 4px rgba(0,0,0,0.5);
    ">${label}</div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

map.on('click', function(e) {
  const lon = e.latlng.lng;
  const lat = e.latlng.lat;

  if (pending === null) {
    pending = { source: 'map', lon, lat };
    if (pendingMapMarker) map.removeLayer(pendingMapMarker);
    pendingMapMarker = L.circleMarker([lat, lon], {
      radius: 8, color: '#4ecdc4', weight: 2, fillColor: '#4ecdc4', fillOpacity: 0.25,
    }).addTo(map);
    updateHints();
    renderGCPList();
  } else if (pending.source === 'img') {
    pending.lon = lon;
    pending.lat = lat;
    commitPair();
  } else if (pending.source === 'map') {
    pending.lon = lon;
    pending.lat = lat;
    if (pendingMapMarker) map.removeLayer(pendingMapMarker);
    pendingMapMarker = L.circleMarker([lat, lon], {
      radius: 8, color: '#4ecdc4', weight: 2, fillColor: '#4ecdc4', fillOpacity: 0.25,
    }).addTo(map);
    renderGCPList();
  }
});

// ─────────────────────────────────────────────
//  Image pan / zoom
// ─────────────────────────────────────────────
const viewport = document.getElementById('img-viewport');
const stage = document.getElementById('img-stage');
const sourceImg = document.getElementById('source-img');

let scale = 1, tx = 0, ty = 0;
let isDragging = false, dragStartX, dragStartY, dragTx, dragTy;

function applyTransform() {
  stage.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  stage.style.transformOrigin = '0 0';
  const svgEl = document.getElementById('img-markers');
  svgEl.style.width = sourceImg.naturalWidth + 'px';
  svgEl.style.height = sourceImg.naturalHeight + 'px';
}

sourceImg.addEventListener('load', () => {
  const vw = viewport.clientWidth;
  const vh = viewport.clientHeight;
  const iw = sourceImg.naturalWidth;
  const ih = sourceImg.naturalHeight;
  scale = Math.min(vw / iw, vh / ih) * 0.9;
  tx = (vw - iw * scale) / 2;
  ty = (vh - ih * scale) / 2;
  applyTransform();
  const svgEl = document.getElementById('img-markers');
  svgEl.setAttribute('width', iw);
  svgEl.setAttribute('height', ih);
});

viewport.addEventListener('wheel', (e) => {
  e.preventDefault();
  const rect = viewport.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  const newScale = Math.max(0.05, Math.min(30, scale * factor));
  tx = mx - (mx - tx) * (newScale / scale);
  ty = my - (my - ty) * (newScale / scale);
  scale = newScale;
  applyTransform();
}, { passive: false });

viewport.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  isDragging = false;
  dragStartX = e.clientX;
  dragStartY = e.clientY;
  dragTx = tx; dragTy = ty;
  viewport.style.cursor = 'grabbing';

  const onMove = (e2) => {
    const dx = e2.clientX - dragStartX;
    const dy = e2.clientY - dragStartY;
    if (Math.abs(dx) + Math.abs(dy) > 3) isDragging = true;
    tx = dragTx + dx;
    ty = dragTy + dy;
    applyTransform();
  };
  const onUp = (e2) => {
    viewport.style.cursor = 'crosshair';
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    if (!isDragging) handleImageClick(e2);
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}, false);

function handleImageClick(e) {
  const rect = viewport.getBoundingClientRect();
  const vx = e.clientX - rect.left;
  const vy = e.clientY - rect.top;
  const px = Math.round((vx - tx) / scale);
  const py = Math.round((vy - ty) / scale);

  if (px < 0 || py < 0 || px > sourceImg.naturalWidth || py > sourceImg.naturalHeight) return;

  if (pending === null) {
    pending = { source: 'img', px, py };
    renderPendingImgMarker();
    updateHints();
    renderGCPList();
  } else if (pending.source === 'map') {
    pending.px = px;
    pending.py = py;
    commitPair();
  } else if (pending.source === 'img') {
    pending.px = px;
    pending.py = py;
    renderPendingImgMarker();
    renderGCPList();
  }
}

// ─────────────────────────────────────────────
//  Pending + committed markers
// ─────────────────────────────────────────────
let pendingImgMarkerEl = null;

function renderPendingImgMarker() {
  if (pendingImgMarkerEl) pendingImgMarkerEl.remove();
  if (!pending || pending.px === undefined) return;
  const el = makeSvgMarker(pending.px, pending.py, '#4ecdc4', '?', true);
  document.getElementById('img-markers').appendChild(el);
  pendingImgMarkerEl = el;
}

function makeSvgMarker(px, py, color, label, isPending) {
  const g = document.createElementNS('http://www.w3.org/2000/svg','g');
  const circle = document.createElementNS('http://www.w3.org/2000/svg','circle');
  circle.setAttribute('cx', px);
  circle.setAttribute('cy', py);
  circle.setAttribute('r', '11');
  circle.setAttribute('fill', isPending ? 'rgba(78,205,196,0.2)' : color);
  circle.setAttribute('stroke', color);
  circle.setAttribute('stroke-width', '2');
  g.appendChild(circle);
  const text = document.createElementNS('http://www.w3.org/2000/svg','text');
  text.setAttribute('x', px);
  text.setAttribute('y', py);
  text.setAttribute('text-anchor','middle');
  text.setAttribute('dominant-baseline','central');
  text.setAttribute('fill','#fff');
  text.setAttribute('font-size','10');
  text.setAttribute('font-weight','700');
  text.setAttribute('font-family','ui-monospace,monospace');
  text.textContent = label;
  g.appendChild(text);
  return g;
}

function redrawImageMarkers() {
  const svg = document.getElementById('img-markers');
  Array.from(svg.querySelectorAll('.committed')).forEach(el => el.remove());
  gcps.forEach((g, i) => {
    const color = GCP_COLORS[i % GCP_COLORS.length];
    const m = makeSvgMarker(g.px, g.py, color, i + 1, false);
    m.classList.add('committed');
    svg.appendChild(m);
  });
}

function redrawMapMarkers() {
  Object.values(mapMarkers).forEach(m => map.removeLayer(m));
  for (const k in mapMarkers) delete mapMarkers[k];
  gcps.forEach((g, i) => {
    const color = GCP_COLORS[i % GCP_COLORS.length];
    const marker = L.marker([g.lat, g.lon], {
      icon: makeLeafletIcon(color, i + 1),
    }).addTo(map);
    mapMarkers[i] = marker;
  });
}

// ─────────────────────────────────────────────
//  Commit pair
// ─────────────────────────────────────────────
function commitPair() {
  gcps.push({ px: pending.px, py: pending.py, lon: pending.lon, lat: pending.lat });
  pending = null;
  if (pendingImgMarkerEl) { pendingImgMarkerEl.remove(); pendingImgMarkerEl = null; }
  if (pendingMapMarker) { map.removeLayer(pendingMapMarker); pendingMapMarker = null; }
  redrawImageMarkers();
  redrawMapMarkers();
  renderGCPList();
  updateHints();
  updateExportButton();
}

// ─────────────────────────────────────────────
//  GCP list
// ─────────────────────────────────────────────
function renderGCPList() {
  const list = document.getElementById('gcp-list');
  list.innerHTML = '';

  gcps.forEach((g, i) => {
    const color = GCP_COLORS[i % GCP_COLORS.length];
    const row = document.createElement('div');
    row.className = 'gcp-row';
    row.innerHTML = `
      <div class="gcp-dot" style="background:${color}">${i+1}</div>
      <div class="gcp-coords">
        img <span>(${g.px}, ${g.py})</span>
        &nbsp;↔&nbsp;
        map <span>${g.lat.toFixed(5)}°, ${g.lon.toFixed(5)}°</span>
      </div>
      <button class="gcp-del" data-i="${i}" title="Remove">×</button>
    `;
    list.appendChild(row);
  });

  if (pending) {
    const row = document.createElement('div');
    row.className = 'gcp-row pending';
    let desc = '';
    if (pending.source === 'img') {
      desc = `img (${pending.px}, ${pending.py}) &nbsp;↔&nbsp; <span class="gcp-pending-label">waiting for map click…</span>`;
    } else {
      desc = `<span class="gcp-pending-label">waiting for image click…</span> &nbsp;↔&nbsp; map ${pending.lat.toFixed(5)}°, ${pending.lon.toFixed(5)}°`;
    }
    row.innerHTML = `
      <div class="gcp-dot" style="background:#4ecdc4;opacity:0.5">?</div>
      <div class="gcp-coords">${desc}</div>
    `;
    list.appendChild(row);
  }

  list.querySelectorAll('.gcp-del').forEach(btn => {
    btn.addEventListener('click', () => {
      const i = parseInt(btn.dataset.i);
      gcps.splice(i, 1);
      redrawImageMarkers();
      redrawMapMarkers();
      renderGCPList();
      updateExportButton();
    });
  });

  document.getElementById('gcp-count').textContent = `${gcps.length} GCP${gcps.length !== 1 ? 's' : ''}`;
}

// ─────────────────────────────────────────────
//  Hints
// ─────────────────────────────────────────────
function updateHints() {
  const imgHint = document.getElementById('img-hint');
  const mapHint = document.getElementById('map-hint');
  imgHint.className = 'pane-hint';
  mapHint.className = 'pane-hint';

  if (!pending) {
    imgHint.textContent = 'click to place point';
    mapHint.textContent = 'click to place point';
  } else if (pending.source === 'img') {
    imgHint.textContent = `point placed at (${pending.px}, ${pending.py})`;
    mapHint.textContent = 'now click matching location on map ←';
    mapHint.className = 'pane-hint active';
  } else {
    imgHint.textContent = 'now click matching location on image ←';
    imgHint.className = 'pane-hint active';
    mapHint.textContent = `point placed at ${pending.lat.toFixed(4)}°, ${pending.lon.toFixed(4)}°`;
  }
}

// ─────────────────────────────────────────────
//  Export
// ─────────────────────────────────────────────
function updateExportButton() {
  const btn = document.getElementById('btn-export');
  if (gcps.length >= 3) {
    btn.disabled = false;
    btn.classList.add('ready');
  } else {
    btn.disabled = true;
    btn.classList.remove('ready');
  }
}

document.getElementById('btn-export').addEventListener('click', async () => {
  if (gcps.length < 3) return;
  const btn = document.getElementById('btn-export');
  btn.textContent = 'Exporting…';
  btn.disabled = true;

  try {
    const res = await fetch('/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gcps, epsg: EPSG }),
    });
    const data = await res.json();
    if (res.ok) {
      const fname = data.output.split('/').pop();
      document.getElementById('toast-msg').textContent = `✓ Saved as ${fname}`;
      document.getElementById('toast').classList.add('show');
    } else {
      alert('Export failed: ' + (data.error || 'unknown error'));
      btn.textContent = 'Export GeoTIFF';
      btn.disabled = false;
      btn.classList.add('ready');
    }
  } catch(e) {
    alert('Export failed: ' + e.message);
    btn.textContent = 'Export GeoTIFF';
    btn.disabled = false;
    btn.classList.add('ready');
  }
});

document.getElementById('btn-close').addEventListener('click', async () => {
  await fetch('/shutdown', { method: 'POST' }).catch(() => {});
  window.close();
});

const EPSG = __EPSG__;

updateHints();
updateExportButton();
renderGCPList();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["PROPAGATE_EXCEPTIONS"] = True

_source_png: Path = None
_output_path: Path = None
_epsg: int = 4326


@app.route("/")
def index():
    html = HTML_TEMPLATE.replace("__EPSG__", str(_epsg))
    return Response(html, mimetype="text/html")


@app.route("/image")
def serve_image():
    return send_file(str(_source_png), mimetype="image/png", conditional=True)


@app.route("/export", methods=["POST"])
def export():
    body = request.get_json(force=True)
    gcps_data = body.get("gcps", [])
    epsg = int(body.get("epsg", _epsg))

    if len(gcps_data) < 3:
        return jsonify({"error": "Need at least 3 GCPs"}), 400

    try:
        run_georef(_source_png, gcps_data, epsg, _output_path)
        return jsonify({"output": str(_output_path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/shutdown", methods=["POST"])
def shutdown():
    def _kill():
        import time
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.option("--port", default=5000, show_default=True, help="Local server port")
@click.option("--epsg", default=4326, show_default=True, help="Output CRS EPSG code")
def main(input, port, epsg):
    """Lightweight georeferencing tool — place GCPs on the source image and map, then export a GeoTIFF."""
    global _source_png, _output_path, _epsg

    input_path = Path(input).resolve()
    _epsg = epsg
    _output_path = input_path.parent / (input_path.stem + "_georef.tif")

    work_dir = Path(tempfile.mkdtemp(prefix="georef_"))
    click.echo(f"Working directory: {work_dir}")

    try:
        click.echo(f"Preparing image: {input_path.name} …")
        _source_png = prepare_image(input_path, work_dir)
        click.echo(f"Source PNG ready: {_source_png} ({_source_png.stat().st_size // 1024} KB)")
    except Exception as e:
        raise click.ClickException(f"Error preparing image: {e}")

    url = f"http://localhost:{port}"
    click.echo(f"\nStarting server at {url}")
    click.echo(f"Output will be: {_output_path}")
    click.echo("Press Ctrl+C to quit.\n")

    def _open_browser():
        import time
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        click.echo("\nShutting down.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
