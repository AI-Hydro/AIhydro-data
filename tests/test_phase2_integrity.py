"""Phase 2 scientific-integrity tests (offline).

S1 — spatial_support declared on point/reach/gauge products; aggregation_actual
     reports point values; basin_sum hard-fails into the fallback chain.
S3 — empty-but-successful results are rejected so fallback continues, and are
     never cached.
"""
from __future__ import annotations

import pandas as pd
import pytest

from aihydro_data.contracts import FetchRequest, ProductSpec


# ── S1: registry declares honest spatial support ─────────────────────────────

class TestSpatialSupportRegistry:
    def test_point_and_reach_products_declared(self):
        from aihydro_data.products import get_product
        expected = {
            "NWIS_STREAMFLOW": "gauge_point",
            "GEOGLOWS_RETRO": "reach",
            "OPENMETEO_FLOOD": "reach",
            "GLOFAS_STREAMFLOW": "reach",
            "OPEN_METEO_TMAX": "point",
            "OPEN_METEO_TMIN": "point",
            "OPEN_METEO_PET": "point",
        }
        for pid, support in expected.items():
            assert get_product(pid).spatial_support == support, pid

    def test_gridded_products_stay_areal(self):
        from aihydro_data.products import get_product
        for pid in ("CHIRPS", "GRIDMET_PRECIP", "ERA5L_TMAX"):
            assert get_product(pid).spatial_support == "areal", pid

    def test_default_spatial_support_is_areal(self):
        spec = ProductSpec(id="X", variable="precipitation", source="gee")
        assert spec.spatial_support == "areal"
        assert spec.allow_empty is False


# ── S1: _fetch_one honesty + basin_sum hard-fail ─────────────────────────────

class _StubBackend:
    """Minimal backend returning a fixed point series."""
    def is_available(self, spec=None):
        return True, None

    def fetch_timeseries(self, spec, geometry, start, end, aggregation):
        return pd.DataFrame({"date": pd.to_datetime(["2020-01-01", "2020-01-02"]),
                             spec.variable: [1.0, 2.0]})

    def fetch_raster(self, spec, geometry, start, end, **kw):  # pragma: no cover
        raise NotImplementedError


def _point_spec():
    return ProductSpec(id="PT", variable="tmax", source="direct_api",
                       timestep="daily", spatial_support="point")


def _req(agg="basin_mean"):
    return FetchRequest(variable="tmax", geometry=(39.1, -94.5),
                        start="2020-01-01", end="2020-01-02", aggregation=agg)


def _patch_backend(monkeypatch, backend):
    import aihydro_data.sources.base as base
    monkeypatch.setattr(base, "get_backend", lambda src: backend)


class TestAggregationHonesty:
    def test_basin_mean_on_point_reports_point_value(self, monkeypatch):
        from aihydro_data._pipeline import _fetch_one
        from shapely.geometry import Point
        _patch_backend(monkeypatch, _StubBackend())
        res = _fetch_one(_point_spec(), Point(-94.5, 39.1),
                         "2020-01-01", "2020-01-02", "basin_mean", _req())
        assert res.spatial_support == "point"
        assert res.aggregation_actual == "point_value"
        assert any("NOT an areal average" in n for n in res.notes)

    def test_basin_sum_on_point_raises_aggregation_unsupported(self, monkeypatch):
        from aihydro_data._pipeline import _fetch_one
        from aihydro_data.exceptions import AggregationUnsupported
        from shapely.geometry import Point
        _patch_backend(monkeypatch, _StubBackend())
        with pytest.raises(AggregationUnsupported) as ei:
            _fetch_one(_point_spec(), Point(-94.5, 39.1),
                       "2020-01-01", "2020-01-02", "basin_sum", _req("basin_sum"))
        assert ei.value.code == "AGGREGATION_UNSUPPORTED"

    def test_basin_sum_falls_through_to_areal_product(self, monkeypatch):
        """A reach product first, an areal product second: basin_sum should
        skip the reach product (hard-fail) and be served by the areal one."""
        from aihydro_data import _pipeline
        from shapely.geometry import Point

        reach = ProductSpec(id="REACH", variable="streamflow",
                            source="geoglows_retro", timestep="daily",
                            spatial_support="reach")
        areal = ProductSpec(id="AREAL", variable="streamflow",
                            source="gee", timestep="daily",
                            spatial_support="areal")

        import aihydro_data.routing as routing
        import aihydro_data.products as products
        monkeypatch.setattr(routing, "resolve_product_ids", lambda v, r: ["REACH", "AREAL"])
        monkeypatch.setattr(routing, "detect_region", lambda g: "global")
        monkeypatch.setattr(_pipeline, "_is_registered", lambda pid: True)
        reg = {"REACH": reach, "AREAL": areal}
        monkeypatch.setattr(products, "get_product", lambda pid: reg[pid])
        import aihydro_data.sources.base as base
        monkeypatch.setattr(base, "get_backend", lambda src: _StubBackend())

        res = _pipeline.fetch("streamflow", Point(10.0, 50.0),
                              "2020-01-01", "2020-01-02",
                              aggregation="basin_sum", cache=False)
        assert res.product == "AREAL"
        # Decision trail shows the reach product was rejected, areal served.
        outcomes = {h["product"]: h["outcome"] for h in res.fallback_history}
        assert outcomes["REACH"] == "failed"   # AggregationUnsupported in the chain
        assert outcomes["AREAL"] == "served"


# ── S3: empty-result gate ────────────────────────────────────────────────────

class _EmptyBackend:
    def is_available(self, spec=None):
        return True, None

    def fetch_timeseries(self, spec, geometry, start, end, aggregation):
        return pd.DataFrame(columns=["date", spec.variable])

    def fetch_raster(self, spec, geometry, start, end, **kw):  # pragma: no cover
        raise NotImplementedError


class TestEmptyResultGate:
    def test_has_signal(self):
        from aihydro_data._pipeline import _has_signal
        d = pd.to_datetime(["2020-01-01"])
        assert _has_signal(pd.DataFrame({"date": d, "v": [3.0]})) is True
        assert _has_signal(pd.DataFrame(columns=["date", "v"])) is False
        # all-NaN value column → no signal (date is datetime, not numeric)
        assert _has_signal(pd.DataFrame({"date": d, "v": [float("nan")]})) is False
        # zero is valid signal (dry-season flow, categorical landcover)
        assert _has_signal(pd.DataFrame({"date": d, "v": [0.0]})) is True

    def test_empty_result_rejected_and_falls_through(self, monkeypatch):
        from aihydro_data import _pipeline
        from shapely.geometry import Point

        empty = ProductSpec(id="EMPTY", variable="streamflow",
                            source="direct_api", timestep="daily",
                            spatial_support="areal")
        full = ProductSpec(id="FULL", variable="streamflow",
                           source="gee", timestep="daily",
                           spatial_support="areal")
        import aihydro_data.routing as routing
        import aihydro_data.products as products
        monkeypatch.setattr(routing, "resolve_product_ids", lambda v, r: ["EMPTY", "FULL"])
        monkeypatch.setattr(routing, "detect_region", lambda g: "global")
        monkeypatch.setattr(_pipeline, "_is_registered", lambda pid: True)
        reg = {"EMPTY": empty, "FULL": full}
        monkeypatch.setattr(products, "get_product", lambda pid: reg[pid])

        backends = {"direct_api": _EmptyBackend(), "gee": _StubBackend()}
        import aihydro_data.sources.base as base
        monkeypatch.setattr(base, "get_backend", lambda src: backends[src])

        res = _pipeline.fetch("streamflow", Point(10.0, 50.0),
                              "2020-01-01", "2020-01-02", cache=False)
        assert res.product == "FULL"
        outcomes = {h["product"]: h["outcome"] for h in res.fallback_history}
        assert outcomes["EMPTY"] == "rejected"
        reasons = {h["product"]: h["reason"] for h in res.fallback_history}
        assert reasons["EMPTY"] == "empty result"

    def test_allow_empty_opt_out_keeps_empty(self, monkeypatch):
        from aihydro_data._pipeline import _fetch_one
        from shapely.geometry import Point
        spec = ProductSpec(id="E", variable="streamflow", source="direct_api",
                           timestep="daily", allow_empty=True)
        _patch_backend(monkeypatch, _EmptyBackend())
        res = _fetch_one(spec, Point(10.0, 50.0), "2020-01-01", "2020-01-02",
                         "basin_mean",
                         FetchRequest(variable="streamflow", geometry=(50.0, 10.0),
                                      start="2020-01-01", end="2020-01-02"))
        assert res.product == "E"
        assert res.data.empty
