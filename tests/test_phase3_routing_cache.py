"""Phase 3 tests (offline): verify-on-read cache, region override, outlet kwarg."""
from __future__ import annotations

import pandas as pd
import pytest
from shapely.geometry import Point

from aihydro_data.contracts import FetchRequest, ProductSpec


# ── S2: verify-on-read cache ─────────────────────────────────────────────────

class TestVerifyOnReadCache:
    def _write_entry(self, tmp_path, product):
        from aihydro_data.cache.manifest import ManifestEntry, write_manifest
        (tmp_path / "k1.parquet").write_bytes(b"")  # presence marker
        pd.DataFrame({"date": pd.to_datetime(["2020-01-01"]),
                      "precipitation": [1.0]}).to_parquet(tmp_path / "k1.parquet")
        write_manifest(tmp_path, ManifestEntry(
            cache_key="k1", variable="precipitation", product=product,
            source="gee", start="2020-01-01", end="2020-01-31",
            geom_wkt="POINT (0 0)", aggregation="basin_mean",
            fetched_at="2026-06-11T00:00:00+00:00",
        ))

    def test_hit_when_product_in_chain(self, tmp_path, monkeypatch):
        from aihydro_data import cache as cachemod
        monkeypatch.setattr(cachemod, "cache_dir", lambda: tmp_path)
        self._write_entry(tmp_path, "CHIRPS")
        res = cachemod.cache_read("k1", allowed_products=["CHIRPS", "IMERG_PRECIP"])
        assert res is not None
        assert res.product == "CHIRPS"

    def test_miss_when_product_not_in_chain(self, tmp_path, monkeypatch):
        from aihydro_data import cache as cachemod
        monkeypatch.setattr(cachemod, "cache_dir", lambda: tmp_path)
        self._write_entry(tmp_path, "CHIRPS")
        # current chain no longer contains CHIRPS → treat as miss
        res = cachemod.cache_read("k1", allowed_products=["GRIDMET_PRECIP"])
        assert res is None

    def test_none_allowed_disables_check(self, tmp_path, monkeypatch):
        from aihydro_data import cache as cachemod
        monkeypatch.setattr(cachemod, "cache_dir", lambda: tmp_path)
        self._write_entry(tmp_path, "CHIRPS")
        res = cachemod.cache_read("k1", allowed_products=None)
        assert res is not None


# ── S4: region override ──────────────────────────────────────────────────────

class _StubBackend:
    def is_available(self, spec=None):
        return True, None

    def fetch_timeseries(self, spec, geometry, start, end, aggregation, **kw):
        df = pd.DataFrame({"date": pd.to_datetime(["2020-01-01"]),
                           spec.variable: [1.0]})
        df.attrs["_kw"] = kw
        return df

    def fetch_raster(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


class TestRegionOverride:
    def test_invalid_region_raises(self):
        from aihydro_data import fetch
        from aihydro_data.exceptions import RegionUnsupported
        with pytest.raises(RegionUnsupported) as ei:
            fetch("precipitation", Point(0, 0), "2020-01-01", "2020-01-31",
                  region="ATLANTIS")
        assert ei.value.code == "REGION_INVALID"

    def test_region_override_skips_detection(self, monkeypatch):
        from aihydro_data import _pipeline
        import aihydro_data.routing as routing
        import aihydro_data.products as products

        called = {"detect": False}

        def fake_detect(geom):
            called["detect"] = True
            return "CONUS"

        monkeypatch.setattr(routing, "detect_region", fake_detect)
        seen_region = {}

        def fake_resolve(variable, region):
            seen_region["region"] = region
            return ["P"]

        monkeypatch.setattr(routing, "resolve_product_ids", fake_resolve)
        spec = ProductSpec(id="P", variable="precipitation", source="gee",
                           timestep="daily")
        monkeypatch.setattr(products, "get_product", lambda pid: spec)
        monkeypatch.setattr(_pipeline, "_is_registered", lambda pid: True)
        import aihydro_data.sources.base as base
        monkeypatch.setattr(base, "get_backend", lambda src: _StubBackend())

        res = _pipeline.fetch("precipitation", Point(0, 0),
                              "2020-01-01", "2020-01-31",
                              region="EUROPE", cache=False)
        assert res.product == "P"
        assert seen_region["region"] == "EUROPE"
        assert called["detect"] is False  # detection skipped


# ── S5: outlet kwarg reaches the snap backend ────────────────────────────────

class _OutletCaptureBackend:
    def __init__(self):
        self.seen_outlet = "UNSET"

    def is_available(self, spec=None):
        return True, None

    def fetch_timeseries(self, spec, geometry, start, end, aggregation, outlet=None):
        self.seen_outlet = outlet
        return pd.DataFrame({"date": pd.to_datetime(["2020-01-01"]),
                             spec.variable: [10.0]})

    def fetch_raster(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


class TestOutletPassthrough:
    def test_outlet_reaches_backend(self, monkeypatch):
        from aihydro_data import _pipeline
        import aihydro_data.routing as routing
        import aihydro_data.products as products

        backend = _OutletCaptureBackend()
        spec = ProductSpec(id="GEO", variable="streamflow",
                           source="geoglows_retro", timestep="daily",
                           spatial_support="reach")
        monkeypatch.setattr(routing, "detect_region", lambda g: "global")
        monkeypatch.setattr(routing, "resolve_product_ids", lambda v, r: ["GEO"])
        monkeypatch.setattr(products, "get_product", lambda pid: spec)
        monkeypatch.setattr(_pipeline, "_is_registered", lambda pid: True)
        import aihydro_data.sources.base as base
        monkeypatch.setattr(base, "get_backend", lambda src: backend)

        _pipeline.fetch("streamflow", Point(10.0, 50.0),
                        "2020-01-01", "2020-01-31",
                        outlet=(50.1, 10.2), cache=False)
        assert backend.seen_outlet == (50.1, 10.2)

    def test_outlet_dropped_for_backend_without_param(self, monkeypatch):
        """A backend whose fetch_timeseries has no `outlet` param must not blow
        up when outlet is supplied — _call filters the kwarg."""
        from aihydro_data import _pipeline
        import aihydro_data.routing as routing
        import aihydro_data.products as products

        spec = ProductSpec(id="P", variable="precipitation", source="gee",
                           timestep="daily")
        monkeypatch.setattr(routing, "detect_region", lambda g: "global")
        monkeypatch.setattr(routing, "resolve_product_ids", lambda v, r: ["P"])
        monkeypatch.setattr(products, "get_product", lambda pid: spec)
        monkeypatch.setattr(_pipeline, "_is_registered", lambda pid: True)
        import aihydro_data.sources.base as base
        monkeypatch.setattr(base, "get_backend", lambda src: _StubBackend())

        res = _pipeline.fetch("precipitation", Point(0, 0),
                              "2020-01-01", "2020-01-31",
                              outlet=(1.0, 2.0), cache=False)
        assert res.product == "P"  # no TypeError from the extra kwarg
