"""
Shared viz utilities — figure setup, column detection, unit-aware labels.
"""
from __future__ import annotations

from typing import Any


# Default figure size used across all viz modules (good for notebooks + reports).
DEFAULT_FIGSIZE = (10, 5)

# Hydrology colourway — distinguishable, print-friendly, paper-grade.
PRODUCT_COLORS = {
    "CHIRPS":          "#1f78b4",
    "CHIRPS_IRI":      "#a6cee3",
    "IMERG_PRECIP":    "#33a02c",
    "ERA5L_PRECIP":    "#fb9a99",
    "GRIDMET_PRECIP":  "#e31a1c",
    "DAYMET_PRECIP":   "#ff7f00",
    "MOD16_ET":        "#6a3d9a",
    "TERRACLIMATE_AET":"#cab2d6",
    "ERA5L_PET":       "#b15928",
    "MOD16_PET":       "#ffff99",
    "GRIDMET_PET":     "#b2df8a",
    "NWIS_STREAMFLOW": "#000000",
}


def _resolve_color(product: str | None, fallback: str = "#1f78b4") -> str:
    """Pick a consistent product colour, or fall back to default."""
    if product and product in PRODUCT_COLORS:
        return PRODUCT_COLORS[product]
    return fallback


def _detect_value_column(df: Any, exclude: tuple[str, ...] = ("date",)) -> str:
    """Find the value column of a 'date + value' DataFrame."""
    cols = [c for c in df.columns if c not in exclude]
    if not cols:
        raise ValueError(f"DataFrame has no value column (cols: {list(df.columns)}).")
    # Prefer numeric columns
    import pandas as pd
    for c in cols:
        if pd.api.types.is_numeric_dtype(df[c]):
            return c
    return cols[0]


def _ensure_datetime_index(df: Any) -> Any:
    """Return a copy of df with 'date' coerced to datetime and set as index."""
    import pandas as pd
    out = df.copy()
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
        out = out.set_index("date")
    return out


def _new_fig(ax=None, figsize=DEFAULT_FIGSIZE):
    """Return (fig, ax). If ax is provided, reuse it; otherwise create."""
    import matplotlib.pyplot as plt
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.figure
    return fig, ax


def _result_data(result: Any) -> Any:
    """Extract the underlying DataFrame / DataArray from a FetchResult or pass through."""
    if hasattr(result, "data"):
        return result.data
    return result


def _result_meta(result: Any) -> dict[str, str]:
    """Extract product/variable/units/source for labelling."""
    meta = {}
    for k in ("product", "variable", "source", "license", "citation"):
        v = getattr(result, k, None)
        if v:
            meta[k] = str(v)
    # Units come from the ProductSpec via the request
    try:
        from aihydro_data.products import get_product
        if "product" in meta:
            spec = get_product(meta["product"])
            meta["units"] = spec.units
    except Exception:
        pass
    return meta
