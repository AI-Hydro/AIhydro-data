"""
aihydro-data — global hydrology dataverse.

A variable-centric, multi-source, region-aware fetch library that unifies
Google Earth Engine, STAC catalogues, HyRiver, and direct HTTP APIs behind
one contract.

Public API:

    fetch(variable, geometry, start, end, mode="auto", product=None)
        — fetch a single variable for a single geometry/time window
    fetch_batch(variable, geometries, start, end, max_workers=4)
        — parallel fetch over a dict / GeoDataFrame / list of geometries
    list_products(variable=None, region=None)
        — discover what data is available
    get_product(product_id)
        — get a ProductSpec by id
    cache_status()
        — disk cache summary (size, entry count)
    cache_invalidate(cache_key)
        — remove a specific cached result

Design notes:
    - Each variable lives in `products/<variable>.py` and declares an ordered
      list of ProductSpec entries (the routing fallback chain).
    - Each backend (GEE, STAC, HyRiver, direct HTTP) lives in `sources/`.
    - Region detection + per-region product policy live in `routing/`.
    - Every failure returns a structured error envelope (code/recovery/
      next_tools) — see contracts.FetchError. Agents read those fields to
      self-recover instead of failing silently.
    - Results are disk-cached at ~/.aihydro/cache/data/ with a provenance
      manifest sidecar. Disable per-call with cache=False.

See pyproject.toml extras for optional backend installs:
    pip install aihydro-data[gee]      # Google Earth Engine
    pip install aihydro-data[stac]     # Planetary Computer / Element84
    pip install aihydro-data[hyriver]  # CONUS via HyRiver stack
    pip install aihydro-data[all]      # everything
"""
from __future__ import annotations

from aihydro_data._version import __version__

# Public symbols are imported lazily in fetch.py to avoid forcing optional
# deps at import time (e.g. someone with only [hyriver] shouldn't crash
# because `import aihydro_data` tried to load earthengine-api).
from aihydro_data.contracts import (
    FetchError,
    FetchRequest,
    FetchResult,
    ProductSpec,
)

__all__ = [
    "__version__",
    "fetch",
    "fetch_raster",
    "fetch_batch",
    "list_products",
    "get_product",
    "cache_status",
    "cache_invalidate",
    "ProductSpec",
    "FetchRequest",
    "FetchResult",
    "FetchError",
    # Transforms (v0.2.0 — torchgeo cherry-pick)
    "compute_index",
    "list_indices",
    "mask_clouds",
    "INDEX_REGISTRY",
    # Sampling (v0.2.0 — torchgeo cherry-pick)
    "CatchmentGridSampler",
    "CatchmentRandomSampler",
    "chunked_raster_apply",
]


def compute_index(*args, **kwargs):
    """Lazy proxy to aihydro_data.transforms.indices.compute_index."""
    from aihydro_data.transforms.indices import compute_index as _ci
    return _ci(*args, **kwargs)


def list_indices(*args, **kwargs):
    """Lazy proxy to aihydro_data.transforms.indices.list_indices."""
    from aihydro_data.transforms.indices import list_indices as _li
    return _li(*args, **kwargs)


def mask_clouds(*args, **kwargs):
    """Lazy proxy to aihydro_data.transforms.cloud_mask.mask_clouds."""
    from aihydro_data.transforms.cloud_mask import mask_clouds as _mc
    return _mc(*args, **kwargs)


def chunked_raster_apply(*args, **kwargs):
    """Lazy proxy to aihydro_data.sampling.chunked.chunked_raster_apply."""
    from aihydro_data.sampling.chunked import chunked_raster_apply as _cra
    return _cra(*args, **kwargs)


def __getattr__(name):
    # Lazy-import heavy classes so we don't pay xarray / rasterio import cost
    # at `import aihydro_data` time.
    if name == "INDEX_REGISTRY":
        from aihydro_data.transforms.indices import INDEX_REGISTRY
        return INDEX_REGISTRY
    if name == "CatchmentGridSampler":
        from aihydro_data.sampling.catchment import CatchmentGridSampler
        return CatchmentGridSampler
    if name == "CatchmentRandomSampler":
        from aihydro_data.sampling.catchment import CatchmentRandomSampler
        return CatchmentRandomSampler
    raise AttributeError(f"module 'aihydro_data' has no attribute {name!r}")


def fetch(*args, **kwargs):
    """Lazy proxy to aihydro_data._pipeline.fetch — see that function's docstring."""
    from aihydro_data._pipeline import fetch as _fetch
    return _fetch(*args, **kwargs)


def fetch_raster(variable, geometry, start="", end="", *, mode="auto", product=None, region=None):
    """Fetch a raw raster (xr.DataArray) via the full routing chain.

    Convenience wrapper over fetch(..., aggregation='raw_raster') for callers
    that need the spatial DataArray directly — e.g. aihydro-watershed's DEM
    pipeline — without having to unpack a FetchResult.

    Returns the xr.DataArray from result.data.  The full provider fallback
    chain, retry/backoff, and disk cache behave identically to fetch().
    """
    result = fetch(
        variable, geometry, start, end,
        mode=mode, product=product, region=region,
        aggregation="raw_raster",
    )
    return result.data


def fetch_batch(*args, **kwargs):
    """Lazy proxy to aihydro_data._pipeline.fetch_batch — parallel multi-geometry fetch."""
    from aihydro_data._pipeline import fetch_batch as _fb
    return _fb(*args, **kwargs)


def list_products(*args, **kwargs):
    """Lazy proxy to aihydro_data.products.list_products."""
    from aihydro_data.products import list_products as _list
    return _list(*args, **kwargs)


def get_product(*args, **kwargs):
    """Lazy proxy to aihydro_data.products.get_product."""
    from aihydro_data.products import get_product as _get
    return _get(*args, **kwargs)


def cache_status() -> dict:
    """Return disk cache summary — size, entry count, per-entry details."""
    from aihydro_data.cache import cache_status as _cs
    return _cs()


def cache_invalidate(ck: str) -> bool:
    """Remove a cached result by cache key. Returns True if anything was deleted."""
    from aihydro_data.cache import cache_invalidate as _ci
    return _ci(ck)
