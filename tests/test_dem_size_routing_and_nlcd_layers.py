"""
Size-aware CONUS DEM routing, and NLCD layer/year handling. All offline.

DEM: USGS 3DEP 10 m (bare earth) leads the CONUS chain for requests whose
bounding box is under DEM3DEP_MAX_BBOX_KM2; larger requests keep GLO-30 first
because 3DEP's WCS times out on them.

NLCD: the backend used to ignore the product's ``nlcd_layer`` and always
request "cover", so NLCD_IMPERVIOUS returned land-cover class codes. The
default epoch (no start date) is now 2021.
"""
from __future__ import annotations

import sys
import types

import pytest
from shapely.geometry import Point, box

from aihydro_data.routing.policy import (
    DEM3DEP_MAX_BBOX_KM2,
    bbox_area_km2,
    resolve_product_ids,
)

CULVERT_BOX = box(-86.93, 40.42, -86.92, 40.43)      # ~0.95 km², Indiana
BIG_BOX = box(-87.0, 40.0, -86.0, 41.0)              # ~9,400 km²
STATIC_CONUS_DEM = ["GLO30", "DEM3DEP_10M", "SRTM", "GLO30_STAC", "GLO30_ELEMENT84"]


# ── bbox area ─────────────────────────────────────────────────────────────

def test_bbox_area_known_answer():
    # 0.01° x 0.01° at 40.4°N: 1.111 km (lat) x 0.848 km (lon) ≈ 0.94 km².
    assert bbox_area_km2(CULVERT_BOX) == pytest.approx(0.94, rel=0.02)
    assert bbox_area_km2(Point(-86.9, 40.4)) == 0.0
    assert bbox_area_km2(object()) is None


# ── size-aware DEM order ──────────────────────────────────────────────────

def test_small_conus_request_gets_3dep_first():
    ids = resolve_product_ids("dem", "CONUS", CULVERT_BOX)
    assert ids[0] == "DEM3DEP_10M"
    assert sorted(ids) == sorted(STATIC_CONUS_DEM)       # same chain, reordered


def test_large_conus_request_keeps_glo30_first():
    assert resolve_product_ids("dem", "CONUS", BIG_BOX) == STATIC_CONUS_DEM


def test_threshold_boundary():
    # A 0.2° square at 40°N is ~375 km² (under); a 0.3° square ~845 km² (over).
    under, over = box(-86.2, 40.0, -86.0, 40.2), box(-86.3, 40.0, -86.0, 40.3)
    assert bbox_area_km2(under) < DEM3DEP_MAX_BBOX_KM2 < bbox_area_km2(over)
    assert resolve_product_ids("dem", "CONUS", under)[0] == "DEM3DEP_10M"
    assert resolve_product_ids("dem", "CONUS", over)[0] == "GLO30"


@pytest.mark.parametrize("geom", [None, Point(-86.9, 40.4)])
def test_no_area_keeps_static_order(geom):
    assert resolve_product_ids("dem", "CONUS", geom) == STATIC_CONUS_DEM


def test_rule_is_conus_only_and_dem_only():
    # 3DEP does not cover Canada/Mexico; other variables are untouched.
    assert resolve_product_ids("dem", "NORTH_AMERICA", CULVERT_BOX)[0] == "GLO30"
    assert (resolve_product_ids("landcover", "CONUS", CULVERT_BOX)
            == resolve_product_ids("landcover", "CONUS"))


def test_resolve_product_auto_uses_geometry():
    from aihydro_data.contracts import FetchRequest
    from aihydro_data.products import list_products
    from aihydro_data.routing import resolve_product

    if "DEM3DEP_10M" not in {p.id for p in list_products(variable="dem")}:
        pytest.skip("DEM3DEP_10M not registered in this install")
    req = FetchRequest(variable="dem", geometry=CULVERT_BOX, start="", end="")
    assert resolve_product(req).id == "DEM3DEP_10M"


def test_fetch_tries_3dep_first_for_small_basin(monkeypatch):
    """End to end through fetch(): the first product attempted is 3DEP."""
    import aihydro_data
    from aihydro_data import _pipeline

    tried = []

    def fake_fetch_one(spec, *a, **k):
        tried.append(spec.id)
        raise RuntimeError("offline test: stop here")

    monkeypatch.setattr(_pipeline, "_fetch_one", fake_fetch_one)
    with pytest.raises(Exception):
        aihydro_data.fetch("dem", CULVERT_BOX, "", "", aggregation="raw_raster", cache=False)
    assert tried and tried[0] == "DEM3DEP_10M"

    tried.clear()
    with pytest.raises(Exception):
        aihydro_data.fetch("dem", BIG_BOX, "", "", aggregation="raw_raster", cache=False)
    assert tried and tried[0] == "GLO30"


# ── NLCD layer + default year ─────────────────────────────────────────────

@pytest.fixture
def fake_pygeohydro(monkeypatch):
    calls = []
    mod = types.ModuleType("pygeohydro")

    def nlcd_bygeom(gdf, years=None, resolution=30, **kw):
        calls.append(dict(years))
        return {0: "dataset"}

    mod.nlcd_bygeom = nlcd_bygeom
    monkeypatch.setitem(sys.modules, "pygeohydro", mod)
    return calls


def _fetch_nlcd(product_id, start):
    from aihydro_data.products import get_product
    from aihydro_data.sources.hyriver import Backend

    spec = get_product(product_id)
    return Backend()._fetch_pygeohydro_raster(
        spec, dict(spec.backend_config), CULVERT_BOX, start, "",
    )


def test_impervious_requests_impervious_layer(fake_pygeohydro):
    _fetch_nlcd("NLCD_IMPERVIOUS", "2021-01-01")
    assert fake_pygeohydro == [{"impervious": [2021]}]


def test_landcover_still_requests_cover_layer(fake_pygeohydro):
    _fetch_nlcd("NLCD", "2016-06-01")
    assert fake_pygeohydro == [{"cover": [2016]}]


@pytest.mark.parametrize("product_id, layer", [("NLCD", "cover"), ("NLCD_IMPERVIOUS", "impervious")])
def test_no_start_date_defaults_to_2021(fake_pygeohydro, product_id, layer):
    _fetch_nlcd(product_id, "")
    assert fake_pygeohydro == [{layer: [2021]}]


def test_product_default_year_is_2021():
    from aihydro_data.products import get_product
    from aihydro_data.sources.hyriver import NLCD_LATEST_YEAR, _nearest_nlcd_year

    assert NLCD_LATEST_YEAR == 2021
    assert _nearest_nlcd_year("") == 2021
    for pid in ("NLCD", "NLCD_IMPERVIOUS"):
        assert get_product(pid).backend_config["default_year"] == 2021


@pytest.mark.live
def test_live_impervious_returns_percent_not_class_codes():
    import numpy as np

    import aihydro_data

    r = aihydro_data.fetch("impervious", CULVERT_BOX, "2021-01-01", "2021-12-31",
                           aggregation="raw_raster", cache=False)
    ds = r.data
    v = ds[list(ds.data_vars)[0]].values
    v = v[np.isfinite(v)]
    assert list(ds.data_vars)[0].startswith("impervious")
    assert v.min() >= 0 and v.max() <= 100


# ── cache invalidation for the two changed variables ──────────────────────

def test_cache_key_changes_only_for_revised_variables():
    from aihydro_data._pipeline import RESULT_SCHEMA_VERSION, _result_schema_for

    assert _result_schema_for("precipitation") == RESULT_SCHEMA_VERSION
    assert _result_schema_for("landcover") == RESULT_SCHEMA_VERSION
    assert _result_schema_for("impervious") != RESULT_SCHEMA_VERSION
    assert _result_schema_for("dem") != RESULT_SCHEMA_VERSION


def test_old_impervious_cache_entry_is_not_served(tmp_path, monkeypatch):
    """An entry written under the old key (land-cover codes) must be a miss."""
    import aihydro_data
    from aihydro_data import _pipeline, cache

    old_key = cache.cache_key({
        "result_schema": _pipeline.RESULT_SCHEMA_VERSION, "variable": "impervious",
        "start": "2021-01-01", "end": "2021-12-31", "aggregation": "raw_raster",
        "geom_wkt": CULVERT_BOX.wkt,
    })
    seen_keys = []

    def spy_read(ck, *a, **k):
        seen_keys.append(ck)
        return None

    monkeypatch.setattr(cache, "cache_read", spy_read)
    monkeypatch.setattr(_pipeline, "_fetch_one",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(Exception):
        aihydro_data.fetch("impervious", CULVERT_BOX, "2021-01-01", "2021-12-31",
                           aggregation="raw_raster")
    assert seen_keys and old_key not in seen_keys
