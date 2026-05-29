"""
aihydro-data sampling — chip-based raster iteration.

Two submodules:

  - ``catchment`` — :class:`CatchmentGridSampler` and
    :class:`CatchmentRandomSampler` extract fixed-size chips from a raster
    that intersect a watershed polygon.  Pattern ported from
    ``torchgeo/samplers/single.py`` (MIT) and reimplemented natively on
    xarray + rasterio.windows + shapely with zero torch dependency.
  - ``chunked`` — :func:`chunked_raster_apply`, the generalized chunked
    applier that uses the sampler to scale pixel-local raster operations
    (slope, NDWI, LULC×soil overlay, etc.) to continent-sized inputs.
"""
from __future__ import annotations

from aihydro_data.sampling.catchment import (
    ChipInfo,
    CatchmentGridSampler,
    CatchmentRandomSampler,
)
from aihydro_data.sampling.chunked import chunked_raster_apply

__all__ = [
    "ChipInfo",
    "CatchmentGridSampler",
    "CatchmentRandomSampler",
    "chunked_raster_apply",
]
