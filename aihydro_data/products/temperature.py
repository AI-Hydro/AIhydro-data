"""
Temperature product registry.

IDs:
    GRIDMET_TMAX    – CONUS daily max temp, ~4 km, HyRiver (pygridmet)
    GRIDMET_TMIN    – CONUS daily min temp, ~4 km, HyRiver (pygridmet)
    DAYMET_TMAX     – North America daily max temp, 1 km, HyRiver (pydaymet)
    DAYMET_TMIN     – North America daily min temp, 1 km, HyRiver (pydaymet)
    ERA5L_TMAX      – Global daily max temp, ~11 km, GEE
    ERA5L_TMIN      – Global daily min temp, ~11 km, GEE
    ERA5L_TMEAN     – Global daily mean temp, ~11 km, GEE
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_TEMP_NEXT_STEPS = [
    {"tool": "extract_hydrological_signatures", "rationale": "Temperature series ready — compute PET or signatures."},
    {"tool": "data_describe_product", "rationale": "Include citation before using in publications."},
]

_ERA5_CITATION = (
    "Muñoz Sabater, J. (2019). ERA5-Land hourly data from 1950 to present. "
    "Copernicus Climate Change Service (C3S) Climate Data Store (CDS). "
    "https://doi.org/10.24381/cds.e2161bac"
)
_ERA5_BIBTEX = (
    "@dataset{munoz2019era5land,\n"
    "  author    = {Muñoz Sabater, J.},\n"
    "  title     = {{ERA5-Land} hourly data from 1950 to present},\n"
    "  publisher = {Copernicus Climate Change Service},\n"
    "  year      = {2019},\n"
    "  doi       = {10.24381/cds.e2161bac}\n"
    "}"
)
_GRIDMET_CITATION = (
    "Abatzoglou, J. T. (2013). Development of gridded surface meteorological data "
    "for ecological applications and modelling. "
    "International Journal of Climatology 33(1), 121–131. "
    "https://doi.org/10.1002/joc.3413"
)
_GRIDMET_BIBTEX = (
    "@article{abatzoglou2013gridmet,\n"
    "  author  = {Abatzoglou, John T.},\n"
    "  title   = {Development of gridded surface meteorological data},\n"
    "  journal = {International Journal of Climatology},\n"
    "  year    = {2013},\n"
    "  volume  = {33},\n"
    "  number  = {1},\n"
    "  pages   = {121--131},\n"
    "  doi     = {10.1002/joc.3413}\n"
    "}"
)
_DAYMET_CITATION = (
    "Thornton, M. M. et al. (2022). Daymet: Daily Surface Weather Data on a "
    "1-km Grid for North America, Version 4 R1. ORNL DAAC. "
    "https://doi.org/10.3334/ORNLDAAC/2129"
)
_DAYMET_BIBTEX = (
    "@dataset{thornton2022daymet,\n"
    "  author    = {Thornton, Michele M. and others},\n"
    "  title     = {{Daymet}: Daily Surface Weather Data on a 1-km Grid, Version 4 R1},\n"
    "  publisher = {ORNL DAAC},\n"
    "  year      = {2022},\n"
    "  doi       = {10.3334/ORNLDAAC/2129}\n"
    "}"
)


PRODUCTS: list[ProductSpec] = [

    # ── GridMET ───────────────────────────────────────────────────────────
    ProductSpec(
        id="GRIDMET_TMAX",
        variable="tmax",
        source="hyriver",
        source_dataset_id="tmmx",
        coverage=["CONUS"],
        temporal_start="1979-01-01",
        temporal_end="present",
        resolution_m=4000,
        timestep="daily",
        units="K",
        license="public domain (USGS / University of Idaho)",
        citation=_GRIDMET_CITATION,
        bibtex=_GRIDMET_BIBTEX,
        homepage="https://www.climatologylab.org/gridmet.html",
        requires_extras=["hyriver"],
        common_pitfalls=["Native units are Kelvin; subtract 273.15 for Celsius.", "CONUS only."],
        examples=["fetch('tmax', gdf, '2010-01-01', '2010-12-31')  # auto → GRIDMET inside CONUS"],
        next_steps=_TEMP_NEXT_STEPS,
        backend_config={"pygridmet_variable": "tmmx"},
    ),

    ProductSpec(
        id="GRIDMET_TMIN",
        variable="tmin",
        source="hyriver",
        source_dataset_id="tmmn",
        coverage=["CONUS"],
        temporal_start="1979-01-01",
        temporal_end="present",
        resolution_m=4000,
        timestep="daily",
        units="K",
        license="public domain (USGS / University of Idaho)",
        citation=_GRIDMET_CITATION,
        bibtex=_GRIDMET_BIBTEX,
        homepage="https://www.climatologylab.org/gridmet.html",
        requires_extras=["hyriver"],
        common_pitfalls=["Native units are Kelvin; subtract 273.15 for Celsius.", "CONUS only."],
        examples=["fetch('tmin', gdf, '2010-01-01', '2010-12-31')"],
        next_steps=_TEMP_NEXT_STEPS,
        backend_config={"pygridmet_variable": "tmmn"},
    ),

    # ── Daymet ────────────────────────────────────────────────────────────
    ProductSpec(
        id="DAYMET_TMAX",
        variable="tmax",
        source="hyriver",
        source_dataset_id="tmax",
        coverage=["NORTH_AMERICA"],
        temporal_start="1980-01-01",
        temporal_end="present",
        resolution_m=1000,
        timestep="daily",
        units="degC",
        license="public domain (NASA / ORNL DAAC)",
        citation=_DAYMET_CITATION,
        bibtex=_DAYMET_BIBTEX,
        homepage="https://daymet.ornl.gov/",
        requires_extras=["hyriver"],
        common_pitfalls=["North America only.", "Requires `pip install aihydro-data[hyriver]`."],
        examples=["fetch('tmax', gdf, '2010-01-01', '2010-12-31', mode='manual', product='DAYMET_TMAX')"],
        next_steps=_TEMP_NEXT_STEPS,
        backend_config={"pydaymet_variable": "tmax"},
    ),

    ProductSpec(
        id="DAYMET_TMIN",
        variable="tmin",
        source="hyriver",
        source_dataset_id="tmin",
        coverage=["NORTH_AMERICA"],
        temporal_start="1980-01-01",
        temporal_end="present",
        resolution_m=1000,
        timestep="daily",
        units="degC",
        license="public domain (NASA / ORNL DAAC)",
        citation=_DAYMET_CITATION,
        bibtex=_DAYMET_BIBTEX,
        homepage="https://daymet.ornl.gov/",
        requires_extras=["hyriver"],
        common_pitfalls=["North America only."],
        examples=["fetch('tmin', gdf, '2010-01-01', '2010-12-31', mode='manual', product='DAYMET_TMIN')"],
        next_steps=_TEMP_NEXT_STEPS,
        backend_config={"pydaymet_variable": "tmin"},
    ),

    # ── ERA5-Land (global) ────────────────────────────────────────────────
    ProductSpec(
        id="ERA5L_TMAX",
        variable="tmax",
        source="gee",
        source_dataset_id="ECMWF/ERA5_LAND/DAILY_AGGR",
        coverage=["global"],
        temporal_start="1950-01-01",
        temporal_end="present",
        resolution_m=11132,
        timestep="daily",
        units="K",
        license="Copernicus License (free for commercial and non-commercial use with attribution)",
        citation=_ERA5_CITATION,
        bibtex=_ERA5_BIBTEX,
        homepage="https://cds.climate.copernicus.eu/cdsapp#!/dataset/reanalysis-era5-land",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=["Units are Kelvin — subtract 273.15 for Celsius.", "GEE auth required."],
        examples=["fetch('tmax', gdf, '2000-01-01', '2020-12-31', mode='manual', product='ERA5L_TMAX')"],
        next_steps=_TEMP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "ECMWF/ERA5_LAND/DAILY_AGGR",
            "band": "temperature_2m_max",
            "scale_m": 11132,
            "unit_conversion": 1.0,
        },
    ),

    ProductSpec(
        id="ERA5L_TMIN",
        variable="tmin",
        source="gee",
        source_dataset_id="ECMWF/ERA5_LAND/DAILY_AGGR",
        coverage=["global"],
        temporal_start="1950-01-01",
        temporal_end="present",
        resolution_m=11132,
        timestep="daily",
        units="K",
        license="Copernicus License (free for commercial and non-commercial use with attribution)",
        citation=_ERA5_CITATION,
        bibtex=_ERA5_BIBTEX,
        homepage="https://cds.climate.copernicus.eu/cdsapp#!/dataset/reanalysis-era5-land",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=["Units are Kelvin — subtract 273.15 for Celsius.", "GEE auth required."],
        examples=["fetch('tmin', gdf, '2000-01-01', '2020-12-31', mode='manual', product='ERA5L_TMIN')"],
        next_steps=_TEMP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "ECMWF/ERA5_LAND/DAILY_AGGR",
            "band": "temperature_2m_min",
            "scale_m": 11132,
            "unit_conversion": 1.0,
        },
    ),

    ProductSpec(
        id="ERA5L_TMEAN",
        variable="tmean",
        source="gee",
        source_dataset_id="ECMWF/ERA5_LAND/DAILY_AGGR",
        coverage=["global"],
        temporal_start="1950-01-01",
        temporal_end="present",
        resolution_m=11132,
        timestep="daily",
        units="K",
        license="Copernicus License (free for commercial and non-commercial use with attribution)",
        citation=_ERA5_CITATION,
        bibtex=_ERA5_BIBTEX,
        homepage="https://cds.climate.copernicus.eu/cdsapp#!/dataset/reanalysis-era5-land",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=["Units are Kelvin.", "GEE auth required."],
        examples=["fetch('tmean', gdf, '2000-01-01', '2020-12-31', mode='manual', product='ERA5L_TMEAN')"],
        next_steps=_TEMP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "ECMWF/ERA5_LAND/DAILY_AGGR",
            "band": "temperature_2m",
            "scale_m": 11132,
            "unit_conversion": 1.0,
        },
    ),
]
