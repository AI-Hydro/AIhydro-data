"""Daily gridded-fetch output validation (Science Wave 3, PR-2).

Offline: pygridmet is replaced by a synthetic per-year client. The defect being
modelled is documented in docs/vision-2040/evidence/gridmet-chunk-defect-1.
"""
from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from shapely.geometry import box

from aihydro_data import _daily_validation as dv
from aihydro_data._pipeline import (
    RESULT_SCHEMA_VERSION,
    _result_schema_for,
    _temporal_coverage,
)
from aihydro_data.sources import hyriver

GEOM = box(-69.77, 45.07, -69.31, 45.45)
SPEC = SimpleNamespace(variable="precipitation", source="hyriver",
                       backend_config={"pygridmet_variable": "pr"})


def _year_ds(year: int, a: str, b: str, drop_leap_dec31: bool = True) -> xr.Dataset:
    days = pd.date_range(a, b)
    if drop_leap_dec31:                       # what pygridmet's dates_tolist requests
        days = days[~((days.month == 12) & (days.day == 31) & days.is_leap_year)]
    vals = np.broadcast_to((year * 1000 + days.dayofyear.values)[:, None, None],
                           (len(days), 2, 2)).astype("f4")
    return xr.Dataset({"pr": (("time", "y", "x"), vals)},
                      coords={"time": days.values, "y": [0, 1], "x": [0, 1]})


class FakeGridmet:
    """Stand-in for pygridmet. `poison` maps a requested year to the year whose
    data the SHARED cache returns; a private cache (HYRIVER_CACHE_NAME set) is
    clean unless `poison_private` is also set.

    `faults[year] = {"kind": "truncate"|"gap"|"raise", "arg": ..., "persistent": bool}`
    injects a fault on the shared cache (and on the private one when persistent)."""

    def __init__(self, poison=None, poison_private=False, drop_leap_dec31=True, faults=None):
        self.poison = poison or {}
        self.poison_private = poison_private
        self.drop = drop_leap_dec31
        self.faults = faults or {}
        self.calls: list[tuple[str, str, bool]] = []

    def _chunk(self, a, b):
        private = bool(os.environ.get("HYRIVER_CACHE_NAME"))
        self.calls.append((a, b, private))
        y = pd.Timestamp(a).year
        if pd.Timestamp(a) == pd.Timestamp(b) and pd.Timestamp(a).is_leap_year \
                and pd.Timestamp(a).month == 12 and pd.Timestamp(a).day == 31:
            return _year_ds(y, a, b, True).isel(time=slice(0, 0))      # real pygridmet: []
        f = self.faults.get(y)
        active = f and (f.get("persistent") or not private)
        if active and f["kind"] == "raise":
            raise ConnectionError("simulated THREDDS failure")
        if y in self.poison and (self.poison_private or not private):
            wy = self.poison[y]
            return _year_ds(wy, f"{wy}-01-01", f"{wy}-12-31", self.drop)
        ds = _year_ds(y, a, b, self.drop)
        if active and f["kind"] == "truncate":
            ds = ds.isel(time=slice(0, f["arg"]))
        if active and f["kind"] == "gap":
            keep = ~np.isin(ds["time"].values, pd.to_datetime(f["arg"]).values)
            ds = ds.isel(time=np.where(keep)[0])
        return ds

    def get_bygeom(self, geom, dates, variables):
        return self._chunk(*dates)

    def get_bycoords(self, coords, dates, variables):
        ds = self._chunk(*dates)
        out = ds["pr"].mean(dim=["y", "x"]).to_series().to_frame("pr")
        out.index.name = "time"
        return out


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.delenv("HYRIVER_CACHE_NAME", raising=False)
    monkeypatch.setenv("AIHYDRO_DATA_NO_RETRY", "1")

    def install(client):
        mod = types.ModuleType("pygridmet")
        mod.get_bygeom = client.get_bygeom
        mod.get_bycoords = client.get_bycoords
        monkeypatch.setitem(sys.modules, "pygridmet", mod)
        return client
    return install


def _fetch(start="1998-01-01", end="2000-12-31", aggregation="basin_mean"):
    return hyriver.Backend()._fetch_gridmet(SPEC, {"pygridmet_variable": "pr"}, GEOM,
                                            start, end, aggregation)


def _coverage(df, start, end):
    """What the pipeline computes from the backend's attrs."""
    dropped, hints = dv.chunk_hints(df.attrs.get("aihydro_chunk_validation", {}))
    return _temporal_coverage(df, start, end, timestep="daily", skip_leap_dec31=True,
                              chunk_mismatch_ranges=dropped, reason_hints=hints)


def test_clean_fetch_passes_unchanged(fake):
    client = fake(FakeGridmet(drop_leap_dec31=False))
    df = _fetch()
    assert len(df) == 365 + 365 + 366
    assert not df["date"].duplicated().any()
    assert "aihydro_chunk_validation" not in df.attrs
    assert all(not private for *_, private in client.calls)      # no retry happened
    cov = _temporal_coverage(df, "1998-01-01", "2000-12-31", timestep="daily")
    assert cov["coverage_complete"] is True
    assert cov["days_missing_internal"] == 0 and cov["missing_ranges"] == []


def test_wrong_year_chunk_detected_and_retried_with_private_cache(fake):
    client = fake(FakeGridmet(poison={1999: 2006}))
    df = _fetch()
    # the 1999 chunk was fetched twice: shared cache first, then a private one
    assert [c for c in client.calls if c[0].startswith("1999")] == [
        ("1999-01-01", "1999-12-31", False), ("1999-01-01", "1999-12-31", True)]
    assert (df["date"].dt.year == 1999).sum() == 365          # real 1999, not 2006
    assert not df["date"].duplicated().any()
    assert "aihydro_chunk_validation" not in df.attrs
    assert "HYRIVER_CACHE_NAME" not in os.environ             # env restored


def test_persistent_wrong_year_not_merged_and_reported(fake):
    fake(FakeGridmet(poison={1999: 2006}, poison_private=True))
    df = _fetch()
    assert (df["date"].dt.year == 1999).sum() == 0            # bad chunk dropped
    assert not df["date"].duplicated().any()                   # never merged silently
    assert (df["date"].dt.year == 2006).sum() == 0
    fails = df.attrs["aihydro_chunk_validation"]["failures"]
    assert [(f["start"], f["end"], f["reason"]) for f in fails] == [
        ("1999-01-01", "1999-12-31", "chunk_mismatch")]
    assert any("NOT merged" in n for n in df.attrs["aihydro_notes"])

    cov = _temporal_coverage(
        df, "1998-01-01", "2000-12-31", timestep="daily", skip_leap_dec31=True,
        chunk_mismatch_ranges=[(f["start"], f["end"]) for f in fails])
    assert cov["coverage_complete"] is False
    assert cov["days_missing_internal"] == 365                # 1999 only; 2000-12-31 is the tail
    assert cov["days_missing_tail"] == 1
    by_reason = {}
    for r in cov["missing_ranges"]:
        by_reason.setdefault(r["reason"], []).append((r["start"], r["end"], r["days"]))
    assert by_reason["chunk_mismatch"] == [("1999-01-01", "1999-12-31", 365)]
    assert by_reason["client_calendar_assumption"] == [("2000-12-31", "2000-12-31", 1)]


def test_leap_year_dec31_reported_as_client_calendar_assumption(fake):
    fake(FakeGridmet(drop_leap_dec31=True))
    df = _fetch("1999-01-01", "2001-12-31")
    cov = _temporal_coverage(df, "1999-01-01", "2001-12-31", timestep="daily",
                             skip_leap_dec31=True)
    assert cov["days_missing_internal"] == 1
    assert cov["missing_ranges"] == [
        {"start": "2000-12-31", "end": "2000-12-31", "days": 1,
         "reason": "client_calendar_assumption"}]
    # disclosed, not a coverage failure, and never worded as missing in GridMET
    assert cov["coverage_complete"] is True


def test_same_gap_without_the_assumption_is_absent_and_incomplete():
    idx = pd.date_range("2000-01-01", "2000-01-31").delete(range(9, 14))   # 5 days out
    df = pd.DataFrame({"date": idx, "precipitation": 1.0})
    cov = _temporal_coverage(df, "2000-01-01", "2000-01-31", timestep="daily")
    assert cov["days_missing_internal"] == 5
    assert cov["missing_ranges"] == [{"start": "2000-01-10", "end": "2000-01-14",
                                      "days": 5, "reason": "absent_in_response"}]
    assert cov["coverage_complete"] is False


def test_non_daily_timestep_has_no_gap_analysis():
    df = pd.DataFrame({"date": pd.to_datetime(["2000-01-01", "2000-06-01"]), "v": 1.0})
    cov = _temporal_coverage(df, "2000-01-01", "2000-06-01", timestep="monthly")
    assert cov["days_missing_internal"] == 0 and cov["missing_ranges"] == []


def test_validator_flags_duplicates_missing_year_and_cross_wired_chunk():
    t = pd.to_datetime(["2006-01-01", "2006-01-01", "2006-01-02"])
    v = dv.validate_daily_series(t, "1999-01-01", "2006-12-31")
    assert not v["ok"] and v["duplicates"] == 1 and 1999 in v["years_missing"]
    ch = dv.validate_daily_series(
        pd.date_range("2006-01-01", "2006-12-31"), "1999-01-01", "1999-12-31",
        chunks=[("1999-01-01", "1999-12-31", pd.date_range("2006-01-01", "2006-12-31"))])
    assert not ch["ok"] and ch["bad_chunks"][0]["start"] == "1999-01-01"


def test_precipitation_cache_key_changes_and_others_unchanged():
    for var in ("precipitation", "tmax", "tmin", "pet"):
        assert _result_schema_for(var) == f"{RESULT_SCHEMA_VERSION}.{var}.2"
    assert _result_schema_for("srad") == RESULT_SCHEMA_VERSION      # unlisted: same key


# ── caching of degraded results ───────────────────────────────────────────

def _gridmet_result(drop_days, ranges):
    from aihydro_data.contracts import FetchRequest, FetchResult
    days = pd.date_range("2000-12-20", "2001-01-10").difference(pd.DatetimeIndex(drop_days))
    df = pd.DataFrame({"date": days, "precipitation": 1.0})
    return FetchResult(
        variable="precipitation", product="GRIDMET_PRECIP", source="hyriver",
        request=FetchRequest(variable="precipitation", geometry=GEOM,
                             start="2000-12-20", end="2001-01-10"),
        cache_key="gridmetkey000000000000", data=df, license="pd", citation="c", bibtex="b",
        missing_ranges=ranges,
    )


def test_result_with_dropped_chunk_is_not_cached(tmp_path):
    from unittest.mock import patch

    from aihydro_data.cache import cache_write
    res = _gridmet_result(
        pd.date_range("2000-12-25", "2000-12-27"),
        [{"start": "2000-12-25", "end": "2000-12-27", "days": 3, "reason": "chunk_mismatch"}])
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        assert cache_write(res) is None
    assert list(tmp_path.iterdir()) == []                 # no data file, no manifest


def test_calendar_assumption_gap_is_cached_and_reasons_survive_restore(tmp_path):
    import json
    from unittest.mock import patch

    from aihydro_data.cache import cache_read, cache_write
    rng = [{"start": "2000-12-31", "end": "2000-12-31", "days": 1,
            "reason": "client_calendar_assumption"}]
    res = _gridmet_result([pd.Timestamp("2000-12-31")], rng)
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        assert cache_write(res)
        stored = json.loads((tmp_path / "gridmetkey000000000000.manifest.json").read_text())
        stored = stored[-1] if isinstance(stored, list) else stored
        assert stored["missing_ranges"] == rng             # persisted with its reason code
        back = cache_read("gridmetkey000000000000")
    assert back.missing_ranges == rng
    assert back.days_missing_internal == 1
    assert back.coverage_complete is True


def test_stored_reasons_take_precedence_over_rederivation():
    t = pd.date_range("2000-12-25", "2001-01-03").delete(6)       # 2000-12-31 absent
    derived = dv.find_missing_ranges(t, skip_leap_dec31=True)
    assert derived[0]["reason"] == "client_calendar_assumption"
    hinted = dv.find_missing_ranges(
        t, skip_leap_dec31=True,
        reason_hints=[{"start": "2000-12-31", "end": "2000-12-31", "reason": "absent_in_response"}])
    assert hinted[0]["reason"] == "absent_in_response"


# ── review should-fixes: truncation, leap Dec 31 windows, gaps, exceptions ──

def _never_cached(df, start, end, tmp_path):
    from unittest.mock import patch

    from aihydro_data.cache import cache_write
    cov = _coverage(df, start, end)
    res = _gridmet_result([], cov["missing_ranges"])
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        return cache_write(res) is None and list(tmp_path.iterdir()) == []


def test_truncated_chunk_is_retried_and_healed(fake):
    client = fake(FakeGridmet(faults={2005: {"kind": "truncate", "arg": 180}}))
    df = _fetch("2004-01-01", "2006-12-31")
    assert (df["date"].dt.year == 2005).sum() == 365
    assert [c[2] for c in client.calls if c[0].startswith("2005")] == [False, True]
    assert "aihydro_chunk_validation" not in df.attrs


def test_persistently_truncated_chunk_is_dropped_and_not_cached(fake, tmp_path):
    fake(FakeGridmet(faults={2005: {"kind": "truncate", "arg": 180, "persistent": True}}))
    df = _fetch("2004-01-01", "2006-12-31")
    assert (df["date"].dt.year == 2005).sum() == 0
    f = df.attrs["aihydro_chunk_validation"]["failures"]
    assert f[0]["reason"] == "chunk_mismatch" and "truncated" in f[0]["problems"][0]
    cov = _coverage(df, "2004-01-01", "2006-12-31")
    assert cov["coverage_complete"] is False
    assert {r["reason"] for r in cov["missing_ranges"]} >= {"chunk_mismatch"}
    assert _never_cached(df, "2004-01-01", "2006-12-31", tmp_path)


def test_tail_shortfall_near_today_is_archive_extent_not_a_drop(fake, monkeypatch):
    monkeypatch.setattr(dv, "_today", lambda: pd.Timestamp("2007-01-05"))   # 5 days after the request end
    client = fake(FakeGridmet(faults={2006: {"kind": "truncate", "arg": 360, "persistent": True}}))
    df = _fetch("2005-01-01", "2006-12-31")
    assert (df["date"].dt.year == 2006).sum() == 360         # kept
    assert all(c[2] is False for c in client.calls)          # no retry for an archive edge
    cov = _coverage(df, "2005-01-01", "2006-12-31")
    assert cov["days_missing_tail"] == 5


def test_recent_request_but_large_tail_shortfall_is_truncation(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(dv, "_today", lambda: pd.Timestamp("2007-01-05"))   # request ended 5 days ago
    client = fake(FakeGridmet(faults={2006: {"kind": "truncate", "arg": 59, "persistent": True}}))
    df = _fetch("2005-01-01", "2006-12-31")                                  # chunk stops on Feb 28
    assert (df["date"].dt.year == 2006).sum() == 0
    assert [c[2] for c in client.calls if c[0].startswith("2006")] == [False, True]
    assert df.attrs["aihydro_chunk_validation"]["failures"][0]["reason"] == "chunk_mismatch"
    assert _never_cached(df, "2005-01-01", "2006-12-31", tmp_path)


def test_historical_final_chunk_truncation_is_retried_dropped_and_not_cached(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(dv, "_today", lambda: pd.Timestamp("2026-10-22"))
    client = fake(FakeGridmet(faults={2006: {"kind": "truncate", "arg": 300, "persistent": True}}))
    df = _fetch("2005-01-01", "2006-12-31")
    assert (df["date"].dt.year == 2006).sum() == 0
    assert [c[2] for c in client.calls if c[0].startswith("2006")] == [False, True]
    assert df.attrs["aihydro_chunk_validation"]["failures"][0]["reason"] == "chunk_mismatch"
    assert _never_cached(df, "2005-01-01", "2006-12-31", tmp_path)


def test_historical_final_chunk_truncation_heals_on_retry(fake, monkeypatch):
    monkeypatch.setattr(dv, "_today", lambda: pd.Timestamp("2026-10-22"))
    fake(FakeGridmet(faults={2006: {"kind": "truncate", "arg": 300}}))
    df = _fetch("2005-01-01", "2006-12-31")
    assert (df["date"].dt.year == 2006).sum() == 365


def test_recent_window_boundary_is_14_days():
    assert dv.RECENT_PUBLICATION_DAYS == 14
    now = dv._today()
    assert dv.tail_may_be_archive_edge(now - pd.Timedelta(days=14))
    assert not dv.tail_may_be_archive_edge(now - pd.Timedelta(days=15))


def test_window_starting_on_leap_dec31_skips_the_empty_chunk(fake):
    client = fake(FakeGridmet())
    df = _fetch("2000-12-31", "2001-03-31")
    assert ("2000-12-31", "2000-12-31", False) not in client.calls
    assert "aihydro_chunk_validation" not in df.attrs
    assert df["date"].min() == pd.Timestamp("2001-01-01")
    cov = _coverage(df, "2000-12-31", "2001-03-31")
    assert cov["missing_ranges"] == [{"start": "2000-12-31", "end": "2000-12-31", "days": 1,
                                      "reason": "client_calendar_assumption"}]
    assert cov["coverage_complete"] is True


def test_year_chunks_skip_only_the_leap_dec31_only_chunk():
    assert dv.year_chunks("2000-12-31", "2000-12-31", skip_leap_dec31=True) == []
    assert len(dv.year_chunks("2000-12-31", "2000-12-31")) == 1
    assert len(dv.year_chunks("2001-12-31", "2001-12-31", skip_leap_dec31=True)) == 1


def test_transient_internal_gap_heals_on_retry(fake):
    fake(FakeGridmet(faults={2005: {"kind": "gap", "arg": ["2005-03-01", "2005-03-02"]}}))
    df = _fetch("2004-06-01", "2006-06-30")
    assert (df["date"].dt.year == 2005).sum() == 365
    assert "aihydro_chunk_validation" not in df.attrs


def test_persistent_gap_keeps_the_year_discloses_days_and_is_not_cached(fake, tmp_path):
    fake(FakeGridmet(faults={2005: {"kind": "gap", "persistent": True,
                                    "arg": ["2005-03-01", "2005-03-02", "2005-03-03", "2005-07-04"]}}))
    df = _fetch("2004-06-01", "2006-06-30")
    assert (df["date"].dt.year == 2005).sum() == 361                 # year kept, not dropped
    info = df.attrs["aihydro_chunk_validation"]
    assert info["failures"] == []
    cov = _coverage(df, "2004-06-01", "2006-06-30")
    gaps = [(r["start"], r["end"], r["days"]) for r in cov["missing_ranges"]
            if r["reason"] == "absent_after_retry"]
    assert gaps == [("2005-03-01", "2005-03-03", 3), ("2005-07-04", "2005-07-04", 1)]
    assert cov["coverage_complete"] is False
    assert any("absent_after_retry" in n for n in df.attrs["aihydro_notes"])
    assert _never_cached(df, "2004-06-01", "2006-06-30", tmp_path)


def test_exception_on_first_fetch_triggers_private_retry(fake):
    client = fake(FakeGridmet(faults={1999: {"kind": "raise"}}))
    df = _fetch()
    assert (df["date"].dt.year == 1999).sum() == 365
    assert [c[2] for c in client.calls if c[0].startswith("1999")] == [False, True]


def test_exception_on_retry_records_one_year_and_keeps_the_rest(fake):
    fake(FakeGridmet(faults={1999: {"kind": "raise", "persistent": True}}))
    df = _fetch()
    assert (df["date"].dt.year == 1999).sum() == 0
    assert (df["date"].dt.year == 1998).sum() == 365 and (df["date"].dt.year == 2000).sum() == 365
    f = df.attrs["aihydro_chunk_validation"]["failures"]
    assert f[0]["reason"] == "chunk_mismatch" and "fetch error: ConnectionError" in f[0]["problems"][0]


def test_all_chunks_failing_raises(fake):
    fake(FakeGridmet(faults={1999: {"kind": "raise", "persistent": True}}))
    with pytest.raises(RuntimeError, match="every yearly chunk failed"):
        _fetch("1999-01-01", "1999-12-31")


def test_centroid_path_validates_and_retries_too(fake):
    client = fake(FakeGridmet(poison={1999: 2006}))
    df = _fetch(aggregation="centroid")
    assert (df["date"].dt.year == 1999).sum() == 365
    assert [c[2] for c in client.calls if c[0].startswith("1999")] == [False, True]
    assert not df["date"].duplicated().any()


def test_centroid_path_persistent_fault_is_not_merged(fake):
    fake(FakeGridmet(poison={1999: 2006}, poison_private=True))
    df = _fetch(aggregation="centroid")
    assert (df["date"].dt.year == 1999).sum() == 0
    assert df.attrs["aihydro_chunk_validation"]["failures"][0]["reason"] == "chunk_mismatch"


def test_daymet_calls_run_under_the_hyriver_lock(monkeypatch):
    seen = []

    def get_bygeom(geom, dates, variables):
        seen.append(hyriver._HYRIVER_ENV_LOCK._is_owned())
        return _year_ds(2001, "2001-01-01", "2001-01-10", False).rename({"pr": "prcp"})

    mod = types.ModuleType("pydaymet")
    mod.get_bygeom = get_bygeom
    monkeypatch.setitem(sys.modules, "pydaymet", mod)
    monkeypatch.setenv("AIHYDRO_DATA_NO_RETRY", "1")
    hyriver.Backend()._fetch_daymet(SimpleNamespace(variable="precipitation"),
                                    {"pydaymet_variable": "prcp"}, GEOM,
                                    "2001-01-01", "2001-01-10", "basin_mean")
    assert seen == [True]


def test_cache_read_of_a_legacy_manifest_without_missing_ranges(tmp_path):
    import json
    from unittest.mock import patch

    from aihydro_data.cache import cache_read, cache_write
    rng = [{"start": "2000-12-31", "end": "2000-12-31", "days": 1,
            "reason": "client_calendar_assumption"}]
    res = _gridmet_result([pd.Timestamp("2000-12-31")], rng)
    mf = tmp_path / "gridmetkey000000000000.manifest.json"
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        assert cache_write(res)
        entries = json.loads(mf.read_text())
        for e in (entries if isinstance(entries, list) else [entries]):
            e.pop("missing_ranges")                           # a pre-0.2.5 manifest
        mf.write_text(json.dumps(entries))
        back = cache_read("gridmetkey000000000000")
    assert back is not None and back.missing_ranges == rng     # re-derived, same reason
