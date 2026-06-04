import tempfile
from pathlib import Path

import pytest
import rasterio
from PIL import Image
from click.testing import CliRunner

from georeferencer.main import main, prepare_image, run_georef


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp(tmp_path):
    return tmp_path


@pytest.fixture()
def small_png(tmp_path):
    """A 100×100 white PNG on disk."""
    img = Image.new("RGB", (100, 100), color=(255, 255, 255))
    path = tmp_path / "test.png"
    img.save(str(path))
    return path


@pytest.fixture()
def four_gcps():
    """Four corner GCPs mapping a 100×100 image to a small lon/lat box."""
    return [
        {"px": 0,   "py": 0,   "lon": -80.0, "lat": 40.0},
        {"px": 100, "py": 0,   "lon": -79.0, "lat": 40.0},
        {"px": 100, "py": 100, "lon": -79.0, "lat": 39.0},
        {"px": 0,   "py": 100, "lon": -80.0, "lat": 39.0},
    ]


# ---------------------------------------------------------------------------
# prepare_image
# ---------------------------------------------------------------------------

class TestPrepareImage:
    def test_rgb_png_is_converted(self, small_png, tmp_path):
        out = prepare_image(small_png, tmp_path)
        assert out.exists()
        assert out.name == "source.png"
        img = Image.open(out)
        assert img.format == "PNG"

    def test_rgba_image_flattened_to_rgb(self, tmp_path):
        rgba = Image.new("RGBA", (50, 50), color=(0, 128, 255, 128))
        src = tmp_path / "rgba.png"
        rgba.save(str(src))
        work = tmp_path / "work"
        work.mkdir()
        out = prepare_image(src, work)
        result = Image.open(out)
        assert result.mode == "RGB"

    def test_palette_image_converted(self, tmp_path):
        pal = Image.new("P", (50, 50))
        src = tmp_path / "palette.png"
        pal.save(str(src))
        work = tmp_path / "work"
        work.mkdir()
        out = prepare_image(src, work)
        result = Image.open(out)
        assert result.mode == "RGB"


# ---------------------------------------------------------------------------
# run_georef
# ---------------------------------------------------------------------------

class TestRunGeoref:
    def test_creates_geotiff_with_correct_crs(self, small_png, four_gcps, tmp_path):
        out = tmp_path / "output.tif"
        run_georef(small_png, four_gcps, 4326, out)
        assert out.exists()
        with rasterio.open(str(out)) as ds:
            assert ds.crs.to_epsg() == 4326

    def test_output_has_pixel_data(self, small_png, four_gcps, tmp_path):
        out = tmp_path / "output.tif"
        run_georef(small_png, four_gcps, 4326, out)
        with rasterio.open(str(out)) as ds:
            data = ds.read()
        assert data.shape[1] == 100
        assert data.shape[2] == 100

    def test_epsg_3857_accepted(self, small_png, four_gcps, tmp_path):
        gcps = [
            {"px": 0,   "py": 0,   "lon": -8905559.0, "lat": 4865942.0},
            {"px": 100, "py": 0,   "lon": -8794111.0, "lat": 4865942.0},
            {"px": 100, "py": 100, "lon": -8794111.0, "lat": 4721671.0},
            {"px": 0,   "py": 100, "lon": -8905559.0, "lat": 4721671.0},
        ]
        out = tmp_path / "output_3857.tif"
        run_georef(small_png, gcps, 3857, out)
        with rasterio.open(str(out)) as ds:
            assert ds.crs.to_epsg() == 3857


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "GeoTIFF" in result.output

    def test_missing_file_exits_nonzero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["/nonexistent/file.png"])
        assert result.exit_code != 0

    def test_default_options_shown_in_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "5000" in result.output
        assert "4326" in result.output
