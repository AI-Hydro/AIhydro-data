"""GridMET over THREDDS NCSS: large boxes, error bodies, leap days (all offline).

The server is a synthetic stand-in that speaks the NCSS URL contract and returns
real NetCDF bytes (written by xarray). Its shape comes from live probes of the
approved host on 2026-11-10 (docs: CHANGELOG, ARCHITECTURE "GridMET transport"):

* the whole Fish River box (0.89 x 0.63 deg) is one valid NetCDF response; its
  north-west corner lies in Canada, where GridMET has no data (NaN cells);
* NCSS returns every grid cell that intersects the requested box;
* 31 Dec 2000 is served with real values.
"""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from shapely.geometry import Polygon, box

from aihydro_data import _daily_validation as dv
from aihydro_data.exceptions import AihydroDataError
from aihydro_data.sources import _gridmet_ncss as ncss
from aihydro_data.sources import hyriver
from aihydro_data.sources._gridmet_ncss import GridmetRequestRejected, HttpResponse, NcssClient

pytest.importorskip("netCDF4", reason="the NCSS client reads NetCDF with the netCDF4 engine")

CELL = 1.0 / 24.0
LON0, LAT0 = -124.7666667, 49.4
# Fish River basin box from the DEMO-1 rehearsal (0.893 x 0.632 deg)
FISH = box(-68.966, 46.684, -68.073, 47.316)
FISH_POLY = Polygon([(-68.90, 46.75), (-68.20, 46.72), (-68.10, 47.20), (-68.55, 47.31), (-68.92, 47.10)])


def _is_canada(lat: float, lon: float) -> bool:
    """Synthetic domain edge: no data north of 47.25 N (the border in the live probe)."""
    return lat > 47.25


def _value(i: int, j: int, day: pd.Timestamp) -> float:
    return round(1.0 + 0.1 * i + 0.01 * j + 0.001 * day.dayofyear + (day.year - 1900) * 1e-4, 4)


def _netcdf3_bytes(ds) -> bytes:
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:           # file-backed (netCDF4 engine; CI has no scipy): xarray's in-memory scipy writer
        path = os.path.join(d, "r.nc")                 # leaves a BytesIO export that warns at GC
        ds.to_netcdf(path, format="NETCDF3_CLASSIC", engine="netcdf4")
        with open(path, "rb") as fh:
            return fh.read()


class FakeThredds:
    """NCSS stand-in. ``max_cells`` makes it reject larger boxes with a 200 error page;
    ``reject_all`` rejects everything; ``fail_status`` serves that HTTP status N times."""

    ERROR_PAGE = (b"<html><body><h1>Request failed</h1><p>GridMET did NOT process your request "
                  b"successfully: requested subset is too large</p></body></html>")

    def __init__(self, max_cells=None, reject_all=False, fail_status=None, fail_times=0,
                 partial_nan=None, lacks_leap_dec31=False):
        self.max_cells, self.reject_all = max_cells, reject_all
        self.fail_status, self.fail_times = fail_status, fail_times
        self.partial_nan = partial_nan          # (lat, lon) cell NaN on every other day, N times
        self.partial_nan_left = 1 if partial_nan else 0
        self.lacks_leap_dec31 = lacks_leap_dec31
        self.calls: list[str] = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError(f"{self.fail_status} Server Error: Service Unavailable for url")
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        w, e, s, n = (float(q[k]) for k in ("west", "east", "south", "north"))
        ks = range(math.floor((LAT0 - n) / CELL + 1e-9), math.ceil((LAT0 - s) / CELL - 1e-9) + 1)
        ms = range(math.floor((w - LON0) / CELL + 1e-9), math.ceil((e - LON0) / CELL - 1e-9) + 1)
        lats = np.array([LAT0 - k * CELL for k in ks])
        lons = np.array([LON0 + m * CELL for m in ms])
        if self.reject_all or (self.max_cells and len(lats) * len(lons) > self.max_cells):
            return HttpResponse(200, "text/html", self.ERROR_PAGE)
        days = pd.date_range(q["time_start"][:10], q["time_end"][:10])
        if self.lacks_leap_dec31:
            days = days[~((days.month == 12) & (days.day == 31) & days.is_leap_year)]
        vals = np.empty((len(days), len(lats), len(lons)), "float64")
        for a, d in enumerate(days):
            for b, k in enumerate(ks):
                for c, m in enumerate(ms):
                    vals[a, b, c] = np.nan if _is_canada(lats[b], lons[c]) else _value(k, m, d)
        if self.partial_nan and self.partial_nan_left:
            self.partial_nan_left -= 1
            b = int(np.argmin(np.abs(lats - self.partial_nan[0])))
            c = int(np.argmin(np.abs(lons - self.partial_nan[1])))
            vals[::2, b, c] = np.nan
        name = ncss.LONG_NAMES[re.search(r"agg_met_(\w+)_1979", url).group(1)]
        ds = xr.Dataset({name: (("day", "lat", "lon"), vals, {"units": "mm"})},
                        coords={"day": days.values, "lat": lats, "lon": lons})
        ds["day"].encoding["units"] = "days since 1900-01-01"
        body = _netcdf3_bytes(ds)                      # NetCDF-3 classic, like the live response
        return HttpResponse(200, "application/x-netcdf", body)


def _expected_mean(geom, day) -> tuple[float, int, int]:
    """Independent reference: cell centres inside ``geom`` that have data."""
    vals, inside = [], 0
    w, s, e, n = geom.bounds
    for k in range(math.floor((LAT0 - n) / CELL) - 1, math.ceil((LAT0 - s) / CELL) + 2):
        for m in range(math.floor((w - LON0) / CELL) - 1, math.ceil((e - LON0) / CELL) + 2):
            lat, lon = LAT0 - k * CELL, LON0 + m * CELL
            if geom.contains(box(lon, lat, lon, lat).centroid):
                inside += 1
                if not _is_canada(lat, lon):
                    vals.append(_value(k, m, day))
    return float(np.mean(vals)), inside, len(vals)


def _client(server, **kw):
    kw.setdefault("min_interval_s", 0.0)               # the production default is 1 s
    return NcssClient(http_get=server, **kw)


def _mean_series(ds, abbr="pr"):
    dims = [d for d in ds[abbr].dims if d != "time"]
    return ds[abbr].mean(dim=dims).to_series()


# ── 1. a large box is ONE request; NaN cells outside the grid are not "corruption" ──

def test_full_fish_river_box_is_a_single_request_and_the_mean_skips_no_data_cells():
    server = FakeThredds()
    c = _client(server)
    ds = c.get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-04"), ["pr"])
    assert len(server.calls) == 1
    for day in ("2000-01-03", "2000-01-04"):
        want, inside, with_data = _expected_mean(FISH_POLY, pd.Timestamp(day))
        assert _mean_series(ds)[pd.Timestamp(day)] == pytest.approx(want, rel=1e-5)
    bm = c.ledger.upstream()["gridmet_ncss"]["basin_mean"]
    assert bm["method"] == "cell_centre_in_polygon_unweighted_mean"
    assert bm["cells_in_geometry"] == inside and bm["cells_with_data"] == with_data
    assert bm["cells_without_data"] == inside - with_data > 0        # the Canadian cells, counted
    assert bm["all_touched_fallback"] is False
    assert any("no GridMET data" in n for n in c.ledger.notes)


def test_bounding_box_polygon_of_the_whole_basin_box_also_works_in_one_request():
    server = FakeThredds()
    ds = _client(server).get_bygeom(FISH, ("2000-01-03", "2000-01-03"), ["pr"])
    assert len(server.calls) == 1
    want, _, _ = _expected_mean(FISH, pd.Timestamp("2000-01-03"))
    assert _mean_series(ds).iloc[0] == pytest.approx(want, rel=1e-5)


def test_the_mean_is_unweighted_cell_centre_not_area_weighted():
    """A polygon covering exactly 2 cell centres of unequal value: the mean is their plain
    average whatever fraction of each cell the polygon overlaps."""
    server = FakeThredds()
    lat = LAT0 - 250 * CELL
    lon_a, lon_b = LON0 + 1350 * CELL, LON0 + 1351 * CELL
    poly = box(lon_a - 0.001, lat - 0.001, lon_b + 0.001, lat + 0.001)     # tiny overlap of each cell
    ds = _client(server).get_bygeom(poly, ("2000-02-01", "2000-02-01"), ["pr"])
    a, b = _value(250, 1350, pd.Timestamp("2000-02-01")), _value(250, 1351, pd.Timestamp("2000-02-01"))
    assert _mean_series(ds).iloc[0] == pytest.approx((a + b) / 2, rel=1e-5)


# ── 2. an error body served with HTTP 200 fails fast, structured, once ──────

@pytest.mark.parametrize("retries", ["1", "0"])
def test_error_body_with_http_200_fails_fast_and_is_not_retried(monkeypatch, retries):
    monkeypatch.setenv("AIHYDRO_DATA_NO_RETRY", retries)
    server = FakeThredds(reject_all=True)
    c = _client(server, max_split_depth=0)
    with pytest.raises(GridmetRequestRejected) as ei:
        c.get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-04"), ["pr"])
    assert len(server.calls) == 1                         # not 15, not 270
    err = ei.value
    assert isinstance(err, AihydroDataError) and err.code == "GRIDMET_REQUEST_REJECTED"
    d = err.to_dict()
    assert d["error"] is True and "did NOT process" in d["message"] and d["recovery"]
    assert d["details"]["status"] == 200 and "too large" in d["details"]["body_excerpt"]
    assert c.ledger.rejected and c.ledger.rejected[0]["bytes"] == len(FakeThredds.ERROR_PAGE)


def test_an_error_body_aborts_the_whole_fetch_not_one_chunk(monkeypatch):
    server = FakeThredds(reject_all=True)
    monkeypatch.setattr(hyriver, "_gridmet_client", lambda: _client(server, max_split_depth=0))
    with pytest.raises(GridmetRequestRejected):
        hyriver.Backend()._fetch_gridmet(_SPEC, {"pygridmet_variable": "pr"}, FISH_POLY,
                                         "1990-01-01", "2000-12-31", "basin_mean")
    assert len(server.calls) == 1                         # 11 yearly chunks never started


def test_http_4xx_is_rejected_without_retry():
    c = _client(lambda url, timeout: HttpResponse(404, "text/plain", b"Not Found"), max_split_depth=0)
    with pytest.raises(GridmetRequestRejected) as ei:
        c.get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-03"), ["pr"])
    assert ei.value.details["status"] == 404


def test_a_non_netcdf_200_is_not_parsed_as_data():
    assert not ncss.is_netcdf(FakeThredds.ERROR_PAGE) and not ncss.is_netcdf(b"")
    assert ncss.is_netcdf(_netcdf3_bytes(xr.Dataset({"a": ("x", [1.0])})))


# ── 3. splitting: a server with a size limit is answered by tiles + mosaic ─────────

def test_a_server_size_limit_is_met_by_splitting_and_the_mosaic_equals_the_whole_box():
    cw = _client(FakeThredds())
    whole = cw.get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-05"), ["pr"])
    limited = FakeThredds(max_cells=60)
    c = _client(limited)
    tiled = c.get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-05"), ["pr"])
    assert c.ledger.splits > 0 and 2 < len(limited.calls) <= ncss.MAX_REQUESTS_PER_CALL
    xr.testing.assert_allclose(_mean_series(tiled).to_xarray(), _mean_series(whole).to_xarray())
    assert int(tiled["pr"].notnull().sum()) == int(whole["pr"].notnull().sum())   # no hole, no double cell
    assert (c.ledger.cells_in_geometry, c.ledger.cells_with_data) == (
        cw.ledger.cells_in_geometry, cw.ledger.cells_with_data)
    assert c.ledger.upstream()["gridmet_ncss"]["split_requests"] == c.ledger.splits


def test_when_nothing_is_accepted_the_requests_are_bounded_by_the_split_depth():
    server = FakeThredds(reject_all=True)
    with pytest.raises(GridmetRequestRejected):
        _client(server, max_split_depth=3).get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-03"), ["pr"])
    assert len(server.calls) == 4                         # depth 0..3, then raise


def test_the_per_call_request_budget_is_enforced():
    server = FakeThredds(max_cells=2)
    with pytest.raises(GridmetRequestRejected) as ei:
        _client(server, max_requests_per_call=5, max_split_depth=9).get_bygeom(
            FISH_POLY, ("2000-01-03", "2000-01-03"), ["pr"])
    assert ei.value.code == "GRIDMET_REQUEST_BUDGET_EXCEEDED" and len(server.calls) == 5


# ── 4. transient server errors: bounded retries ──────────────────────────────────

def test_5xx_is_retried_exactly_three_times(monkeypatch):
    monkeypatch.setenv("AIHYDRO_DATA_NO_RETRY", "0")
    monkeypatch.setattr("aihydro_data.sources._retry.time.sleep", lambda s: None)
    server = FakeThredds(fail_status=503, fail_times=99)
    with pytest.raises(RuntimeError, match="503"):
        _client(server).get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-03"), ["pr"])
    assert len(server.calls) == 3


def test_5xx_then_success_recovers(monkeypatch):
    monkeypatch.setenv("AIHYDRO_DATA_NO_RETRY", "0")
    monkeypatch.setattr("aihydro_data.sources._retry.time.sleep", lambda s: None)
    server = FakeThredds(fail_status=504, fail_times=2)
    ds = _client(server).get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-03"), ["pr"])
    assert len(server.calls) == 3 and ds["pr"].notnull().any()


# ── 5. NaN on some days only (a flaky download) is re-requested once and disclosed ──

def test_partial_nan_cell_is_rerequested_once_and_recovers():
    server = FakeThredds(partial_nan=(46.9, -68.5))
    c = _client(server)
    ds = c.get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-06"), ["pr"])
    assert len(server.calls) == 2 and c.ledger.refetches == 1
    assert ncss._partial_nan_cells(ds["pr"]) == 0
    assert not any("NaN on some days" in n for n in c.ledger.notes)


def test_partial_nan_that_persists_is_disclosed():
    server = FakeThredds(partial_nan=(46.9, -68.5))
    server.partial_nan_left = 5
    c = _client(server)
    c.get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-06"), ["pr"])
    assert len(server.calls) == 2                         # one re-request, then stop
    assert c.ledger.partial_nan_cells >= 1
    assert any("NaN on some days" in n for n in c.ledger.notes)


# ── 6. small and awkward geometries ──────────────────────────────────────────────

def test_geometry_smaller_than_a_cell_falls_back_to_the_cells_it_touches_and_says_so():
    lat, lon = LAT0 - 250 * CELL + 0.004, LON0 + 1350 * CELL + 0.004
    tiny = box(lon, lat, lon + 0.002, lat + 0.002)
    c = _client(FakeThredds())
    ds = c.get_bygeom(tiny, ("2000-02-01", "2000-02-01"), ["pr"])
    assert c.ledger.all_touched_fallback is True and c.ledger.cells_with_data >= 1
    assert ds["pr"].notnull().any()


def test_geometry_outside_the_grid_is_refused_structurally():
    with pytest.raises(AihydroDataError) as ei:
        _client(FakeThredds()).get_bygeom(box(2.0, 48.0, 2.2, 48.2), ("2000-01-03", "2000-01-03"), ["pr"])
    assert ei.value.code == "GRIDMET_OUTSIDE_DOMAIN"


def test_point_fetch_returns_the_nearest_cell_series_and_discloses_a_no_data_cell():
    c = _client(FakeThredds())
    df = c.get_bycoords((-68.5, 46.9), ("2000-01-03", "2000-01-05"), ["pr"])
    assert list(df.columns) == ["pr"] and df.index.name == "time" and len(df) == 3
    assert df["pr"].notnull().all()
    c2 = _client(FakeThredds())
    df2 = c2.get_bycoords((-68.5, 47.3), ("2000-01-03", "2000-01-05"), ["pr"])      # "Canada"
    assert df2["pr"].isna().all() and any("no data" in n for n in c2.ledger.notes)


# ── 7. leap days ─────────────────────────────────────────────────────────────────

_SPEC = type("S", (), {"variable": "precipitation", "source": "hyriver",
                       "backend_config": {"pygridmet_variable": "pr"}})()


def _backend_fetch(server, start, end, geom=FISH_POLY, aggregation="basin_mean"):
    import unittest.mock as um
    with um.patch.object(hyriver, "_gridmet_client", lambda: _client(server)):
        return hyriver.Backend()._fetch_gridmet(_SPEC, {"pygridmet_variable": "pr"}, geom,
                                                start, end, aggregation)


def test_leap_year_window_has_every_day_including_31_dec():
    """Calendar 2000 (a leap year) plus the next Jan: 366 + 31 days, 31 Dec 2000 present."""
    server = FakeThredds()
    df = _backend_fetch(server, "2000-01-01", "2001-01-31")
    assert len(df) == 366 + 31
    assert pd.Timestamp("2000-12-31") in set(df["date"])
    assert not df["date"].duplicated().any()
    assert any(u.count("time_end=2000-12-31T00%3A00%3A00Z") for u in server.calls)   # asked for it
    cov = dv.validate_daily_series(df["date"], "2000-01-01", "2001-01-31")
    assert cov["ok"] and cov["missing_ranges"] == []
    assert "aihydro_chunk_validation" not in df.attrs


def test_water_year_2000_across_the_leap_day_29_feb_is_complete():
    df = _backend_fetch(FakeThredds(), "1999-10-01", "2000-09-30")
    assert len(df) == 366 and pd.Timestamp("2000-02-29") in set(df["date"])


def test_four_water_years_1461_days_not_1460():
    """The rehearsal window WY2000-2003 (1999-10-01..2003-09-30) was 1460 of 1461 days."""
    df = _backend_fetch(FakeThredds(), "1999-10-01", "2003-09-30")
    assert len(df) == 1461
    assert pd.Timestamp("2000-12-31") in set(df["date"])


def test_a_server_that_lacks_31_dec_is_counted_and_reported_never_filled():
    df = _backend_fetch(FakeThredds(lacks_leap_dec31=True), "2000-01-01", "2001-01-31")
    assert pd.Timestamp("2000-12-31") not in set(df["date"])              # no invented value
    fails = df.attrs["aihydro_chunk_validation"]["failures"]
    assert fails and fails[0]["reason"] == "chunk_mismatch" and "truncated" in fails[0]["problems"][0]
    cov = dv.find_missing_ranges(df["date"], "2000-01-01", "2001-01-31",
                                 chunk_mismatch_ranges=[(f["start"], f["end"]) for f in fails])
    assert sum(r["days"] for r in cov) == 366                              # counted


def test_leap_day_can_be_the_only_day_requested():
    df = _backend_fetch(FakeThredds(), "2000-12-31", "2000-12-31")
    assert list(df["date"]) == [pd.Timestamp("2000-12-31")]


# ── 8. metadata that reaches the result ─────────────────────────────────────────

def test_backend_attaches_method_requests_and_hashes_for_the_result():
    server = FakeThredds()
    df = _backend_fetch(server, "1999-10-01", "2000-09-30")
    up = df.attrs["aihydro_upstream"]["gridmet_ncss"]
    assert up["pygridmet_used"] is False and up["requests"] == len(server.calls) == 2
    assert len(up["request_urls"]) == len(up["response_sha256"]) == 2
    assert up["basin_mean"]["method"] == "cell_centre_in_polygon_unweighted_mean"
    assert up["basin_mean"]["cells_without_data"] > 0
    notes = " ".join(df.attrs["aihydro_notes"])
    assert "cell_centre_in_polygon_unweighted_mean" in notes and "not area-weighted" in notes


def test_pipeline_surfaces_the_method_in_notes_and_upstream(monkeypatch):
    """End to end through fetch(): notes and upstream on the FetchResult."""
    from aihydro_data import fetch
    server = FakeThredds()
    monkeypatch.setattr(hyriver, "_gridmet_client", lambda: _client(server))
    res = fetch("precipitation", FISH_POLY, "1999-10-01", "2000-09-30", mode="manual",
                product="GRIDMET_PRECIP", cache=False)
    assert res.product == "GRIDMET_PRECIP" and len(res.data) == 366
    assert res.coverage_complete is True and res.missing_ranges == []
    assert res.upstream["gridmet_ncss"]["basin_mean"]["method"] == "cell_centre_in_polygon_unweighted_mean"
    assert any("cell_centre_in_polygon_unweighted_mean" in n for n in res.notes)


# ── 9. the hard-coded table agrees with the library it replaces ─────────────────

def test_long_names_match_pygridmet():
    core = pytest.importorskip("pygridmet.core")
    g = core.GridMET(("2000-01-01", "2000-01-03"), list(ncss.LONG_NAMES), False)
    assert {k: g.long_names[k] for k in ncss.LONG_NAMES} == ncss.LONG_NAMES


def test_every_gridmet_variable_the_backend_maps_has_a_long_name():
    assert {k for k in hyriver._GRIDMET_VAR_MAP if k != "eto"} <= set(ncss.LONG_NAMES)


def test_request_url_has_the_ncss_contract():
    url = ncss.build_url("pr", (-68.966, 46.684, -68.073, 47.316), "2000-12-31", "2000-12-31")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("http://thredds.northwestknowledge.net:8080/thredds/ncss/agg_met_pr_1979_CurrentYear_CONUS.nc?")
    assert q["var"] == ["precipitation_amount"] and q["accept"] == ["netcdf"]
    assert q["time_start"] == q["time_end"] == ["2000-12-31T00:00:00Z"]
    assert (q["west"], q["east"], q["south"], q["north"]) == (["-68.966000"], ["-68.073000"], ["46.684000"], ["47.316000"])


def test_default_throttle_is_about_one_second():
    assert NcssClient(http_get=FakeThredds()).min_interval_s == ncss.DEFAULT_MIN_INTERVAL_S == 1.0


def test_missing_engine_is_a_structured_error(monkeypatch):
    import sys
    canned = FakeThredds()(ncss.build_url("pr", FISH_POLY.bounds, "2000-01-03", "2000-01-03"), None)
    monkeypatch.setitem(sys.modules, "netCDF4", None)           # after the fake wrote its bytes
    with pytest.raises(AihydroDataError) as ei:
        _client(lambda url, timeout: canned).get_bygeom(FISH_POLY, ("2000-01-03", "2000-01-03"), ["pr"])
    assert ei.value.code == "HYRIVER_NOT_INSTALLED" and "aihydro-data[hyriver]" in ei.value.recovery


def test_is_available_checks_the_real_gridmet_dependencies(monkeypatch):
    import sys
    from aihydro_data.products import get_product
    spec = get_product("GRIDMET_PRECIP")
    ok, _ = hyriver.Backend().is_available(spec)
    assert ok is True
    monkeypatch.setitem(sys.modules, "netCDF4", None)
    ok, why = hyriver.Backend().is_available(spec)
    assert ok is False and "netCDF4" in why and "hyriver" in why
