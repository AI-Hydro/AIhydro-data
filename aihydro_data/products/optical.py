"""
Optical surface-reflectance product registry (variable: ``optical``).

These products return a **multi-band** cloud-masked median reflectance
composite as an :class:`xarray.Dataset` (one ``data_var`` per friendly band
name: ``green``, ``red``, ``nir``, ``swir1`` …) rather than a single derived
index.  They are the raw-band fetch that lets ``compute_spectral_index``
compute *any* of the spectral indices (NDWI, MNDWI, NBR, NDBI, NDRE, …) locally
through the full routing + fallback + cache machinery.

Routing (see routing/policy.py):
    ("optical", "global") → [SENTINEL2_SR, LANDSAT9_SR, LANDSAT8_SR]

The GEE backend dispatches these to ``Backend.fetch_multiband_composite`` based
on the ``multiband`` flag in ``backend_config``.

IDs:
    SENTINEL2_SR  – Sentinel-2 L2A surface reflectance 10–20 m, GEE
    LANDSAT9_SR   – Landsat 9 Collection-2 L2 surface reflectance 30 m, GEE
    LANDSAT8_SR   – Landsat 8 Collection-2 L2 surface reflectance 30 m, GEE
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_OPTICAL_NEXT_STEPS = [
    {"tool": "compute_spectral_index", "rationale": "Reflectance bands ready — compute NDWI / NDVI / NBR / MNDWI locally."},
    {"tool": "data_describe_product", "rationale": "Include the sensor citation before publishing index results."},
]

# Sentinel-2 SCL classes to drop: 3 cloud-shadow, 8 cloud-medium, 9 cloud-high,
# 10 thin-cirrus, 11 snow.  Landsat masking uses QA_PIXEL bitmask (handled in
# the backend by the ``cloud_mask`` key).

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="SENTINEL2_SR",
        variable="optical",
        source="gee",
        source_dataset_id="COPERNICUS/S2_SR_HARMONIZED",
        coverage=["global"],
        temporal_start="2017-03-28",
        temporal_end="present",
        resolution_m=10,
        timestep="composite",
        units="reflectance",
        license="Copernicus Sentinel data terms (free open use with attribution)",
        citation=(
            "European Space Agency (2021). Copernicus Sentinel-2 (processed by ESA), "
            "MSI Level-2A. https://doi.org/10.5270/S2_-742ikth"
        ),
        bibtex=(
            "@dataset{esa2021sentinel2,\n"
            "  author    = {{European Space Agency}},\n"
            "  title     = {Copernicus Sentinel-2 MSI Level-2A},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5270/S2_-742ikth}\n"
            "}"
        ),
        homepage="https://sentinel.esa.int/web/sentinel/missions/sentinel-2",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Per-pixel SCL cloud masking is applied server-side before the median.",
            "Large watersheds may exceed GEE's ~32 MB download limit — coarsen scale_m.",
            "5-day revisit; persistently cloudy regions may yield sparse composites.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('optical', gdf, '2020-01-01', '2020-12-31')  # → Sentinel-2 reflectance Dataset",
        ],
        next_steps=_OPTICAL_NEXT_STEPS,
        backend_config={
            "multiband": True,
            "gee_dataset_id": "COPERNICUS/S2_SR_HARMONIZED",
            "sensor": "sentinel2",
            "band_map": {
                "blue": "B2", "green": "B3", "red": "B4",
                "re1": "B5", "re2": "B6", "re3": "B7",
                "nir": "B8", "nir2": "B8A",
                "swir1": "B11", "swir2": "B12",
            },
            "cloud_property": "CLOUDY_PIXEL_PERCENTAGE",
            "max_cloud_pct": 60,
            "cloud_mask": "sentinel2_scl",
            "scale_m": 20,
            "scale_factor": 0.0001,   # DN → surface reflectance
            "offset": 0.0,
        },
    ),

    ProductSpec(
        id="LANDSAT9_SR",
        variable="optical",
        source="gee",
        source_dataset_id="LANDSAT/LC09/C02/T1_L2",
        coverage=["global"],
        temporal_start="2021-10-31",
        temporal_end="present",
        resolution_m=30,
        timestep="composite",
        units="reflectance",
        license="public domain (USGS)",
        citation=(
            "U.S. Geological Survey (2021). Landsat 9 Collection 2 Level-2 Science Products. "
            "https://doi.org/10.5066/P9OGBGM6"
        ),
        bibtex=(
            "@dataset{usgs2021landsat9,\n"
            "  author    = {{U.S. Geological Survey}},\n"
            "  title     = {Landsat 9 Collection 2 Level-2 Science Products},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5066/P9OGBGM6}\n"
            "}"
        ),
        homepage="https://www.usgs.gov/landsat-missions/landsat-collection-2-level-2-science-products",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "QA_PIXEL bitmask cloud masking applied server-side before the median.",
            "SR bands need scale 2.75e-5 and offset -0.2 to reach true reflectance.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('optical', gdf, '2022-01-01', '2022-12-31', mode='manual', product='LANDSAT9_SR')",
        ],
        next_steps=_OPTICAL_NEXT_STEPS,
        backend_config={
            "multiband": True,
            "gee_dataset_id": "LANDSAT/LC09/C02/T1_L2",
            "sensor": "landsat9",
            "band_map": {
                "blue": "SR_B2", "green": "SR_B3", "red": "SR_B4",
                "nir": "SR_B5", "swir1": "SR_B6", "swir2": "SR_B7",
            },
            "cloud_property": "CLOUD_COVER",
            "max_cloud_pct": 60,
            "cloud_mask": "landsat_qapixel",
            "scale_m": 30,
            "scale_factor": 2.75e-5,
            "offset": -0.2,
        },
    ),

    ProductSpec(
        id="LANDSAT8_SR",
        variable="optical",
        source="gee",
        source_dataset_id="LANDSAT/LC08/C02/T1_L2",
        coverage=["global"],
        temporal_start="2013-03-18",
        temporal_end="present",
        resolution_m=30,
        timestep="composite",
        units="reflectance",
        license="public domain (USGS)",
        citation=(
            "U.S. Geological Survey (2021). Landsat 8 Collection 2 Level-2 Science Products. "
            "https://doi.org/10.5066/P9OGBGM6"
        ),
        bibtex=(
            "@dataset{usgs2021landsat8,\n"
            "  author    = {{U.S. Geological Survey}},\n"
            "  title     = {Landsat 8 Collection 2 Level-2 Science Products},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5066/P9OGBGM6}\n"
            "}"
        ),
        homepage="https://www.usgs.gov/landsat-missions/landsat-collection-2-level-2-science-products",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "QA_PIXEL bitmask cloud masking applied server-side before the median.",
            "SR bands need scale 2.75e-5 and offset -0.2 to reach true reflectance.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('optical', gdf, '2015-01-01', '2015-12-31', mode='manual', product='LANDSAT8_SR')",
        ],
        next_steps=_OPTICAL_NEXT_STEPS,
        backend_config={
            "multiband": True,
            "gee_dataset_id": "LANDSAT/LC08/C02/T1_L2",
            "sensor": "landsat8",
            "band_map": {
                "blue": "SR_B2", "green": "SR_B3", "red": "SR_B4",
                "nir": "SR_B5", "swir1": "SR_B6", "swir2": "SR_B7",
            },
            "cloud_property": "CLOUD_COVER",
            "max_cloud_pct": 60,
            "cloud_mask": "landsat_qapixel",
            "scale_m": 30,
            "scale_factor": 2.75e-5,
            "offset": -0.2,
        },
    ),

    # ── STAC fallbacks (no auth, no download-size ceiling) ────────────────
    # These stream Cloud-Optimised GeoTIFFs lazily via stackstac, so they have
    # no ~32 MB cap. They sit AFTER the GEE products in the routing chain and
    # auto-engage for large watersheds where GEE raises GEE_AREA_TOO_LARGE.

    ProductSpec(
        id="SENTINEL2_SR_STAC",
        variable="optical",
        source="stac",
        source_dataset_id="sentinel-2-l2a",
        coverage=["global"],
        temporal_start="2015-06-27",
        temporal_end="present",
        resolution_m=10,
        timestep="composite",
        units="reflectance",
        license="Copernicus Sentinel data terms (free open use with attribution)",
        citation=(
            "European Space Agency (2021). Copernicus Sentinel-2 (processed by ESA), "
            "MSI Level-2A. https://doi.org/10.5270/S2_-742ikth"
        ),
        bibtex=(
            "@dataset{esa2021sentinel2,\n"
            "  author    = {{European Space Agency}},\n"
            "  title     = {Copernicus Sentinel-2 MSI Level-2A},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5270/S2_-742ikth}\n"
            "}"
        ),
        homepage="https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a",
        requires_extras=["stac"],
        requires_auth=[],   # Planetary Computer anonymous read works
        common_pitfalls=[
            "stackstac reads lazily (dask) — large composites can be slow but never truncate.",
            "Asset ids follow Planetary Computer naming (B02, B03, … SCL).",
            "Cloud masking via SCL applied before the median composite.",
        ],
        examples=[
            "fetch('optical', big_basin_gdf, '2020-01-01', '2020-12-31')  # GEE too big → STAC",
        ],
        next_steps=_OPTICAL_NEXT_STEPS,
        backend_config={
            "multiband": True,
            "stac_endpoint": "https://planetarycomputer.microsoft.com/api/stac/v1",
            "stac_collection": "sentinel-2-l2a",
            "sensor": "sentinel2",
            "band_map": {
                "blue": "B02", "green": "B03", "red": "B04",
                "re1": "B05", "re2": "B06", "re3": "B07",
                "nir": "B08", "nir2": "B8A",
                "swir1": "B11", "swir2": "B12",
            },
            "qa_asset": "SCL",
            "cloud_mask": "sentinel2_scl",
            "stac_query": {"eo:cloud_cover": {"lt": 60}},
            "stac_resolution": 20,
            "scale_factor": 0.0001,
            "offset": 0.0,
        },
    ),

    ProductSpec(
        id="LANDSAT_SR_STAC",
        variable="optical",
        source="stac",
        source_dataset_id="landsat-c2-l2",
        coverage=["global"],
        temporal_start="1982-08-22",
        temporal_end="present",
        resolution_m=30,
        timestep="composite",
        units="reflectance",
        license="public domain (USGS)",
        citation=(
            "U.S. Geological Survey (2021). Landsat Collection 2 Level-2 Science Products. "
            "https://doi.org/10.5066/P9OGBGM6"
        ),
        bibtex=(
            "@dataset{usgs2021landsatc2,\n"
            "  author    = {{U.S. Geological Survey}},\n"
            "  title     = {Landsat Collection 2 Level-2 Science Products},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5066/P9OGBGM6}\n"
            "}"
        ),
        homepage="https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2",
        requires_extras=["stac"],
        requires_auth=[],
        common_pitfalls=[
            "Planetary Computer Landsat assets use common-band names (red, green, nir08, …).",
            "SR bands need scale 2.75e-5 and offset -0.2 to reach true reflectance.",
            "QA_PIXEL bitmask cloud masking applied before the median composite.",
        ],
        examples=[
            "fetch('optical', big_basin_gdf, '2015-01-01', '2015-12-31', mode='manual', product='LANDSAT_SR_STAC')",
        ],
        next_steps=_OPTICAL_NEXT_STEPS,
        backend_config={
            "multiband": True,
            "stac_endpoint": "https://planetarycomputer.microsoft.com/api/stac/v1",
            "stac_collection": "landsat-c2-l2",
            "sensor": "landsat8",
            "band_map": {
                "blue": "blue", "green": "green", "red": "red",
                "nir": "nir08", "swir1": "swir16", "swir2": "swir22",
            },
            "qa_asset": "qa_pixel",
            "cloud_mask": "landsat_qapixel",
            "stac_query": {"eo:cloud_cover": {"lt": 60}},
            "stac_resolution": 30,
            "scale_factor": 2.75e-5,
            "offset": -0.2,
        },
    ),
]
