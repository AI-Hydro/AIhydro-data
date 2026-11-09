"""Static STAC rasters are served on the source pixel grid (scout finding 2026-11-09).

Offline: synthetic GeoTIFF windows laid out exactly like Copernicus GLO-30 COGs (3600 px/deg,
origin ``lon - res/2``, ``lat + 1 + res/2``) are read through the real ``stackstac.stack``
from ``file://`` hrefs. Elevation is the analytic plane ``z = A*(lat-46) + B*(lon-8)`` at pixel
centres, so any half-pixel shift, lost row or wrong lattice shows up as a value error.
"""
from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
pystac = pytest.importorskip("pystac")
pytest.importorskip("stackstac")
pytest.importorskip("rioxarray")

from rasterio.transform import Affine  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from aihydro_data.products import get_product  # noqa: E402
from aihydro_data.sources.stac import Backend  # noqa: E402

A, B = 1000.0, 100.0          # degrees of lat / lon to elevation units
N = 3600                      # GLO-30 rows per degree (and columns in the 0-50 degN band)


def _z(lon, lat):
    return A * (np.asarray(lat) - 46.0) + B * (np.asarray(lon) - 8.0)


def _tile(d, tid, lon0, lat_top, *, xfac=1, c0, r0, nx, ny, base=46.0, dtype="float32", value=None):
    """A window of a GLO-30-layout tile; centres at ``lon0 + (c0+j)*rx``, ``lat_top - (r0+i)*ry``."""
    rx, ry = xfac / N, 1.0 / N
    tr = Affine(rx, 0, lon0 - rx / 2 + c0 * rx, 0, -ry, lat_top + ry / 2 - r0 * ry)
    lon = lon0 + (c0 + np.arange(nx)) * rx
    lat = lat_top - (r0 + np.arange(ny)) * ry
    z = _z(lon[None, :], lat[:, None]) if value is None else np.full((ny, nx), value)
    path = os.path.join(str(d), f"{tid}.tif")
    with rasterio.open(path, "w", driver="GTiff", height=ny, width=nx, count=1, dtype=dtype,
                       crs="EPSG:4326", transform=tr) as ds:
        ds.write(z.astype(dtype), 1)
    it = pystac.Item(id=tid, geometry=None, bbox=[0, 0, 1, 1], datetime=dt.datetime(2021, 4, 22),
                     properties={"proj:epsg": 4326, "proj:transform": list(tr)[:6], "proj:shape": [ny, nx]})
    it.add_asset("data", pystac.Asset(href="file://" + path, media_type="image/tiff"))
    return it


def _seam_items(d):
    """N46_E007 | N46_E008 windows around the 8 degE seam, lat 46.2..46.7."""
    return [
        _tile(d, "Copernicus_DSM_COG_10_N46_00_E007_00_DEM", 7, 47.0, c0=3300, r0=1700, nx=300, ny=600),
        _tile(d, "Copernicus_DSM_COG_10_N46_00_E008_00_DEM", 8, 47.0, c0=0, r0=1700, nx=300, ny=600),
    ]


def _backend(monkeypatch, items):
    be = Backend()
    monkeypatch.setattr(be, "_assert_available", lambda: None)
    cat = SimpleNamespace(search=lambda **k: SimpleNamespace(items=lambda: iter(items)))
    monkeypatch.setattr(be, "_open_catalog", lambda url: cat)
    try:
        import planetary_computer as pc
        monkeypatch.setattr(pc, "sign", lambda item, *a, **k: item)
    except ImportError:
        pass
    return be


def _geom(bbox):
    return box(*bbox)


SEAM_BBOX = (7.95, 46.40, 8.05, 46.50)


@pytest.fixture
def seam(tmp_path, monkeypatch):
    be = _backend(monkeypatch, _seam_items(tmp_path))
    da = be.fetch_raster(get_product("GLO30_STAC"), _geom(SEAM_BBOX), "", "")
    return be, da


def test_output_is_on_the_source_lattice_with_exact_values(seam):
    _, da = seam
    assert "time" not in da.dims and da.dtype == np.float32
    v = np.asarray(da)
    assert np.isfinite(v).all()                                # 0 % NaN across the seam
    x, y = da.x.values.astype("float64"), da.y.values.astype("float64")
    # pixel centres sit on integer arcseconds (the GLO-30 post lattice), < 0.01 px off
    assert np.abs(x * N - np.round(x * N)).max() < 0.01
    assert np.abs(y * N - np.round(y * N)).max() < 0.01
    err = np.abs(v - _z(x[None, :], y[:, None])).max()
    assert err < 0.01 * A / N                                  # < 0.01 px of latitude


def test_native_count_not_the_metric_conversion_count(seam):
    _, da = seam
    # 0.1 deg at 1 arcsec is 360 pixels (361 where the box edge falls inside a cell);
    # the old 30 m / (111320 cos lat) conversion gave 257 x 257 at 46 degN.
    assert da.shape[1] in (360, 361) and da.shape[0] in (360, 361)
    x, y = da.x.values, da.y.values
    assert np.allclose(np.diff(x), 1 / N, atol=1e-9) and np.allclose(-np.diff(y), 1 / N, atol=1e-9)


def test_rioxarray_transform_has_no_half_pixel_shift(seam):
    _, da = seam
    assert da.rio.crs.to_epsg() == 4326
    tr = da.rio.transform()
    assert tr.a == pytest.approx(1 / N) and tr.e == pytest.approx(-1 / N)
    # the transform's pixel edges are on the lattice k/N - 1/(2N)
    assert abs((tr.c + 0.5 / N) * N - round((tr.c + 0.5 / N) * N)) < 0.01
    assert abs((tr.f - 0.5 / N) * N - round((tr.f - 0.5 / N) * N)) < 0.01


def test_rioxarray_clip_and_reproject_round_trip(seam):
    _, da = seam
    sub = da.rio.clip_box(7.99, 46.44, 8.01, 46.46)
    x, y = sub.x.values.astype("float64"), sub.y.values.astype("float64")
    assert np.abs(np.asarray(sub) - _z(x[None, :], y[:, None])).max() < 0.01 * A / N
    # same-grid reproject is an identity
    same = da.rio.reproject("EPSG:4326", shape=da.shape, transform=da.rio.transform())
    assert np.abs(np.asarray(same) - np.asarray(da)).max() < 1e-3
    # warp to UTM, then compare against the analytic plane at each warped pixel's lon/lat
    from pyproj import Transformer
    from rasterio.enums import Resampling
    utm = da.rio.reproject("EPSG:32632", resolution=30.0, resampling=Resampling.bilinear)
    inv = Transformer.from_crs(32632, 4326, always_xy=True)
    xs, ys = np.meshgrid(utm.x.values[100:240:20], utm.y.values[100:240:20])
    lon, lat = inv.transform(xs, ys)
    got = np.asarray(utm.sel(x=utm.x.values[100:240:20], y=utm.y.values[100:240:20]))
    assert np.nanmax(np.abs(got - _z(lon, lat))) < 0.1 * A / N      # < 0.1 px; a half-pixel shift in lat alone is 0.14 units


def test_site_resolution_records_grid_and_items(seam):
    _, da = seam
    sr = da.attrs["aihydro_site_resolution"]
    assert sr["grid"]["grid_source"] == "proj:transform" and sr["grid"]["dtype"] == "float32"
    assert sr["grid"]["nx"] == da.shape[1] and sr["grid"]["ny"] == da.shape[0]
    assert len(sr["items"]) == 2


def test_spacing_band_crossing_uses_finest_spacing_and_notes_it(tmp_path, monkeypatch):
    # N59 (1 arcsec lon) over N60 (2 arcsec lon): a request crossing 60 degN.
    items = [
        _tile(tmp_path, "N59_E008", 8, 60.0, c0=0, r0=0, nx=400, ny=200),
        _tile(tmp_path, "N60_E008", 8, 61.0, xfac=2, c0=0, r0=3400, nx=200, ny=200),
    ]
    be = _backend(monkeypatch, items)
    bbox = (8.02, 59.96, 8.08, 60.04)
    da = be.fetch_raster(get_product("GLO30_STAC"), _geom(bbox), "", "")
    assert np.isfinite(np.asarray(da)).all()
    assert np.diff(da.x.values).mean() == pytest.approx(1 / N)       # finest spacing wins
    assert any("longitude-spacing bands" in n and "bilinear" in n for n in da.attrs["aihydro_notes"])
    x, y = da.x.values.astype("float64"), da.y.values.astype("float64")
    z_ref = A * (y[:, None] - 46.0) + B * (x[None, :] - 8.0)
    assert np.abs(np.asarray(da) - z_ref).max() < 0.05 * A / N       # linear plane: bilinear is exact


def test_single_band_request_gets_no_band_note(seam):
    _, da = seam
    assert not any("spacing bands" in n for n in da.attrs.get("aihydro_notes", []))


def test_size_guard_fails_clearly_and_does_not_coarsen(tmp_path, monkeypatch):
    from aihydro_data.exceptions import FetchTooLarge
    be = _backend(monkeypatch, _seam_items(tmp_path))
    spec = get_product("GLO30_STAC")
    small = spec.model_copy(update={"backend_config": {**spec.backend_config, "stac_max_output_bytes": 100_000}})
    with pytest.raises(FetchTooLarge) as ei:
        be.fetch_raster(small, _geom(SEAM_BBOX), "", "")
    assert ei.value.code == "STAC_OUTPUT_TOO_LARGE" and "not coarsened" in ei.value.message


def test_native_resolution_is_honoured_or_recorded_as_ignored(tmp_path, monkeypatch):
    be = _backend(monkeypatch, _seam_items(tmp_path))
    da = be.fetch_raster(get_product("GLO30_STAC"), _geom(SEAM_BBOX), "", "", native_resolution=True)
    assert any("native_resolution=True: honoured" in n for n in da.attrs["aihydro_notes"])


# ── categorical (WorldCover layout): uint8, nodata 0, nearest ────────────────

def test_worldcover_is_uint8_with_zero_nodata(tmp_path, monkeypatch):
    # WorldCover tiles are 3 deg wide at 1/12000 deg; use windows near a seam with classes 10 / 40.
    def wc(tid, lon0, c0, v):
        rx = 1 / 12000
        tr = Affine(rx, 0, lon0 - rx / 2 + c0 * rx, 0, -rx, 48.0 + rx / 2)
        p = os.path.join(str(tmp_path), tid + ".tif")
        with rasterio.open(p, "w", driver="GTiff", height=240, width=240, count=1, dtype="uint8",
                           crs="EPSG:4326", transform=tr, nodata=0) as ds:
            ds.write(np.full((240, 240), v, dtype="uint8"), 1)
        it = pystac.Item(id=tid, geometry=None, bbox=[0, 0, 1, 1], datetime=dt.datetime(2021, 1, 1),
                         properties={"proj:epsg": 4326, "proj:transform": list(tr)[:6], "proj:shape": [240, 240]})
        it.add_asset("map", pystac.Asset(href="file://" + p, media_type="image/tiff"))
        return it
    items = [wc("wcA", 6, 35880, 10), wc("wcB", 9, 0, 40)]
    # window A: cols 35880..36119 of the 6 degE tile spans lon 8.99-9.01, so tiles abut at 9 degE
    items[0] = wc("wcA", 6, 35880, 10)
    be = _backend(monkeypatch, items)
    da = be.fetch_raster(get_product("ESA_WORLDCOVER_STAC"), _geom((8.995, 47.99, 9.005, 48.0)), "", "")
    v = np.asarray(da)
    assert da.dtype == np.uint8 and (v != 0).all()
    assert set(np.unique(v).tolist()) <= {10, 40} and (v == 10).any() and (v == 40).any()


# ── second defect: static basin mean ─────────────────────────────────────────

def test_basin_mean_is_nan_aware_polygon_mean_with_coverage(tmp_path, monkeypatch):
    be = _backend(monkeypatch, _seam_items(tmp_path))
    tri = Polygon([(7.97, 46.42), (8.03, 46.42), (8.0, 46.48)])
    df = be.fetch_timeseries(get_product("GLO30_STAC"), tri, "2020-01-01", "2020-01-02", "basin_mean")
    cx, cy = (7.97 + 8.03 + 8.0) / 3, (46.42 + 46.42 + 46.48) / 3
    assert df["dem"].iloc[0] == pytest.approx(float(_z(cx, cy)), rel=1e-3, abs=0.02)   # plane: mean = centroid
    q = df.attrs["aihydro_quality"]
    assert q["basin_coverage_fraction"] == 1.0 and q["mask_basis"] == "polygon"
    # the polygon mean differs from the bbox mean (the old behaviour)
    bbox_mean = float(np.nanmean(np.asarray(be.fetch_raster(get_product("GLO30_STAC"), tri, "", ""))))
    assert abs(bbox_mean - df["dem"].iloc[0]) > 0.5


def test_basin_mean_flags_partial_and_refuses_sliver(tmp_path, monkeypatch):
    from aihydro_data.exceptions import SourceUnavailable
    west_only = _seam_items(tmp_path)[:1]                       # east tile missing (dropped)
    be = _backend(monkeypatch, west_only)
    spec = get_product("GLO30_STAC")
    # 80 % of the basin is west of 8 degE -> flagged, mean of the valid part is served
    poly = box(7.96, 46.42, 8.01, 46.48)
    df = be.fetch_timeseries(spec, poly, "2020-01-01", "2020-01-02", "basin_mean")
    cov = df.attrs["aihydro_quality"]["basin_coverage_fraction"]
    assert 0.5 < cov < 1.0
    assert any("STAC_BASIN_COVERAGE" in n for n in df.attrs["aihydro_notes"])
    assert np.isfinite(df["dem"].iloc[0])
    # 10 % covered -> refused (the chain continues), never a silent NaN
    sliver = box(7.995, 46.42, 8.04, 46.48)
    with pytest.raises(SourceUnavailable) as ei:
        be.fetch_timeseries(spec, sliver, "2020-01-01", "2020-01-02", "basin_mean")
    assert ei.value.code == "STAC_LOW_COVERAGE"


# ── follow-ups: fallback grid, memory guard, WorldCover metadata shape ────────

def test_item_grid_reads_asset_level_transform_without_proj_epsg():
    """PC WorldCover items: transform on the asset, `proj:code` and no `proj:epsg`, datetime null."""
    it = pystac.Item(id="ESA_WorldCover_10m_2021_v200_N39W078", geometry=None, bbox=[-78, 39, -75, 42],
                     datetime=None, properties={"proj:code": "EPSG:4326", "start_datetime": "2021-01-01T00:00:00Z",
                                 "end_datetime": "2021-12-31T23:59:59Z"})
    it.add_asset("map", pystac.Asset(href="file:///x.tif", extra_fields={
        "proj:shape": [36000, 36000],
        "proj:transform": [8.333333333333333e-05, 0.0, -78.0, 0.0, -8.333333333333333e-05, 42.0]}))
    from aihydro_data.sources.stac import _item_epoch, _item_grid
    g = _item_grid(it, "map")
    assert g == {"xres": pytest.approx(1 / 12000), "yres": pytest.approx(1 / 12000), "x0": -78.0, "y0": 42.0}
    assert _item_epoch(it) == "2021-01-01T00:00:00Z"


def test_fallback_grid_without_proj_transform_is_per_axis_and_not_lattice_snapped():
    """Items with no proj:transform: configured resolution per axis, stackstac default snap
    (so this path keeps the half-pixel offset) and an explicit note."""
    from aihydro_data.sources.stac import _static_stack_kwargs
    bare = [SimpleNamespace(id="a", datetime="2021-04-22", properties={}, assets={})]
    kw, info, notes = _static_stack_kwargs(bare, "data", [7.95, 46.40, 8.05, 46.50], 30, 4326, False)
    xres, yres = kw["resolution"]
    assert yres == pytest.approx(30 / 111_320) and xres > yres          # separate axes, lon wider in deg
    assert kw["snap_bounds"] is True and info["grid_source"] == "configured_resolution"
    assert any("Native grid not read from the items" in n and "not lattice-snapped" in n for n in notes)
    assert kw["dtype"] == "float32" and kw["xy_coords"] == "center"      # dtype/centres still apply


def test_size_guard_counts_slices_when_several_epochs_are_held():
    from aihydro_data.exceptions import FetchTooLarge
    from aihydro_data.sources.stac import _check_output_size
    info = {"nx": 1000, "ny": 1000, "itemsize": 4}                      # 4 MB output
    cfg = {"stac_max_output_bytes": 10_000_000}
    _check_output_size(dict(info), cfg, "c", [0, 0, 1, 1], n_slices=1)  # 4 MB: fine
    _check_output_size(dict(info), cfg, "c", [0, 0, 1, 1], n_slices=2)  # 8 MB: fine
    with pytest.raises(FetchTooLarge) as ei:
        _check_output_size(dict(info), cfg, "c", [0, 0, 1, 1], n_slices=3)   # 12 MB
    assert "3 epoch slices" in ei.value.message
    assert "fetch() has no argument" in ei.value.recovery


def test_multi_epoch_fetch_hits_the_slice_aware_guard(tmp_path, monkeypatch):
    from aihydro_data.exceptions import FetchTooLarge
    base = _seam_items(tmp_path)
    old = [pystac.Item.from_dict(i.to_dict()) for i in base]
    for i in old:
        i.id += "_old"
        i.datetime = dt.datetime(2020, 4, 22)
    spec = get_product("GLO30_STAC")
    out = 360 * 361 * 4
    one = spec.model_copy(update={"backend_config": {**spec.backend_config, "stac_max_output_bytes": out * 2}})
    # single epoch (2 tiles, 1 datetime): 1 slice of ~0.5 MB passes the 1 MB cap
    _backend(monkeypatch, base).fetch_raster(one, _geom(SEAM_BBOX), "", "")
    # two epochs, 4 slices -> 4 x output > cap: refused before any read
    with pytest.raises(FetchTooLarge):
        _backend(monkeypatch, base + old).fetch_raster(one, _geom(SEAM_BBOX), "", "")
