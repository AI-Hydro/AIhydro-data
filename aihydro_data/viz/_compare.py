"""
Multi-source comparison API.

`compare()` fetches multiple products for the same geometry/window and renders
them on a multi-panel figure. The whole point of aihydro-data's multi-source
design is to make this trivial.

Example:
    fig = compare(
        ["GRIDMET_PRECIP", "CHIRPS", "ERA5L_PRECIP"],
        geometry=gdf, start="2015-01-01", end="2020-12-31",
        plots=["timeseries", "climatology", "scatter"],
    )
"""
from __future__ import annotations

from typing import Any

from aihydro_data.viz._common import (
    _detect_value_column,
    _ensure_datetime_index,
    _resolve_color,
)


_PLOT_KINDS = ("timeseries", "climatology", "scatter", "fdc", "double_mass")


def compare(
    products: list[str],
    geometry: Any,
    start: str,
    end: str,
    *,
    variable: str | None = None,
    plots: list[str] | None = None,
    aggregation: str = "basin_mean",
    figsize: tuple[float, float] | None = None,
    **fetch_kwargs: Any,
) -> Any:
    """
    Fetch each product and render side-by-side panels.

    Parameters
    ----------
    products : list[str]
        Product IDs to compare (e.g. ['CHIRPS', 'GRIDMET_PRECIP']).
    geometry, start, end
        Standard fetch() args.
    variable : str, optional
        Auto-inferred from products[0]'s ProductSpec if omitted.
    plots : list[str], optional
        Subset of {'timeseries', 'climatology', 'scatter', 'fdc', 'double_mass'}.
        Defaults to ['timeseries', 'climatology'].

    Returns
    -------
    matplotlib.figure.Figure
        Multi-panel figure. Each panel's matplotlib Axes is also accessible
        via fig.axes.
    """
    import matplotlib.pyplot as plt
    import pandas as pd

    # NOTE: import aihydro_data as a module (not `from aihydro_data import fetch`)
    # so monkeypatching `aihydro_data.fetch` works in tests. The `from x import y`
    # form triggers a submodule re-import that shadows the patch.
    import aihydro_data
    from aihydro_data.products import get_product
    from aihydro_data.viz.hydrology import (
        climatology, double_mass, flow_duration_curve,
    )

    plots = plots or ["timeseries", "climatology"]
    bad = [p for p in plots if p not in _PLOT_KINDS]
    if bad:
        raise ValueError(f"Unknown plot kinds: {bad}. Valid: {_PLOT_KINDS}")

    # Infer variable from first product if not supplied
    if variable is None:
        variable = get_product(products[0]).variable

    # Fetch each product
    results = {}
    failures = {}
    for pid in products:
        try:
            res = aihydro_data.fetch(
                variable, geometry, start, end,
                mode="manual", product=pid,
                aggregation=aggregation,
                **fetch_kwargs,
            )
            results[pid] = res
        except Exception as exc:
            failures[pid] = str(exc)

    if not results:
        raise RuntimeError(
            f"All product fetches failed:\n" +
            "\n".join(f"  {p}: {e}" for p, e in failures.items())
        )

    # Build figure
    n = len(plots)
    cols = min(n, 2)
    rows = (n + cols - 1) // cols
    if figsize is None:
        figsize = (7 * cols, 4.5 * rows)
    fig, axes = plt.subplots(rows, cols, figsize=figsize)
    axes = axes.flatten() if hasattr(axes, "flatten") else [axes]

    for ax, kind in zip(axes, plots):
        if kind == "timeseries":
            _panel_timeseries(ax, results, variable)
        elif kind == "climatology":
            for pid, res in results.items():
                climatology(res, ax=ax, label=pid,
                            color=_resolve_color(pid), title="Climatology")
        elif kind == "scatter":
            _panel_scatter(ax, results)
        elif kind == "fdc":
            for pid, res in results.items():
                flow_duration_curve(res, ax=ax, label=pid,
                                    color=_resolve_color(pid), title="FDC")
        elif kind == "double_mass":
            keys = list(results.keys())
            if len(keys) >= 2:
                double_mass(results[keys[0]], results[keys[1]],
                            ax=ax, label_a=keys[0], label_b=keys[1])

    # Hide unused axes
    for ax in axes[n:]:
        ax.axis("off")

    title = f"Product comparison — {variable} | {start} to {end}"
    if failures:
        title += f"  (skipped: {', '.join(failures.keys())})"
    fig.suptitle(title, fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def _to_indexed_series(res, variable):
    """Helper: pull (date-indexed) Series for the given variable from a result."""
    import pandas as pd
    df = res.data if hasattr(res, "data") else res
    if not isinstance(df, pd.DataFrame):
        return None
    df = _ensure_datetime_index(df)
    if variable in df.columns:
        return df[variable]
    col = _detect_value_column(df.reset_index())
    return df[col]


def _panel_timeseries(ax, results: dict, variable: str) -> None:
    """Overlay multiple sources as a single time series panel."""
    for pid, res in results.items():
        s = _to_indexed_series(res, variable)
        if s is None or s.empty:
            continue
        ax.plot(s.index, s.values, label=pid, lw=1.0,
                color=_resolve_color(pid), alpha=0.85)
    ax.set_title(f"{variable} — time series")
    ax.set_xlabel("date")
    ax.set_ylabel(variable)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", frameon=False, fontsize=9)


def _panel_scatter(ax, results: dict) -> None:
    """Pairwise scatter of the first two products (sorted by ID for stability)."""
    keys = list(results.keys())
    if len(keys) < 2:
        ax.text(0.5, 0.5, "scatter needs ≥2 products", ha="center", va="center",
                transform=ax.transAxes, color="gray")
        return

    a_id, b_id = keys[0], keys[1]
    sa = _to_indexed_series(results[a_id], None)
    sb = _to_indexed_series(results[b_id], None)
    if sa is None or sb is None:
        return

    common = sa.index.intersection(sb.index)
    sa = sa.loc[common]
    sb = sb.loc[common]

    ax.scatter(sa.values, sb.values, s=6, alpha=0.5,
               color=_resolve_color(a_id))
    lo = min(sa.min(), sb.min())
    hi = max(sa.max(), sb.max())
    ax.plot([lo, hi], [lo, hi], "--", color="gray", lw=1, label="1:1")
    ax.set_xlabel(a_id)
    ax.set_ylabel(b_id)
    ax.set_title("Scatter (matched dates)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", frameon=False)
    ax.set_aspect("equal", "box")
