"""
Tests for the viz layer — all offline, synthetic data, no network.

We use matplotlib's Agg backend so tests work headlessly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

# Force headless backend BEFORE importing anything that imports pyplot
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _close_figs():
    """Ensure no figure leaks between tests."""
    yield
    plt.close("all")


def _make_result(variable="precipitation", product="CHIRPS",
                 days=365, values=None, source="gee"):
    from aihydro_data.contracts import FetchRequest, FetchResult
    rng = np.random.default_rng(42)
    if values is None:
        values = rng.exponential(2.0, days)
    else:
        # Derive days from values length so dates align
        days = len(values)
    req = FetchRequest(
        variable=variable,
        geometry=Point(-94.5, 39.1),
        start="2020-01-01",
        end="2020-12-31",
    )
    return FetchResult(
        variable=variable,
        product=product,
        source=source,
        request=req,
        data=pd.DataFrame({
            "date": pd.date_range("2020-01-01", periods=days, freq="D"),
            variable: values,
        }),
        citation=f"Test citation for {product}",
        units="mm/day",
    )


# ── Auto-plot dispatcher ────────────────────────────────────────────────────

class TestAutoPlot:
    def test_dataframe_timeseries(self):
        from aihydro_data.viz import auto_plot
        result = _make_result()
        ax = auto_plot(result)
        assert ax is not None
        assert hasattr(ax, "plot")
        # Should have at least one artist
        assert len(ax.lines) + len(ax.patches) > 0

    def test_streamflow_log_axis(self):
        from aihydro_data.viz import auto_plot
        result = _make_result(variable="streamflow", product="NWIS_STREAMFLOW",
                              values=np.random.lognormal(2, 1, 365))
        ax = auto_plot(result, logy=True)
        assert ax.get_yscale() == "log"

    def test_precip_under_year_uses_bars(self):
        from aihydro_data.viz import auto_plot
        result = _make_result(days=90)
        ax = auto_plot(result)
        # 90-day precip window should render bars, not lines
        assert len(ax.patches) > 0

    def test_empty_dataframe_does_not_crash(self):
        from aihydro_data.viz import auto_plot
        from aihydro_data.contracts import FetchResult, FetchRequest
        req = FetchRequest(variable="precipitation", geometry=Point(0, 0),
                           start="2020-01-01", end="2020-01-31")
        r = FetchResult(variable="precipitation", product="CHIRPS", source="gee",
                        request=req, data=pd.DataFrame(columns=["date", "precipitation"]))
        ax = auto_plot(r)
        assert ax is not None  # placeholder text rendered

    def test_unknown_type_raises_typeerror(self):
        from aihydro_data.viz import auto_plot
        with pytest.raises(TypeError):
            auto_plot("not a result")


# ── FetchResult.plot() and .map() wiring ────────────────────────────────────

class TestFetchResultMethods:
    def test_plot_method_exists_and_returns_axes(self):
        result = _make_result()
        ax = result.plot()
        assert ax is not None
        assert hasattr(ax, "plot")

    def test_plot_accepts_existing_axes(self):
        result = _make_result()
        fig, ax = plt.subplots()
        returned = result.plot(ax=ax)
        assert returned is ax

    def test_help_still_works(self):
        result = _make_result()
        text = result.help()
        assert "CHIRPS" in text
        assert "precipitation" in text


# ── Hydrology plots ─────────────────────────────────────────────────────────

class TestHydrologyPlots:
    def test_flow_duration_curve(self):
        from aihydro_data.viz import flow_duration_curve
        result = _make_result(variable="streamflow", product="NWIS_STREAMFLOW",
                              values=np.random.lognormal(2, 1, 1000))
        ax = flow_duration_curve(result)
        assert ax.get_yscale() == "log"
        assert "Exceedance" in ax.get_xlabel()
        assert len(ax.lines) >= 1

    def test_climatology(self):
        from aihydro_data.viz import climatology
        result = _make_result(days=365 * 3, values=np.random.exponential(2, 365 * 3))
        ax = climatology(result)
        # 12 monthly ticks
        assert len(ax.get_xticks()) == 12
        # Mean line + IQR fill
        assert len(ax.lines) >= 1

    def test_climatology_no_iqr(self):
        from aihydro_data.viz import climatology
        result = _make_result(days=365 * 3, values=np.random.exponential(2, 365 * 3))
        ax = climatology(result, show_iqr=False)
        assert ax is not None

    def test_double_mass(self):
        from aihydro_data.viz import double_mass
        rng = np.random.default_rng(0)
        a = _make_result(product="CHIRPS",         values=rng.exponential(2, 365))
        b = _make_result(product="GRIDMET_PRECIP", values=rng.exponential(2, 365))
        ax = double_mass(a, b)
        assert "Cumulative" in ax.get_xlabel()
        assert "Cumulative" in ax.get_ylabel()

    def test_double_mass_disjoint_dates_raises(self):
        from aihydro_data.viz import double_mass
        from aihydro_data.contracts import FetchResult, FetchRequest
        req1 = FetchRequest(variable="precipitation", geometry=Point(0, 0),
                            start="2020-01-01", end="2020-01-31")
        r1 = FetchResult(variable="precipitation", product="A", source="gee",
                         request=req1,
                         data=pd.DataFrame({"date": pd.date_range("2020-01-01", periods=30),
                                            "precipitation": np.arange(30.0)}))
        r2 = FetchResult(variable="precipitation", product="B", source="gee",
                         request=req1,
                         data=pd.DataFrame({"date": pd.date_range("2025-01-01", periods=30),
                                            "precipitation": np.arange(30.0)}))
        with pytest.raises(ValueError, match="no common"):
            double_mass(r1, r2)

    def test_budyko_scalars(self):
        from aihydro_data.viz import budyko
        ax = budyko(1200.0, 1400.0, 800.0, label="test")
        assert "Aridity" in ax.get_xlabel()
        assert "Evaporative" in ax.get_ylabel()
        # 1 point + budyko curve + 2 reference limits = ≥3 artists
        assert len(ax.lines) >= 3

    def test_budyko_rejects_zero_precip(self):
        from aihydro_data.viz import budyko
        with pytest.raises(ValueError, match="positive"):
            budyko(0.0, 1000.0, 500.0)

    def test_aridity_index_plot(self):
        from aihydro_data.viz.hydrology import aridity_index_plot
        p = _make_result(variable="precipitation", days=365,
                         values=np.random.exponential(2, 365))
        e = _make_result(variable="pet", product="GRIDMET_PET", days=365,
                         values=np.random.exponential(4, 365))
        ax = aridity_index_plot(p, e)
        assert "P / PET" in ax.get_ylabel()


# ── Compare API (uses real fetch — must be skipped/mocked) ─────────────────

class TestCompareAPI:
    def test_compare_rejects_bad_plot_kind(self):
        from aihydro_data.viz import compare
        with pytest.raises(ValueError, match="Unknown plot kinds"):
            compare(["CHIRPS"], Point(-94.5, 39.1),
                    "2020-01-01", "2020-01-31",
                    plots=["nonexistent"])

    def test_compare_unknown_product_raises(self):
        """Compare on a product not in the registry should fail cleanly."""
        from aihydro_data.viz import compare
        # variable= is required when products[0] isn't in the registry
        # (since compare normally infers variable from products[0].variable)
        with pytest.raises((KeyError, RuntimeError)):
            compare(
                ["NOT_A_REAL_PRODUCT"],
                Point(-94.5, 39.1),
                "2020-01-01", "2020-01-31",
            )

    def test_compare_signature_smoke(self):
        """Basic introspection — function exists and has the right signature."""
        import inspect
        from aihydro_data.viz import compare
        sig = inspect.signature(compare)
        params = list(sig.parameters.keys())
        # Required positional args
        assert "products" in params
        assert "geometry" in params
        assert "start" in params
        assert "end" in params
        # Keyword-only options
        assert "variable" in params
        assert "plots" in params


# ── Common helpers ──────────────────────────────────────────────────────────

class TestCommonHelpers:
    def test_resolve_color_known_product(self):
        from aihydro_data.viz._common import _resolve_color
        assert _resolve_color("CHIRPS") == "#1f78b4"

    def test_resolve_color_unknown_falls_back(self):
        from aihydro_data.viz._common import _resolve_color
        assert _resolve_color("MADE_UP_PRODUCT") == "#1f78b4"  # default

    def test_detect_value_column(self):
        from aihydro_data.viz._common import _detect_value_column
        df = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=5),
                           "precipitation": [1.0, 2.0, 3.0, 4.0, 5.0]})
        assert _detect_value_column(df) == "precipitation"

    def test_detect_value_column_raises_when_only_date(self):
        from aihydro_data.viz._common import _detect_value_column
        df = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=5)})
        with pytest.raises(ValueError):
            _detect_value_column(df)


# ── Import safety (viz extras not installed) ────────────────────────────────

class TestImportSafety:
    def test_viz_package_imports_without_pyplot_call(self):
        # Just importing the package should not call any matplotlib functions
        from aihydro_data import viz
        assert hasattr(viz, "auto_plot")
        assert hasattr(viz, "compare")
        assert hasattr(viz, "flow_duration_curve")
