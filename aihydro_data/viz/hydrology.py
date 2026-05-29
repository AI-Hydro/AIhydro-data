"""
Research-grade hydrology plots.

These are the plots hydrologists actually publish. Every function accepts
either a FetchResult or a raw pd.DataFrame / pd.Series so they're useful
both inside the aihydro-data flow and standalone.

Functions:
  - flow_duration_curve(series)         FDC with exceedance probability
  - climatology(series)                 Mean ± IQR by month (annual cycle)
  - double_mass(a, b)                   Cumulative A vs cumulative B (consistency check)
  - budyko(precip, pet, et)             Budyko diagram (P/PET vs ET/P)
  - aridity_index_plot(precip, pet)     Aridity index (P/PET) bar/line
"""
from __future__ import annotations

from typing import Any

from aihydro_data.viz._common import (
    _detect_value_column,
    _ensure_datetime_index,
    _new_fig,
    _resolve_color,
    _result_data,
    _result_meta,
)


def _to_series(obj: Any) -> Any:
    """Coerce FetchResult | DataFrame | Series → pd.Series with DatetimeIndex."""
    import pandas as pd
    data = _result_data(obj)
    if isinstance(data, pd.Series):
        return data
    if isinstance(data, pd.DataFrame):
        df = _ensure_datetime_index(data)
        col = _detect_value_column(df) if "date" not in df.columns else _detect_value_column(df.reset_index())
        return df[col]
    raise TypeError(f"Cannot coerce {type(data).__name__} to pd.Series.")


# ────────────────────────────────────────────────────────────────────────────
# Flow Duration Curve
# ────────────────────────────────────────────────────────────────────────────

def flow_duration_curve(
    series: Any,
    *,
    ax: Any = None,
    label: str | None = None,
    color: str | None = None,
    logy: bool = True,
    title: str = "Flow Duration Curve",
) -> Any:
    """
    Plot the flow duration curve (FDC) of a series.

    The FDC shows the % of time a given flow value is exceeded — the standard
    way to compare streamflow regimes between catchments or models.

    Parameters
    ----------
    series : FetchResult | pd.DataFrame | pd.Series
        Streamflow (or any flux) time series.
    logy : bool
        Use log y-scale (recommended for streamflow).
    """
    import numpy as np

    s = _to_series(series).dropna()
    if s.empty:
        raise ValueError("Empty series — nothing to plot.")

    fig, ax = _new_fig(ax=ax)

    sorted_vals = np.sort(s.values)[::-1]
    exceedance = 100.0 * (np.arange(1, len(sorted_vals) + 1)) / (len(sorted_vals) + 1)

    meta = _result_meta(series)
    label = label or meta.get("product") or "FDC"
    color = color or _resolve_color(meta.get("product"))

    ax.plot(exceedance, sorted_vals, label=label, color=color, lw=1.5)
    ax.set_xlabel("Exceedance probability (%)")
    ax.set_ylabel(f"Flow ({meta.get('units', '')})" if meta.get("units") else "Flow")
    if logy:
        ax.set_yscale("log")
    ax.set_title(title)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="best", frameon=False)
    return ax


# ────────────────────────────────────────────────────────────────────────────
# Climatology / annual cycle
# ────────────────────────────────────────────────────────────────────────────

def climatology(
    series: Any,
    *,
    ax: Any = None,
    label: str | None = None,
    color: str | None = None,
    show_iqr: bool = True,
    title: str = "Climatology",
) -> Any:
    """
    Monthly climatology: mean ± inter-quartile range.

    Useful for QA-ing fetched data ("does this look like Iowa precip?") and
    for poster/paper figures of seasonal cycles.
    """
    import pandas as pd

    s = _to_series(series).dropna()
    s.index = pd.to_datetime(s.index)

    fig, ax = _new_fig(ax=ax)

    monthly = s.groupby(s.index.month)
    mean = monthly.mean()
    q25 = monthly.quantile(0.25)
    q75 = monthly.quantile(0.75)

    meta = _result_meta(series)
    label = label or meta.get("product") or "monthly mean"
    color = color or _resolve_color(meta.get("product"))

    months = list(range(1, 13))
    mean_v = [mean.get(m, float("nan")) for m in months]
    q25_v = [q25.get(m, float("nan")) for m in months]
    q75_v = [q75.get(m, float("nan")) for m in months]

    ax.plot(months, mean_v, "-o", color=color, label=label, lw=2)
    if show_iqr:
        ax.fill_between(months, q25_v, q75_v, color=color, alpha=0.25, label="IQR")

    ax.set_xticks(months)
    ax.set_xticklabels(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    ax.set_xlabel("Month")
    ax.set_ylabel(f"{meta.get('variable', 'value')} ({meta.get('units', '')})")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", frameon=False)
    return ax


# ────────────────────────────────────────────────────────────────────────────
# Double-mass curve
# ────────────────────────────────────────────────────────────────────────────

def double_mass(
    a: Any,
    b: Any,
    *,
    ax: Any = None,
    label_a: str | None = None,
    label_b: str | None = None,
    title: str = "Double-Mass Curve",
) -> Any:
    """
    Cumulative A vs cumulative B — the gold-standard "are these consistent?"
    plot. A perfectly consistent pair lies on a straight line; departures from
    linearity indicate bias change-points.
    """
    import pandas as pd

    sa = _to_series(a).dropna()
    sb = _to_series(b).dropna()

    # Align on common timestamps
    common = sa.index.intersection(sb.index)
    if len(common) == 0:
        raise ValueError("Series A and B share no common timestamps.")
    sa = sa.loc[common]
    sb = sb.loc[common]

    cum_a = sa.cumsum()
    cum_b = sb.cumsum()

    fig, ax = _new_fig(ax=ax, figsize=(6, 6))

    meta_a = _result_meta(a)
    meta_b = _result_meta(b)
    label_a = label_a or meta_a.get("product", "A")
    label_b = label_b or meta_b.get("product", "B")

    ax.plot(cum_a, cum_b, "-o", ms=2, color=_resolve_color(meta_a.get("product")))

    # 1:1 reference line
    lo = min(cum_a.min(), cum_b.min())
    hi = max(cum_a.max(), cum_b.max())
    ax.plot([lo, hi], [lo, hi], "--", color="gray", lw=1, label="1:1")

    ax.set_xlabel(f"Cumulative {label_a}")
    ax.set_ylabel(f"Cumulative {label_b}")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", frameon=False)
    ax.set_aspect("equal", "box")
    return ax


# ────────────────────────────────────────────────────────────────────────────
# Budyko diagram
# ────────────────────────────────────────────────────────────────────────────

def budyko(
    precip: Any,
    pet: Any,
    et: Any,
    *,
    ax: Any = None,
    label: str | None = None,
    title: str = "Budyko Diagram",
) -> Any:
    """
    Budyko diagram: PET/P (aridity index) on x-axis vs ET/P (evaporative
    fraction) on y-axis. A single catchment is one point. Energy and water
    limits are shown as reference curves.

    Inputs may be FetchResults, DataFrames, or scalars. Long time series
    are reduced to their long-term means before plotting.
    """
    import numpy as np

    def _mean(obj):
        if isinstance(obj, (int, float)):
            return float(obj)
        return float(_to_series(obj).mean())

    import warnings

    p_mean = _mean(precip)
    pet_mean = _mean(pet)
    et_mean = _mean(et)

    if p_mean <= 0:
        raise ValueError("Mean precipitation must be positive.")

    aridity = pet_mean / p_mean        # x: PET/P
    evap_frac = et_mean / p_mean        # y: ET/P

    # Physical sanity guards — warn rather than silently produce wrong science.
    if evap_frac > 1.05:
        warnings.warn(
            f"Budyko: ET/P = {evap_frac:.2f} > 1 violates water balance. "
            "This usually means the P, PET, and ET series have different units "
            "(e.g. mm/day vs mm/8-day) or come from fallback products with "
            "incompatible scales. Verify that all three FetchResults share the "
            "same units before interpreting this diagram.",
            stacklevel=2,
        )
    if aridity > 8:
        warnings.warn(
            f"Budyko: PET/P = {aridity:.2f} is unusually high (hyper-arid or "
            "unit mismatch). Expected range is roughly 0.3–5 for most climates.",
            stacklevel=2,
        )

    fig, ax = _new_fig(ax=ax, figsize=(6, 6))

    # Reference limit lines
    x = np.linspace(0.01, 5, 200)
    energy_limit = np.minimum(x, np.ones_like(x))  # ET/P ≤ PET/P (water-limited)
    water_limit = np.ones_like(x)                   # ET/P ≤ 1 (energy-limited)
    ax.plot(x, energy_limit, "k--", lw=1, alpha=0.6, label="Energy limit (ET=PET)")
    ax.plot(x, water_limit, "k:", lw=1, alpha=0.6, label="Water limit (ET=P)")

    # Budyko curve (Choudhury n=2 form)
    n = 2.6
    budyko_curve = (1 + (1 / x) ** n) ** (-1 / n) * x / x  # ET/P = [1+(P/PET)^n]^(-1/n)
    # Recompute properly:
    budyko_curve = (1 + (x ** -n)) ** (-1 / n)
    ax.plot(x, budyko_curve, "-", color="gray", lw=1.5, alpha=0.8,
            label=f"Budyko (n={n})")

    # The catchment point
    ax.plot(aridity, evap_frac, "o", ms=10, color="#e31a1c",
            label=label or "catchment")
    ax.annotate(
        f"({aridity:.2f}, {evap_frac:.2f})",
        (aridity, evap_frac), xytext=(10, 5), textcoords="offset points",
        fontsize=9,
    )

    ax.set_xlabel("Aridity index (PET / P)")
    ax.set_ylabel("Evaporative fraction (ET / P)")
    ax.set_xlim(0, max(2.5, aridity * 1.3))
    ax.set_ylim(0, max(1.2, evap_frac * 1.3))
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    return ax


# ────────────────────────────────────────────────────────────────────────────
# Aridity index
# ────────────────────────────────────────────────────────────────────────────

def aridity_index_plot(
    precip: Any,
    pet: Any,
    *,
    ax: Any = None,
    title: str = "Aridity Index Time Series",
) -> Any:
    """Monthly P/PET ratio. >1 = humid, <1 = arid."""
    import pandas as pd

    p = _to_series(precip).resample("ME").sum()
    e = _to_series(pet).resample("ME").sum()
    common = p.index.intersection(e.index)
    if len(common) == 0:
        raise ValueError("Precipitation and PET series share no common dates.")
    ai = (p.loc[common] / e.loc[common]).replace([float("inf"), -float("inf")], float("nan"))

    fig, ax = _new_fig(ax=ax)
    ax.plot(ai.index, ai.values, "-o", ms=3, color="#2c7fb8")
    ax.axhline(1.0, color="red", ls="--", lw=1, label="P = PET")
    ax.set_ylabel("P / PET")
    ax.set_xlabel("date")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", frameon=False)
    return ax
