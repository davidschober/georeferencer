# georeferencer

A lightweight browser-based tool for georeferencing raster images. Open a map image on the left, click matching points on OpenStreetMap on the right, and export a GeoTIFF.

![screenshot placeholder](docs/screenshot.png)

## Installation

**Via uv (recommended):**

```bash
uv tool install git+https://github.com/davidschober/georeferencer
```

This installs the `georef` command globally. Requires [uv](https://docs.astral.sh/uv/).

**One-shot with uvx (no install):**

```bash
uvx --from git+https://github.com/davidschober/georeferencer georef mymap.jpg
```

**PDF support** requires poppler (for best results) or the `pdf2image` extra:

```bash
# macOS
brew install poppler

# Ubuntu/Debian
apt install poppler-utils

# Or install the Python extra instead
uv tool install "georeferencer[pdf] @ git+https://github.com/davidschober/georeferencer"
```

## Usage

```
georef [OPTIONS] INPUT
```

`INPUT` can be a JPG, PNG, TIF, or PDF file.

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `5000` | Local server port |
| `--epsg` | `4326` | Output coordinate reference system (EPSG code) |
| `--help` | | Show help and exit |

**Examples:**

```bash
# Basic usage — opens browser at http://localhost:5000
georef mymap.jpg

# Use a different port
georef mymap.jpg --port 8080

# Output in Web Mercator instead of WGS 84
georef mymap.tif --epsg 3857

# Georeference a scanned PDF
georef scan.pdf
```

## How it works

1. Run `georef` with your image — a browser window opens automatically.
2. Click a recognizable point on the **source image** (left pane).
3. Click the **matching location** on the OpenStreetMap (right pane). This completes a Ground Control Point (GCP).
4. Repeat until you have at least 3 GCPs. More GCPs = better accuracy.
5. Click **Export GeoTIFF**. The output is written next to your input file as `<name>_georef.tif`.
6. Click **Close & quit** to shut down the server.

GCPs can be deleted from the panel at the bottom of the screen.

## Output

The georeferenced file is saved as `<input_name>_georef.tif` in the same directory as the input. It is a GeoTIFF with the CRS set to the requested EPSG code.

## Development

```bash
git clone https://github.com/davidschober/georeferencer
cd georeferencer
uv venv && uv pip install -e ".[dev]"
pytest
```
