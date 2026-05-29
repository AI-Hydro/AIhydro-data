"""
Visualization layer for aihydro-data.

Three tiers, smallest first:

  1. `auto_plot(result)`        — auto-dispatched plot based on data shape.
                                  Wired to FetchResult.plot() for `r.plot()` ergonomics.

  2. `hydrology.*`              — research-grade plots hydrologists actually publish:
                                    flow_duration_curve, climatology, double_mass,
                                    budyko, aridity_map

  3. `compare(products, ...)`   — multi-source side-by-side comparison.
                                  Fetches each product then renders a panel.

All plots use matplotlib (and folium for maps). Install with:

    pip install aihydro-data[viz]

The viz layer is OPTIONAL — the rest of aihydro-data works fine without it.
A clean ImportError with install hint is raised if matplotlib is missing.
"""
from __future__ import annotations

from typing import Any


def _require_matplotlib() -> None:
    """Raise an informative error if matplotlib isn't installed."""
    try:
        import matplotlib  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "matplotlib is required for the viz layer. "
            "Install with: pip install aihydro-data[viz]"
        ) from exc


# ── Public API re-exports ───────────────────────────────────────────────────

def auto_plot(result: Any, **kwargs: Any) -> Any:
    """Dispatch on FetchResult data shape → best-fit plot. See viz.auto."""
    _require_matplotlib()
    from aihydro_data.viz.auto import auto_plot as _impl
    return _impl(result, **kwargs)


def compare(
    products: list[str],
    geometry: Any,
    start: str,
    end: str,
    *,
    variable: str | None = None,
    plots: list[str] | None = None,
    **fetch_kwargs: Any,
) -> Any:
    """Fetch multiple products and render side-by-side comparison. See viz.compare."""
    _require_matplotlib()
    # NOTE: submodule is _compare (underscore) to avoid shadowing this function
    # when Python registers the submodule on parent-package access.
    from aihydro_data.viz._compare import compare as _impl
    return _impl(
        products, geometry, start, end,
        variable=variable, plots=plots, **fetch_kwargs,
    )


# Hydrology plot re-exports — most-used names at top level.
def flow_duration_curve(series: Any, **kwargs: Any) -> Any:
    _require_matplotlib()
    from aihydro_data.viz.hydrology import flow_duration_curve as _impl
    return _impl(series, **kwargs)


def climatology(series: Any, **kwargs: Any) -> Any:
    _require_matplotlib()
    from aihydro_data.viz.hydrology import climatology as _impl
    return _impl(series, **kwargs)


def double_mass(a: Any, b: Any, **kwargs: Any) -> Any:
    _require_matplotlib()
    from aihydro_data.viz.hydrology import double_mass as _impl
    return _impl(a, b, **kwargs)


def budyko(precip: Any, pet: Any, et: Any, **kwargs: Any) -> Any:
    _require_matplotlib()
    from aihydro_data.viz.hydrology import budyko as _impl
    return _impl(precip, pet, et, **kwargs)


def map_preview(result: Any, **kwargs: Any) -> Any:
    """Folium interactive map preview of a fetched geometry/raster."""
    from aihydro_data.viz.spatial import map_preview as _impl
    return _impl(result, **kwargs)


__all__ = [
    "auto_plot",
    "compare",
    "flow_duration_curve",
    "climatology",
    "double_mass",
    "budyko",
    "map_preview",
]
