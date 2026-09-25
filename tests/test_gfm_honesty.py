"""Offline outage and real-raster support tests; not a live GFM validation."""
from __future__ import annotations

import io
import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from aihydro_data.exceptions import SourceUnavailable
from aihydro_data.flood import gfm, gfm_stac

BOUNDS = [0, 0, 1, 1]
DAY = "2024-01-01"


def item(href, name="tile"):
    return {"id": name, "properties": {"datetime": DAY + "T12:00:00Z"},
            "assets": {gfm_stac.GFM_FLOOD_ASSET: {"href": str(href)}}}


def raster(tmp_path, values, name="mask", crs="EPSG:4326", bounds=BOUNDS):
    path = tmp_path / f"{name}.tif"
    values = np.asarray(values, dtype="uint8")
    with rasterio.open(path, "w", driver="GTiff", width=values.shape[1], height=values.shape[0],
                       count=1, dtype="uint8", crs=crs, nodata=255,
                       transform=from_bounds(*bounds, values.shape[1], values.shape[0])) as dst:
        dst.write(values, 1)
    return path


def test_outage_never_creates_fixture(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr(gfm_stac, "fetch_gfm_stac_geojson", fail)
    monkeypatch.setattr(gfm, "fixture_gfm_geojson", lambda *args: pytest.fail("synthetic fallback"))
    with pytest.raises(SourceUnavailable, match="GFM_FETCH_FAILED"):
        gfm.fetch_gfm_extent(BOUNDS, DAY)
    with pytest.raises(SourceUnavailable, match="GFM_NETWORK_DISABLED"):
        gfm.fetch_gfm_extent(BOUNDS, DAY, allow_network=False)


def test_explicit_fixture_not_observational():
    result = gfm.fetch_gfm_extent(BOUNDS, DAY, use_fixture=True)
    assert result["synthetic"] and not result["validation_ready"]
    assert result["citation"] == ""
    assert result["geojson"]["features"][0]["properties"]["synthetic"]


def test_no_acquisitions_not_no_flood(monkeypatch):
    monkeypatch.setattr(gfm_stac, "search_gfm_items", lambda *a, **kw: [])
    result = gfm.fetch_gfm_extent(BOUNDS, DAY)
    assert result["status"] == "no_observations"
    assert not result["validation_ready"]


@pytest.mark.parametrize("values,status,valid", [([[255, 255], [255, 255]], "no_valid_pixels", 0),
                                                  ([[0, 255], [0, 0]], "no_flood_detected", 3),
                                                  ([[1, 255], [0, 1]], "flood_detected", 3)])
def test_real_raster_distinguishes_support(tmp_path, monkeypatch, values, status, valid):
    path = raster(tmp_path, values)
    monkeypatch.setattr(gfm_stac, "search_gfm_items", lambda *a, **kw: [item(path)])
    result = gfm.fetch_gfm_extent(BOUNDS, DAY)
    assert result["status"] == status
    assert result["valid_pixel_count"] == valid
    assert result["n_assets_read"] == 1
    assert result["items"][0]["datetime"] == DAY + "T12:00:00Z"
    assert not result["validation_ready"]
    assert bool(result["geojson"]["features"]) == (status == "flood_detected")


@pytest.mark.parametrize("partial", [False, True])
def test_failed_assets_never_no_flood(tmp_path, monkeypatch, partial):
    items = [item(tmp_path / "missing.tif")]
    if partial:
        items.append(item(raster(tmp_path, [[1, 0], [0, 0]]), "valid"))
    monkeypatch.setattr(gfm_stac, "search_gfm_items", lambda *a, **kw: items)
    with pytest.raises(SourceUnavailable) as caught:
        gfm.fetch_gfm_extent(BOUNDS, DAY)
    assert caught.value.code == "GFM_ASSET_READ_FAILED"
    assert caught.value.details["n_assets_read"] == int(partial)
    assert caught.value.details["failures"][0]["id"] == "tile"


def test_missing_asset_is_failure(monkeypatch):
    monkeypatch.setattr(gfm_stac, "search_gfm_items", lambda *a, **kw: [{"id": "missing", "assets": {}}])
    with pytest.raises(SourceUnavailable, match="GFM_ASSET_READ_FAILED"):
        gfm.fetch_gfm_extent(BOUNDS, DAY)


def test_search_does_not_silently_truncate(monkeypatch):
    monkeypatch.setattr(gfm_stac, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps({
        "features": [item("test")], "links": [{"rel": "next", "href": "next"}]}).encode()))
    with pytest.raises(SourceUnavailable, match="GFM_SEARCH_TRUNCATED"):
        gfm_stac.search_gfm_items(BOUNDS, DAY)


def test_tiles_reprojected_before_union(tmp_path, monkeypatch):
    from pyproj import Transformer
    from shapely.geometry import shape
    transform = Transformer.from_crs(4326, 3857, always_xy=True)
    x, y = transform.transform(1, 1)
    paths = [raster(tmp_path, [[1, 1], [1, 1]], name="geo"),
             raster(tmp_path, [[1, 1], [1, 1]], name="merc", crs="EPSG:3857", bounds=[0, 0, x, y])]
    monkeypatch.setattr(gfm_stac, "search_gfm_items", lambda *a, **kw: [item(p, str(n)) for n, p in enumerate(paths)])
    result = gfm.fetch_gfm_extent(BOUNDS, DAY)
    geom = shape(result["geojson"]["features"][0]["geometry"])
    assert geom.bounds == pytest.approx(BOUNDS, abs=1e-6)
    assert geom.area == pytest.approx(1, abs=1e-6)
    assert result["n_assets_read"] == 2
