"""Tool audit D10: GEE ``filterDate`` has an exclusive end; the aihydro-data contract is an inclusive one.

No network: a fake ``ee`` applies the documented exclusive-end semantics.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

import aihydro_data
from aihydro_data._pipeline import _temporal_coverage
from aihydro_data.sources._gee_vendored import timeseries as vt
from aihydro_data.sources._gee_vendored.dates import filter_end


@pytest.mark.parametrize("end, want", [
    ("2020-01-31", "2020-02-01"),
    ("2020-02-28", "2020-02-29"),          # leap year
    ("2019-12-31", "2020-01-01"),
    ("2020-01-15T00:00:00", "2020-01-16"),
    ("2020-01-15T12:30:00", "2020-01-15T12:30:00"),   # an instant is left as given
])
def test_filter_end(end, want):
    assert filter_end(end) == want


def _fake_ee(log):
    class Coll:
        def __init__(self, d): log["id"] = d
        def filterDate(self, a, b): log["filterDate"] = (a, b); return self
        def select(self, b): return self
        def map(self, f): return self

    class FC:
        def __init__(self, c): pass
        def getInfo(self):
            a, b = log["filterDate"]
            d = pd.date_range(a, b, inclusive="left")          # GEE semantics: end exclusive
            return {"features": [{"properties": {"date": x.strftime("%Y-%m-%d"), "value": 2.0}} for x in d]}

    return type("ee", (), {
        "Initialize": staticmethod(lambda **k: None),
        "Geometry": staticmethod(lambda g: g),
        "ImageCollection": Coll, "FeatureCollection": FC,
        "Reducer": type("R", (), {"mean": staticmethod(lambda: 1)}),
    })


def _run(monkeypatch, start, end):
    log: dict = {}
    ee = _fake_ee(log)
    monkeypatch.setattr(vt, "_import_ee", lambda: (True, ee, None))
    r = vt.extract_timeseries(dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT", band="precipitation",
                              start_date=start, end_date=end, roi_geojson={"type": "Point", "coordinates": [0, 0]})
    return log, r


def test_january_window_returns_all_31_days_including_the_last(monkeypatch):
    log, r = _run(monkeypatch, "2020-01-01", "2020-01-31")
    assert log["filterDate"] == ("2020-01-01", "2020-02-01")
    assert len(r["rows"]) == 31 and r["rows"][-1]["date"] == "2020-01-31"      # was 30 rows, last 01-30


def test_single_day_request_returns_that_day(monkeypatch):
    _, r = _run(monkeypatch, "2020-01-15", "2020-01-15")
    assert [x["date"] for x in r["rows"]] == ["2020-01-15"]                    # was 0 rows


def test_every_filterDate_call_in_the_package_goes_through_filter_end():
    root = Path(aihydro_data.__file__).parent
    bad = []
    for p in (q for q in root.rglob("*.py") if q.name != "dates.py"):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if ".filterDate(" in line and "filter_end(" not in line and "query_end" not in line:
                bad.append(f"{p.relative_to(root)}:{n}")
    assert not bad, f"filterDate call without filter_end: {bad}"


def _daily(start, end):
    return pd.DataFrame({"date": pd.date_range(start, end), "v": 1.0})


def test_daily_coverage_detects_a_missing_last_day():
    cov = _temporal_coverage(_daily("2020-01-01", "2020-01-30"), "2020-01-01", "2020-01-31", timestep="daily")
    assert cov["days_missing_tail"] == 1
    assert cov["coverage_complete"] is False                                    # was True (tolerance 1)
    assert cov["coverage_tolerance_days"] == 0


def test_daily_coverage_complete_when_last_day_present():
    cov = _temporal_coverage(_daily("2020-01-01", "2020-01-31"), "2020-01-01", "2020-01-31", timestep="daily")
    assert cov["coverage_complete"] is True


def test_leap_day_skip_by_the_client_is_still_only_disclosed():
    cov = _temporal_coverage(_daily("2000-01-01", "2000-12-30"), "2000-01-01", "2000-12-31",
                             timestep="daily", skip_leap_dec31=True)
    assert cov["days_missing_tail"] == 1 and cov["coverage_complete"] is True


def test_non_daily_timestep_keeps_the_caller_tolerance():
    cov = _temporal_coverage(_daily("2020-01-01", "2020-01-30"), "2020-01-01", "2020-01-31", timestep="monthly")
    assert cov["coverage_complete"] is True
