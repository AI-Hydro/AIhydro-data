"""Static tiled STAC products are mosaicked, not first-tile-only (camels-slope-1 pilot, 2026-11-09).

Offline: ``stackstac.stack`` is replaced by a synthetic cube whose ``time`` axis holds one
slice per STAC item (per tile), NaN outside the tile, exactly the shape stackstac returns.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

stackstac = pytest.importorskip("stackstac")

from aihydro_data.products import get_product  # noqa: E402
from aihydro_data.sources.stac import Backend  # noqa: E402

BBOX = (-77.4, 40.6, -76.6, 41.4)        # spans two 1-degree tiles (lon -77|-76 edge at -77)
H, W = 4, 8


def _tile_cube(tiles, times=None, dask=False, fill=np.nan, dtype=float):
    """tiles: list of (item_id, col_slice, value_array|scalar). One time entry per item."""
    n = len(tiles)
    data = np.full((n, 1, H, W), fill, dtype=dtype)
    for i, (_, cols, val) in enumerate(tiles):
        data[i, 0, :, cols] = val
    arr = xr.DataArray(
        data, dims=("time", "band", "y", "x"),
        coords={
            "time": times if times is not None else np.array(["2021-04-22"] * n, dtype="datetime64[ns]"),
            "band": ["data"], "y": np.arange(H)[::-1], "x": np.arange(W),
        },
    )
    return arr.chunk({"time": 1}) if dask else arr


def _run(monkeypatch, spec_id, cube, items):
    spec = get_product(spec_id)
    be = Backend()
    monkeypatch.setattr(be, "_assert_available", lambda: None)
    cat = SimpleNamespace(search=lambda **k: SimpleNamespace(items=lambda: iter(items)))
    monkeypatch.setattr(be, "_open_catalog", lambda url: cat)
    seen = {}

    def fake_stack(sign_items, **kw):
        seen["fill_value"] = kw.get("fill_value", "unset")
        seen["order"] = [it.id for it in sign_items]
        return cube

    monkeypatch.setattr(stackstac, "stack", fake_stack)
    _no_signing(monkeypatch)
    out = be.fetch_raster(spec, SimpleNamespace(bounds=BBOX, geom_type="Polygon"), "", "")
    return out, seen


def _no_signing(monkeypatch):
    try:
        import planetary_computer as pc
        monkeypatch.setattr(pc, "sign", lambda item, *a, **k: item)
    except ImportError:
        pass


def _items(*ids, dt="2021-04-22T00:00:00Z"):
    return [SimpleNamespace(id=i, datetime=dt) for i in ids]


@pytest.mark.parametrize("dask", [False, True])
def test_two_tile_static_dem_has_no_nan_and_exact_values(monkeypatch, dask):
    a = np.arange(H * 4, dtype=float).reshape(H, 4) + 100.0
    b = np.arange(H * 4, dtype=float).reshape(H, 4) + 500.0
    cube = _tile_cube([("t1", slice(0, 4), a), ("t2", slice(4, 8), b)], dask=dask)
    out, seen = _run(monkeypatch, "GLO30_STAC", cube, _items("t1", "t2"))
    assert "time" not in out.dims
    v = np.asarray(out)
    assert v.shape == (H, W) and not np.isnan(v).any()          # 0 % NaN over the union
    np.testing.assert_array_equal(v[:, :4], a)
    np.testing.assert_array_equal(v[:, 4:], b)
    assert seen["fill_value"] is np.nan or np.isnan(seen["fill_value"])


def test_regression_static_stac_not_first_tile_only(monkeypatch):
    """Defect: isel(time=0) kept tile 1 and left every other tile NaN (17-76 % NaN in the pilot)."""
    cube = _tile_cube([("t1", slice(0, 3), 1.0), ("t2", slice(3, 8), 2.0)])
    out, _ = _run(monkeypatch, "GLO30_ELEMENT84", cube, _items("t1", "t2"))
    assert float(np.isnan(np.asarray(out)).mean()) == 0.0
    assert float(cube.isel(time=0, band=0).isnull().mean()) > 0.5   # what the old code returned


def test_overlap_rule_greater_id_wins_and_order_is_deterministic(monkeypatch):
    # Cols 3-4 are covered by both tiles. Items arrive in the "wrong" order; the backend
    # sorts by (datetime, id) before stacking, so the cube is built t1, t2 and t2 wins.
    cube = _tile_cube([("t1", slice(0, 5), 1.0), ("t2", slice(3, 8), 2.0)])
    out, seen = _run(monkeypatch, "GLO30_STAC", cube, _items("t2", "t1"))
    assert seen["order"] == ["t1", "t2"]
    v = np.asarray(out)
    assert (v[:, :3] == 1.0).all() and (v[:, 3:] == 2.0).all() and not np.isnan(v).any()


def test_worldcover_newest_epoch_wins_older_fills_gaps(monkeypatch):
    times = np.array(["2020-01-01", "2021-01-01"], dtype="datetime64[ns]")
    cube = _tile_cube([("wc2020", slice(0, 8), 10), ("wc2021", slice(0, 6), 20)], times=times,
                      fill=0, dtype="uint8")
    items = [SimpleNamespace(id="wc2021", datetime="2021-01-01T00:00:00Z"),
             SimpleNamespace(id="wc2020", datetime="2020-01-01T00:00:00Z")]
    out, seen = _run(monkeypatch, "ESA_WORLDCOVER_STAC", cube, items)
    assert seen["order"] == ["wc2020", "wc2021"]
    v = np.asarray(out)
    assert (v[:, :6] == 20).all() and (v[:, 6:] == 10).all()


def test_time_varying_product_is_untouched(monkeypatch):
    spec = get_product("GLO30_STAC")
    assert spec.timestep == "static"
    ts_spec = spec.model_copy(update={"timestep": "daily"}) if hasattr(spec, "model_copy") else None
    if ts_spec is None:
        import dataclasses
        ts_spec = dataclasses.replace(spec, timestep="daily")
    times = np.array(["2020-01-01", "2020-01-02"], dtype="datetime64[ns]")
    cube = _tile_cube([("d1", slice(0, 8), 1.0), ("d2", slice(0, 8), 2.0)], times=times)
    be = Backend()
    monkeypatch.setattr(be, "_assert_available", lambda: None)
    cat = SimpleNamespace(search=lambda **k: SimpleNamespace(items=lambda: iter(_items("d1", "d2"))))
    monkeypatch.setattr(be, "_open_catalog", lambda url: cat)
    seen = {}
    _no_signing(monkeypatch)
    monkeypatch.setattr(stackstac, "stack", lambda items, **kw: seen.update(order=[i.id for i in items]) or cube)
    out = be.fetch_raster(ts_spec, SimpleNamespace(bounds=BBOX, geom_type="Polygon"), "2020-01-01", "2020-01-02")
    assert out.dims[0] == "time" and out.sizes["time"] == 2
    assert float(out.isel(time=0).mean()) == 1.0 and float(out.isel(time=1).mean()) == 2.0
    assert seen["order"] == ["d1", "d2"]           # input order untouched


# ── cache key: entries written by the first-tile-only code must miss ─────────

def _capture_payloads(monkeypatch, product, variable):
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
        aihydro_data.fetch(variable, box(-77.4, 40.6, -76.6, 41.4), "", "",
                           mode="manual", product=product, fallback=[])
    except Exception:
        pass
    return seen[-1]


@pytest.mark.parametrize("product,variable", [
    ("GLO30_STAC", "dem"), ("GLO30_ELEMENT84", "dem"), ("ESA_WORLDCOVER_STAC", "landcover"),
])
def test_stac_static_cache_key_changed(monkeypatch, product, variable):
    import aihydro_data.cache as cache_mod
    payload = _capture_payloads(monkeypatch, product, variable)
    assert payload["stac_static_mosaic"] == 3
    old = {k: v for k, v in payload.items() if k != "stac_static_mosaic"}
    assert cache_mod.cache_key(old) != cache_mod.cache_key(payload)


def test_non_stac_static_key_unchanged(monkeypatch):
    # A non-STAC static DEM (3DEP) keeps its key shape.
    p = _capture_payloads(monkeypatch, "DEM3DEP_10M", "dem")
    assert "stac_static_mosaic" not in p



# ── provenance, mixed-epoch flag, NaN guard (through the public fetch()) ─────

def _fetch(monkeypatch, product, variable, cube, items, **kw):
    import aihydro_data
    from shapely.geometry import box
    be_cls = Backend
    monkeypatch.setattr(be_cls, "_assert_available", lambda self: None)
    cat = SimpleNamespace(search=lambda **k: SimpleNamespace(items=lambda: iter(items)))
    monkeypatch.setattr(be_cls, "_open_catalog", lambda self, url: cat)
    monkeypatch.setattr(stackstac, "stack", lambda its, **k: cube)
    _no_signing(monkeypatch)
    return aihydro_data.fetch(variable, box(*BBOX), "", "", mode="manual", product=product,
                              fallback=[], aggregation="raw_raster", **kw)


def test_item_ids_and_datetimes_reach_result_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cube = _tile_cube([("t1", slice(0, 4), 1.0), ("t2", slice(4, 8), 2.0)])
    res = _fetch(monkeypatch, "GLO30_STAC", "dem", cube, _items("t2", "t1"), cache=True)
    sr = res.site_resolution
    assert sr["method"] == "stac_static_mosaic"
    assert [i["id"] for i in sr["items"]] == ["t1", "t2"]               # sorted order
    assert all(i["datetime"] == "2021-04-22T00:00:00Z" for i in sr["items"])
    assert sr["nan_fraction_bbox"] == 0.0 and sr["flags"] == []
    manifests = [p for p in tmp_path.rglob("*") if p.is_file() and "t1" in p.read_text(errors="ignore")
                 and "stac_static_mosaic" in p.read_text(errors="ignore")]
    assert manifests, "item ids were not written to the cache manifest"


def _wc_items():
    return [SimpleNamespace(id="wc2021", datetime="2021-01-01T00:00:00Z"),
            SimpleNamespace(id="wc2020", datetime="2020-01-01T00:00:00Z")]


_WC_TIMES = np.array(["2020-01-01", "2021-01-01"], dtype="datetime64[ns]")


def test_mixed_epoch_flag_fires_when_2020_fills_a_2021_gap(monkeypatch):
    cube = _tile_cube([("wc2020", slice(0, 8), 10), ("wc2021", slice(0, 6), 20)], times=_WC_TIMES,
                      fill=0, dtype="uint8")
    res = _fetch(monkeypatch, "ESA_WORLDCOVER_STAC", "landcover", cube, _wc_items(), cache=False)
    assert "WORLDCOVER_MIXED_EPOCHS" in res.site_resolution["flags"]
    assert res.site_resolution["epochs_contributing"] == ["2020-01-01", "2021-01-01"]
    assert any("WORLDCOVER_MIXED_EPOCHS" in n and "not marked per pixel" in n for n in res.notes)


def test_mixed_epoch_flag_silent_when_2021_is_complete(monkeypatch):
    cube = _tile_cube([("wc2020", slice(0, 8), 10), ("wc2021", slice(0, 8), 20)], times=_WC_TIMES,
                      fill=0, dtype="uint8")
    res = _fetch(monkeypatch, "ESA_WORLDCOVER_STAC", "landcover", cube, _wc_items(), cache=False)
    assert res.site_resolution["flags"] == []
    assert res.site_resolution["epochs_contributing"] == ["2021-01-01"]
    assert not any("MIXED_EPOCHS" in n for n in res.notes)


def test_nan_warning_fires_on_missing_tile_but_does_not_fail(monkeypatch):
    # only the first tile was returned (e.g. stac_limit dropped the other): 50 % NaN
    cube = _tile_cube([("t1", slice(0, 4), 1.0)])
    res = _fetch(monkeypatch, "GLO30_STAC", "dem", cube, _items("t1"), cache=False)
    assert res.site_resolution["nan_fraction_bbox"] == pytest.approx(0.5)
    assert "STATIC_MOSAIC_NAN" in res.site_resolution["flags"]
    assert any("STATIC_MOSAIC_NAN" in n and "heuristic" in n for n in res.notes)
    assert res.data is not None
