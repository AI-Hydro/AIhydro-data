"""
Soil moisture product registry.

IDs:
    SMAP_SM         – NASA SMAP Level-3 global 9 km, GEE
    ASCAT_SM        – EUMETSAT ASCAT global 12.5 km, GEE
    ESA_CCI_SM      – ESA CCI Combined SM global 0.25°, GEE community datasets
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_SM_NEXT_STEPS = [
    {"tool": "extract_hydrological_signatures", "rationale": "Soil moisture ready — compute antecedent moisture conditions."},
    {"tool": "data_describe_product", "rationale": "Include citation before using in publications."},
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="SMAP_SM",
        variable="soil_moisture",
        source="gee",
        source_dataset_id="NASA/SMAP/SPL3SMP_E/006",
        coverage=["global"],
        temporal_start="2015-03-31",
        temporal_end="present",
        resolution_m=9000,
        timestep="daily",
        units="cm3/cm3",
        license="public domain (NASA NSIDC DAAC)",
        citation=(
            "O'Neill, P. et al. (2021). SMAP Enhanced L3 Radiometer Global and Polar Grid Daily "
            "9 km Soil Moisture. NASA NSIDC DAAC. "
            "https://doi.org/10.5067/4DQ54OUIJ9DL"
        ),
        bibtex=(
            "@dataset{oneill2021smap,\n"
            "  author    = {O'Neill, Peggy and others},\n"
            "  title     = {{SMAP} Enhanced L3 Radiometer Global Soil Moisture 9km},\n"
            "  publisher = {NASA NSIDC DAAC},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5067/4DQ54OUIJ9DL}\n"
            "}"
        ),
        homepage="https://nsidc.org/data/spl3smp_e",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Available from 31 March 2015 only.",
            "9 km resolution — poor for small catchments (<100 km²).",
            "Frozen soils (winter) are masked.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('soil_moisture', gdf, '2016-01-01', '2020-12-31')  # global → SMAP",
            "fetch('soil_moisture', gdf, '2016-01-01', '2020-12-31', mode='manual', product='SMAP_SM')",
        ],
        next_steps=_SM_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "NASA/SMAP/SPL3SMP_E/006",
            "band": "soil_moisture_am",   # AM overpass (0600 local)
            "scale_m": 9000,
            "unit_conversion": 1.0,
        },
    ),

    # NOTE: ESA CCI SM previously lived here but the GEE Community asset
    # (`projects/sat-io/open-datasets/ESA_CCI/ESA_CCI_SM_COMBINED`) was
    # removed. Until a stable replacement is published we expose SMAP only.
    # The Copernicus Climate Data Store hosts the canonical ESA CCI SM —
    # follow-up task: add a CDS-backed direct_api product.
]
