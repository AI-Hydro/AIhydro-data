"""ADR-003 amendment A2 gauge resolution (rule_version ADR-003-A2-2026-10-17), Wave U / U1b.

Synthetic worlds (T1-T24 of docs/vision-2040/findings/ruling-gauge-resolution-A2.md) plus the
four recorded fixture sites. Offline: ``usgs_waterdata._http_get`` is replaced.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest
from shapely.geometry import Polygon, mapping

from aihydro_data.exceptions import SourceUnavailable, TerminalSourceError
from aihydro_data.sources import usgs_waterdata as uw
from tests.test_usgs_waterdata_contract import INDEX, Replay, doc, ok, body, EMPTY, LEGACY, LEGACY_INDEX, _read

R = uw.EARTH_R_M
LON0, LAT0 = -90.0, 40.0
SQMI = uw.SQMI_TO_KM2


def lat_at(m: float) -> float:
    return LAT0 + math.degrees(m / R)


def reach_k(m: float) -> int:
    """Reach k covers [(2k-1)*1000, (2k+1)*1000] m along the meridian; k=0 is the outlet's reach."""
    return int(math.floor((m + 1000.0) / 2000.0))


def poly_of(area_km2: float) -> Polygon:
    side = math.sqrt(area_km2) / 111.19          # degrees, first guess
    for _ in range(6):                             # refine on the sphere
        p = Polygon([(LON0 - side / 2, LAT0 - side / 2), (LON0 + side / 2, LAT0 - side / 2),
                     (LON0 + side / 2, LAT0 + side / 2), (LON0 - side / 2, LAT0 + side / 2)])
        side *= math.sqrt(area_km2 / uw.polygon_area_km2(p))
    return p


class World:
    """A straight north-flowing river (downstream = +m) with an outlet at m=0."""

    def __init__(self, sites=(), *, offset_m=0.0, position="ok", shuffle=False, basin_km2=100.0, poly_km2=None):
        self.sites = list(sites)
        self.offset_m = offset_m
        self.position = position
        self.shuffle = shuffle
        self.basin = poly_of(basin_km2)
        self.calls: list[str] = []

    # --- helpers
    @property
    def outlet(self):
        return (LON0 + math.degrees(self.offset_m / (R * math.cos(math.radians(LAT0)))), LAT0)

    def reach(self, k):
        return [[LON0, lat_at((2 * k - 1) * 1000.0)], [LON0, lat_at((2 * k + 1) * 1000.0)]]

    def _site_feature(self, s):
        comid = s.get("comid", 5000 + reach_k(s["m"]))
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [LON0, lat_at(s["m"])]},
                "properties": {"identifier": s["id"], "comid": comid, "name": s["id"]}}

    def _order(self, xs):
        xs = list(xs)
        if self.shuffle:
            random.Random(7).shuffle(xs)
        return xs

    def __call__(self, url, params=None, headers=None):
        self.calls.append(url + (("?" + str(sorted((params or {}).items()))) if params else ""))
        if url.endswith("/comid/position"):
            if self.position == "http500":
                return 500, {}, b"{}"
            if self.position == "empty":
                return ok(EMPTY)
            return ok({"type": "FeatureCollection", "features": [{
                "type": "Feature", "geometry": {"type": "LineString", "coordinates": self.reach(0)},
                "properties": {"comid": 5000, "identifier": "x"}}]})
        if "/navigation/" in url and url.endswith("/nwissite"):
            um = "/UM/" in url
            feats = [self._site_feature(s) for s in self.sites
                     if (s["m"] <= 1000.0 if um else s["m"] >= -1000.0) and abs(s["m"]) <= 11000]
            return ok({"type": "FeatureCollection", "features": self._order(feats)})
        if "/navigation/" in url and url.endswith("/flowlines"):
            um = "/UM/" in url
            ks = range(-6, 1) if um else range(0, 7)
            feats = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": self.reach(k)},
                      "properties": {"nhdplus_comid": 5000 + k}} for k in ks]
            return ok({"type": "FeatureCollection", "features": self._order(feats)})
        if url.endswith("/basin"):
            return ok({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": mapping(self.basin), "properties": {}}]})
        if "time-series-metadata" in url:
            sid = params["monitoring_location_id"]
            s = next(x for x in self.sites if x["id"] == sid)
            code, stat = ("00060", "00003") if s.get("discharge", True) else ("00065", None)
            return ok({"features": [{"properties": {"id": "ts-" + sid, "primary": "Primary", "parameter_code": code,
                                                    "statistic_id": stat, "monitoring_location_id": sid}}], "links": []})
        if "monitoring-locations" in url:
            sid = params["id"]
            s = next((x for x in self.sites if x["id"] == sid), None)
            if s is None:
                return ok(EMPTY)
            pr = {"id": sid, "monitoring_location_name": sid, "drainage_area": s.get("area_mi2"),
                  "contributing_drainage_area": s.get("contrib_mi2"), "original_horizontal_datum": "NAD83"}
            return ok({"features": [{"type": "Feature", "properties": pr,
                                     "geometry": {"type": "Point", "coordinates": [LON0, lat_at(s["m"])]}}], "links": []})
        raise AssertionError("unexpected request " + url)


def mi2(ratio: float, basin_km2: float) -> float:
    return ratio * basin_km2 / SQMI


def site(i, m, ratio=None, *, basin_km2, discharge=True, **kw):
    return {"id": f"USGS-{i:08d}", "m": m, "discharge": discharge,
            "area_mi2": None if ratio is None else mi2(ratio, basin_km2), **kw}


@pytest.fixture
def run(monkeypatch):
    def _run(world, *, polygon=True, **kw):
        monkeypatch.setattr(uw, "_http_get", world)
        geom = world.basin if polygon else None
        return uw.resolve_gauge(geom, outlet=world.outlet, **kw)
    return _run


def basin_area(world):
    return uw.polygon_area_km2(world.basin)


def mk(sites_spec, **kw):
    """sites_spec: list of (m, ratio, extra-dict); builds the world with consistent areas."""
    probe = World(**kw)
    b = basin_area(probe)
    sites = [site(100 + i, m, ratio, basin_km2=b, **ex) for i, (m, ratio, ex) in enumerate(sites_spec)]
    return World(sites, **kw)


def refusal(run, world, **kw):
    with pytest.raises(TerminalSourceError) as ei:
        run(world, **kw)
    return ei.value


# ------------------------------------------------------------------ R0 precedence
def _named_world():
    b = basin_area(World())
    return World([site(1013500, 0, 1.0, basin_km2=b)])


def test_T1_explicit_id_skips_navigation_and_records_ratio(monkeypatch):
    w = _named_world(); monkeypatch.setattr(uw, "_http_get", w)
    g = uw.resolve_gauge(w.basin, gauge_id="01013500", outlet=w.outlet)
    assert (g.method, g.site, g.area_basis, g.area_check) == ("explicit_id", "USGS-01013500", "request_geometry", "pass")
    assert abs(g.area_ratio - 1.0) < 1e-9 and g.log_area_ratio is not None
    assert not [c for c in w.calls if "/navigation/" in c or "comid/position" in c]
    assert g.rule_version == "ADR-003-A2-2026-10-17"


def test_T2_area_failure_is_a_warning_not_a_refusal(monkeypatch):
    b = basin_area(World())
    w = World([site(1013500, 0, 1 / 1.5, basin_km2=b)])          # polygon is 1.5x the gauge area
    monkeypatch.setattr(uw, "_http_get", w)
    g = uw.resolve_gauge(w.basin, gauge_id="01013500")
    assert g.method == "explicit_id" and g.area_check == "fail"
    assert any("area_check=fail" in x for x in g.warnings) and g.to_dict()["warnings"] == g.warnings


def test_T3_alias_records_verification_and_warns_when_unverified(monkeypatch):
    w = _named_world(); monkeypatch.setattr(uw, "_http_get", w)
    ref = {"aliases": [{"scheme": "usgs", "id": "01013500", "relation": "same_as", "verified": True}]}
    g = uw.resolve_gauge(basin_ref=ref)
    assert (g.method, g.alias_verified, g.warnings) == ("basin_ref_alias", True, [])
    ref["aliases"][0]["verified"] = False
    g = uw.resolve_gauge(basin_ref=ref)
    assert g.alias_verified is False and any("not verified" in x for x in g.warnings)


def test_T4_explicit_id_alias_conflict(monkeypatch):
    w = _named_world(); monkeypatch.setattr(uw, "_http_get", w)
    ref = {"aliases": [{"scheme": "usgs", "id": "01646500", "verified": True}]}
    with pytest.raises(TerminalSourceError) as ei:
        uw.resolve_gauge(gauge_id="01013500", basin_ref=ref)
    assert ei.value.code == "GAUGE_AMBIGUOUS" and ei.value.details["reason"] == "identity_conflict"
    assert w.calls == []


def test_T5_polygon_only_is_required_with_no_network(monkeypatch):
    w = World(); monkeypatch.setattr(uw, "_http_get", w)
    with pytest.raises(TerminalSourceError) as ei:
        uw.resolve_gauge(w.basin)
    assert ei.value.code == "GAUGE_REQUIRED" and w.calls == []
    assert "gauge_id" in ei.value.recovery and "modelled product" in ei.value.recovery


# ------------------------------------------------------------------ R2-R5
def test_T6_downstream_gauge(run):
    w = mk([(1200, 1.04, {})])
    g = run(w)
    assert (g.method, g.direction, g.comid, g.comid_outlet) == ("outlet_snap", "downstream", 5001, 5000)
    assert abs(g.distance_m - 1200) < 0.01 and abs(g.area_ratio - 1.04) < 1e-9
    assert g.area_basis == "request_geometry" and g.distance_basis and g.outlet_offset_m < 0.01
    assert g.thresholds == {"max_distance_m": 5000.0, "max_abs_log_area_ratio": math.log(1.10),
                            "outlet_offset_max_m": 200.0, "query_distance_km": 10}
    assert g.thresholds_overridden is False


def test_T7_both_bounds_inclusive(run):
    g = run(mk([(-5000, 1.10, {})]))
    assert g.direction == "upstream" and abs(g.distance_m + 5000) < 1e-6 and g.method == "outlet_snap"


def test_T8_just_outside_distance(run):
    e = refusal(run, mk([(-5001, 1.00, {})]))
    assert (e.code, e.details["reason"]) == ("GAUGE_NOT_FOUND", "outside_distance")
    assert [c["status"] for c in e.details["candidates"]] == ["outside_distance"]


def test_T9_just_outside_area(run):
    e = refusal(run, mk([(300, 0.9090, {})]))
    assert (e.code, e.details["reason"]) == ("GAUGE_NOT_FOUND", "area_mismatch")


def test_T10_area_beats_distance_below_a_confluence(run):
    e = refusal(run, mk([(600, 1.8, {})]))
    assert e.details["reason"] == "area_mismatch" and e.details["candidates"][0]["site"].endswith("00000100")


def test_T11_two_admissible_is_ambiguous_without_tie_break(run):
    e = refusal(run, mk([(-800, 0.98, {}), (2000, 1.03, {})]))
    assert (e.code, e.details["reason"]) == ("GAUGE_AMBIGUOUS", "multiple_candidates")
    assert [c["status"] for c in e.details["candidates"]] == ["admissible", "admissible"]
    assert [c["direction"] for c in e.details["candidates"]] == ["upstream", "downstream"]


def test_T12_unverifiable_neighbour_makes_it_ambiguous(run):
    e = refusal(run, mk([(300, 1.0, {}), (1000, None, {})]))
    assert e.code == "GAUGE_AMBIGUOUS"
    assert sorted(c["status"] for c in e.details["candidates"]) == ["admissible", "area_missing"]


def test_T13_only_candidate_has_no_area(run):
    e = refusal(run, mk([(300, None, {})]))
    assert (e.code, e.details["reason"]) == ("GAUGE_NOT_FOUND", "area_unverifiable")


def test_T14_same_site_in_um_and_dm_is_one_candidate(run):
    w = mk([(100, 1.0, {})])
    g = run(w)
    assert len(g.candidates) == 1 and g.direction == "downstream" and abs(g.distance_m - 100) < 0.01
    assert sum("/nwissite" in c and "/UM/" in c for c in w.calls) == 1 == sum("/nwissite" in c and "/DM/" in c for c in w.calls)


def test_T15_water_quality_site_is_excluded_and_recorded(run):
    g = run(mk([(500, 1.0, {}), (100, None, {"discharge": False})]))
    st = {c["site"][-3:]: c["status"] for c in g.candidates}
    assert g.site.endswith("100") and st["101"] == "no_discharge_series"


def test_T16_missing_flowline_is_unverifiable_and_counts(run):
    e = refusal(run, mk([(1200, 1.04, {}), (-2000, 1.0, {"comid": 9999})]))
    assert e.code == "GAUGE_AMBIGUOUS"
    assert sorted(c["status"] for c in e.details["candidates"]) == ["admissible", "distance_unverifiable"]
    e2 = refusal(run, mk([(-2000, 1.0, {"comid": 9999})]))
    assert (e2.code, e2.details["reason"]) == ("GAUGE_NOT_FOUND", "distance_unverifiable")


def test_T16b_unverifiable_distance_with_verified_area_mismatch_is_excluded(run):
    g = run(mk([(1200, 1.04, {}), (-2000, 3.0, {"comid": 9999})]))
    assert g.site.endswith("100")


# ------------------------------------------------------------------ R6 no-geometry guard
def test_T17_no_polygon_and_far_offset(run):
    e = refusal(run, mk([(0, 1.0, {})], offset_m=350.0), polygon=False)
    assert (e.code, e.details["reason"]) == ("GAUGE_REQUIRED", "outlet_off_network_no_geometry")
    assert abs(e.details["outlet_offset_m"] - 350) < 1


def test_T18_no_polygon_near_offset_uses_the_comid_basin(run):
    w = World(offset_m=150.0)
    w.sites = [site(100, 0, 1.02, basin_km2=basin_area(w))]
    g = run(w, polygon=False)
    assert g.area_basis == "nldi_comid_basin" and abs(g.outlet_offset_m - 150) < 1
    assert abs(g.area_ratio - 1.02) < 1e-9 and any("not independent" in x for x in g.warnings)


def test_offset_is_only_recorded_when_geometry_is_present(run):
    g = run(mk([(100, 1.0, {})], offset_m=350.0))
    assert g.area_basis == "request_geometry" and abs(g.outlet_offset_m - 350) < 1


# ------------------------------------------------------------------ R7 failures
def test_T19_position_http_error_is_unavailable_not_not_found(run):
    w = mk([(100, 1.0, {})], position="http500")
    with pytest.raises(SourceUnavailable) as ei:
        run(w)
    assert not isinstance(ei.value, TerminalSourceError) and ei.value.code == "USGS_SERVER_ERROR"


def test_nldi_timeout_is_unavailable(monkeypatch):
    import requests
    def boom(url, params=None, headers=None):
        raise uw._err("USGS_TIMEOUT", "timeout")
    monkeypatch.setattr(uw, "_http_get", boom)
    with pytest.raises(SourceUnavailable) as ei:
        uw.resolve_gauge(outlet=(LON0, LAT0))
    assert ei.value.code == "USGS_TIMEOUT" and not isinstance(ei.value, TerminalSourceError)


def test_T20_empty_position_is_not_found_no_candidates(run):
    e = refusal(run, mk([(100, 1.0, {})], position="empty"))
    assert (e.code, e.details["reason"]) == ("GAUGE_NOT_FOUND", "no_candidates")


def test_pool_with_only_water_quality_sites_is_no_candidates(run):
    e = refusal(run, mk([(100, None, {"discharge": False})]))
    assert (e.code, e.details["reason"]) == ("GAUGE_NOT_FOUND", "no_candidates")


# ------------------------------------------------------------------ R4 area basis, R8 order/determinism
def test_T21_total_area_is_compared_not_contributing(run):
    w = World()
    P = basin_area(w)
    w.sites = [{"id": "USGS-00000100", "m": 200, "area_mi2": (P * 100 / 98) / SQMI,
                "contrib_mi2": 0.4 * (P * 100 / 98) / SQMI}]
    g = run(w)
    assert abs(g.A_gauge_km2 - P * 100 / 98) < 1e-6 and abs(g.A_gauge_contributing_km2 - 0.4 * P * 100 / 98) < 1e-6
    assert abs(g.area_ratio - 100 / 98) < 1e-9


def test_T22_response_order_does_not_matter(run):
    spec = [(1200, 1.04, {}), (-2500, 3.0, {}), (4000, 0.2, {}), (-700, None, {"discharge": False}), (-9000, 1.0, {})]
    a = run(mk(spec)); b = run(mk(spec, shuffle=True))
    da, db = a.to_dict(), b.to_dict()
    for d in (da, db):
        d.pop("upstream")
    assert da == db
    keys = [(abs(c["distance_m"]), c["site"]) for c in a.candidates]
    assert keys == sorted(keys)


def test_T23_library_override_is_recorded(run):
    g = run(mk([(-7000, 1.0, {})]), max_distance_m=25000)
    assert g.thresholds_overridden is True and g.thresholds["max_distance_m"] == 25000.0
    assert g.thresholds["query_distance_km"] == 50 and g.to_dict()["thresholds_overridden"] is True
    assert g.site.endswith("100")


def test_T23b_mcp_surface_has_no_threshold_parameters():
    import inspect
    import aihydro_data.mcp as m
    src = inspect.getsource(m)
    assert "max_distance" not in src and "thresholds" not in inspect.signature(m._data_fetch).parameters


def test_record_has_everything_in_r8(run):
    d = run(mk([(1200, 1.04, {})])).to_dict()
    for k in ("site", "method", "comid_outlet", "outlet_offset_m", "direction", "distance_m", "distance_basis",
              "A_gauge_km2", "A_gauge_contributing_km2", "A_basin_km2", "area_basis", "log_area_ratio",
              "area_check", "thresholds", "thresholds_overridden", "rule_version", "candidates", "upstream",
              "area_units_note", "alias_verified"):
        assert k in d, k
    assert set(d["candidates"][0]) >= {"site", "comid", "direction", "distance_m", "A_gauge_km2", "log_area_ratio", "status"}
    assert d["upstream"]["request_urls"] and d["upstream"]["response_sha256"]
    json.dumps(d)


# ------------------------------------------------------------------ geometry helpers
def test_projection_and_lengths_are_spherical():
    line = [[0.0, 0.0], [0.0, 1.0]]
    assert abs(uw._line_length(line) - math.radians(1) * R) < 1e-6
    s, off = uw._project(line, 0.001, 0.5)
    assert abs(s - 0.5 * math.radians(1) * R) < 1.0 and abs(off - math.radians(0.001) * R) < 1.0


def test_polygon_area_matches_a_known_square():
    p = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    exact = (R / 1000) ** 2 * math.radians(1) * (math.sin(math.radians(1)) - 0.0)    # lon-lat rectangle on a sphere
    assert abs(uw.polygon_area_km2(p) - exact) / exact < 1e-3


# ------------------------------------------------------------------ recorded fixtures
SITES = ["01013500", "01516500", "01109000", "03245500"]


@pytest.fixture
def fixtures(monkeypatch):
    monkeypatch.setattr(uw, "_http_get", Replay())


def _legacy_drain(site):
    txt = _read(LEGACY / LEGACY_INDEX[f"legacy_site_{site}"]["file"]).decode()
    lines = [l for l in txt.splitlines() if l and not l.startswith("#")]
    hdr, row = lines[0].split("\t"), lines[2].split("\t")
    return float(row[hdr.index("drain_area_va")])


@pytest.mark.parametrize("s", SITES)
def test_recorded_outlets_resolve_to_their_own_gauge(fixtures, s):
    lon, lat = doc(f"nldi_nwissite_{s}")["features"][0]["geometry"]["coordinates"][:2]
    g = uw.resolve_gauge(outlet=(lon, lat))
    assert g.site == f"USGS-{s}" and g.method == "outlet_snap" and g.area_basis == "nldi_comid_basin"
    assert g.outlet_offset_m < 200 and abs(g.log_area_ratio) < 0.02
    # the area conversion is cross-checked against the legacy drain_area_va of the same site
    assert abs(g.A_gauge_km2 - _legacy_drain(s) * 2.589988110336) < 1e-9
    assert g.location.drainage_area_raw == _legacy_drain(s)
    assert {c["status"] for c in g.candidates} <= {"admissible", "no_discharge_series", "outside_distance"}
    assert len([c for c in g.candidates if c["status"] == "admissible"]) == 1


def test_01013500_pool_matches_the_recorded_navigation(fixtures):
    lon, lat = doc("nldi_nwissite_01013500")["features"][0]["geometry"]["coordinates"][:2]
    g = uw.resolve_gauge(outlet=(lon, lat))
    st = {c["site"]: c["status"] for c in g.candidates}
    assert st["USGS-01014000"] == "outside_distance"
    assert [v for k, v in st.items() if k.startswith("USGS-01014000") and k != "USGS-01014000"] == ["no_discharge_series"] * 10
    assert g.A_gauge_km2 == pytest.approx(870 * 2.589988110336)
    assert 870 == _legacy_drain("01013500")


# ------------------------------------------------------------------ network topology (U1b review B1)
class TopoWorld(World):
    """World plus extra flowlines in the UM/DM responses (tributaries, braids) and response reversal."""

    def __init__(self, *a, extra=(), rev=False, **k):
        super().__init__(*a, **k)
        self.extra, self.rev = list(extra), rev

    def __call__(self, url, params=None, headers=None):
        if "/navigation/" in url and url.endswith("/flowlines"):
            um = "/UM/" in url
            ks = range(-6, 1) if um else range(0, 7)
            feats = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": self.reach(k)},
                      "properties": {"nhdplus_comid": 5000 + k}} for k in ks]
            feats += [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": c},
                       "properties": {"nhdplus_comid": cid}} for cid, c, d in self.extra if (d == "UM") == um]
            if self.rev:
                feats.reverse()
            return ok({"type": "FeatureCollection", "features": feats})
        return super().__call__(url, params, headers)


def _trib_world(rev):
    b = basin_area(World())
    # a tributary joins the main stem at node m=-1000 (the outlet reach's upstream end); gauge 2 sits on it
    trib = [(9999, [[LON0 + 0.02, lat_at(-3000)], [LON0, lat_at(-1000)]], "UM")]
    return TopoWorld([site(100, -2000, 1.0, basin_km2=b), site(101, -2500, 1.0, basin_km2=b, comid=9999)], extra=trib, rev=rev)


def test_confluence_distance_follows_every_branch_not_the_last_one_seen(run):
    for rev in (False, True):
        e = refusal(run, _trib_world(rev))
        rows = {c["site"][-3:]: c for c in e.details["candidates"]}
        assert rows["100"]["status"] == rows["101"]["status"] == "admissible"          # both are within 5 km
        assert rows["100"]["distance_m"] == pytest.approx(-2000, abs=0.01)             # main-stem gauge: via the main stem
        trib_len = uw._line_length([[LON0 + 0.02, lat_at(-3000)], [LON0, lat_at(-1000)]])
        assert rows["101"]["distance_m"] == pytest.approx(-(1000 + trib_len / 2), abs=trib_len * 0.5 + 1)   # via the tributary


def test_tributary_only_gauge_resolves_identically_in_both_orders(run):
    b = basin_area(World())
    out = []
    for rev in (False, True):
        w = TopoWorld([site(101, -2500, 1.0, basin_km2=b, comid=9999)], rev=rev,
                      extra=[(9999, [[LON0 + 0.02, lat_at(-3000)], [LON0, lat_at(-1000)]], "UM")])
        g = run(w)
        d = g.to_dict(); d.pop("upstream"); out.append(d)
    assert out[0] == out[1] and out[0]["direction"] == "upstream"


def test_downstream_braid_is_deterministic_and_shortest(run):
    b = basin_area(World())
    braid = [(8888, [[LON0, lat_at(1000)], [LON0 + 0.02, lat_at(4000)]], "DM")]       # a second reach leaves node +1000
    res = []
    for rev in (False, True):
        e = refusal(run, TopoWorld([site(6, 1500, 1.0, basin_km2=b), site(7, 3500, 1.0, basin_km2=b, comid=8888)],
                                   extra=braid, rev=rev))
        res.append([(c["site"], c["status"], round(c["distance_m"], 6)) for c in e.details["candidates"]])
    assert res[0] == res[1] and {r[1] for r in res[0]} == {"admissible"}


def test_loop_in_the_returned_network_terminates(run):
    b = basin_area(World())
    loop = [(7777, [[LON0, lat_at(7000)], [LON0, lat_at(1000)]], "DM")]
    g = run(TopoWorld([site(5, 3000, 1.0, basin_km2=b)], extra=loop))
    assert g.direction == "downstream" and abs(g.distance_m - 3000) < 0.01


def test_T22_shuffles_cover_the_braid_world_too(run):
    spec = [(1200, 1.04, {}), (-2500, 3.0, {}), (4000, 0.2, {}), (-9000, 1.0, {})]
    b = basin_area(World())
    extra = [(8888, [[LON0, lat_at(1000)], [LON0 + 0.02, lat_at(4000)]], "DM")]
    out = []
    for rev, sh in ((False, False), (True, True)):
        w = TopoWorld([site(100 + i, m, r, basin_km2=b, **ex) for i, (m, r, ex) in enumerate(spec)], extra=extra, rev=rev, shuffle=sh)
        d = run(w).to_dict(); d.pop("upstream"); out.append(d)
    assert out[0] == out[1]


# ------------------------------------------------------------------ review items
def test_area_tolerance_is_1e_minus_12():
    lim = math.log(1.10)
    for delta, expect in ((5e-13, "pass"), (-5e-13, "pass"), (5e-11, "fail")):
        _, _, chk = uw._area_check(100.0 * math.exp(lim + delta), 100.0, lim)
        assert chk == expect, delta


@pytest.mark.parametrize("geom", [Polygon([(500000, 4400000), (500100, 4400000), (500100, 4400100)]),
                                  {"type": "Polygon", "coordinates": [[[500000, 4400000], [500100, 4400000], [500100, 4400100], [500000, 4400000]]]}])
def test_projected_coordinates_are_refused(monkeypatch, geom):
    w = World(); monkeypatch.setattr(uw, "_http_get", w)
    with pytest.raises(TerminalSourceError) as ei:
        uw.resolve_gauge(geom, outlet=w.outlet)
    assert ei.value.code == "GEOMETRY_CRS_INVALID" and w.calls == []


def test_outlet_out_of_range_is_refused(monkeypatch):
    w = World(); monkeypatch.setattr(uw, "_http_get", w)
    with pytest.raises(TerminalSourceError) as ei:
        uw.resolve_gauge(outlet=(40.0, -190.0))
    assert ei.value.code == "GEOMETRY_CRS_INVALID"


def test_geojson_dict_geometry_is_used_for_the_area_basis(monkeypatch):
    w = mk([(100, 1.0, {})])
    monkeypatch.setattr(uw, "_http_get", w)
    out = uw.resolve_gauge(mapping(w.basin), outlet=w.outlet)
    assert out.area_basis == "request_geometry" and abs(out.area_ratio - 1.0) < 1e-9


@pytest.mark.parametrize("bad", ["a string", 5, object()])
def test_unusable_geometry_is_refused_not_ignored(monkeypatch, bad):
    w = World(); monkeypatch.setattr(uw, "_http_get", w)
    with pytest.raises(TerminalSourceError) as ei:
        uw.resolve_gauge(bad, outlet=w.outlet)
    assert ei.value.code == "GEOMETRY_UNSUPPORTED" and w.calls == []


def test_point_geometry_is_an_outlet_only_and_uses_the_comid_basin(run, monkeypatch):
    from shapely.geometry import Point
    w = mk([(100, 1.0, {})]); monkeypatch.setattr(uw, "_http_get", w)
    g = uw.resolve_gauge(Point(*w.outlet), outlet=w.outlet)
    assert g.area_basis == "nldi_comid_basin"


def test_conflicting_usgs_aliases_are_an_identity_conflict(monkeypatch):
    w = _named_world(); monkeypatch.setattr(uw, "_http_get", w)
    ref = {"aliases": [{"scheme": "usgs", "id": "01013500", "verified": True}, {"scheme": "usgs", "id": "01646500", "verified": True}]}
    with pytest.raises(TerminalSourceError) as ei:
        uw.resolve_gauge(basin_ref=ref)
    assert ei.value.code == "GAUGE_AMBIGUOUS" and ei.value.details["reason"] == "identity_conflict" and w.calls == []
    ref["aliases"][1]["id"] = "USGS-01013500"                          # same site twice is not a conflict
    assert uw.resolve_gauge(basin_ref=ref).site == "USGS-01013500"
