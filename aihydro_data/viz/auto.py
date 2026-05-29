"""
Auto-plot dispatcher.

`auto_plot(result)` inspects the FetchResult's data shape + variable and picks
the best-fit plot. This is what `FetchResult.plot()` calls.

Dispatch rules:
  - DataFrame with ['date', 'streamflow']      → hydrograph (line, log-y option)
  - DataFrame with ['date', <numeric>]         → time series line plot
  - DataFrame with multiple value columns      → multi-line plot
  - xarray.DataArray (raster)                  → imshow with colorbar
  - xarray.Dataset                             → first variable imshow
  - GeoDataFrame                               → GeoPandas plot
"""
from __future__ import annotations

from typing import Any

from aihydro_data.viz._common import (
    DEFAULT_FIGSIZE,
    _detect_value_column,
    _ensure_datetime_index,
    _new_fig,
    _resolve_color,
    _result_data,
    _result_meta,
)


def auto_plot(result: Any, *, ax: Any = None, **kwargs: Any) -> Any:
    """
    Plot a FetchResult (or raw data) auto-detecting the right plot type.

    Parameters
    ----------
    result : FetchResult | pd.DataFrame | xr.DataArray | xr.Dataset
        Result from ``fetch()``, or raw data.
    ax : matplotlib.axes.Axes, optional
        Existing axes to draw onto. If None, a new figure is created.
    **kwargs
        Forwarded to the selected backend plotter.

    Returns
    -------
    matplotlib.axes.Axes
    """
    data = _result_data(result)
    meta = _result_meta(result)

    # xarray DataArray → raster imshow
    try:
        import xarray as xr
        if isinstance(data, xr.DataArray):
            return _plot_raster(data, meta, ax=ax, **kwargs)
        if isinstance(data, xr.Dataset):
            # Pick the first N-dim variable, skipping 0-dim scalars such as
            # rioxarray's `spatial_ref` CRS token that lands in data_vars on
            # round-trip through netCDF (write DataArray → read as Dataset).
            first_var = next(
                (v for v in data.data_vars if data[v].ndim > 0),
                next(iter(data.data_vars), None),   # fallback: any var
            )
            if first_var is None:
                raise TypeError("xarray Dataset has no plottable variables.")
            return _plot_raster(data[first_var], meta, ax=ax, **kwargs)
    except ImportError:
        pass

    # GeoDataFrame → polygon plot
    try:
        import geopandas as gpd
        if isinstance(data, gpd.GeoDataFrame):
            fig, ax = _new_fig(ax=ax)
            data.plot(ax=ax, **kwargs)
            ax.set_title(meta.get("product", "geometry"))
            return ax
    except ImportError:
        pass

    # pandas DataFrame → time series
    import pandas as pd
    if isinstance(data, pd.DataFrame):
        return _plot_dataframe(data, meta, ax=ax, **kwargs)

    raise TypeError(
        f"auto_plot() does not know how to plot {type(data).__name__}. "
        f"Try the specialised plotters in aihydro_data.viz.hydrology."
    )


def _plot_dataframe(df: Any, meta: dict, *, ax: Any = None, **kwargs: Any) -> Any:
    """Time-series line plot for a date-indexed DataFrame."""
    import pandas as pd
    import matplotlib.pyplot as plt

    fig, ax = _new_fig(ax=ax)

    # Normalise: ensure 'date' column or DatetimeIndex
    if "date" in df.columns:
        df = _ensure_datetime_index(df)

    if df.empty:
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                transform=ax.transAxes, fontsize=14, color="gray")
        ax.set_title(meta.get("product", "fetched data"))
        return ax

    product = meta.get("product")
    variable = meta.get("variable", "value")
    units = meta.get("units", "")
    color = _resolve_color(product)

    # Select numeric columns to plot
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if not numeric_cols:
        raise ValueError(f"No numeric columns to plot. Columns: {list(df.columns)}")

    # Streamflow gets log-y option; precip gets bars on small windows
    if variable == "streamflow":
        for col in numeric_cols:
            ax.plot(df.index, df[col], color=color, lw=1.0, label=col)
        ax.set_ylabel(f"streamflow ({units})" if units else "streamflow")
        if kwargs.get("logy", False):
            ax.set_yscale("log")
    elif variable == "precipitation" and len(df) <= 366:
        # Daily-ish window → bar chart looks better
        for col in numeric_cols:
            ax.bar(df.index, df[col], width=1.0, color=color, alpha=0.85, label=col)
        ax.set_ylabel(f"{variable} ({units})" if units else variable)
    else:
        for col in numeric_cols:
            ax.plot(df.index, df[col], lw=1.0, label=col,
                    color=color if len(numeric_cols) == 1 else None)
        ax.set_ylabel(f"{variable} ({units})" if units else variable)

    title_bits = [product] if product else []
    if "source" in meta:
        title_bits.append(f"[{meta['source']}]")
    ax.set_title(" ".join(title_bits) if title_bits else variable)

    ax.set_xlabel("date")
    ax.grid(True, alpha=0.3)
    if len(numeric_cols) > 1:
        ax.legend(loc="best", frameon=False)

    # Add citation note (small text bottom-right) if available
    citation = meta.get("citation", "")
    if citation:
        short_cite = citation.split(".")[0][:60] + "..."
        ax.text(0.99, -0.18, short_cite, transform=ax.transAxes,
                fontsize=7, color="gray", ha="right", va="top")

    fig.tight_layout()
    return ax


def _plot_raster(da: Any, meta: dict, *, ax: Any = None, **kwargs: Any) -> Any:
    """imshow a raster DataArray with colorbar + title."""
    import matplotlib.pyplot as plt

    fig, ax = _new_fig(ax=ax, figsize=(8, 6))

    # If raster has a time dim, take the first slice (or mean if requested)
    reduce = kwargs.pop("reduce", "first")
    if "time" in da.dims:
        if reduce == "mean":
            da = da.mean(dim="time")
        else:
            da = da.isel(time=0)

    cmap = kwargs.pop("cmap", _suggest_cmap(meta.get("variable", "")))
    im = da.plot(ax=ax, cmap=cmap, add_colorbar=True, **kwargs)

    product = meta.get("product", "")
    variable = meta.get("variable", "")
    units = meta.get("units", "")
    title = f"{product} — {variable}" if product else variable
    if units:
        title += f" ({units})"
    ax.set_title(title)
    return ax


def _suggest_cmap(variable: str) -> str:
    """Variable-appropriate colormap."""
    return {
        "precipitation": "Blues",
        "et":            "YlGnBu",
        "pet":           "YlOrBr",
        "tmax":          "Reds",
        "tmin":          "Blues_r",
        "tmean":         "RdYlBu_r",
        "ndvi":          "RdYlGn",
        "lai":           "Greens",
        "dem":           "terrain",
        "soil_moisture": "BrBG",
        "landcover":     "tab20",
    }.get(variable, "viridis")
