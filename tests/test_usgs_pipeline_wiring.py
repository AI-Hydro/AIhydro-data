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


# ---------------------------------------------------------------- U1b review: cache key, sha on the miss result, legacy rules
def test_cache_key_depends_on_the_usgs_backend(replay, monkeypatch):
    k1 = pipeline.fetch("streamflow", SITE, *FULL, cache=False).cache_key
    monkeypatch.setenv("AIHYDRO_USGS_BACKEND", "legacy_waterservices")
    stub = types.ModuleType("dataretrieval.nwis")
    idx = pd.date_range("1989-10-01", periods=2, tz="UTC", name="datetime")
    stub.get_dv = lambda **kw: (pd.DataFrame({"00060_Mean": [1.0, 2.0], "00060_Mean_cd": ["A", "A"]}, index=idx), None)
    monkeypatch.setitem(sys.modules, "dataretrieval.nwis", stub)
    k2 = pipeline.fetch("streamflow", SITE, *FULL, cache=False).cache_key
    assert k1 != k2


def test_miss_result_carries_data_sha256(replay):
    r = pipeline.fetch("streamflow", SITE, *FULL)
    assert not r.cache_hit and r.upstream["data_sha256"] == _latest(r.cache_key).data_sha256


def test_no_cache_entry_when_the_hash_cannot_be_computed(replay, monkeypatch):
    monkeypatch.setattr(cache_mod, "file_sha256", lambda p: (_ for _ in ()).throw(OSError("nope")))
    r = pipeline.fetch("streamflow", SITE, *FULL)
    assert cache_mod.cache_status()["entry_count"] == 0 and not list(Path(cache_mod.cache_dir()).glob("*.parquet"))
    assert "data_sha256" not in r.upstream


@pytest.mark.parametrize("val", [-999999.0, float("inf")])
def test_legacy_path_refuses_sentinel_and_non_finite(monkeypatch, val):
    monkeypatch.setattr(uw, "_http_get", Replay())
    stub = types.ModuleType("dataretrieval.nwis")
    idx = pd.date_range("2020-01-01", periods=2, tz="UTC", name="datetime")
    stub.get_dv = lambda **kw: (pd.DataFrame({"00060_Mean": [1.0, val], "00060_Mean_cd": ["A", "A"]}, index=idx), None)
    monkeypatch.setitem(sys.modules, "dataretrieval.nwis", stub)
    monkeypatch.setenv("AIHYDRO_USGS_BACKEND", "legacy_waterservices")
    with pytest.raises(TerminalSourceError) as ei:
        pipeline.fetch("streamflow", SITE, "2020-01-01", "2020-01-02", cache=False)
    assert ei.value.code in ("USGS_VALUE_SENTINEL", "USGS_VALUE_UNPARSEABLE")


def test_legacy_path_keeps_genuine_missing_and_flags_negatives(monkeypatch):
    monkeypatch.setattr(uw, "_http_get", Replay())
    stub = types.ModuleType("dataretrieval.nwis")
    idx = pd.date_range("2020-01-01", periods=3, tz="UTC", name="datetime")
    stub.get_dv = lambda **kw: (pd.DataFrame({"00060_Mean": [1.0, float("nan"), -2.0], "00060_Mean_cd": ["A", "A", "A"]}, index=idx), None)
    monkeypatch.setitem(sys.modules, "dataretrieval.nwis", stub)
    monkeypatch.setenv("AIHYDRO_USGS_BACKEND", "legacy_waterservices")
    r = pipeline.fetch("streamflow", SITE, "2020-01-01", "2020-01-03", cache=False)
    assert len(r.data) == 2 and r.quality["n_null_values"] == 1 and r.quality["n_negative"] == 1
    assert r.data["negative_flag"].tolist() == [False, True]


# ---------------------------------------------------------------- gauge identity through fetch() (A2 R0, T1-T4 end to end)
def _id_world(monkeypatch, ratio=1.0):
    from tests.test_usgs_waterdata_contract import _feat
    b = gw.basin_area(gw.World())
    w = gw.World([gw.site(1013500, 0, ratio, basin_km2=b)])
    def transport(url, params=None, headers=None):
        if "/daily/items" in url:
            return ok({"features": [_feat("2020-01-01", v="3"), _feat("2020-01-02", v="4")], "links": []})
        return w(url, params, headers)
    monkeypatch.setattr(uw, "_http_get", transport)
    return w


def test_T1_gauge_id_with_polygon_through_fetch(monkeypatch):
    w = _id_world(monkeypatch)
    r = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", gauge_id="01013500")
    sr = r.site_resolution
    assert (sr["method"], sr["site"], sr["area_basis"], sr["area_check"]) == ("explicit_id", "USGS-01013500", "request_geometry", "pass")
    assert abs(sr["area_ratio"] - 1.0) < 1e-9 and not [c for c in w.calls if "/navigation/" in c]
    assert _latest(r.cache_key).site_resolution["method"] == "explicit_id"


def test_T2_area_mismatch_warns_in_result_and_manifest_never_refuses(monkeypatch):
    w = _id_world(monkeypatch, ratio=1 / 1.5)
    r = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", gauge_id="USGS-01013500")
    assert r.site_resolution["area_check"] == "fail" and r.data is not None and len(r.data) == 2
    assert any("area_check=fail" in x for x in r.site_resolution["warnings"]) and any("area_check=fail" in n for n in r.notes)
    assert any("area_check=fail" in x for x in _latest(r.cache_key).site_resolution["warnings"])


def test_T3_alias_path_and_unverified_warning(monkeypatch):
    w = _id_world(monkeypatch)
    ref = {"aliases": [{"scheme": "usgs", "id": "01013500", "relation": "same_as", "verified": True}]}
    r = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", basin_ref=ref)
    assert (r.site_resolution["method"], r.site_resolution["alias_verified"], r.site_resolution["warnings"]) == ("basin_ref_alias", True, [])
    ref2 = {"aliases": [{"scheme": "usgs", "id": "01013500", "verified": False}]}
    r2 = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", basin_ref=ref2)
    assert r2.site_resolution["alias_verified"] is False and r2.cache_key != r.cache_key
    assert any("not verified" in x for x in r2.site_resolution["warnings"])


def test_T4_identity_conflict_is_terminal_end_to_end(monkeypatch):
    w = _id_world(monkeypatch)
    _forbid_modelled(monkeypatch)
    ref = {"aliases": [{"scheme": "usgs", "id": "01646500", "verified": True}]}
    with pytest.raises(TerminalSourceError) as ei:
        pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", gauge_id="01013500", basin_ref=ref)
    assert ei.value.code == "GAUGE_AMBIGUOUS" and ei.value.details["reason"] == "identity_conflict"
    assert [h["outcome"] for h in ei.value.details["fallback_history"]] == ["refused"]
    with pytest.raises(TerminalSourceError) as e2:                      # geometry names one gauge, gauge_id another
        pipeline.fetch("streamflow", "01013500", "2020-01-01", "2020-01-02", gauge_id="01646500")
    assert e2.value.details["reason"] == "identity_conflict"


def test_gauge_identity_is_in_the_cache_key(monkeypatch):
    w = _id_world(monkeypatch)
    a = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", gauge_id="01013500", cache=False).cache_key
    b = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", gauge_id="USGS-01013500", cache=False).cache_key
    assert a == b
    d = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", outlet=(gw.LAT0, gw.LON0), cache=False).cache_key
    assert a != d


def test_collection_requires_per_item_identity(monkeypatch):
    with pytest.raises(GeometryInvalid) as ei:
        pipeline.fetch("streamflow", ["03245500", "01646500"], "2020-01-01", "2020-01-02", gauge_id="03245500")
    assert ei.value.code == "GAUGE_WITH_COLLECTION"
    with pytest.raises(GeometryInvalid):                                  # one BasinRef is not a per-item mapping
        pipeline.fetch("streamflow", ["03245500", "01646500"], "2020-01-01", "2020-01-02",
                       basin_ref={"aliases": [{"scheme": "usgs", "id": "03245500"}]})
    got = {}
    def fake_batch(variable, geometries, start, end, **kw):
        got.update(kw)
        return {"results": {}, "errors": {}, "labels": [], "variable": variable, "start": start, "end": end}
    monkeypatch.setattr(pipeline, "fetch_batch", fake_batch)
    pipeline.fetch("streamflow", {"a": "03245500", "b": "01646500"}, "2020-01-01", "2020-01-02",
                   gauge_id={"a": "03245500"}, basin_ref={"b": {"aliases": []}})
    assert got["gauge_ids"] == {"a": "03245500"} and got["basin_refs"] == {"b": {"aliases": []}}


def test_batch_forwards_per_label_identity(monkeypatch):
    seen = {}
    def fake_fetch(variable, geom, start, end, **kw):
        seen[str(geom)] = (kw.get("gauge_id"), kw.get("basin_ref"))
        raise RuntimeError("stop")
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    pipeline.fetch_batch("streamflow", {"a": Point(0, 0), "b": Point(1, 1)}, "2020-01-01", "2020-01-02",
                         gauge_ids={"a": "03245500"}, basin_refs={"b": {"aliases": []}}, on_error="skip")
    assert seen[str(Point(0, 0))] == ("03245500", None) and seen[str(Point(1, 1))] == (None, {"aliases": []})


def test_outlet_cache_key_is_rounded_to_1e_minus_7(monkeypatch):
    w = _id_world(monkeypatch)
    lat, lon = gw.LAT0, gw.LON0
    a = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", outlet=(lat, lon), gauge_id="01013500", cache=False).cache_key
    b = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", outlet=(lat + 2e-9, lon - 2e-9), gauge_id="01013500", cache=False).cache_key
    c = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", outlet=(lat + 1e-6, lon), gauge_id="01013500", cache=False).cache_key
    assert a == b and a != c


def test_version_is_0_2_5():
    from aihydro_data._version import __version__
    assert __version__ == "0.2.5"


# ---------------------------------------------------------------- final polish
def test_batch_rejects_identity_keys_that_match_no_label():
    with pytest.raises(GeometryInvalid) as ei:
        pipeline.fetch_batch("streamflow", {"a": Point(0, 0)}, "2020-01-01", "2020-01-02", gauge_ids={"typo": "03245500"})
    assert ei.value.code == "GAUGE_LABEL_UNKNOWN"
    with pytest.raises(GeometryInvalid) as e2:
        pipeline.fetch_batch("streamflow", {"a": Point(0, 0)}, "2020-01-01", "2020-01-02", basin_refs={"b": {"aliases": []}})
    assert e2.value.code == "GAUGE_LABEL_UNKNOWN"


def test_agreeing_explicit_id_and_alias_records_alias_verified(monkeypatch):
    w = _id_world(monkeypatch)
    ref = {"aliases": [{"scheme": "usgs", "id": "01013500", "verified": True}]}
    r = pipeline.fetch("streamflow", w.basin, "2020-01-01", "2020-01-02", gauge_id="01013500", basin_ref=ref)
    assert r.site_resolution["method"] == "explicit_id" and r.site_resolution["alias_verified"] is True


@pytest.mark.parametrize("bad", [1013500, 13500000, 3.5, ["01013500"]])
def test_non_string_gauge_id_is_a_coded_refusal(bad):
    with pytest.raises(GeometryInvalid) as ei:
        pipeline.fetch("streamflow", Point(-90, 40).buffer(0.1), "2020-01-01", "2020-01-02", gauge_id=bad)
    assert ei.value.code == "GAUGE_ID_INVALID"
    with pytest.raises(SourceUnavailable) as e2:
        uw.resolve_gauge(gauge_id=bad)
    assert e2.value.code == "USGS_SITE_INVALID"


def test_non_mapping_basin_ref_is_a_coded_refusal():
    with pytest.raises(GeometryInvalid) as ei:
        pipeline.fetch("streamflow", "01013500", "2020-01-01", "2020-01-02", basin_ref="usgs:01013500")
    assert ei.value.code == "GAUGE_REF_INVALID"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_terminal_and_name_the_key(monkeypatch, status):
    monkeypatch.setattr(uw, "_http_get", lambda *a, **k: (status, {}, b'{"error":"denied"}'))
    with pytest.raises(TerminalSourceError) as ei:
        uw._request(uw.BASE + "/collections/daily/items", {"f": "json"}, uw._new_record())
    assert ei.value.code == "USGS_HTTP_ERROR" and "API_USGS_PAT" in ei.value.recovery
