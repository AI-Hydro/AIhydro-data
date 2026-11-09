"""Modelled streamflow records which reach/cell served it (Nairobi River case, 2026-11-07).

Offline: mocked HTTP / geoglows. Values come from the finding
`docs/vision-2040/findings/non-us-e2e-defects-2026-11-07.md`: GEOGLOWS reach
110042835 (12.0 km2) walks downstream; 110275060 (116 km2) matches the 166.9 km2
basin, 110345907 (657 km2) matches the 819.4 km2 bbox; Open-Meteo returned the
cell centred (-1.275, 36.825) at 1660 m for a request at (-1.28, 36.83). The
intermediate hops are synthetic filler: the 5 hops leading to 110275060 are
invented, and only the 10 hops leading to 110345907 come from the finding.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest
from shapely.geometry import Point, box, shape

from aihydro_data.geometry.measures import area_basis, is_bbox_like, outlet_and_area
from aihydro_data.products import get_product
from aihydro_data.sources.geoglows_retro import Backend as GeoglowsBackend
from aihydro_data.sources.openmeteo_flood import Backend as OpenMeteoBackend

BBOX = box(36.70, -1.33, 36.80, -1.23)             # ~123 km2 here; shape is what matters
NAIROBI_BBOX_AREA = 819.4                           # km2, evidence bbox (finding 2b)
BASIN_KM2 = 166.9


def _basin_polygon():
    # An irregular polygon (not a rectangle) around the outlet.
    return shape({"type": "Polygon", "coordinates": [[
        [36.74, -1.30], [36.78, -1.31], [36.83, -1.28], [36.80, -1.22],
        [36.75, -1.23], [36.72, -1.26], [36.74, -1.30]]]})


# ── geometry helpers ─────────────────────────────────────────────────────────

def test_bbox_is_not_an_area_target_but_polygon_is():
    assert is_bbox_like(BBOX) and area_basis(BBOX) == "bbox"
    assert area_basis(_basin_polygon()) == "polygon"
    assert area_basis(Point(1, 2)) == "point"
    lat, lon, area = outlet_and_area(BBOX, (-1.28, 36.83))
    assert (lat, lon) == (-1.28, 36.83) and area is None
    _, _, parea = outlet_and_area(_basin_polygon(), (-1.28, 36.83))
    assert parea and parea > 10


def test_bbox_without_outlet_still_gets_a_centroid_snap_point():
    lat, lon, area = outlet_and_area(BBOX)
    assert area is None and lat == pytest.approx(-1.28) and lon == pytest.approx(36.75)


# ── Open-Meteo ───────────────────────────────────────────────────────────────

class _R:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _om_payload(q=5.498, n=5):
    return {"latitude": -1.2749977, "longitude": 36.825012, "elevation": 1660.0,
            "daily_units": {"river_discharge": "m³/s"},
            "daily": {"time": [f"2020-01-0{i + 1}" for i in range(n)], "river_discharge": [q] * n}}


def _om(monkeypatch, geometry, payload, outlet=(-1.28, 36.83)):
    monkeypatch.setattr("requests.get", lambda *a, **k: _R(payload))
    return OpenMeteoBackend().fetch_timeseries(
        get_product("OPENMETEO_FLOOD"), geometry, "2020-01-01", "2020-01-05", "basin_mean", outlet=outlet)


def test_openmeteo_records_the_served_cell_not_the_request(monkeypatch):
    df = _om(monkeypatch, _basin_polygon(), _om_payload())
    sr = df.attrs["aihydro_site_resolution"]
    assert sr["method"] == "openmeteo_cell"
    assert sr["requested"] == {"lat": -1.28, "lon": 36.83}
    assert sr["cell"] == {"lat": pytest.approx(-1.2749977), "lon": pytest.approx(36.825012), "elevation": 1660.0}
    assert sr["grid_deg"] == 0.05 and sr["upstream_area_km2"] is None
    assert sr["target_basis"] == "polygon" and sr["target_area_km2"] > 0


def test_openmeteo_without_area_says_cell_not_validated(monkeypatch):
    df = _om(monkeypatch, BBOX, _om_payload())
    sr = df.attrs["aihydro_site_resolution"]
    assert sr["target_basis"] == "bbox" and sr["target_area_km2"] is None
    assert sr["plausibility"] == "not_validated_no_area"
    notes = " ".join(df.attrs["aihydro_notes"])
    assert "bounding box" in notes and "not validated against basin area" in notes


def test_openmeteo_polygon_flags_missing_runoff_ratio_check(monkeypatch):
    df = _om(monkeypatch, _basin_polygon(), _om_payload(q=0.5))
    sr = df.attrs["aihydro_site_resolution"]
    assert sr["plausibility"] == "not_validated_runoff_ratio"
    assert sr["runoff_depth_mm_day"] == pytest.approx(sr["specific_q_m3s_km2"] * 86.4)


def test_openmeteo_over_yield_is_flagged_not_refused(monkeypatch):
    area = outlet_and_area(_basin_polygon())[2]
    q = area * 10 / 86.4                              # 10 mm/day
    df = _om(monkeypatch, _basin_polygon(), _om_payload(q=q))
    assert df.attrs["aihydro_site_resolution"]["plausibility"] == "over_yield"
    assert any("OPENMETEO_OVER_YIELD" in n for n in df.attrs["aihydro_notes"])


def test_openmeteo_under_yield_still_refuses(monkeypatch):
    from aihydro_data.exceptions import SourceUnavailable
    with pytest.raises(SourceUnavailable) as e:
        _om(monkeypatch, _basin_polygon(), _om_payload(q=1e-6))
    assert e.value.code == "OPENMETEO_CELL_MISS"


def test_openmeteo_site_resolution_reaches_the_fetch_result(monkeypatch):
    import aihydro_data._pipeline as pipeline
    from aihydro_data.contracts import FetchRequest
    monkeypatch.setattr("requests.get", lambda *a, **k: _R(_om_payload()))
    spec = get_product("OPENMETEO_FLOOD")
    req = FetchRequest(variable="streamflow", geometry=BBOX, start="2020-01-01", end="2020-01-05",
                       outlet=(-1.28, 36.83))
    r = pipeline._fetch_one(spec, BBOX, "2020-01-01", "2020-01-05", "basin_mean", req)
    assert r.site_resolution["cell"]["elevation"] == 1660.0
    assert any("not validated against basin area" in n for n in r.notes)
    from aihydro_data.mcp import _result_to_dict
    assert _result_to_dict(r)["site_resolution"]["cell"]["lat"] == pytest.approx(-1.2749977)


# ── GEOGLOWS ─────────────────────────────────────────────────────────────────

# (reach, strmOrder, upstream km2) walking downstream from the nearest reach.
_CHAIN = [(110042835, 2, 12.0), (110100001, 3, 49.0), (110100002, 3, 70.0), (110100003, 3, 90.0),
          (110100004, 4, 105.0), (110275060, 4, 116.0), (110100005, 4, 290.0), (110100006, 5, 400.0),
          (110100007, 5, 500.0), (110100008, 5, 600.0), (110345907, 5, 657.0)]


def _meta():
    rows = []
    for i, (rid, order, km2) in enumerate(_CHAIN):
        ds = _CHAIN[i + 1][0] if i + 1 < len(_CHAIN) else -1
        rows.append({"LINKNO": rid, "DSLINKNO": ds, "strmOrder": order, "USContArea": km2 * 1e6})
    return pd.DataFrame(rows).set_index("LINKNO")


@pytest.fixture
def geo(monkeypatch):
    fake = types.ModuleType("geoglows")

    def retro_daily(river_id, format="df", **kw):
        idx = pd.date_range("2020-01-01", periods=5, tz="UTC")
        return pd.DataFrame({river_id: [0.3] * 5}, index=idx)

    fake.data = types.SimpleNamespace(retro_daily=retro_daily)
    monkeypatch.setitem(sys.modules, "geoglows", fake)
    be = GeoglowsBackend()
    monkeypatch.setattr(be, "_assert_available", lambda: None)
    monkeypatch.setattr(be, "_get_river_id", lambda lat, lon: 110042835)
    monkeypatch.setattr(be, "_metadata", lambda: _meta())
    return be


def _geo_run(be, geometry):
    return be.fetch_timeseries(get_product("GEOGLOWS_RETRO"), geometry, "2020-01-01", "2020-01-05",
                               "basin_mean", outlet=(-1.28, 36.83))


def test_geoglows_bbox_does_not_set_the_area_target(geo):
    df = _geo_run(geo, BBOX)
    sr = df.attrs["aihydro_site_resolution"]
    assert sr["target_area_km2"] is None and sr["target_basis"] == "bbox"
    assert sr["river_id"] == 110042835 and sr["strategy"] == "nearest_reach" and sr["hops"] == 0
    assert sr["uparea_km2"] == 12.0 and sr["area_ratio"] is None
    notes = " ".join(df.attrs["aihydro_notes"])
    assert "bounding box" in notes and "not checked against the basin" in notes


def test_geoglows_basin_area_picks_the_matching_reach_and_records_the_walk(geo, monkeypatch):
    # Pin the target at the evidence value (166.9 km2); the matching reach is 110275060.
    monkeypatch.setattr("aihydro_data.geometry.measures.outlet_and_area",
                        lambda g, o=None: (-1.28, 36.83, BASIN_KM2))
    df = _geo_run(geo, _basin_polygon())
    sr = df.attrs["aihydro_site_resolution"]
    assert sr["river_id"] == 110275060 and sr["nearest_reach"] == 110042835
    assert sr["strategy"] == "area_match_downstream" and sr["hops"] == 5
    assert sr["uparea_km2"] == 116.0 and sr["strm_order"] == 4
    assert sr["target_area_km2"] == BASIN_KM2
    assert sr["area_ratio"] == pytest.approx(116.0 / BASIN_KM2)
    assert sr["requested"] == {"lat": -1.28, "lon": 36.83}


def test_geoglows_wrong_target_reproduces_the_evidence_reach(geo, monkeypatch):
    # Pre-fix behaviour: the 819.4 km2 bbox area as target walks 10 hops to the 657 km2 reach.
    monkeypatch.setattr("aihydro_data.geometry.measures.outlet_and_area",
                        lambda g, o=None: (-1.28, 36.83, NAIROBI_BBOX_AREA))
    sr = _geo_run(geo, _basin_polygon()).attrs["aihydro_site_resolution"]
    assert sr["river_id"] == 110345907 and sr["hops"] == 10


def test_geoglows_ratio_outside_band_adds_a_note(geo, monkeypatch):
    monkeypatch.setattr(geo, "_snap", lambda *a, **k: {"river_id": 110345907, "strategy": "area_match_downstream",
                                                       "uparea_km2": 657.0, "strm_order": 5,
                                                       "nearest_reach": 110042835, "hops": 10})
    monkeypatch.setattr("aihydro_data.geometry.measures.outlet_and_area",
                        lambda g, o=None: (-1.28, 36.83, BASIN_KM2))
    df = _geo_run(geo, _basin_polygon())
    assert any("heuristic (not validated) agreement band" in n and "110345907" in n for n in df.attrs["aihydro_notes"])
    assert df.attrs["aihydro_site_resolution"]["area_ratio"] == pytest.approx(657.0 / BASIN_KM2)


# ── cache key: pre-fix bbox entries must miss ────────────────────────────────

def test_pre_fix_bbox_streamflow_cache_entry_is_not_served(monkeypatch):
    """An entry stored under the old key shape (no modelled_site_identity) is not found."""
    import aihydro_data
    import aihydro_data.cache as cache_mod

    seen: list[tuple[str, dict]] = []
    real_key = cache_mod.cache_key
    store: dict[str, str] = {}

    def spy_key(payload):
        k = real_key(payload)
        seen.append((k, dict(payload)))
        return k

    class _Stop(Exception):
        pass

    def fake_read(ck, *a, **k):
        if ck in store:
            return store[ck]            # a stale hit would be returned (and served)
        raise _Stop()

    monkeypatch.setattr(cache_mod, "cache_key", spy_key)
    monkeypatch.setattr(cache_mod, "cache_read", fake_read)

    def run(geom):
        try:
            aihydro_data.fetch("streamflow", geom, "2020-01-01", "2020-01-05",
                               mode="manual", product="GEOGLOWS_RETRO", fallback=[])
        except _Stop:
            pass
        except Exception:
            pass
        return seen[-1]

    new_key, payload = run(BBOX)
    assert payload["modelled_site_identity"] == 2
    old_payload = {k: v for k, v in payload.items() if k != "modelled_site_identity"}
    old_key = real_key(old_payload)
    assert old_key != new_key
    store[old_key] = "STALE-PRE-FIX-ENTRY"          # the entry written by the old code
    again_key, _ = run(BBOX)
    assert again_key == new_key and again_key not in store   # old entry is not hit

    # Unrelated geometry (a real, non-rectangular polygon) keeps its key shape.
    _, poly_payload = run(_basin_polygon())
    assert "modelled_site_identity" not in poly_payload
