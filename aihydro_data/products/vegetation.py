"""
Vegetation index product registry.

IDs:
    MODIS_NDVI      – MODIS MOD13Q1 NDVI 250 m 16-day composites, GEE
    MODIS_LAI       – MODIS MCD15A3H LAI/FPAR 500 m 4-day, GEE
    SENTINEL2_NDVI  – Sentinel-2 Level-2A NDVI 10 m, GEE
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_VEG_NEXT_STEPS = [
    {"tool": "extract_hydrological_signatures", "rationale": "NDVI / LAI ready — compute vegetation phenology metrics."},
    {"tool": "data_describe_product", "rationale": "Include citation before using vegetation indices in publications."},
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="MODIS_NDVI",
        variable="ndvi",
        source="gee",
        source_dataset_id="MODIS/061/MOD13Q1",
        coverage=["global"],
        temporal_start="2000-02-18",
        temporal_end="present",
        resolution_m=250,
        timestep="monthly",    # native 16-day; backend composites to monthly
        units="scaled_ndvi",   # raw × 0.0001 → NDVI [-1, 1]
        license="public domain (NASA LP DAAC)",
        citation=(
            "Didan, K. (2021). MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V061. "
            "NASA EOSDIS Land Processes DAAC. https://doi.org/10.5067/MODIS/MOD13Q1.061"
        ),
        bibtex=(
            "@dataset{didan2021modisndvi,\n"
            "  author    = {Didan, Kamel},\n"
            "  title     = {{MOD13Q1} MODIS/Terra Vegetation Indices 16-Day L3 Global 250m V061},\n"
            "  publisher = {NASA EOSDIS Land Processes DAAC},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5067/MODIS/MOD13Q1.061}\n"
            "}"
        ),
        homepage="https://lpdaac.usgs.gov/products/mod13q1v061/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Raw values are scaled integers (multiply by 0.0001 for true NDVI).",
            "16-day composites: use median reducer over a month for monthly series.",
            "Cloud/snow quality flags (pixel_reliability) should be applied.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('ndvi', gdf, '2010-01-01', '2020-12-31')  # global → MODIS NDVI",
            "fetch('ndvi', gdf, '2015-01-01', '2020-12-31', mode='manual', product='MODIS_NDVI')",
        ],
        next_steps=_VEG_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "MODIS/061/MOD13Q1",
            "band": "NDVI",
            "scale_m": 250,
            "unit_conversion": 0.0001,   # raw → true NDVI
        },
    ),

    ProductSpec(
        id="MODIS_LAI",
        variable="lai",
        source="gee",
        source_dataset_id="MODIS/061/MCD15A3H",
        coverage=["global"],
        temporal_start="2002-07-04",
        temporal_end="present",
        resolution_m=500,
        timestep="monthly",
        units="m2/m2",
        license="public domain (NASA LP DAAC)",
        citation=(
            "Myneni, R. et al. (2021). MODIS/Terra+Aqua Leaf Area Index/FPAR 4-Day L4 Global 500m V061. "
            "NASA EOSDIS Land Processes DAAC. https://doi.org/10.5067/MODIS/MCD15A3H.061"
        ),
        bibtex=(
            "@dataset{myneni2021mcd15,\n"
            "  author    = {Myneni, R. and others},\n"
            "  title     = {{MCD15A3H} MODIS/Terra+Aqua LAI/FPAR 4-Day L4 Global 500m V061},\n"
            "  publisher = {NASA EOSDIS Land Processes DAAC},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5067/MODIS/MCD15A3H.061}\n"
            "}"
        ),
        homepage="https://lpdaac.usgs.gov/products/mcd15a3hv061/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "4-day composites; apply FparLai_QC quality mask before analysis.",
            "Scaled values: LAI × 0.1, FPAR × 0.01.",
            "GEE auth required.",
        ],
        examples=["fetch('lai', gdf, '2010-01-01', '2020-12-31', mode='manual', product='MODIS_LAI')"],
        next_steps=_VEG_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "MODIS/061/MCD15A3H",
            "band": "Lai",   # MCD15A3H band is plain 'Lai' (not 'Lai_500m')
            "scale_m": 500,
            "unit_conversion": 0.1,
        },
    ),

    ProductSpec(
        id="SENTINEL2_NDVI",
        variable="ndvi",
        source="gee",
        source_dataset_id="COPERNICUS/S2_SR_HARMONIZED",
        coverage=["global"],
        temporal_start="2017-03-28",
        temporal_end="present",
        resolution_m=10,
        timestep="monthly",     # cloud-free monthly median composite by default
        units="ndvi",           # computed as (B8-B4)/(B8+B4)
        license="Copernicus Sentinel data terms (free open use with attribution)",
        citation=(
            "European Space Agency (2021). Copernicus Sentinel-2 (processed by ESA). "
            "https://doi.org/10.5270/S2_-742ikth"
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
            "Cloud masking (SCL band) is critical — backend applies default QA mask.",
            "Large areas generate many tiles; consider monthly composites.",
            "5-day revisit; cloudy regions may have sparse coverage.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('ndvi', gdf, '2020-01-01', '2022-12-31', mode='manual', product='SENTINEL2_NDVI')",
        ],
        next_steps=_VEG_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "COPERNICUS/S2_SR_HARMONIZED",
            "ndvi_bands": ["B8", "B4"],    # NIR, Red
            "qa_band": "SCL",
            "scale_m": 10,
            "compute_ndvi": True,          # backend computes (B8-B4)/(B8+B4) from raw bands
        },
    ),
]
