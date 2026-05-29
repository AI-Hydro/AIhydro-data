"""
Live backend smoke tests — Tier-1 verification.

These actually call GEE, HyRiver, and NWIS over the network. They are gated
behind `-m live` so CI skips them. A maintainer runs them locally before
each release.

Each test fetches a small, fast, well-known data slice and asserts:
  - the fetch succeeded
  - the result has the right shape (rows > 0, expected columns)
  - units / numeric range look sane
  - the cache round-trip works (write → invalidate → re-fetch returns identical)

Run:
    pytest tests/test_live_backends.py -m live -v --tb=short

Skip (default):
    pytest                       # `-m "not live"` is the default in CI
"""
from __future__ import annotations

import pytest
from shapely.geometry import Point


pytestmark = pytest.mark.live

# A reliable test point: Indianapolis, IN — well inside CONUS, well-covered
# by every CONUS product, and inside many global products' coverage.
CONUS_POINT = Point(-86.158, 39.7684)

# A reliable global test point: outside CONUS so the router picks global
# backends. Roorkee, India.
GLOBAL_POINT = Point(77.892, 29.857)

# Short test window — 31 days = small enough to fetch in <30 s, long enough
# to give a real time series.
START = "2018-06-01"
END = "2018-06-30"


# ── GEE: CHIRPS precipitation ─────────────────────────────────────────────────

class TestGEELive:
    def test_chirps_global_point(self):
        """GEE backend → CHIRPS at a global point → 30-day daily precipitation."""
        from aihydro_data import fetch

        result = fetch(
            variable="precipitation",
            geometry=GLOBAL_POINT,
            start=START,
            end=END,
            mode="manual",
            product="CHIRPS",
            aggregation="basin_mean",
            cache=False,
        )

        assert result.product == "CHIRPS"
        assert result.source == "gee"

        df = result.data
        assert df is not None
        assert len(df) >= 25, f"Expected ~30 daily rows, got {len(df)}"
        assert len(df) <= 35

        # CHIRPS is mm/day — June precip in India monsoon onset is non-zero
        # but bounded. Allow wide range for sanity, not exactness.
        precip_col = next(
            (c for c in df.columns if "precipitation" in c.lower() or "precip" in c.lower()),
            None,
        )
        assert precip_col is not None, f"No precipitation column in {list(df.columns)}"

        vals = df[precip_col].dropna()
        assert (vals >= 0).all(), "Precipitation should be non-negative"
        assert vals.max() < 1000, "Daily precipitation > 1000 mm is implausible"
        assert vals.sum() > 0, "Total June precip should be > 0 mm at this point"


# ── HyRiver: GRIDMET precipitation ────────────────────────────────────────────

class TestHyRiverLive:
    def test_gridmet_precip_conus(self):
        """
        HyRiver backend → GRIDMET_PRECIP at CONUS → 30-day daily precipitation.

        The GridMET THREDDS server (thredds.northwestknowledge.net) has
        intermittent 500/timeout windows. We retry via call_with_retry,
        and if it's *still* down after retries we xfail with a clear note
        rather than failing the whole live suite — the bug is upstream.
        """
        import pytest
        from aihydro_data import fetch
        from aihydro_data.exceptions import SourceUnavailable

        try:
            result = fetch(
                variable="precipitation",
                geometry=CONUS_POINT,
                start=START,
                end=END,
                mode="manual",
                product="GRIDMET_PRECIP",
                fallback=[],   # disable fallback — we're testing GRIDMET specifically
                aggregation="centroid",   # use point-mode to avoid heavier polygon fetch
                cache=False,
            )
        except SourceUnavailable as exc:
            pytest.xfail(
                f"GRIDMET upstream (THREDDS) unavailable: {exc.message}. "
                "This is a flaky upstream, not our code."
            )

        assert result.product == "GRIDMET_PRECIP"
        assert result.source == "hyriver"

        df = result.data
        assert df is not None
        assert len(df) >= 25
        assert len(df) <= 35

        precip_col = next(
            (c for c in df.columns if c.lower() not in ("date", "time", "index")),
            None,
        )
        assert precip_col is not None

        vals = df[precip_col].dropna()
        assert (vals >= 0).all()
        assert vals.max() < 500


# ── direct_api: NWIS streamflow ───────────────────────────────────────────────

class TestNWISLive:
    def test_nwis_daily_streamflow(self):
        """direct_api backend → NWIS daily streamflow for a known gauge."""
        from aihydro_data import fetch

        # White River at Indianapolis, IN — a real, active USGS gauge.
        result = fetch(
            variable="streamflow",
            geometry="03353000",
            start=START,
            end=END,
            mode="manual",
            product="NWIS_STREAMFLOW",
            cache=False,
        )

        assert result.product == "NWIS_STREAMFLOW"
        assert result.source == "direct_api"

        df = result.data
        assert df is not None
        assert len(df) >= 25
        assert len(df) <= 35

        flow_col = next(
            (c for c in df.columns if c.lower() not in ("date", "time", "site_no")),
            None,
        )
        assert flow_col is not None

        vals = df[flow_col].dropna()
        # m³/s — the White River at Indy averages ~30–200 m³/s in summer.
        # Wide range allowed; just sanity-check it's not absurd.
        assert (vals >= 0).all(), "Discharge must be non-negative"
        assert vals.max() < 50000, "Daily mean discharge > 50000 m³/s at this gauge is implausible"
        assert vals.median() > 0, "Median June discharge should be > 0"


# ── Auto-mode routing → GRIDMET in CONUS ──────────────────────────────────────

class TestAutoModeRouting:
    def test_conus_precip_routes_through_policy(self):
        """
        ``mode='auto'`` on a CONUS point should return precipitation data
        from one of the policy candidates. We do NOT pin to GRIDMET — its
        upstream THREDDS server is flaky, and the whole *point* of
        auto-mode + fallback is that a transient upstream outage shouldn't
        break the user's call. We just assert (a) we got data, (b) the
        product we got is in the configured chain.
        """
        from aihydro_data import fetch
        from aihydro_data.routing import resolve_product_ids

        expected_chain = resolve_product_ids("precipitation", "CONUS")
        assert len(expected_chain) >= 2, "policy regressed: <2 candidates"

        result = fetch(
            variable="precipitation",
            geometry=CONUS_POINT,
            start=START,
            end=END,
            mode="auto",
            cache=False,
        )

        assert result.product in expected_chain, (
            f"auto-mode picked {result.product!r} which is not in the "
            f"declared chain {expected_chain}"
        )
        assert len(result.data) >= 25, f"too few rows: {len(result.data)}"


# ── Cross-source sanity: GRIDMET vs CHIRPS at same CONUS point ────────────────

class TestCrossSourceSanity:
    def test_gridmet_vs_chirps_correlated(self):
        """GRIDMET and CHIRPS should correlate (>0.3) on the same CONUS point.

        Skipped (xfail) if either upstream is unavailable — the assertion is
        about scientific consistency between products, not about our pipeline.
        """
        import pytest
        import pandas as pd
        from aihydro_data import fetch
        from aihydro_data.exceptions import SourceUnavailable

        # Use a longer window so noise averages out.
        start, end = "2019-04-01", "2019-09-30"

        try:
            g = fetch("precipitation", CONUS_POINT, start, end,
                      mode="manual", product="GRIDMET_PRECIP",
                      fallback=[], aggregation="centroid", cache=False)
        except SourceUnavailable as exc:
            pytest.xfail(f"GRIDMET upstream unavailable: {exc.message}")
        try:
            c = fetch("precipitation", CONUS_POINT, start, end,
                      mode="manual", product="CHIRPS",
                      fallback=[], cache=False)
        except SourceUnavailable as exc:
            pytest.xfail(f"CHIRPS upstream unavailable: {exc.message}")

        # Align on date — different sources may use different column names
        def _to_series(df):
            df = df.copy()
            # find time column
            tcol = next((c for c in df.columns if c.lower() in ("date", "time")), None)
            if tcol is not None:
                df[tcol] = pd.to_datetime(df[tcol]).dt.normalize()
                df = df.set_index(tcol)
            vcol = next((c for c in df.columns if c.lower() not in ("date", "time", "site_no")), None)
            return df[vcol]

        sg, sc = _to_series(g.data), _to_series(c.data)
        joined = pd.concat([sg, sc], axis=1, join="inner").dropna()
        assert len(joined) >= 100, f"Too few overlapping days: {len(joined)}"

        corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
        # 0.3 is a lenient floor — different products, different gridding,
        # different rain detection; the point is they shouldn't be unrelated.
        assert corr > 0.3, f"GRIDMET vs CHIRPS correlation too low: {corr:.3f}"


# ── Cache round-trip on a real fetch ──────────────────────────────────────────

class TestCacheRoundTripLive:
    def test_fetch_then_cache_hit(self, tmp_path, monkeypatch):
        """Real fetch → cache write → re-fetch hits cache → identical data."""
        import pandas as pd
        from unittest.mock import patch
        from aihydro_data import fetch

        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            r1 = fetch("streamflow", "03353000", START, END,
                       mode="manual", product="NWIS_STREAMFLOW", cache=True)
            assert r1.cache_hit is False

            r2 = fetch("streamflow", "03353000", START, END,
                       mode="manual", product="NWIS_STREAMFLOW", cache=True)
            assert r2.cache_hit is True, "Second fetch should be a cache hit"

            # Same shape, same values
            assert len(r1.data) == len(r2.data)
            for col in r1.data.columns:
                if pd.api.types.is_numeric_dtype(r1.data[col]):
                    assert (r1.data[col].fillna(-999) == r2.data[col].fillna(-999)).all(), (
                        f"Cache round-trip mismatch in column {col}"
                    )
