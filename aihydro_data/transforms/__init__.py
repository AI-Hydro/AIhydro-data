"""
aihydro-data transforms — post-fetch raster operations.

Two submodules:

  - ``indices`` — spectral index formulas (NDVI, NDWI, MNDWI, SWI, NBR, NDBI,
    GNDVI, NDRE, NDSI, EVI). Pure-numpy ports of the torchgeo formulas with
    no torch / kornia / lightning dependency.
  - ``cloud_mask`` — sensor-aware cloud/shadow/cirrus masking using each
    sensor's native QA band (Sentinel-2 SCL, Landsat QA_PIXEL, MODIS
    state_1km bits). Auto-applied before optical index computation.

Both modules operate on ``xarray.Dataset`` / ``xarray.DataArray`` inputs
and propagate provenance through ``.attrs``.
"""
from __future__ import annotations

from aihydro_data.transforms.indices import (
    INDEX_REGISTRY,
    SENSOR_BAND_MAPS,
    compute_index,
    list_indices,
    ndvi,
    ndwi,
    mndwi,
    ndbi,
    nbr,
    ndsi,
    gndvi,
    ndre,
    swi,
    evi,
)
from aihydro_data.transforms.cloud_mask import mask_clouds, CLOUD_AWARE_SENSORS

__all__ = [
    "INDEX_REGISTRY",
    "SENSOR_BAND_MAPS",
    "compute_index",
    "list_indices",
    "ndvi",
    "ndwi",
    "mndwi",
    "ndbi",
    "nbr",
    "ndsi",
    "gndvi",
    "ndre",
    "swi",
    "evi",
    "mask_clouds",
    "CLOUD_AWARE_SENSORS",
]
