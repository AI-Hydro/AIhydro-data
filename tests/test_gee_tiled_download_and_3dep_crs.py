"""Native-resolution GEE tiled download fails closed; 3DEP DEM asks for 5070 and warps bilinear.

Offline: ``ee``, ``urlretrieve`` and ``py3dep`` are mocked.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr

rioxarray = pytest.importorskip("rioxarray")

from aihydro_data.exceptions import SourceUnavailable  # noqa: E402
from aihydro_data.sources.gee._download import _DownloadMixin  # noqa: E402

ROI = {"type": "Polygon",
       "coordinates": [[[-100.0, 35.0], [-99.0, 35.0], [-99.0, 36.0], [-100.0, 36.0], [-100.0, 35.0]]]}


class _Downloader(_DownloadMixin):
    def __init__(self):
        self.opened = 0

    def _open_geotiff(self, tmp):  # one distinct, georeferenced tile per call
        i = self.opened
        self.opened += 1
        da = xr.DataArray(np.full((1, 4, 4), float(i + 1)), dims=("band", "y", "x"),
                          coords={"band": [1], "y": 36.0 - i - np.arange(4) * 0.01,
                                  "x": -100.0 + np.arange(4) * 0.01})
        return da.rio.write_crs("EPSG:4326")


class _Image:
    def __init__(self, fail_calls=()):
        self.calls = 0
        self.fail_calls = set(fail_calls)

    def getDownloadURL(self, params):  # noqa: N802
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RuntimeError("Computation timed out.")
        return "http://example.invalid/tile.tif"


@pytest.fixture
def fake_ee():
    ee = types.ModuleType("ee")
    ee.Geometry = types.SimpleNamespace(Rectangle=lambda b: ("rect", tuple(b)))
    with patch.dict(sys.modules, {"ee": ee}), \
            patch("urllib.request.urlretrieve", lambda url, tmp: open(tmp, "wb").close()):
        yield


def test_failed_tile_raises_and_names_it(fake_ee):
    d, img = _Downloader(), _Image(fail_calls={2})
    with pytest.raises(SourceUnavailable) as ei:
        d._download_tiled(img, ROI, ["elevation"], 30.0)
    err = ei.value
    assert err.code == "GEE_TILE_DOWNLOAD_FAILED"
    assert img.calls > 1, "fixture must produce a multi-tile grid"
    assert [t["error"] for t in err.details["failed_tiles"]] == ["RuntimeError: Computation timed out."]
    assert len(err.details["failed_tiles"]) == 1
    assert err.details["n_tiles_ok"] == img.calls - 1
    assert err.details["n_tiles"] == img.calls
    t = err.details["failed_tiles"][0]
    assert f"(r{t['row']},c{t['col']})" in err.message


def test_every_failed_tile_is_reported(fake_ee):
    img = _Image(fail_calls={1, 3})
    with pytest.raises(SourceUnavailable) as ei:
        _Downloader()._download_tiled(img, ROI, ["elevation"], 30.0)
    assert len(ei.value.details["failed_tiles"]) == 2


def test_all_tiles_ok_still_mosaics(fake_ee):
    out = _Downloader()._download_tiled(_Image(), ROI, ["elevation"], 30.0)
    assert out.sizes["x"] >= 4


def test_3dep_requests_5070_and_warps_bilinear():
    from aihydro_data.sources import hyriver
    from shapely.geometry import box

    x = 1_000_000.0 + np.arange(20) * 10.0
    y = 1_500_000.0 - np.arange(20) * 10.0
    dem5070 = xr.DataArray(np.tile(np.arange(20, dtype="float32") * 7.0, (20, 1)), dims=("y", "x"),
                           coords={"y": y, "x": x}).rio.write_crs("EPSG:5070")
    seen = {}

    def fake_get_map(product, geom, resolution, crs):
        seen["crs"] = crs
        return dem5070

    fake = types.SimpleNamespace(get_map=fake_get_map)
    spec = types.SimpleNamespace()
    with patch.object(hyriver, "require_import", return_value=fake):
        out = hyriver.Backend()._fetch_3dep_raster(
            spec, {"py3dep_resolution": 10, "py3dep_product": "DEM"}, box(-100, 35, -99.99, 35.01))
    assert seen["crs"] == "EPSG:5070"
    assert out.rio.crs == "EPSG:4326"
    vals = np.asarray(out.values)
    vals = vals[np.isfinite(vals)]
    # nearest keeps only source values (multiples of 7); bilinear creates in-between values
    assert np.any(np.abs(vals / 7.0 - np.round(vals / 7.0)) > 1e-3)


def test_reproject_continuous_refuses_missing_crs():
    from aihydro_data.sources import hyriver
    da = xr.DataArray(np.zeros((3, 3)), dims=("y", "x"), coords={"y": [2, 1, 0], "x": [0, 1, 2]})
    with pytest.raises(ValueError):
        hyriver.Backend._reproject_continuous(da, "EPSG:4326")


def test_failed_tile_list_in_message_is_truncated_but_complete_in_details(fake_ee):
    img = _Image(fail_calls=set(range(1, 40)))
    big = {"type": "Polygon", "coordinates": [[[-110.0, 30.0], [-100.0, 30.0], [-100.0, 40.0],
                                               [-110.0, 40.0], [-110.0, 30.0]]]}
    with pytest.raises(SourceUnavailable) as ei:
        _Downloader()._download_tiled(img, big, ["elevation"], 30.0)
    err = ei.value
    n = len(err.details["failed_tiles"])
    assert n > 10 and f"and {n - 10} more" in err.message
    assert err.message.count("(r") == 10


# ── pipeline: fallback_history + cache-key markers ───────────────────────────

def _payloads(monkeypatch, variable, product, **kw):
    import aihydro_data
    import aihydro_data.cache as cache_mod
    from shapely.geometry import box

    seen = []
    real_key = cache_mod.cache_key

    class _Stop(Exception):
        pass

    def spy(payload):
        seen.append(dict(payload))
        return real_key(payload)

    def fake_read(*a, **k):
        raise _Stop()

    monkeypatch.setattr(cache_mod, "cache_key", spy)
    monkeypatch.setattr(cache_mod, "cache_read", fake_read)
    try:
        aihydro_data.fetch(variable, box(-100.0, 35.0, -99.9, 35.1), "", "",
                           mode="manual", product=product, fallback=[], **kw)
    except Exception:
        pass
    return seen[-1]


def test_gee_tile_marker_only_for_native_resolution_with_gee(monkeypatch):
    import aihydro_data.cache as cache_mod
    p = _payloads(monkeypatch, "dem", "GLO30", native_resolution=True)
    assert p["gee_tile_integrity"] == 1
    old = {k: v for k, v in p.items() if k != "gee_tile_integrity"}
    assert cache_mod.cache_key(old) != cache_mod.cache_key(p)
    assert "gee_tile_integrity" not in _payloads(monkeypatch, "dem", "GLO30")
    assert "gee_tile_integrity" not in _payloads(monkeypatch, "dem", "DEM3DEP_10M", native_resolution=True)


def test_py3dep_marker_only_for_chains_with_3dep(monkeypatch):
    import aihydro_data.cache as cache_mod
    p = _payloads(monkeypatch, "dem", "DEM3DEP_10M")
    assert p["py3dep_resampling"] == 1
    old = {k: v for k, v in p.items() if k != "py3dep_resampling"}
    assert cache_mod.cache_key(old) != cache_mod.cache_key(p)
    assert "py3dep_resampling" not in _payloads(monkeypatch, "dem", "GLO30")
    assert "py3dep_resampling" not in _payloads(monkeypatch, "dem", "GLO30_STAC")


def test_pipeline_records_tile_failure_and_moves_on(monkeypatch, tmp_path):
    import aihydro_data
    import aihydro_data._pipeline as pl
    from aihydro_data.contracts import FetchResult
    from shapely.geometry import box

    monkeypatch.setenv("HOME", str(tmp_path))
    geom = box(-100.0, 35.0, -99.9, 35.1)
    calls = []

    def fake_one(spec, geom, start, end, aggregation, req, **kw):
        calls.append(spec.id)
        if spec.id == "GLO30":
            raise SourceUnavailable(code="GEE_TILE_DOWNLOAD_FAILED", message="1 of 4 tiles failed: (r0,c1)")
        return FetchResult(variable="dem", product=spec.id, source=spec.source, request=req,
                           cache_key="k", data=xr.DataArray(np.zeros((2, 2))), license="x",
                           citation="c", bibtex="@misc{x}")

    monkeypatch.setattr(pl, "_fetch_one", fake_one)
    res = aihydro_data.fetch("dem", geom, "", "", mode="manual", product="GLO30",
                             fallback=["DEM3DEP_10M"], native_resolution=True, cache=False)
    assert calls == ["GLO30", "DEM3DEP_10M"]
    hist = res.fallback_history
    assert hist[0]["outcome"] == "failed" and "GEE_TILE_DOWNLOAD_FAILED" in hist[0]["reason"]
    assert hist[1]["outcome"] == "served"
