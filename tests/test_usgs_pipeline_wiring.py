"""Wave U / U1b wiring: NWIS through the adapter, terminal refusals in the auto chain, provenance on
FetchResult and ManifestEntry, cache integrity (sha256, provisional TTL), the D3 switch, and two
2026-06 review items. Offline: the adapter transport is replaced; HOME is a temp dir."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import Point, shape

import aihydro_data._pipeline as pipeline
from aihydro_data import cache as cache_mod
from aihydro_data.exceptions import SourceUnavailable, TerminalSourceError, GeometryInvalid
from aihydro_data.sources import usgs_waterdata as uw
from tests.test_usgs_waterdata_contract import INDEX, Replay, doc, body, ok, EMPTY
import tests.test_usgs_gauge_resolution as gw

SITE = "01013500"
FULL = ("1989-10-01", "2009-09-30")
PROV = ("2026-06-19", "2026-10-17")


@pytest.fixture(autouse=True)
def scratch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AIHYDRO_USGS_BACKEND", raising=False)
    monkeypatch.delenv("AIHYDRO_DATA_PROVISIONAL_TTL_DAYS", raising=False)
    monkeypatch.delenv("API_USGS_PAT", raising=False)
    return tmp_path


@pytest.fixture
def replay(monkeypatch):
    r = Replay()
    monkeypatch.setattr(uw, "_http_get", r)
    return r


def _manifest(ck):
    return cache_mod.manifest.latest_manifest(cache_mod.cache_dir(), ck) if hasattr(cache_mod, "manifest") else None


def _latest(ck):
    from aihydro_data.cache.manifest import latest_manifest
    return latest_manifest(cache_mod.cache_dir(), ck)


# ---------------------------------------------------------------- explicit gauge, full provenance
def test_gauge_id_fetch_carries_columns_resolution_quality_upstream(replay):
    r = pipeline.fetch("streamflow", SITE, *FULL)
    assert r.product == "NWIS_STREAMFLOW" and r.units == "m3/s" and r.source == "direct_api"
    df = r.data
    assert len(df) == 7305
    assert {"date", "streamflow", "value_native", "approval_status", "approval_code", "qualifier", "qualifier_code"} <= set(df.columns)
    assert (df["streamflow"] == df["value_native"] * 0.028316846592).all()
    q = r.quality
    assert sum(q["approval_counts"].values()) == q["n_rows"] == len(df)                       # (d)
    assert q["provisional_fraction"] == 0.0 and q["time_series_id"] == "aa8ac20eb72d4fa58ea9c0eaf61e10ee"
    assert r.site_resolution["method"] == "explicit_id" and r.site_resolution["site"] == f"USGS-{SITE}"
    assert r.site_resolution["rule_version"] == "ADR-003-A2-2026-10-17"
    assert r.upstream["response_sha256"] and r.upstream["last_modified"] == [None] and r.upstream["etag"] == [None]
    assert "aihydro_site_resolution" not in df.attrs and "aihydro_upstream" not in df.attrs
    mf = _latest(r.cache_key)                                                                  # (e)
    assert mf.data_sha256 and mf.site_resolution["site"] == f"USGS-{SITE}" and mf.quality == r.quality
    assert mf.upstream["response_sha256"] == r.upstream["response_sha256"]
    assert "waterdata.usgs.gov" in r.citation and "nwis" not in r.citation.lower().replace("waterdata", "")


def test_cache_hit_restores_provenance_and_rehashes(replay):
    r1 = pipeline.fetch("streamflow", SITE, *FULL)
    n = len(replay.calls)
    r2 = pipeline.fetch("streamflow", SITE, *FULL)
    assert r2.cache_hit and len(replay.calls) == n
    assert r2.quality == r1.quality and r2.site_resolution == r1.site_resolution
    assert r2.upstream["data_sha256"] == _latest(r1.cache_key).data_sha256
    # tamper with the cached bytes: the entry must be a miss, not served
    pq = Path(cache_mod.cache_dir()) / f"{r1.cache_key}.parquet"
    pq.write_bytes(pq.read_bytes() + b"x")
    r3 = pipeline.fetch("streamflow", SITE, *FULL)
    assert not r3.cache_hit and len(replay.calls) > n
    assert any(h.get("reason") == "cached data file sha256 mismatch" for h in r3.fallback_history)


def test_streamflow_cache_revision_was_bumped():
    assert pipeline._VARIABLE_CACHE_REVISION["streamflow"] >= 2
    assert pipeline._result_schema_for("streamflow").endswith(".streamflow.2")


# ---------------------------------------------------------------- provisional TTL (f)
def _age_manifest(ck, days):
    p = Path(cache_mod.cache_dir()) / f"{ck}.manifest.json"
    entries = json.loads(p.read_text())
    entries[-1]["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    p.write_text(json.dumps(entries))


def test_provisional_entry_expires_after_the_ttl(replay):
    r1 = pipeline.fetch("streamflow", SITE, *PROV)
    assert 0 < r1.quality["provisional_fraction"] < 1
    assert pipeline.fetch("streamflow", SITE, *PROV).cache_hit                      # fresh: served
    _age_manifest(r1.cache_key, 8)
    r3 = pipeline.fetch("streamflow", SITE, *PROV)
    assert not r3.cache_hit
    assert {"outcome": "rejected", "reason": "provisional entry expired", "product": "streamflow", "source": "cache"} in r3.fallback_history


def test_provisional_ttl_is_tunable(replay, monkeypatch):
    r1 = pipeline.fetch("streamflow", SITE, *PROV)
    _age_manifest(r1.cache_key, 30)
    monkeypatch.setenv("AIHYDRO_DATA_PROVISIONAL_TTL_DAYS", "60")
    assert pipeline.fetch("streamflow", SITE, *PROV).cache_hit
    monkeypatch.setenv("AIHYDRO_DATA_PROVISIONAL_TTL_DAYS", "garbage")
    assert cache_mod.provisional_ttl_days() == 7.0


def test_fully_approved_entries_never_expire(replay):
    r1 = pipeline.fetch("streamflow", SITE, *FULL)
    _age_manifest(r1.cache_key, 4000)
    assert pipeline.fetch("streamflow", SITE, *FULL).cache_hit


# ---------------------------------------------------------------- outlet path through fetch()
def _basin_polygon():
    f = doc("r_comid_724696_basin" if "r_comid_724696_basin" in INDEX else next(n for n in INDEX if n.endswith("comid_724696_basin")))
    return shape(f["features"][0]["geometry"])


def test_polygon_plus_outlet_resolves_and_is_recorded(replay):
    lon, lat = doc("nldi_nwissite_01013500")["features"][0]["geometry"]["coordinates"][:2]
    poly = _basin_polygon()
    r = pipeline.fetch("streamflow", poly, *FULL, outlet=(lat, lon))                       # (lat, lon) as documented
    sr = r.site_resolution
    assert (sr["method"], sr["site"], sr["comid_outlet"], sr["area_basis"]) == ("outlet_snap", "USGS-01013500", 724696, "request_geometry")
    assert sr["distance_m"] == 0 and abs(sr["log_area_ratio"]) < 0.02                         # (c)
    mf = _latest(r.cache_key)
    assert mf.site_resolution["method"] == "outlet_snap" and mf.site_resolution["thresholds_overridden"] is False
    assert len(r.data) == 7305


def test_threshold_override_reaches_the_manifest(monkeypatch):
    """T23: a library override (not reachable from the MCP surface) is recorded in the manifest."""
    import functools
    from tests.test_usgs_waterdata_contract import _feat
    w = gw.mk([(-7000, 1.0, {})])                                  # outside the default 5 km
    def transport(url, params=None, headers=None):
        if "/daily/items" in url:
            return ok({"features": [_feat("2020-01-01"), _feat("2020-01-02")], "links": []})
        return w(url, params, headers)
    monkeypatch.setattr(uw, "_http_get", transport)
    lon, lat = w.outlet
    with pytest.raises(TerminalSourceError):                       # default thresholds refuse
        pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", outlet=(lat, lon), cache=False)
    monkeypatch.setattr(uw, "resolve_gauge", functools.partial(uw.resolve_gauge, max_distance_m=25000))
    r = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", outlet=(lat, lon))
    assert r.site_resolution["thresholds_overridden"] is True and r.site_resolution["thresholds"]["max_distance_m"] == 25000.0
    mf = _latest(r.cache_key)
    assert mf.site_resolution["thresholds_overridden"] is True


# ---------------------------------------------------------------- terminal refusals: no modelled fallthrough
class _Boom:
    called = False


def _forbid_modelled(monkeypatch):
    from aihydro_data.sources import base
    real = base.get_backend

    def guard(source_id):
        if source_id != "direct_api":
            _Boom.called = True
            raise AssertionError(f"fallback reached modelled backend {source_id}")
        return real(source_id)
    monkeypatch.setattr(base, "get_backend", guard)


def test_T5_polygon_without_outlet_refuses_and_never_serves_modelled(monkeypatch):
    calls = []
    monkeypatch.setattr(uw, "_http_get", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("network")))
    _forbid_modelled(monkeypatch)
    poly = Point(gw.LON0, gw.LAT0).buffer(0.2)
    with pytest.raises(TerminalSourceError) as ei:
        pipeline.fetch("streamflow", poly, *FULL)
    assert ei.value.code == "GAUGE_REQUIRED" and calls == []
    hist = ei.value.details["fallback_history"]
    assert [h["product"] for h in hist] == ["NWIS_STREAMFLOW"] and hist[0]["outcome"] == "refused"


def test_T24_ambiguous_in_auto_mode_is_terminal(monkeypatch):
    w = gw.mk([(-800, 0.98, {}), (2000, 1.03, {})])
    monkeypatch.setattr(uw, "_http_get", w)
    _forbid_modelled(monkeypatch)
    lon, lat = w.outlet
    with pytest.raises(TerminalSourceError) as ei:
        pipeline.fetch("streamflow", w.basin, *FULL, outlet=(lat, lon))
    assert ei.value.code == "GAUGE_AMBIGUOUS" and not _Boom.called
    assert [h["outcome"] for h in ei.value.details["fallback_history"]] == ["refused"]
    assert len(ei.value.details["candidates"]) == 2


def test_not_found_and_no_data_are_terminal_too(monkeypatch):
    w = gw.mk([(-5001, 1.0, {})])
    monkeypatch.setattr(uw, "_http_get", w)
    _forbid_modelled(monkeypatch)
    lon, lat = w.outlet
    with pytest.raises(TerminalSourceError) as ei:
        pipeline.fetch("streamflow", w.basin, *FULL, outlet=(lat, lon))
    assert ei.value.code == "GAUGE_NOT_FOUND"


def test_transport_failure_may_fall_through_and_is_recorded(monkeypatch):
    """"Could not look" is not a refusal: the chain may continue and says why."""
    def fail(*a, **k):
        raise uw._err("USGS_TIMEOUT", "timeout")
    monkeypatch.setattr(uw, "_http_get", fail)
    with pytest.raises(SourceUnavailable) as ei:
        pipeline.fetch("streamflow", SITE, *FULL, mode="manual", product="NWIS_STREAMFLOW", fallback=[])
    assert ei.value.code == "ALL_BACKENDS_FAILED" and "USGS_TIMEOUT" in ei.value.details["fallback_history"][0]["reason"]


# ---------------------------------------------------------------- D3 legacy switch
def test_legacy_switch_uses_dataretrieval_and_records_it(monkeypatch):
    called = []
    monkeypatch.setattr(uw, "_http_get", Replay())           # gauge resolution only
    stub = types.ModuleType("dataretrieval.nwis")
    idx = pd.date_range("2020-01-01", periods=3, tz="UTC", name="datetime")
    def get_dv(**kw):
        called.append(kw)
        return pd.DataFrame({"00060_Mean": [10.0, 20.0, 30.0], "00060_Mean_cd": ["A", "A e", "P"]}, index=idx), None
    stub.get_dv = get_dv
    monkeypatch.setitem(sys.modules, "dataretrieval.nwis", stub)
    monkeypatch.setenv("AIHYDRO_USGS_BACKEND", "legacy_waterservices")
    monkeypatch.setattr(uw, "get_daily", lambda *a, **k: (_ for _ in ()).throw(AssertionError("adapter data path used")))
    r = pipeline.fetch("streamflow", SITE, "2020-01-01", "2020-01-03", cache=False)
    assert called and r.data["approval_code"].tolist() == ["A", "A", "P"] and r.data["qualifier_code"].tolist() == ["", "e", ""]
    assert r.upstream["backend"] == "legacy_waterservices" and r.upstream["removal_date"] == "2027-01-08"
    assert r.quality["provisional_fraction"] == pytest.approx(1 / 3)


def test_legacy_switch_unknown_codes_are_refused(monkeypatch):
    monkeypatch.setattr(uw, "_http_get", Replay())
    stub = types.ModuleType("dataretrieval.nwis")
    idx = pd.date_range("2020-01-01", periods=1, tz="UTC", name="datetime")
    stub.get_dv = lambda **kw: (pd.DataFrame({"00060_Mean": [1.0], "00060_Mean_cd": ["A R"]}, index=idx), None)
    monkeypatch.setitem(sys.modules, "dataretrieval.nwis", stub)
    monkeypatch.setenv("AIHYDRO_USGS_BACKEND", "legacy_waterservices")
    with pytest.raises(TerminalSourceError) as ei:
        pipeline.fetch("streamflow", SITE, "2020-01-01", "2020-01-01", cache=False)
    assert ei.value.code == "USGS_APPROVAL_UNKNOWN"


# ---------------------------------------------------------------- acceptance (a): no legacy references
def test_no_legacy_references_outside_the_d3_branch():
    import re
    root = Path(pipeline.__file__).parent
    pat = re.compile(r"waterservices\.usgs\.gov|dataretrieval(\.| import )nwis|hydrofunctions|NWIS\(\)")
    hits = {}
    for p in root.rglob("*.py"):
        if "_gee_vendored" in p.parts:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if pat.search(line):
                hits.setdefault(p.name, []).append((i, line.strip()))
    assert set(hits) <= {"usgs_waterdata.py", "direct_api.py"}, hits
    # direct_api: only inside the D3 function
    src = (root / "sources" / "direct_api.py").read_text().splitlines()
    start = next(i for i, l in enumerate(src, 1) if l.startswith("def _legacy_waterservices_daily"))
    end = next(i for i, l in enumerate(src, 1) if i > start and l.startswith("class Backend"))
    assert all(start <= i < end for i, _ in hits.get("direct_api.py", [])), hits
    assert "_nearest_nwis_gauge" not in "\n".join(src) and "_NLDI_BASE" not in "\n".join(src)
    assert "hydrofunctions" not in (root.parent / "pyproject.toml").read_text()
    assert "legacy-usgs" in (root.parent / "pyproject.toml").read_text()


def test_spec_and_citation_point_at_the_water_data_api():
    from aihydro_data.products import get_product
    s = get_product("NWIS_STREAMFLOW")
    assert s.requires_extras == [] and "api.waterdata.usgs.gov" in s.homepage and "api.waterdata.usgs.gov" in s.citation
    assert "waterservices" not in s.citation + s.bibtex + s.homepage


# ---------------------------------------------------------------- 2026-06 review items
def test_empty_results_are_not_cached_as_success(monkeypatch):
    from aihydro_data.sources.direct_api import Backend
    monkeypatch.setattr(Backend, "fetch_timeseries",
                        lambda self, spec, geom, start, end, agg, outlet=None: pd.DataFrame(columns=["date", "streamflow"]))
    with pytest.raises(SourceUnavailable):
        pipeline.fetch("streamflow", SITE, *FULL, mode="manual", product="NWIS_STREAMFLOW", fallback=[])
    assert cache_mod.cache_status()["entry_count"] == 0


def test_batch_forwards_region_instead_of_dropping_it(monkeypatch):
    seen = []
    def fake_fetch(variable, geom, start, end, **kw):
        seen.append(kw.get("region"))
        raise RuntimeError("stop")
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    pipeline.fetch_batch("streamflow", ["03245500", "01646500"], "2020-01-01", "2020-01-02", region="CONUS", on_error="skip")
    assert seen == ["CONUS", "CONUS"]


def test_collection_dispatch_forwards_region_and_refuses_a_lone_outlet(monkeypatch):
    got = {}
    def fake_batch(variable, geometries, start, end, **kw):
        got.update(kw)
        return {"results": {}, "errors": {}, "labels": [], "variable": variable, "start": start, "end": end}
    monkeypatch.setattr(pipeline, "fetch_batch", fake_batch)
    pipeline.fetch("streamflow", ["03245500", "01646500"], "2020-01-01", "2020-01-02", region="CONUS")
    assert got["region"] == "CONUS"
    with pytest.raises(GeometryInvalid) as ei:
        pipeline.fetch("streamflow", ["03245500", "01646500"], "2020-01-01", "2020-01-02", outlet=(40.0, -90.0))
    assert ei.value.code == "OUTLET_WITH_COLLECTION"
