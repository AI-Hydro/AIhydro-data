"""Phase 1 bug-fix regression tests (offline).

Covers:
  B1 — queued-source redirect fires only when the FIRST viable candidate is queued
  B2 — NLDI nearest-gauge two-step lookup (mocked requests)
  B3 — batch dispatch threads kwargs and surfaces errors (BatchResult)
  B5 — manifest append is thread-safe and atomic
  B6 — hyriver is_available(spec) checks the product's specific library
  R7 — _looks_like_collection handles numpy coords
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from shapely.geometry import Point


# ── B1: queued-source redirect ───────────────────────────────────────────────

class _FakeBackend:
    def __init__(self, available: bool):
        self._available = available

    def is_available(self, spec=None):
        return (self._available, None if self._available else "not installed")


def _patch_backends(monkeypatch, availability: dict[str, bool]):
    """Replace get_backend with a registry of fake availabilities."""
    import aihydro_data.sources.base as base

    def fake_get_backend(source_id: str):
        return _FakeBackend(availability.get(source_id, False))

    monkeypatch.setattr(base, "get_backend", fake_get_backend)


class TestQueuedRedirect:
    def test_nwis_gauge_does_not_redirect(self, monkeypatch):
        """CONUS gauge fetch: NWIS (direct_api) is first and available → no redirect,
        even though GLOFAS sits at the end of the chain."""
        from aihydro_data.mcp import _would_route_to_queued_source
        _patch_backends(monkeypatch, {
            "direct_api": True, "geoglows_retro": True,
            "openmeteo_flood": True, "cds_glofas": True,
        })
        assert _would_route_to_queued_source("streamflow", "03245500", None) == (False, "")

    def test_global_point_with_instant_backend_does_not_redirect(self, monkeypatch):
        """Global streamflow: GEOGLOWS available → it wins, no redirect."""
        from aihydro_data.mcp import _would_route_to_queued_source
        _patch_backends(monkeypatch, {
            "direct_api": False, "geoglows_retro": True,
            "openmeteo_flood": True, "cds_glofas": True,
        })
        ok, src = _would_route_to_queued_source("streamflow", (52.5, 13.4), None)
        assert (ok, src) == (False, "")

    def test_redirects_when_only_queued_backend_remains(self, monkeypatch):
        """All instant backends down, GloFAS up → redirect fires."""
        from aihydro_data.mcp import _would_route_to_queued_source
        _patch_backends(monkeypatch, {
            "direct_api": False, "geoglows_retro": False,
            "openmeteo_flood": False, "cds_glofas": True,
        })
        ok, src = _would_route_to_queued_source("streamflow", (52.5, 13.4), None)
        assert ok is True
        assert src == "cds_glofas"

    def test_manual_pin_on_queued_product_redirects(self, monkeypatch):
        from aihydro_data.mcp import _would_route_to_queued_source
        _patch_backends(monkeypatch, {"cds_glofas": True})
        ok, src = _would_route_to_queued_source(
            "streamflow", (52.5, 13.4), "GLOFAS_STREAMFLOW")
        assert (ok, src) == (True, "cds_glofas")

    def test_nothing_available_does_not_redirect(self, monkeypatch):
        """No viable candidate → let fetch raise its structured error."""
        from aihydro_data.mcp import _would_route_to_queued_source
        _patch_backends(monkeypatch, {})
        assert _would_route_to_queued_source(
            "streamflow", (52.5, 13.4), None) == (False, "")


# ── B2: NLDI nearest-gauge lookup ────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _nldi_router(responses: dict[str, _FakeResp]):
    """Return a fake requests.get keyed on URL substring."""
    calls: list[str] = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        for key, resp in responses.items():
            if key in url:
                return resp
        return _FakeResp(404, {})

    return fake_get, calls


class TestNldiNearestGauge:
    def _backend(self):
        from aihydro_data.sources.direct_api import Backend
        return Backend()

    def test_happy_path_upstream_main(self, monkeypatch):
        import requests
        fake_get, calls = _nldi_router({
            "/comid/position": _FakeResp(200, {
                "features": [{"properties": {"comid": "1234567"}}]}),
            "/navigation/UM/nwissite": _FakeResp(200, {
                "features": [{"properties": {"identifier": "USGS-03245500"}}]}),
        })
        monkeypatch.setattr(requests, "get", fake_get)
        site = self._backend()._nearest_nwis_gauge(Point(-84.3, 39.4))
        assert site == "03245500"
        assert any("/comid/position" in c for c in calls)

    def test_falls_back_to_downstream_main(self, monkeypatch):
        import requests
        fake_get, calls = _nldi_router({
            "/comid/position": _FakeResp(200, {
                "features": [{"properties": {"comid": "1234567"}}]}),
            "/navigation/UM/nwissite": _FakeResp(200, {"features": []}),
            "/navigation/DM/nwissite": _FakeResp(200, {
                "features": [{"properties": {"identifier": "USGS-01646500"}}]}),
        })
        monkeypatch.setattr(requests, "get", fake_get)
        site = self._backend()._nearest_nwis_gauge(Point(-77.1, 38.9))
        assert site == "01646500"
        assert any("/navigation/DM/" in c for c in calls)

    def test_position_miss_returns_none(self, monkeypatch):
        import requests
        fake_get, _ = _nldi_router({
            "/comid/position": _FakeResp(200, {"features": []}),
        })
        monkeypatch.setattr(requests, "get", fake_get)
        assert self._backend()._nearest_nwis_gauge(Point(0.0, 0.0)) is None

    def test_http_error_returns_none(self, monkeypatch):
        import requests
        fake_get, _ = _nldi_router({})  # everything 404s
        monkeypatch.setattr(requests, "get", fake_get)
        assert self._backend()._nearest_nwis_gauge(Point(-84.3, 39.4)) is None


# ── B3: batch dispatch kwargs + error surfacing ──────────────────────────────

class TestBatchDispatch:
    def test_batch_result_carries_errors(self, monkeypatch):
        import aihydro_data._pipeline as pipeline
        from aihydro_data._pipeline import BatchResult

        err = RuntimeError("boom")

        def fake_batch(variable, geometries, start, end, **kwargs):
            return {"results": {"a": "RESULT_A"}, "errors": {"b": err},
                    "labels": ["a", "b"], "variable": variable,
                    "start": start, "end": end}

        monkeypatch.setattr(pipeline, "fetch_batch", fake_batch)
        out = pipeline.fetch("streamflow", ["03245500", "01646500"],
                             "2020-01-01", "2020-12-31")
        assert isinstance(out, BatchResult)
        assert list(out) == ["RESULT_A"]
        assert out.errors == {"b": err}
        assert out.labels == ["a", "b"]

    def test_all_failed_raises(self, monkeypatch):
        import aihydro_data._pipeline as pipeline
        from aihydro_data.exceptions import SourceUnavailable

        def fake_batch(variable, geometries, start, end, **kwargs):
            return {"results": {}, "errors": {"a": RuntimeError("x"),
                                              "b": RuntimeError("y")},
                    "labels": ["a", "b"], "variable": variable,
                    "start": start, "end": end}

        monkeypatch.setattr(pipeline, "fetch_batch", fake_batch)
        with pytest.raises(SourceUnavailable) as ei:
            pipeline.fetch("streamflow", ["g1", "g2"], "2020-01-01", "2020-12-31")
        assert ei.value.code == "ALL_BATCH_ITEMS_FAILED"

    def test_dispatch_threads_kwargs_to_fetch_batch(self, monkeypatch):
        import aihydro_data._pipeline as pipeline
        captured = {}

        def fake_batch(variable, geometries, start, end, **kwargs):
            captured.update(kwargs)
            return {"results": {}, "errors": {}, "labels": [],
                    "variable": variable, "start": start, "end": end}

        monkeypatch.setattr(pipeline, "fetch_batch", fake_batch)
        sentinel = lambda r: True  # noqa: E731
        pipeline.fetch("ndvi", [(39.0, -94.0), (40.0, -95.0)],
                       "2020-01-01", "2020-12-31",
                       index="NDWI", native_resolution=True, validate=sentinel)
        assert captured["index"] == "NDWI"
        assert captured["native_resolution"] is True
        assert captured["validate"] is sentinel

    def test_fetch_batch_workers_receive_kwargs(self, monkeypatch):
        import aihydro_data._pipeline as pipeline
        seen = []

        def fake_fetch(variable, geom, start, end, **kwargs):
            seen.append(kwargs)
            return f"OK-{geom}"

        monkeypatch.setattr(pipeline, "fetch", fake_fetch)
        sentinel = lambda r: True  # noqa: E731
        out = pipeline.fetch_batch(
            "ndvi", {"p1": Point(-94, 39), "p2": Point(-95, 40)},
            "2020-01-01", "2020-12-31",
            index="NDWI", native_resolution=True, validate=sentinel,
        )
        assert len(out["results"]) == 2
        assert all(k["index"] == "NDWI" for k in seen)
        assert all(k["native_resolution"] is True for k in seen)
        assert all(k["validate"] is sentinel for k in seen)


# ── B5: manifest concurrency ─────────────────────────────────────────────────

class TestManifestConcurrency:
    def test_threaded_appends_lose_nothing(self, tmp_path: Path):
        from aihydro_data.cache.manifest import ManifestEntry, write_manifest, read_manifest

        n_threads, n_appends = 16, 20

        def worker(tid: int):
            for i in range(n_appends):
                write_manifest(tmp_path, ManifestEntry(
                    cache_key="shared_key", variable="precipitation",
                    product=f"P{tid}-{i}", source="gee",
                    start="2020-01-01", end="2020-12-31",
                    geom_wkt="POINT (0 0)", aggregation="basin_mean",
                    fetched_at="2026-06-11T00:00:00+00:00",
                ))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = read_manifest(tmp_path, "shared_key")
        assert len(entries) == n_threads * n_appends
        # File is valid JSON (atomic publication — no torn writes)
        raw = json.loads((tmp_path / "shared_key.manifest.json").read_text())
        assert len(raw) == n_threads * n_appends
        # No stray temp files left behind
        assert not list(tmp_path.glob("*.manifest.tmp"))


# ── B6: hyriver per-product availability ─────────────────────────────────────

class TestHyriverAvailability:
    def _spec(self, **cfg):
        from aihydro_data.contracts import ProductSpec
        return ProductSpec(id="X", variable="precipitation", source="hyriver",
                           backend_config=cfg)

    def test_missing_specific_lib_reports_unavailable(self, monkeypatch):
        import sys
        from aihydro_data.sources.hyriver import Backend
        # sys.modules[name] = None makes __import__(name) raise ImportError
        monkeypatch.setitem(sys.modules, "pygridmet", None)
        ok, reason = Backend().is_available(self._spec(pygridmet_variable="pr"))
        assert ok is False
        assert "pygridmet" in (reason or "")

    def test_present_specific_lib_reports_available(self, monkeypatch):
        import sys
        import types
        from aihydro_data.sources.hyriver import Backend
        monkeypatch.setitem(sys.modules, "pydaymet", types.ModuleType("pydaymet"))
        ok, reason = Backend().is_available(self._spec(pydaymet_variable="prcp"))
        assert ok is True

    def test_no_spec_keeps_any_lib_behaviour(self, monkeypatch):
        import sys
        import types
        from aihydro_data.sources.hyriver import Backend
        # Only py3dep "installed" → backend still broadly available
        for lib in ("pygridmet", "pydaymet", "pygeohydro"):
            monkeypatch.setitem(sys.modules, lib, None)
        monkeypatch.setitem(sys.modules, "py3dep", types.ModuleType("py3dep"))
        ok, _ = Backend().is_available()
        assert ok is True


# ── R7: collection detection with numpy scalars ──────────────────────────────

class TestCollectionHeuristics:
    def test_numpy_latlon_tuple_is_not_collection(self):
        np = pytest.importorskip("numpy")
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection((np.float32(39.1), np.float32(-94.5))) is False

    def test_numpy_bbox_is_not_collection(self):
        np = pytest.importorskip("numpy")
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection(
            [np.float64(-95), np.float64(38), np.float64(-94), np.float64(40)]
        ) is False

    def test_list_of_gauge_ids_is_still_a_collection(self):
        from aihydro_data._pipeline import _looks_like_collection
        assert _looks_like_collection(["03245500", "01646500"]) is True
