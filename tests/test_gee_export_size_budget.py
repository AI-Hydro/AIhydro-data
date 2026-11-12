"""EE request-size model, bounded coarsen-and-retry, and the explicit ``scale`` parameter.

DEMO-1 finding F10a: for basin 01013500 the auto-coarsened "33 m" Landsat 8 NDVI
request was 57.8 MB against EE's 50,331,648-byte limit and failed.

Offline: ``ee`` and ``urlretrieve`` are mocked. ``_true_ee_bytes`` below is an
independent statement of how EE sizes a request (degree grid, 111,319.49 m per
degree on both axes, region snapped outward to a pixel grid with an arbitrary
origin, bytes = cols x rows x sum of band widths); it does not call the code
under test.
"""
from __future__ import annotations

import inspect
import math
import sys
import types
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr

from aihydro_data.exceptions import SourceUnavailable
from aihydro_data.sources.gee import Backend
from aihydro_data.sources.gee._download import _DownloadMixin

LIMIT = 50_331_648
M_PER_DEG = 2 * math.pi * 6_378_137.0 / 360.0


def _true_ee_bytes(bounds, scale_m, n_bands, bpp, origin_frac=(0.0, 0.0)):
    """Raster bytes EE would build: pixel grid of ``scale_m / M_PER_DEG`` degrees whose
    origin is offset by ``origin_frac`` of a pixel; the region is snapped outward."""
    minx, miny, maxx, maxy = bounds
    px = scale_m / M_PER_DEG
    ox, oy = origin_frac[0] * px, origin_frac[1] * px
    cols = math.ceil((maxx - ox) / px) - math.floor((minx - ox) / px)
    rows = math.ceil((maxy - oy) / px) - math.floor((miny - oy) / px)
    return cols * rows * n_bands * bpp


def _box(lat_mid, w_deg, h_deg, lon0=-68.0):
    return (lon0, lat_mid - h_deg / 2, lon0 + w_deg, lat_mid + h_deg / 2)


def _roi(bounds):
    minx, miny, maxx, maxy = bounds
    # >5 vertices so the polygon-mask step is not skipped as a plain bbox
    return {"type": "Polygon", "coordinates": [[
        [minx, miny], [maxx, miny], [maxx, maxy], [(minx + maxx) / 2, maxy + 0.001],
        [minx, maxy], [minx, miny]]]}


# Fish River (USGS 01013500) box documented in the 0.2.10 CHANGELOG (GridMET probe).
FISH_RIVER = _box(47.24, 0.893, 0.632, lon0=-69.2)


# ── 1. estimate vs a synthetic true size ──────────────────────────────────────

@pytest.mark.parametrize("lat", [0.0, 35.0, 47.24, 60.0])
@pytest.mark.parametrize("w_deg,h_deg", [(0.3, 0.3), (0.893, 0.632), (2.0, 1.2)])
@pytest.mark.parametrize("n_bands", [1, 2, 4, 7])
def test_planned_scale_fits_true_size_float64_bands(lat, w_deg, h_deg, n_bands):
    """Default planning (no dtype known) must hold for the widest EE type, 8 B/band."""
    b = _box(lat, w_deg, h_deg)
    s = _DownloadMixin._coarsen_scale_for_budget(
        None, _roi(b), n_bands=n_bands, scale_m=30.0)
    for frac in [(0.0, 0.0), (0.5, 0.5), (0.99, 0.01)]:
        assert _true_ee_bytes(b, s, n_bands, 8, frac) <= LIMIT, (s, frac)


@pytest.mark.parametrize("bpp", [1, 2, 4, 8])
@pytest.mark.parametrize("n_bands", [1, 3, 6])
def test_planned_scale_fits_true_size_for_declared_dtype(bpp, n_bands):
    from aihydro_data.sources.gee._download import estimate_request_bytes
    b = FISH_RIVER
    s = _DownloadMixin._coarsen_scale_for_budget(
        None, _roi(b), n_bands=n_bands, scale_m=30.0, bytes_per_pixel=bpp)
    assert _true_ee_bytes(b, s, n_bands, bpp, (0.3, 0.7)) <= LIMIT
    # the model must not under-state the synthetic truth, at any scale
    for sc in (30, 60, 120):
        assert estimate_request_bytes(b, sc, n_bands, bpp) >= _true_ee_bytes(b, sc, n_bands, bpp, (0.9, 0.9))


def test_narrower_dtype_allows_finer_scale():
    b = FISH_RIVER
    s8 = _DownloadMixin._coarsen_scale_for_budget(None, _roi(b), n_bands=1, scale_m=30.0)
    s2 = _DownloadMixin._coarsen_scale_for_budget(None, _roi(b), n_bands=1, scale_m=30.0,
                                                  bytes_per_pixel=2)
    assert s2 < s8


def test_f10a_fish_river_old_choice_exceeded_the_limit_and_new_choice_fits():
    """The old planner chose 33 m for this box and one band; the truth at 33 m is above
    EE's limit (the rehearsal reported 57.8 MB for the request EE actually saw)."""
    assert _true_ee_bytes(FISH_RIVER, 33, 1, 8) > LIMIT
    s = _DownloadMixin._coarsen_scale_for_budget(None, _roi(FISH_RIVER), n_bands=1, scale_m=30.0)
    assert s > 33
    assert _true_ee_bytes(FISH_RIVER, s, 1, 8) <= LIMIT


def test_no_cos_lat_term_a_wide_high_latitude_box_is_not_discounted():
    """EE's degree grid has the same pixel count per square degree at every latitude."""
    from aihydro_data.sources.gee._download import estimate_request_bytes
    eq = estimate_request_bytes(_box(0.0, 1.0, 1.0), 60, 1)
    hi = estimate_request_bytes(_box(60.0, 1.0, 1.0), 60, 1)
    assert hi == eq


def test_never_refines_a_scale_that_already_fits():
    b = _box(40.0, 0.05, 0.05)
    assert _DownloadMixin._coarsen_scale_for_budget(None, _roi(b), n_bands=1, scale_m=30.0) == 30.0


# ── 2. coarsen-and-retry with a mocked EE size error ─────────────────────────

class _Dl(_DownloadMixin):
    def _open_geotiff(self, tmp):
        da = xr.DataArray(np.ones((1, 3, 3)), dims=("band", "y", "x"),
                          coords={"band": [1], "y": [3.0, 2.0, 1.0], "x": [1.0, 2.0, 3.0]})
        return da


def _size_msg(nbytes=57_800_000):
    return (f"Total request size ({nbytes} bytes) must be less than or equal to "
            f"{LIMIT} bytes.")


class _Img:
    """getDownloadURL raises EE's size error while scale < ok_from."""
    def __init__(self, ok_from=0.0, exc=None):
        self.scales, self.ok_from, self.exc = [], ok_from, exc

    def getDownloadURL(self, params):  # noqa: N802
        self.scales.append(params["scale"])
        if self.exc is not None:
            raise self.exc
        if params["scale"] < self.ok_from:
            raise RuntimeError(_size_msg())
        return "http://example.invalid/x.tif"


@pytest.fixture
def no_net():
    with patch("urllib.request.urlretrieve", lambda url, tmp: open(tmp, "wb").close()), \
            patch.object(_DownloadMixin, "_clip_to_polygon", staticmethod(lambda da, r: da)):
        yield


SMALL = _roi(_box(40.0, 0.05, 0.05))   # planner leaves the scale alone: failures are EE-side


def test_retry_coarsens_until_accepted_and_records_every_attempt(no_net):
    img = _Img(ok_from=60.0)
    da, final = _Dl()._download_image_array(img, "roi", SMALL, ["ndvi"], 30.0)
    assert final >= 60.0 and img.scales[-1] == final
    assert img.scales == sorted(img.scales) and len(set(img.scales)) == len(img.scales)
    rec = da.attrs["aihydro_upstream"]["gee_export"]
    assert rec["requested_scale_m"] == 30.0 and rec["final_scale_m"] == final
    assert [a["scale_m"] for a in rec["attempts"]] == img.scales
    assert [a["outcome"] for a in rec["attempts"]] == ["ee_size_error"] * (len(img.scales) - 1) + ["ok"]
    assert rec["attempts"][0]["ee_reported_bytes"] == 57_800_000
    assert any("not the requested 30 m" in n for n in da.attrs["aihydro_notes"])


def test_retry_is_bounded_and_failure_carries_the_record(no_net):
    img = _Img(ok_from=1e9)      # never accepts
    with pytest.raises(SourceUnavailable) as ei:
        _Dl()._download_image_array(img, "roi", SMALL, ["ndvi"], 30.0, max_retries=3)
    assert len(img.scales) == 4                      # 1 try + exactly 3 retries
    assert ei.value.code == "GEE_REQUEST_TOO_LARGE"
    assert len(ei.value.details["attempts"]) == 4
    assert all(a["outcome"] == "ee_size_error" for a in ei.value.details["attempts"])


def test_default_retry_bound_is_three(no_net):
    img = _Img(ok_from=1e9)
    with pytest.raises(SourceUnavailable):
        _Dl()._download_image_array(img, "roi", SMALL, ["ndvi"], 30.0)
    assert len(img.scales) == 4


def test_only_the_size_error_is_retried(no_net):
    img = _Img(exc=RuntimeError("Computation timed out."))
    with pytest.raises(RuntimeError, match="timed out"):
        _Dl()._download_image_array(img, "roi", SMALL, ["ndvi"], 30.0)
    assert len(img.scales) == 1


def test_no_retry_and_no_note_when_first_request_is_accepted(no_net):
    img = _Img()
    da, final = _Dl()._download_image_array(img, "roi", SMALL, ["ndvi"], 30.0)
    rec = da.attrs["aihydro_upstream"]["gee_export"]
    assert final == 30.0 and len(rec["attempts"]) == 1 and "aihydro_notes" not in da.attrs


# ── 3. explicit scale parameter (additive) ───────────────────────────────────

def test_scale_parameter_is_on_every_entry_point_and_defaults_to_none():
    from aihydro_data import _pipeline
    for fn in (_pipeline.fetch, _pipeline.fetch_batch, _pipeline._fetch_one,
               Backend.fetch_raster, Backend.fetch_multiband_composite,
               Backend.fetch_index_composite, Backend._fetch_soilgrids_raster):
        p = inspect.signature(fn).parameters["scale"]
        assert p.default is None


def test_invalid_scale_is_rejected_before_any_work():
    from aihydro_data import fetch
    from aihydro_data.exceptions import GeometryInvalid
    for bad in (0, -5, float("nan"), float("inf"), "30", True):
        with pytest.raises(GeometryInvalid) as ei:
            fetch("precipitation", (-69.0, 47.0), "2000-01-01", "2000-01-02", scale=bad)
        assert ei.value.code == "SCALE_INVALID"


def test_fetch_raster_uses_explicit_scale_over_the_product_scale():
    seen = {}

    def fake_download(self, image, roi, roi_geojson, bands, scale_m, **kw):
        seen["scale_m"] = scale_m
        return xr.DataArray(np.ones((1, 2, 2)), dims=("band", "y", "x")), scale_m

    ee = types.ModuleType("ee")
    ee.Geometry = lambda g: g
    ee.Image = lambda d: types.SimpleNamespace(select=lambda b: "img")
    spec = types.SimpleNamespace(
        backend_config={"static": True, "gee_dataset_id": "X", "band": "b", "scale_m": 30},
        source_dataset_id="X", id="P", variable="v")
    be = Backend()
    with patch.dict(sys.modules, {"ee": ee}), \
            patch.object(Backend, "_assert_available", lambda self: None), \
            patch.object(Backend, "_geom_to_geojson", staticmethod(lambda g: SMALL)), \
            patch.object(Backend, "_download_image_array", fake_download):
        be.fetch_raster(spec, object(), "", "")
        assert seen["scale_m"] == 30.0
        be.fetch_raster(spec, object(), "", "", scale=90)
        assert seen["scale_m"] == 90.0


class _KeyStop(BaseException):
    pass


def _key_payload(**kw):
    """Run the real fetch() up to the cache-key builder and return its payload."""
    from unittest.mock import patch as _p
    from shapely.geometry import box
    import aihydro_data.cache as cache_mod
    from aihydro_data import fetch

    def spy(payload):
        spy.payload = payload
        raise _KeyStop

    with _p.object(cache_mod, "cache_key", spy):
        try:
            fetch(**{"variable": "precipitation", "geometry": box(-69.2, 46.9, -68.3, 47.5),
                     "start": "2000-01-01", "end": "2000-01-05", **kw})
        except _KeyStop:
            pass
    return spy.payload


def test_scale_enters_the_cache_key_only_when_given_and_a_gee_product_is_in_the_chain():
    from aihydro_data.cache import cache_key
    none_ = _key_payload()
    same = _key_payload(scale=None)
    sixty = _key_payload(scale=60)
    ninety = _key_payload(scale=90)
    assert none_ == same and "scale_m" not in none_
    assert cache_key(none_) == cache_key(same)
    assert sixty["scale_m"] == 60.0
    assert cache_key(sixty) != cache_key(none_)
    assert cache_key(sixty) != cache_key(ninety)


def test_scale_is_kept_out_of_the_key_when_no_gee_product_can_serve():
    from aihydro_data.cache import cache_key
    pin = dict(mode="manual", product="GRIDMET_PRECIP", fallback=[])
    assert cache_key(_key_payload(scale=60, **pin)) == cache_key(_key_payload(**pin))


def test_scale_on_a_non_gee_product_is_noted_not_applied():
    import pandas as pd
    from aihydro_data import _pipeline
    from aihydro_data.products import get_product
    spec = get_product("GRIDMET_PRECIP")
    df = pd.DataFrame({"precipitation": [1.0, 2.0]},
                      index=pd.date_range("2000-01-01", periods=2))
    fake = types.SimpleNamespace(
        is_available=lambda spec=None: (True, None),
        fetch_timeseries=lambda *a, **k: df)
    from shapely.geometry import box
    from aihydro_data.contracts import FetchRequest
    req = FetchRequest(variable="precipitation", geometry=box(-69, 47, -68, 48),
                       start="2000-01-01", end="2000-01-02")
    with patch("aihydro_data.sources.base.get_backend", lambda src: fake):
        res = _pipeline._fetch_one(spec, box(-69, 47, -68, 48), "2000-01-01", "2000-01-02",
                                   "basin_mean", req, scale=60)
    assert any("scale=60 m was ignored" in n for n in res.notes)


# ── 4. native-resolution tiling uses the same model ──────────────────────────

def test_tiled_download_sizes_every_tile_under_the_limit(no_net):
    ee = types.ModuleType("ee")
    ee.Geometry = types.SimpleNamespace(Rectangle=lambda b: tuple(b))
    calls = []

    class Img:
        def getDownloadURL(self, params):  # noqa: N802
            calls.append(params["region"])
            return "http://example.invalid/t.tif"

    class Dl(_DownloadMixin):
        def _open_geotiff(self, tmp):
            return xr.DataArray(np.ones((1, 2, 2)), dims=("band", "y", "x"))

    merge = types.ModuleType("rioxarray.merge")
    merge.merge_arrays = lambda tiles: tiles[0]
    pytest.importorskip("rioxarray")
    with patch.dict(sys.modules, {"ee": ee, "rioxarray.merge": merge}):
        Dl()._download_tiled(Img(), _roi(FISH_RIVER), ["ndvi"], 10.0)
    assert len(calls) > 1
    worst = max(_true_ee_bytes(r, 10.0, 1, 8, (0.5, 0.5)) for r in calls)
    assert worst <= LIMIT


# ── 5. bytes per pixel from the export's real band type ──────────────────────

def _types(**bands):
    return types.SimpleNamespace(bandTypes=lambda: types.SimpleNamespace(getInfo=lambda: bands))


@pytest.mark.parametrize("t,expect", [
    ({"type": "PixelType", "precision": "float"}, 4),
    ({"type": "PixelType", "precision": "double"}, 8),
    ({"type": "PixelType", "precision": "int", "min": 0, "max": 255}, 1),
    ({"type": "PixelType", "precision": "int", "min": -32768, "max": 32767}, 2),
    ({"type": "PixelType", "precision": "int", "min": 0, "max": 65535}, 2),
    ({"type": "PixelType", "precision": "int", "min": -2**31, "max": 2**31 - 1}, 4),
    ({"type": "PixelType", "precision": "int", "min": 0, "max": 2**53}, 8),
])
def test_bytes_per_pixel_from_band_types(t, expect):
    from aihydro_data.sources.gee._download import ee_bytes_per_pixel
    assert ee_bytes_per_pixel(_types(b=t), ["b"]) == (expect, "ee_band_types")


def test_widest_band_wins_and_unreadable_types_fall_back_to_float64():
    from aihydro_data.sources.gee._download import ee_bytes_per_pixel
    two = _types(a={"precision": "int", "min": 0, "max": 255}, b={"precision": "float"})
    assert ee_bytes_per_pixel(two, ["a", "b"]) == (4, "ee_band_types")
    assert ee_bytes_per_pixel(object(), ["a"]) == (8, "default_float64")
    assert ee_bytes_per_pixel(_types(a={"precision": "weird"}), ["a"]) == (8, "default_float64")


class _TypedImg(_Img):
    def __init__(self, band_type, **kw):
        super().__init__(**kw)
        self.bandTypes = _types(b=band_type).bandTypes


def _fish_spec(**cfg):
    return types.SimpleNamespace(
        backend_config={"static": True, "gee_dataset_id": "X", "band": "b", "scale_m": 30, **cfg},
        source_dataset_id="X", id="P", variable="v")


def _backend_with(img, roi):
    ee = types.ModuleType("ee")
    ee.Geometry = lambda g: g
    ee.Image = lambda d: types.SimpleNamespace(select=lambda b: img)
    ee.ImageCollection = lambda d: types.SimpleNamespace(mosaic=lambda: types.SimpleNamespace(select=lambda b: img))
    return ee


def test_dem_int_product_plans_finer_than_an_index_composite_and_records_it(no_net):
    roi = _roi(FISH_RIVER)
    dem = _TypedImg({"type": "PixelType", "precision": "int", "min": -32768, "max": 32767})
    ee = _backend_with(dem, roi)
    be = Backend()
    be._open_geotiff = lambda tmp: xr.DataArray(np.ones((1, 3, 3)), dims=("band", "y", "x"))
    with patch.dict(sys.modules, {"ee": ee}), \
            patch.object(Backend, "_assert_available", lambda self: None), \
            patch.object(Backend, "_geom_to_geojson", staticmethod(lambda g: roi)):
        da = be.fetch_raster(_fish_spec(), object(), "", "")
    rec = da.attrs["aihydro_upstream"]["gee_export"]
    assert rec["bytes_per_pixel_assumed"] == 2 and rec["bytes_per_pixel_source"] == "ee_band_types"

    idx = _Img()
    da2, idx_scale = _Dl()._download_image_array(idx, "roi", roi, ["ndvi"], 30.0)
    rec2 = da2.attrs["aihydro_upstream"]["gee_export"]
    assert rec2["bytes_per_pixel_assumed"] == 8 and rec2["bytes_per_pixel_source"] == "default_float64"
    assert rec["final_scale_m"] < idx_scale
    assert _true_ee_bytes(FISH_RIVER, rec["final_scale_m"], 1, 2, (0.4, 0.4)) <= LIMIT


def test_float32_dem_plans_between_int16_and_float64():
    from aihydro_data.sources.gee._download import min_scale_for_budget
    s = {bpp: min_scale_for_budget(FISH_RIVER, 1, bpp) for bpp in (2, 4, 8)}
    assert s[2] < s[4] < s[8]


# ── 6. provenance survives unit conversion, multiband and SoilGrids ──────────

def test_provenance_survives_unit_conversion_in_fetch_raster(no_net):
    img = _TypedImg({"precision": "float"}, ok_from=40.0)
    ee = _backend_with(img, SMALL)
    be = Backend()
    be._open_geotiff = lambda tmp: xr.DataArray(np.ones((1, 3, 3)), dims=("band", "y", "x"))
    with patch.dict(sys.modules, {"ee": ee}), \
            patch.object(Backend, "_assert_available", lambda self: None), \
            patch.object(Backend, "_geom_to_geojson", staticmethod(lambda g: SMALL)):
        da = be.fetch_raster(_fish_spec(unit_conversion=0.1), object(), "", "")
    assert float(da.max()) == pytest.approx(0.1)           # conversion really ran
    up = da.attrs["aihydro_upstream"]["gee_export"]
    assert len(up["attempts"]) > 1 and up["bytes_per_pixel_assumed"] == 4
    assert any("not the requested 30 m" in n for n in da.attrs["aihydro_notes"])


def test_provenance_survives_multiband_dataset(no_net):
    img = _Img(ok_from=40.0)
    be = Backend()
    be._open_geotiff = lambda tmp: xr.DataArray(
        np.ones((2, 3, 3)), dims=("band", "y", "x"), coords={"band": [1, 2]})
    spec = types.SimpleNamespace(
        id="S", source_dataset_id="X",
        backend_config={"gee_dataset_id": "X", "band_map": {"red": "B4", "nir": "B8"},
                        "scale_m": 30, "scale_factor": 0.0001, "offset": 0.0})
    ee = types.ModuleType("ee")
    ee.Geometry = lambda g: g
    with patch.dict(sys.modules, {"ee": ee}), \
            patch.object(Backend, "_assert_available", lambda self: None), \
            patch.object(Backend, "_geom_to_geojson", staticmethod(lambda g: SMALL)), \
            patch.object(Backend, "_masked_median_composite",
                         lambda self, *a, **k: types.SimpleNamespace(clip=lambda r: img)):
        ds = be.fetch_multiband_composite(spec, object(), "2014-06-01", "2014-09-30")
    up = ds.attrs["aihydro_upstream"]["gee_export"]
    assert len(up["attempts"]) > 1 and up["bytes_per_pixel_assumed"] == 8
    assert any("not the requested 30 m" in n for n in ds.attrs["aihydro_notes"])


def test_provenance_survives_soilgrids_dataset(no_net):
    img = _TypedImg({"precision": "float"}, ok_from=40.0)
    img.reproject = lambda crs, scale: img
    be = Backend()
    be._open_geotiff = lambda tmp: xr.DataArray(np.ones((1, 3, 3)), dims=("band", "y", "x"))
    spec = types.SimpleNamespace(
        id="SG", source_dataset_id="X",
        backend_config={"soil_properties": ["sand", "clay"], "soil_depth": "0-5cm",
                        "soil_depth_suffix": "5", "scale_m": 30, "unit_conversion": 0.1})
    ee = types.ModuleType("ee")
    ee.Geometry = lambda g: g
    ee.Image = lambda d: types.SimpleNamespace(select=lambda b: img)
    with patch.dict(sys.modules, {"ee": ee}), \
            patch.object(Backend, "_assert_available", lambda self: None), \
            patch.object(Backend, "_geom_to_geojson", staticmethod(lambda g: SMALL)):
        ds = be._fetch_soilgrids_raster(spec, object())
    exp = ds.attrs["aihydro_upstream"]["gee_export"]
    assert set(exp) == {"sand", "clay"}
    assert all(len(v["attempts"]) > 1 and v["bytes_per_pixel_assumed"] == 4 for v in exp.values())
    assert any("not the requested 30 m" in n for n in ds.attrs["aihydro_notes"])
