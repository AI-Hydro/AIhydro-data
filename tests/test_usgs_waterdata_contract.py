"""Schema-contract, behaviour and legacy-parity tests for the USGS Water Data
adapter (Wave U / U1a). Fully offline: ``_http_get`` is replaced by a replay of
the bodies recorded in tests/fixtures/usgs_waterdata/ (gzip supported).
"""
from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pandas as pd
import pytest

from aihydro_data.exceptions import SourceUnavailable
from aihydro_data.sources import usgs_waterdata as uw

FIX = Path(__file__).parent / "fixtures" / "usgs_waterdata"
INDEX = {e["name"]: e for e in json.loads((FIX / "index.json").read_text())["entries"]}
LEGACY = Path(FIX / "legacy_waterservices")
LEGACY_INDEX = {e["name"]: e for e in json.loads((LEGACY / "index.json").read_text())["entries"]}


def _read(path: Path) -> bytes:
    b = path.read_bytes()
    return gzip.decompress(b) if path.suffix == ".gz" else b


def body(name: str) -> bytes:
    return _read(FIX / INDEX[name]["file"])


def doc(name: str) -> dict:
    return json.loads(body(name))


def _key(url: str, params=None) -> tuple:
    if params:
        from urllib.parse import urlencode
        url = url + "?" + urlencode(params)
    u = urlparse(url)
    q = []
    for k, v in parse_qsl(u.query):
        if k == "bbox":
            v = ",".join(f"{float(x):.4f}" for x in v.split(","))
        q.append((k, v))
    return (u.hostname, u.path, tuple(sorted(q)))


class Replay:
    """Serves recorded bodies; ``override`` (callable(url, params) -> tuple|None) goes first."""

    def __init__(self, override=None):
        self.table = {_key(e["url"]): e for e in INDEX.values()}
        self.override = override
        self.calls: list[tuple] = []

    def __call__(self, url, params=None, headers=None):
        self.calls.append((url, dict(params) if params else None))
        if self.override:
            r = self.override(url, params)
            if r is not None:
                return r
        e = self.table.get(_key(url, params))
        assert e is not None, f"unrecorded request: {url} {params}"
        return e["status"], dict(e["headers"]), _read(FIX / e["file"])


def ok(obj, status=200, headers=None):
    return status, {"Date": "Sat, 17 Oct 2026 00:00:00 GMT", **(headers or {})}, json.dumps(obj).encode()


EMPTY = {"type": "FeatureCollection", "features": [], "numberReturned": 0, "links": []}


@pytest.fixture
def replay(monkeypatch):
    r = Replay()
    monkeypatch.setattr(uw, "_http_get", r)
    return r


def _feat(t, v="1.0", ts="ts1", ap="Approved", q=None, unit="ft^3/s", lm=None):
    return {"type": "Feature", "properties": {
        "time_series_id": ts, "monitoring_location_id": "USGS-01013500", "parameter_code": "00060",
        "statistic_id": "00003", "time": t, "value": v, "unit_of_measure": unit,
        "approval_status": ap, "qualifier": q, "last_modified": lm}, "geometry": None}


# ------------------------------------------------------------- schema contract
RECORDED_DAILY = ["daily_01013500_wy1990_2009", "daily_01516500_wy1990_2009", "daily_01109000_2019",
                  "daily_01109000_2020", "daily_03245500_2010_2020", "daily_01013500_last120d_provisional"]
DAILY_PROPS = {"time_series_id", "monitoring_location_id", "parameter_code", "statistic_id", "time", "value",
               "unit_of_measure", "approval_status", "qualifier", "last_modified"}


@pytest.mark.parametrize("name", RECORDED_DAILY)
def test_daily_schema_exact_field_names(name):
    feats = doc(name)["features"]
    assert feats
    for f in feats:
        p = f["properties"]
        assert set(p) == DAILY_PROPS            # exact names: approval_status, NOT approvals_status
        assert "approvals_status" not in p
        assert isinstance(p["value"], str)       # values arrive as strings
        assert p["qualifier"] is None or (isinstance(p["qualifier"], list) and all(isinstance(t, str) for t in p["qualifier"]))
        assert p["unit_of_measure"] == "ft^3/s"
        assert p["approval_status"] in ("Approved", "Provisional")
        assert len(p["time"]) == 10             # daily: calendar date, no zone


def test_continuous_schema_is_utc_and_has_method_category():
    p = doc("continuous_01013500_7d")["features"][0]["properties"]
    assert set(p) == DAILY_PROPS | {"method_category"}
    assert p["time"].endswith("+00:00") and p["statistic_id"] == "00011"


def test_metadata_has_primary_flag_and_series_ids():
    for s in ("01013500", "01516500", "01109000", "03245500"):
        for f in doc(f"time_series_metadata_{s}")["features"]:
            p = f["properties"]
            assert {"id", "primary", "parameter_code", "statistic_id", "unit_of_measure"} <= set(p)
            assert p["primary"] == "Primary"
            assert f["properties"]["id"] == f["id"]


def test_documents_have_no_total_count_and_use_cursor_next():
    d = doc("daily_01013500_wy1990_2009_paging_limit2000")
    assert "numberMatched" not in d and d["numberReturned"] == 2000
    nxt = [l for l in d["links"] if l["rel"] == "next"]
    assert len(nxt) == 1 and "cursor=" in nxt[0]["href"]
    last = doc("daily_01013500_wy1990_2009_paging_limit2000_page4")
    assert not [l for l in last["links"] if l["rel"] == "next"] and last["numberReturned"] == 1305


def test_recorded_index_matches_bodies_and_has_no_key():
    for e in list(INDEX.values()):
        assert "api_key" not in e["url"].lower() and "x-api-key" not in {k.lower() for k in e["headers"]}
        import hashlib
        assert hashlib.sha256(body(e["name"])).hexdigest() == e["body_sha256"]
    assert not [k for e in INDEX.values() for k in e["headers"] if k.lower() in ("last-modified", "etag")]


# ------------------------------------------------------------- get_daily
def test_daily_01013500_full_period(replay):
    s = uw.get_daily("01013500", "1989-10-01", "2009-09-30")
    df = s.frame
    assert (s.site_id, s.time_series_id, s.units_native) == ("USGS-01013500", "aa8ac20eb72d4fa58ea9c0eaf61e10ee", "ft^3/s")
    assert len(df) == 7305 and df["time"].is_monotonic_increasing and df["time"].dt.tz is None
    assert df["time"].iloc[0] == pd.Timestamp("1989-10-01") and df["time"].iloc[-1] == pd.Timestamp("2009-09-30")
    assert not df["value_native"].isna().any()
    assert (df["value_m3s"] == df["value_native"] * 0.028316846592).all()
    assert set(df.columns) >= {"time", "value_native", "value_m3s", "approval_status", "approval_code", "qualifier", "qualifier_code"}
    qs = s.quality_summary
    assert qs["approval_counts"] == {"Approved": 7305} and qs["provisional_fraction"] == 0.0
    assert qs["qualifier_counts"] == {"ESTIMATED": 2100} and qs["qualifiers_other"] == {}
    assert sum(qs["approval_counts"].values()) == qs["n_rows"] == len(df)
    assert (df.loc[df["qualifier"].map(len) > 0, "qualifier_code"] == "e").all()
    up = s.upstream
    assert up["api_version"] == "ogcapi/v1" and up["n_pages"] == 1
    assert up["last_modified"] == [None] and up["etag"] == [None]            # server does not send them
    assert up["response_sha256"] == [INDEX["daily_01013500_wy1990_2009"]["body_sha256"]]
    assert up["x_build_version"] == ["1.10.3-7de16842"] and up["date"][0].endswith("GMT")
    assert up["feature_last_modified_max"].startswith("2025-")
    assert all("key" not in u.lower() for u in up["request_urls"])


def test_daily_other_qualifiers_are_kept_and_reported(replay):
    s = uw.get_daily("03245500", "2010-01-01", "2020-12-31")
    qs = s.quality_summary
    assert len(s.frame) == 4018
    assert qs["qualifiers_other"] == {"FORCEINTERPOLATION": 35}
    assert qs["qualifier_counts"] == {"ESTIMATED": 212, "FORCEINTERPOLATION": 35}
    mixed = s.frame[s.frame["qualifier"].map(lambda q: len(q) == 2)]
    assert len(mixed) == 4 and set(mixed["qualifier_code"]) == {"e,FORCEINTERPOLATION"}
    assert set(s.frame.loc[s.frame["qualifier"] == ("FORCEINTERPOLATION",), "qualifier_code"]) == {"FORCEINTERPOLATION"}


def test_daily_provisional_is_reported(replay):
    s = uw.get_daily("01013500", "2026-06-19", "2026-10-17")
    qs = s.quality_summary
    assert qs["approval_counts"] == {"Approved": 77, "Provisional": 43} and qs["n_rows"] == 120
    assert qs["provisional_fraction"] == 43 / 120
    assert qs["provisional_first"] and qs["provisional_last"] and qs["provisional_first"] <= qs["provisional_last"]
    assert set(s.frame["approval_code"]) == {"A", "P"}


def test_paging_follows_next_links_and_matches_single_page(replay):
    paged = uw.get_daily("01013500", "1989-10-01", "2009-09-30", limit=2000)
    assert paged.upstream["n_pages"] == 4 and len(paged.upstream["response_sha256"]) == 4
    single = uw.get_daily("01013500", "1989-10-01", "2009-09-30")
    pd.testing.assert_frame_equal(paged.frame, single.frame)
    assert any("cursor=" in u for u in paged.upstream["request_urls"])


def test_nonexistent_site_is_site_not_found_not_empty_success(monkeypatch):
    def ov(url, params):
        if "time-series-metadata" in url and "USGS-00000000" in str(params):
            return ok(EMPTY)
    monkeypatch.setattr(uw, "_http_get", Replay(ov))
    with pytest.raises(SourceUnavailable) as ei:
        uw.get_daily("00000000", "2020-01-01", "2020-12-31")
    assert ei.value.code == "USGS_SITE_NOT_FOUND"
    assert doc("daily_nonexistent_00000000")["features"] == []     # the real 200-empty shape


def test_empty_window_on_real_site_is_no_data(monkeypatch):
    def ov(url, params):
        if "/daily/items" in url:
            return ok(EMPTY)
    monkeypatch.setattr(uw, "_http_get", Replay(ov))
    with pytest.raises(SourceUnavailable) as ei:
        uw.get_daily("01109000", "1900-01-01", "1900-12-31")
    assert ei.value.code == "USGS_NO_DATA" and ei.value.details["series"]


def test_site_validation(replay):
    with pytest.raises(SourceUnavailable) as ei:
        uw.get_daily("abc", "2020-01-01", "2020-01-02")
    assert ei.value.code == "USGS_SITE_INVALID"
    assert uw._site_id("USGS-01013500") == "USGS-01013500"


# ------------------------------------------------------------- errors, approvals, series identity
def test_unknown_approval_refuses(monkeypatch):
    def ov(url, params):
        if "/daily/items" in url:
            return ok({"features": [_feat("2020-01-01"), _feat("2020-01-02", ap="Revised")], "links": []})
    monkeypatch.setattr(uw, "_http_get", Replay(ov))
    with pytest.raises(SourceUnavailable) as ei:
        uw.get_daily("01013500", "2020-01-01", "2020-01-02")
    assert ei.value.code == "USGS_APPROVAL_UNKNOWN" and ei.value.details["values"] == ["Revised"]


def test_null_value_becomes_nan_and_is_counted(monkeypatch):
    def ov(url, params):
        return ok({"features": [_feat("2020-01-01", v=None), _feat("2020-01-02", v="2")], "links": []})
    monkeypatch.setattr(uw, "_http_get", Replay(ov))
    s = uw.get_daily("01013500", "2020-01-01", "2020-01-02")
    assert s.frame["value_native"].isna().tolist() == [True, False]
    assert s.quality_summary["n_null_values"] == 1 and pd.isna(s.frame["value_m3s"].iloc[0])


def test_unparseable_value_and_unknown_unit_and_duplicates(monkeypatch):
    def run(feats):
        monkeypatch.setattr(uw, "_http_get", Replay(lambda u, p: ok({"features": feats, "links": []})))
        with pytest.raises(SourceUnavailable) as ei:
            uw.get_daily("01013500", "2020-01-01", "2020-01-02")
        return ei.value.code
    assert run([_feat("2020-01-01", v="Ice")]) == "USGS_VALUE_UNPARSEABLE"
    assert run([_feat("2020-01-01", unit="m^3/s")]) == "USGS_UNIT_UNKNOWN"
    assert run([_feat("2020-01-01"), _feat("2020-01-01", v="3")]) == "USGS_DUPLICATE_TIME"


def _two_series(meta_primary):
    feats = [_feat("2020-01-01", ts="tsA"), _feat("2020-01-02", ts="tsB", v="9")]
    meta = {"features": [{"properties": {"id": i, "primary": pr, "parameter_code": "00060", "statistic_id": "00003"}}
                         for i, pr in meta_primary], "links": []}
    def ov(url, params):
        return ok(meta if "time-series-metadata" in url else {"features": feats, "links": []})
    return Replay(ov)


def test_multiple_series_uses_single_primary(monkeypatch):
    monkeypatch.setattr(uw, "_http_get", _two_series([("tsA", "Secondary"), ("tsB", "Primary")]))
    s = uw.get_daily("01013500", "2020-01-01", "2020-01-02")
    assert s.time_series_id == "tsB" and len(s.frame) == 1 and s.frame["value_native"].iloc[0] == 9.0


@pytest.mark.parametrize("flags", [[("tsA", "Primary"), ("tsB", "Primary")], [("tsA", "Secondary"), ("tsB", "Secondary")]])
def test_multiple_series_without_unique_primary_refuses(monkeypatch, flags):
    monkeypatch.setattr(uw, "_http_get", _two_series(flags))
    with pytest.raises(SourceUnavailable) as ei:
        uw.get_daily("01013500", "2020-01-01", "2020-01-02")
    assert ei.value.code == "USGS_MULTIPLE_SERIES" and ei.value.details["time_series_ids"] == ["tsA", "tsB"]


def test_explicit_time_series_id_skips_metadata(monkeypatch):
    r = _two_series([("tsA", "Primary"), ("tsB", "Primary")])
    monkeypatch.setattr(uw, "_http_get", r)
    s = uw.get_daily("01013500", "2020-01-01", "2020-01-02", time_series_id="tsA")
    assert s.time_series_id == "tsA" and not any("time-series-metadata" in c[0] for c in r.calls)
    assert any(c[1] and c[1].get("time_series_id") == "tsA" for c in r.calls)


def test_rate_limit_maps_to_structured_error(monkeypatch):
    monkeypatch.setattr(uw, "_http_get", lambda u, p=None, h=None: (429, {"Retry-After": "30"}, b"{}"))
    with pytest.raises(SourceUnavailable) as ei:
        uw.get_daily("01013500", "2020-01-01", "2020-01-02")
    assert ei.value.code == "USGS_RATE_LIMITED" and "API_USGS_PAT" in ei.value.recovery


def test_gt_1100_day_400_is_parsed(replay):
    e = INDEX["continuous_01013500_gt3y_error"]
    p = dict(parse_qsl(urlparse(e["url"]).query))
    with pytest.raises(SourceUnavailable) as ei:
        uw._request(uw.BASE + "/collections/continuous/items", p, uw._new_record())
    assert ei.value.code == "USGS_INVALID_QUERY" and "1100" in ei.value.message
    assert ei.value.details["status"] == 400 and ei.value.details["body"]["code"] == "InvalidQuery"


# ------------------------------------------------------------- continuous
def test_continuous_7d_is_utc_and_provisional(replay):
    s = uw.get_continuous("01013500", "2026-10-10", "2026-10-17")
    assert len(s.frame) == 673 and str(s.frame["time"].dt.tz) == "UTC"
    assert s.time_series_id == "cdaf3cebfd0e49ebbe16231e6cd947e0"
    assert s.quality_summary["approval_counts"] == {"Provisional": 673} and s.quality_summary["provisional_fraction"] == 1.0
    assert s.frame["time"].iloc[0] == pd.Timestamp("2026-10-10T00:00:00Z")


def test_continuous_is_chunked_below_1100_days_and_boundaries_dedupe(monkeypatch):
    seen = []
    def ov(url, params):
        if "/continuous/items" in url:
            a, b = params["time"].split("/"); seen.append((a, b))
            return ok({"features": [_feat(a.replace("Z", "+00:00"), ts="c"), _feat(b.replace("Z", "+00:00"), ts="c")], "links": []})
    monkeypatch.setattr(uw, "_http_get", Replay(ov))
    s = uw.get_continuous("01013500", "2020-01-01", "2024-01-01")
    spans = [(pd.Timestamp(b) - pd.Timestamp(a)).days for a, b in seen]
    assert len(seen) == 2 and max(spans) <= 1100 and seen[0][1] == seen[1][0] and seen[0][0] == "2020-01-01T00:00:00Z"
    assert seen[-1][1] == "2024-01-01T00:00:00Z"
    assert len(s.frame) == 3                    # shared boundary instant appears once


# ------------------------------------------------------------- locations
def test_monitoring_location_fields(replay):
    (loc,) = uw.get_monitoring_locations(ids=["01013500"])
    assert (loc.site_id, loc.name) == ("USGS-01013500", "Fish River near Fort Kent, Maine")
    assert (loc.lon, loc.lat) == (-68.5827777777778, 47.2375)
    assert loc.horizontal_datum == "NAD83" and loc.vertical_datum == "NAVD88" and loc.site_type_code == "ST"
    assert loc.drainage_area_raw == 870.0 and loc.drainage_area_units.startswith("unstated")
    assert "WGS" not in loc.geometry_crs_note and "does not state" in loc.geometry_crs_note


def test_drainage_area_units_claim_matches_recorded_openapi():
    api = json.loads(body("openapi"))
    txt = json.dumps(api)
    assert "in square miles" in txt                       # stated for contributing_drainage_area ...
    def walk(o):
        if isinstance(o, dict):
            if "drainage_area" in o and isinstance(o["drainage_area"], dict):
                yield o["drainage_area"].get("description", "")
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    descs = {d for d in walk(api) if d.startswith("The area enclosed")}
    assert descs and all("square mile" not in d for d in descs)   # ... but not for drainage_area itself


def test_locations_bbox_and_exact_name(replay):
    bb = uw.get_monitoring_locations(bbox=[-68.70, 47.10, -68.40, 47.40], limit=50)
    assert len(bb) == 19 and all(b.site_type_code == "ST" for b in bb)
    nm = uw.get_monitoring_locations(name="Fish River near Fort Kent, Maine", site_type_code=None, limit=10)
    assert [n.site_id for n in nm] == ["USGS-01013500"]
    with pytest.raises(SourceUnavailable) as ei:
        uw.get_monitoring_locations(name_contains="Fish")
    assert ei.value.code == "USGS_UNSUPPORTED"


# ------------------------------------------------------------- gauge resolution
def _site_xy(s):
    return doc(f"nldi_nwissite_{s}")["features"][0]["geometry"]["coordinates"][:2]


def test_resolve_explicit_alias_and_required(replay):
    g = uw.resolve_gauge(gauge_id="01013500")
    assert (g.site, g.method, g.alias_verified) == ("USGS-01013500", "explicit_id", None)
    ref = {"aliases": [{"scheme": "usgs", "id": "01013500", "relation": "same_as", "verified": True}]}
    g = uw.resolve_gauge(basin_ref=ref)
    assert (g.method, g.alias_verified, g.site) == ("basin_ref_alias", True, "USGS-01013500")
    with pytest.raises(SourceUnavailable) as ei:
        uw.resolve_gauge(geometry=object())
    assert ei.value.code == "GAUGE_REQUIRED"


def test_resolve_outlet_single_candidate(replay):
    lon, lat = _site_xy("01013500")
    g = uw.resolve_gauge(outlet=(lon, lat), max_distance_m=500)
    assert (g.site, g.method, g.comid) == ("USGS-01013500", "outlet_snap", 724696)
    assert g.distance_m < 1 and [c["site"] for c in g.candidates] == ["USGS-01013500"]


def test_resolve_outlet_ambiguous_and_not_found_and_threshold(replay, monkeypatch):
    lon, lat = _site_xy("01013500")
    with pytest.raises(SourceUnavailable) as ei:
        uw.resolve_gauge(outlet=(lon, lat), max_distance_m=30000)
    assert ei.value.code == "GAUGE_AMBIGUOUS" and len(ei.value.details["candidates"]) > 1
    with pytest.raises(SourceUnavailable) as ei:
        uw.resolve_gauge(outlet=(lon, lat))
    assert ei.value.code == "GAUGE_THRESHOLD_REQUIRED"
    real = Replay()
    def ov(url, params):               # the shifted outlet snaps to the same recorded COMID
        if url.endswith("/comid/position"):
            return real(url, {"coords": f"POINT({lon} {lat})", "f": "json"})
    monkeypatch.setattr(uw, "_http_get", Replay(ov))
    with pytest.raises(SourceUnavailable) as ei:
        uw.resolve_gauge(outlet=(lon + 0.01, lat), max_distance_m=10)
    assert ei.value.code == "GAUGE_NOT_FOUND"


def test_transport_refuses_other_hosts():
    with pytest.raises(SourceUnavailable) as ei:
        uw._http_get("https://waterservices.usgs.gov/nwis/dv/")
    assert ei.value.code == "USGS_HOST_NOT_ALLOWED"


# ------------------------------------------------------------- parity vs legacy bytes
PARITY = [("01013500", "daily_01013500_wy1990_2009", "legacy_dv_01013500", "1989-10-01", "2009-09-30"),
          ("01516500", "daily_01516500_wy1990_2009", "legacy_dv_01516500", "1989-10-01", "2009-09-30"),
          ("03245500", "daily_03245500_2010_2020", "legacy_dv_03245500", "2010-01-01", "2020-12-31")]


@pytest.mark.parametrize("site,_new,legacy,a,b", PARITY)
def test_values_and_codes_match_legacy_dv(replay, site, _new, legacy, a, b):
    leg = json.loads(_read(LEGACY / LEGACY_INDEX[legacy]["file"]))
    ts = leg["value"]["timeSeries"]
    assert len(ts) == 1
    assert ts[0]["variable"]["unit"]["unitCode"] == "ft3/s"
    rows = ts[0]["values"][0]["value"]
    s = uw.get_daily(site, a, b)
    df = s.frame.set_index("time")
    ldates = pd.to_datetime([r["dateTime"] for r in rows])
    assert list(df.index) == list(ldates)                      # same dates, same order, none missing
    diffs = []
    for r, (t, row) in zip(rows, df.iterrows()):
        lv = float(r["value"]); q = set(r["qualifiers"])
        if lv != row["value_native"]:
            diffs.append((t, "value", lv, row["value_native"]))
        if uw.to_m3s(pd.Series([lv]), "ft3/s").iloc[0] != row["value_m3s"]:
            diffs.append((t, "m3s", lv, row["value_m3s"]))
        if ("A" in q) != (row["approval_code"] == "A") or ("P" in q) != (row["approval_code"] == "P"):
            diffs.append((t, "approval", sorted(q), row["approval_status"]))
        if ("e" in q) != ("ESTIMATED" in row["qualifier"]):
            diffs.append((t, "estimated", sorted(q), row["qualifier"]))
        if q - {"A", "P", "e"}:
            diffs.append((t, "legacy-only-qualifier", sorted(q), row["qualifier"]))
    assert not diffs, f"{len(diffs)} differences vs legacy dv, first: {diffs[:5]}"
