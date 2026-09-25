"""Contract tests for interpretation metadata on FetchResult.

Written after a real analysis (Nepal, 26 Aug 2026) in which a product whose
archive stopped 26 days before the requested end date returned a short series
with nothing to say so. A `.tail(7).sum()` over that series returned 0.0 mm,
which reads as "no rain fell" and means "no data exists".
"""
import pandas as pd
import pytest

from aihydro_data._pipeline import _temporal_coverage, RESULT_SCHEMA_VERSION
from aihydro_data.contracts import FetchResult


def _frame(start, end):
    return pd.DataFrame({"date": pd.date_range(start, end), "v": 1.0})


class TestTemporalCoverage:
    def test_complete_window_is_complete(self):
        cov = _temporal_coverage(_frame("2026-06-01", "2026-08-26"),
                                 "2026-06-01", "2026-08-26")
        assert cov["coverage_complete"] is True
        assert cov["days_missing_tail"] == 0

    def test_archive_ending_early_is_flagged(self):
        cov = _temporal_coverage(_frame("2026-06-01", "2026-07-31"),
                                 "2026-06-01", "2026-08-26")
        assert cov["coverage_complete"] is False
        assert cov["days_missing_tail"] == 26
        assert cov["coverage_end"] == "2026-07-31"

    def test_record_starting_late_is_flagged(self):
        cov = _temporal_coverage(_frame("2026-07-01", "2026-08-26"),
                                 "2026-06-01", "2026-08-26")
        assert cov["coverage_complete"] is False
        assert cov["days_missing_head"] == 30

    def test_one_day_short_is_within_tolerance(self):
        cov = _temporal_coverage(_frame("2026-06-01", "2026-08-25"),
                                 "2026-06-01", "2026-08-26")
        assert cov["coverage_complete"] is True

    def test_datetime_index_without_date_column(self):
        df = _frame("2026-06-01", "2026-07-31").set_index("date")
        cov = _temporal_coverage(df, "2026-06-01", "2026-08-26")
        assert cov["days_missing_tail"] == 26

    @pytest.mark.parametrize("data", [None, object(), pd.DataFrame()])
    def test_undatable_data_never_invents_a_warning(self, data):
        assert _temporal_coverage(data, "2026-06-01", "2026-08-26")["coverage_complete"]


class TestFetchResultDefaults:
    def test_units_default_is_empty_not_assumed(self):
        r = FetchResult(variable="precipitation", product="X", source="gee",
                        request={"variable": "precipitation", "geometry": None,
                                 "start": "2026-01-01", "end": "2026-01-02"},
                        data=None)
        assert r.units == ""
        assert r.timestep == ""
        assert r.common_pitfalls == []
        assert r.coverage_complete is True

    def test_schema_version_is_set(self):
        assert RESULT_SCHEMA_VERSION >= 2
