"""
Tier-2 live sweep — exercise every registered time-series product.

Strategy:
  - Parametrize across all time-series products in the registry.
  - Pick a geometry that's inside each product's coverage region.
  - Use a SHORT window (5–30 days) that's safely inside each product's
    temporal range (some products start late, e.g. SMAP starts 2015).
  - On success, sanity-check the returned DataFrame: non-empty, numeric
    column, no nonsensical values.
  - On transient upstream outage → xfail with the upstream error message
    (so we can tell "our bug" from "their bug" at a glance).
  - On a real fault (wrong asset ID, wrong band, unit mismatch, ...) →
    hard fail with a useful message.

Run:
    pytest tests/test_live_sweep.py -m live -v --tb=line

Skip:
    pytest                # default `-m "not live"` filters us out
"""
from __future__ import annotations

import pytest
from shapely.geometry import Point


pytestmark = pytest.mark.live


# A reliable CONUS point (Indianapolis) for CONUS-only products
CONUS_POINT = Point(-86.158, 39.7684)
# A reliable global point (Roorkee, India) for globally-covered products
GLOBAL_POINT = Point(77.892, 29.857)
# A vegetated CONUS point (Iowa cropland) for products that mask out
# urban/barren pixels — MOD16 ET/PET and MCD15A3H LAI are computed only
# over vegetated land surfaces, so Indianapolis (urban) returns all-null
# from those products even though the upstream is healthy.
VEGETATED_POINT = Point(-93.5, 41.9)

# Per-product geometry overrides for products with known coverage gaps.
_GEOM_OVERRIDES: dict[str, Point] = {
    "MOD16_ET":   VEGETATED_POINT,
    "MOD16_PET":  VEGETATED_POINT,
    "MODIS_LAI":  VEGETATED_POINT,
    "MODIS_NDVI": VEGETATED_POINT,  # MOD13Q1 also fares better over crops
}


def _pick_geom(spec) -> Point:
    """Choose a sensible test geometry based on a product's coverage."""
    if spec.id in _GEOM_OVERRIDES:
        return _GEOM_OVERRIDES[spec.id]
    cov = set(spec.coverage)
    if "CONUS" in cov:
        return CONUS_POINT
    if "NORTH_AMERICA" in cov:
        return CONUS_POINT
    return GLOBAL_POINT


def _pick_window(spec) -> tuple[str, str]:
    """Pick a (start, end) window that's safely inside the product's temporal range."""
    # Default: a 30-day stable window in mid-2018, monsoon for India, summer for CONUS.
    default_start, default_end = "2018-06-01", "2018-06-30"

    # If product starts after our default, push start forward
    if spec.temporal_start and spec.temporal_start > default_start:
        # Take a 30-day window starting from product start
        # (parse year-month-day to bump by 30 days)
        ys, ms, ds = spec.temporal_start.split("-")
        ys = int(ys)
        # For products starting late (e.g. SMAP 2015), pick mid-year after start
        new_year = max(ys, 2018)
        return f"{new_year}-06-01", f"{new_year}-06-30"

    return default_start, default_end


# ── Build the parameter list ──────────────────────────────────────────────────

def _all_timeseries_products():
    """Return every product in the registry whose timestep isn't 'static'."""
    from aihydro_data.products import list_products
    out = []
    for spec in list_products():
        if spec.timestep == "static":
            continue
        out.append(spec)
    return out


# Generate ids for pytest display
_PARAMS = _all_timeseries_products()
_IDS = [p.id for p in _PARAMS]


# ── Sweep test ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec", _PARAMS, ids=_IDS)
def test_product_fetch_live(spec):
    """One live fetch per time-series product. Skips upstream outages."""
    from aihydro_data import fetch
    from aihydro_data.exceptions import (
        AihydroDataError, AuthRequired, SourceUnavailable,
    )

    # If the backend isn't installed/authed in this env, skip rather than fail.
    from aihydro_data.sources.base import get_backend
    try:
        backend = get_backend(spec.source)
        ok, reason = backend.is_available()
        if not ok:
            pytest.skip(f"{spec.source} backend unavailable: {reason}")
    except Exception as exc:
        pytest.skip(f"Could not load {spec.source} backend: {exc}")

    geom = _pick_geom(spec)
    start, end = _pick_window(spec)

    # Monthly products need a longer window to return >1 row
    if spec.timestep == "monthly":
        # Pick a 6-month window in the product's range
        ys = max(int(start.split("-")[0]), 2018)
        start, end = f"{ys}-01-01", f"{ys}-06-30"

    try:
        result = fetch(
            variable=spec.variable,
            geometry=geom,
            start=start,
            end=end,
            mode="manual",
            product=spec.id,
            fallback=[],   # critical — we're testing THIS product, not its fallbacks
            aggregation="centroid" if spec.source == "hyriver" else "basin_mean",
            cache=False,
        )
    except AuthRequired as exc:
        pytest.xfail(f"{spec.id} auth missing: {exc.code} — {exc.message}")
    except SourceUnavailable as exc:
        # Need to distinguish "upstream outage" (xfail OK) from "config bug"
        # (asset not found, band not found — must HARD FAIL so we notice).
        msg = (exc.message or "").lower()
        config_bug_markers = (
            "not found", "does not exist", "did not match any bands",
            "all-null", "0 rows", "asset", "imagecollection.load",
        )
        upstream_markers = (
            "timeout", "timed out", "500", "502", "503", "504",
            "connection", "service returned", "temporarily",
        )
        looks_like_config_bug = any(m in msg for m in config_bug_markers)
        looks_like_upstream = any(m in msg for m in upstream_markers)
        if looks_like_config_bug and not looks_like_upstream:
            pytest.fail(
                f"{spec.id}: CONFIG BUG (likely wrong asset/band): {exc.code} — {exc.message}"
            )
        # Otherwise treat as transient upstream blip
        pytest.xfail(f"{spec.id} upstream unavailable: {exc.code} — {exc.message}")
    except AihydroDataError as exc:
        # Other structured errors → real bug to surface
        pytest.fail(f"{spec.id}: {exc.code} — {exc.message}")
    except Exception as exc:
        pytest.fail(f"{spec.id}: unexpected {type(exc).__name__}: {exc}")

    # ── shape checks ──
    assert result.product == spec.id, (
        f"{spec.id}: product mismatch (got {result.product!r})"
    )
    assert result.source == spec.source

    df = result.data
    assert df is not None, f"{spec.id}: returned None for data"
    assert hasattr(df, "columns"), f"{spec.id}: not a DataFrame ({type(df).__name__})"
    assert len(df) > 0, f"{spec.id}: 0 rows — likely a silent-empty backend bug"

    # ── value checks ──
    # Find the numeric data column (anything other than date / time / site_no)
    skip_cols = {"date", "time", "datetime", "index", "site_no", "siteCode"}
    val_col = next(
        (c for c in df.columns if c.lower() not in skip_cols),
        None,
    )
    assert val_col is not None, f"{spec.id}: no value column in {list(df.columns)}"

    vals = df[val_col].dropna()
    assert len(vals) > 0, f"{spec.id}: all-null values — likely backend masking bug"

    # Sanity ranges per variable
    if spec.variable == "precipitation":
        assert (vals >= -0.1).all(), f"{spec.id}: negative precipitation: min={vals.min()}"
        assert vals.max() < 2000, f"{spec.id}: precipitation > 2000 implausible: max={vals.max()}"
    elif spec.variable in ("tmax", "tmin", "tmean"):
        # Either K (200–340) or degC (-80 to +60). Accept both.
        is_k = vals.median() > 200
        if is_k:
            assert 200 < vals.median() < 340, (
                f"{spec.id}: temperature out of range (K assumed): median={vals.median()}"
            )
            assert spec.units in ("K", "kelvin"), (
                f"{spec.id}: values look like Kelvin but units={spec.units!r}"
            )
        else:
            assert -80 < vals.median() < 60, (
                f"{spec.id}: temperature out of range (degC assumed): median={vals.median()}"
            )
    elif spec.variable in ("et", "pet"):
        assert (vals >= -0.5).all(), f"{spec.id}: negative ET: min={vals.min()}"
        assert vals.max() < 500, f"{spec.id}: ET > 500 mm implausible: max={vals.max()}"
    elif spec.variable == "soil_moisture":
        # m³/m³ or %
        assert (vals >= 0).all(), f"{spec.id}: negative soil moisture: min={vals.min()}"
        assert vals.max() <= 100, f"{spec.id}: soil moisture > 100 implausible: max={vals.max()}"
    elif spec.variable in ("ndvi", "evi"):
        # NDVI ∈ [-1, 1] (or [-10000, 10000] if unit_conversion missed)
        assert vals.min() >= -1.1, f"{spec.id}: NDVI < -1: min={vals.min()}"
        assert vals.max() <= 1.1, (
            f"{spec.id}: NDVI > 1: max={vals.max()} — likely unit_conversion missing"
        )
    elif spec.variable == "lai":
        # m²/m² ∈ [0, 10]
        assert vals.min() >= 0, f"{spec.id}: negative LAI: min={vals.min()}"
        assert vals.max() < 20, (
            f"{spec.id}: LAI > 20 implausible: max={vals.max()} — unit_conversion bug?"
        )
    elif spec.variable == "streamflow":
        assert (vals >= 0).all(), f"{spec.id}: negative discharge: min={vals.min()}"
