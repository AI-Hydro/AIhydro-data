"""Slice 3 / P2 (offline): cache identity uses the canonical geometry id."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from shapely.geometry import LineString, Polygon

from aihydro_data.contracts import ProductSpec
from aihydro_data.geometry import GaugeID, geometry_id
from aihydro_data.routing.detect import detect_region

RING = [(10.0, 50.0), (11.0, 50.0), (11.0, 51.0), (10.0, 51.0)]


class _Stub:
    calls = 0

    def is_available(self, spec=None):
        return True, None

    def fetch_timeseries(self, spec, geometry, start, end, aggregation, **kw):
        type(self).calls += 1
        return pd.DataFrame({"date": pd.to_datetime(["2020-01-01"]),
                             spec.variable: [1.0]})

    def fetch_raster(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def harness(monkeypatch, tmp_path):
    import aihydro_data.products as products
    import aihydro_data.routing as routing
    import aihydro_data.sources.base as base
    from aihydro_data import _pipeline
    from aihydro_data import cache as cachemod

    spec = ProductSpec(id="P", variable="precipitation", source="gee",
                       timestep="daily")
    monkeypatch.setattr(cachemod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(routing, "detect_region", lambda g: "global")
    monkeypatch.setattr(routing, "resolve_product_ids", lambda v, r, geometry=None: ["P"])
    monkeypatch.setattr(products, "get_product", lambda pid: spec)
    monkeypatch.setattr(_pipeline, "_is_registered", lambda pid: True)
    monkeypatch.setattr(base, "get_backend", lambda src: _Stub())
    _Stub.calls = 0

    def run(geom, **kw):
        return _pipeline.fetch("precipitation", geom, "2020-01-01", "2020-01-31",
                               cache=True, **kw)

    run.tmp = tmp_path
    return run


def test_ring_reordering_hits_same_key(harness):
    a = harness(Polygon(RING))
    rotated = RING[2:] + RING[:2]
    b = harness(Polygon(list(reversed(rotated))))
    assert b.cache_hit is True
    assert b.cache_key == a.cache_key
    assert _Stub.calls == 1
    assert a.geometry_id == b.geometry_id


def test_different_outlets_still_separate_keys(harness):
    poly = Polygon(RING)
    a = harness(poly, outlet=(50.1, 10.2))
    b = harness(poly, outlet=(50.9, 10.8))
    assert a.cache_key != b.cache_key
    assert a.geometry_id == b.geometry_id


def test_different_geometries_separate_keys(harness):
    a = harness(Polygon(RING))
    shifted = [(x + 0.5, y) for x, y in RING]
    b = harness(Polygon(shifted))
    assert a.cache_key != b.cache_key
    assert a.geometry_id != b.geometry_id


def test_geometry_id_on_fresh_and_cached_results(harness):
    poly = Polygon(RING)
    fresh = harness(poly)
    assert fresh.geometry_id == geometry_id(poly)
    assert fresh.geometry_id.startswith("sha256:")
    hit = harness(poly)
    assert hit.cache_hit is True
    assert hit.geometry_id == fresh.geometry_id


def test_manifest_records_geom_id_and_wkt(harness):
    poly = Polygon(RING)
    res = harness(poly)
    mf = json.loads((harness.tmp / f"{res.cache_key}.manifest.json").read_text())
    entry = mf[-1] if isinstance(mf, list) else mf
    assert entry["geom_id"] == res.geometry_id
    assert entry["geom_wkt"] == poly.wkt


def test_legacy_manifest_without_geom_id_still_reads(tmp_path, monkeypatch):
    from aihydro_data import cache as cachemod
    from aihydro_data.cache.manifest import ManifestEntry, write_manifest

    monkeypatch.setattr(cachemod, "cache_dir", lambda: tmp_path)
    pd.DataFrame({"date": pd.to_datetime(["2020-01-01"]),
                  "precipitation": [1.0]}).to_parquet(tmp_path / "old.parquet")
    entry = ManifestEntry(
        cache_key="old", variable="precipitation", product="P", source="gee",
        start="2020-01-01", end="2020-01-31", geom_wkt="POINT (0 0)",
        aggregation="basin_mean", fetched_at="2026-06-11T00:00:00+00:00")
    d = entry.to_dict()
    d.pop("geom_id")
    assert ManifestEntry.from_dict(d).geom_id is None
    write_manifest(tmp_path, entry)
    res = cachemod.cache_read("old", allowed_products=None)
    assert res is not None
    assert res.geometry_id is None


def test_result_schema_bumped_and_wkt_not_in_key_payload():
    from aihydro_data._pipeline import RESULT_SCHEMA_VERSION
    assert RESULT_SCHEMA_VERSION == 5


def test_gaugeid_scheme_routing():
    assert GaugeID("03353000").scheme == "usgs"
    assert detect_region(GaugeID("03353000")) == "CONUS"
    assert detect_region(GaugeID("6335020", scheme="grdc")) == "global"
    assert GaugeID("1", "grdc") != GaugeID("1")
    assert GaugeID("1", "grdc").wkt != GaugeID("1").wkt


def test_gaugeid_geometry_id_is_scheme_sensitive():
    assert geometry_id(GaugeID("1", "grdc")) != geometry_id(GaugeID("1"))
    assert geometry_id(GaugeID("1")) == geometry_id(GaugeID("1"))


def test_invalid_geometry_fails_clearly(harness):
    from aihydro_data.exceptions import GeometryInvalid
    with pytest.raises(GeometryInvalid):
        harness(LineString([(0, 0), (1, 1)]))
    with pytest.raises(GeometryInvalid):
        harness(Polygon([(0, 0), (1, 1), (2, 2)]))   # zero-area, degenerate
