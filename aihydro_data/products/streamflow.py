"""
Streamflow product registry.

IDs:
    NWIS_STREAMFLOW – USGS NWIS daily streamflow, CONUS, direct_api backend
    GRDC_STREAMFLOW – Global Runoff Data Centre, global, direct_api backend (Phase 4)
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_SF_NEXT_STEPS = [
    {"tool": "extract_hydrological_signatures", "rationale": "Streamflow ready — compute flow signatures."},
    {"tool": "train_hydro_model", "rationale": "Streamflow + forcing → calibrate a rainfall-runoff model."},
    {"tool": "data_describe_product", "rationale": "Include NWIS citation before publishing."},
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="NWIS_STREAMFLOW",
        variable="streamflow",
        source="direct_api",
        source_dataset_id="usgs_nwis_dv",
        coverage=["CONUS"],
        temporal_start="1900-01-01",    # NWIS records vary by gauge; some go back to ~1900
        temporal_end="present",
        resolution_m=0,                 # point measurement (gauge)
        timestep="daily",
        units="m3/s",
        license="public domain (USGS)",
        citation=(
            "U.S. Geological Survey (2024). National Water Information System. "
            "U.S. Geological Survey Water Resources, https://waterdata.usgs.gov/nwis."
        ),
        bibtex=(
            "@misc{NWIS2024,\n"
            "  title        = {National Water Information System ({NWIS})},\n"
            "  author       = {{U.S. Geological Survey Water Resources Mission Area}},\n"
            "  year         = {2024},\n"
            "  howpublished = {\\url{https://waterdata.usgs.gov/nwis}}\n"
            "}"
        ),
        homepage="https://waterdata.usgs.gov/nwis",
        requires_extras=["hyriver"],    # dataretrieval is part of the hyriver stack
        requires_auth=[],
        common_pitfalls=[
            "CONUS gauges only — for global streamflow use GRDC_STREAMFLOW (Phase 4).",
            "geometry is used only to look up the nearest gauge if gauge_id is not provided.",
            "Pass gauge_id in backend_config or as the geometry to fetch a specific gauge.",
            "Units are m³/s; divide by watershed area × 86400 for mm/day.",
        ],
        examples=[
            "fetch('streamflow', '03245500', '2010-01-01', '2020-12-31')  # gauge_id as geometry",
            "fetch('streamflow', gdf, '2010-01-01', '2020-12-31')  # nearest NWIS gauge to centroid",
        ],
        next_steps=_SF_NEXT_STEPS,
        backend_config={
            "service": "nwis_dv",
            "parameter_code": "00060",   # USGS param code for discharge
            "stat_code": "00003",        # daily mean
        },
    ),
]
