"""
Evapotranspiration product registry.

IDs:
    MOD16_ET        – MODIS MOD16 global actual ET, 500 m, GEE
    MOD16_PET       – MODIS MOD16 global potential ET, 500 m, GEE
    GLEAM_ET        – GLEAM v3 global ET, 0.25°, GEE community datasets
    SSEBOP_ET       – SSEBop CONUS/global actual ET, 1 km, GEE
    ERA5L_PET       – ERA5-Land potential ET, ~11 km, GEE
    GRIDMET_PET     – GridMET reference ET, CONUS ~4 km, HyRiver
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_ET_NEXT_STEPS = [
    {"tool": "extract_hydrological_signatures", "rationale": "ET ready — compute aridity index or water balance."},
    {"tool": "data_describe_product", "rationale": "Include citation before using ET data in publications."},
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="MOD16_ET",
        variable="et",
        source="gee",
        source_dataset_id="MODIS/061/MOD16A2GF",
        coverage=["global"],
        temporal_start="2001-01-01",
        temporal_end="present",
        resolution_m=500,
        timestep="monthly",
        units="kg/m2/8day",
        license="public domain (NASA LP DAAC)",
        citation=(
            "Running, S. W., Q. Mu, M. Zhao, and A. Moreno (2019). "
            "MOD16A2GF MODIS/Terra Net Evapotranspiration Gap-Filled 8-Day L4 Global 500m. "
            "NASA EOSDIS Land Processes DAAC. https://doi.org/10.5067/MODIS/MOD16A2GF.061"
        ),
        bibtex=(
            "@dataset{running2019mod16,\n"
            "  author    = {Running, Steven W. and others},\n"
            "  title     = {{MOD16A2GF} MODIS/Terra Net Evapotranspiration Gap-Filled 8-Day L4 Global 500m},\n"
            "  publisher = {NASA EOSDIS Land Processes DAAC},\n"
            "  year      = {2019},\n"
            "  doi       = {10.5067/MODIS/MOD16A2GF.061}\n"
            "}"
        ),
        homepage="https://lpdaac.usgs.gov/products/mod16a2gfv061/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Native cadence is 8-day composites — backend aggregates to monthly or annual.",
            "Units are kg/m²/8day; divide by 8 for daily equivalent.",
            "Water, barren, AND URBAN pixels are masked (fill value 32761) — "
            "point-fetches over cities return all-null. Use a watershed polygon "
            "or a vegetated point instead.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('et', gdf, '2015-01-01', '2020-12-31')  # global → MOD16",
            "fetch('et', gdf, '2010-01-01', '2015-12-31', mode='manual', product='MOD16_ET')",
        ],
        next_steps=_ET_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "MODIS/061/MOD16A2GF",
            "band": "ET",
            "scale_m": 500,
            # Native ET / PET are stored as integers × 10 per LP DAAC scale
            # factor (https://lpdaac.usgs.gov/products/mod16a2gfv061/).
            # Multiply by 0.1 to recover mm/8day. Verified live at Iowa
            # cropland: raw 631 → 63.1 mm/8day = 7.9 mm/day (sensible
            # mid-summer corn-belt PET).
            "unit_conversion": 0.1,
            "agg_to_monthly": True,
        },
    ),

    ProductSpec(
        id="MOD16_PET",
        variable="pet",
        source="gee",
        source_dataset_id="MODIS/061/MOD16A2GF",
        coverage=["global"],
        temporal_start="2001-01-01",
        temporal_end="present",
        resolution_m=500,
        timestep="monthly",
        units="kg/m2/8day",
        license="public domain (NASA LP DAAC)",
        citation=(
            "Running, S. W., Q. Mu, M. Zhao, and A. Moreno (2019). "
            "MOD16A2GF MODIS/Terra Net Evapotranspiration Gap-Filled 8-Day L4 Global 500m. "
            "NASA EOSDIS Land Processes DAAC. https://doi.org/10.5067/MODIS/MOD16A2GF.061"
        ),
        bibtex=(
            "@dataset{running2019mod16,\n"
            "  author    = {Running, Steven W. and others},\n"
            "  title     = {{MOD16A2GF} MODIS/Terra Potential ET, 8-Day Global 500m},\n"
            "  publisher = {NASA EOSDIS Land Processes DAAC},\n"
            "  year      = {2019},\n"
            "  doi       = {10.5067/MODIS/MOD16A2GF.061}\n"
            "}"
        ),
        homepage="https://lpdaac.usgs.gov/products/mod16a2gfv061/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=["8-day composites.", "Units: kg/m²/8day.", "GEE auth required."],
        examples=["fetch('pet', gdf, '2010-01-01', '2020-12-31', mode='manual', product='MOD16_PET')"],
        next_steps=_ET_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "MODIS/061/MOD16A2GF",
            "band": "PET",
            "scale_m": 500,
            # Same scale factor as ET — multiply by 0.1 to recover mm/8day.
            "unit_conversion": 0.1,
        },
    ),

    # SSEBop is not currently published as a public GEE ImageCollection
    # (the old USGS/fews_net path was deprecated). We expose TerraClimate's
    # actual evapotranspiration (`aet`) instead — same scientific concept,
    # globally available, monthly, ~4 km.
    ProductSpec(
        id="TERRACLIMATE_AET",
        variable="et",
        source="gee",
        source_dataset_id="IDAHO_EPSCOR/TERRACLIMATE",
        coverage=["global"],
        temporal_start="1958-01-01",
        temporal_end="present",
        resolution_m=4638,
        timestep="monthly",
        units="mm/month",
        license="public domain (University of Idaho / TerraClimate)",
        citation=(
            "Abatzoglou, J. T., S. Z. Dobrowski, S. A. Parks, and K. C. Hegewisch (2018). "
            "TerraClimate, a high-resolution global dataset of monthly climate and climatic "
            "water balance from 1958–2015. Scientific Data 5, 170191. "
            "https://doi.org/10.1038/sdata.2017.191"
        ),
        bibtex=(
            "@article{abatzoglou2018terraclimate,\n"
            "  author  = {Abatzoglou, John T. and Dobrowski, Solomon Z. and Parks, Sean A. and Hegewisch, Katherine C.},\n"
            "  title   = {{TerraClimate}, a high-resolution global dataset of monthly climate and climatic water balance from 1958--2015},\n"
            "  journal = {Scientific Data},\n"
            "  volume  = {5},\n"
            "  pages   = {170191},\n"
            "  year    = {2018},\n"
            "  doi     = {10.1038/sdata.2017.191}\n"
            "}"
        ),
        homepage="https://www.climatologylab.org/terraclimate.html",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Native scale stored as `aet * 10` — backend applies 0.1 conversion to mm/month.",
            "Monthly product — request a multi-month window to get >1 row.",
            "Stops at ~2-month lag from real time.",
        ],
        examples=["fetch('et', gdf, '2015-01-01', '2020-12-31', mode='manual', product='TERRACLIMATE_AET')"],
        next_steps=_ET_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "IDAHO_EPSCOR/TERRACLIMATE",
            "band": "aet",
            "scale_m": 4638,
            "unit_conversion": 0.1,   # native is mm/month × 10
        },
    ),

    ProductSpec(
        id="ERA5L_PET",
        variable="pet",
        source="gee",
        source_dataset_id="ECMWF/ERA5_LAND/DAILY_AGGR",
        coverage=["global"],
        temporal_start="1950-01-01",
        temporal_end="present",
        resolution_m=11132,
        timestep="daily",
        units="mm/day",
        license="Copernicus License (free for commercial and non-commercial use with attribution)",
        citation=(
            "Muñoz Sabater, J. (2019). ERA5-Land hourly data from 1950 to present. "
            "Copernicus Climate Change Service (C3S) Climate Data Store (CDS). "
            "https://doi.org/10.24381/cds.e2161bac"
        ),
        bibtex=(
            "@dataset{munoz2019era5land,\n"
            "  author    = {Muñoz Sabater, J.},\n"
            "  title     = {{ERA5-Land} hourly data from 1950 to present},\n"
            "  publisher = {Copernicus Climate Change Service},\n"
            "  year      = {2019},\n"
            "  doi       = {10.24381/cds.e2161bac}\n"
            "}"
        ),
        homepage="https://cds.climate.copernicus.eu/cdsapp#!/dataset/reanalysis-era5-land",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "ERA5-Land PET band is potential_evaporation_sum in metres — backend converts to mm/day.",
            "GEE auth required.",
        ],
        examples=["fetch('pet', gdf, '1980-01-01', '2023-12-31', mode='manual', product='ERA5L_PET')"],
        next_steps=_ET_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "ECMWF/ERA5_LAND/DAILY_AGGR",
            "band": "potential_evaporation_sum",
            "scale_m": 11132,
            # ERA5-Land reports evaporation as a *downward* flux to surface
            # in metres of water equivalent. PET (evaporation upward from
            # surface) is therefore the negative of that value. Multiply by
            # -1000 to flip sign AND convert m → mm/day.
            "unit_conversion": -1000.0,
        },
    ),

    ProductSpec(
        id="GRIDMET_PET",
        variable="pet",
        source="hyriver",
        source_dataset_id="pet",
        coverage=["CONUS"],
        temporal_start="1979-01-01",
        temporal_end="present",
        resolution_m=4000,
        timestep="daily",
        units="mm/day",
        license="public domain (USGS / University of Idaho)",
        citation=(
            "Abatzoglou, J. T. (2013). Development of gridded surface meteorological data "
            "for ecological applications and modelling. "
            "International Journal of Climatology 33(1), 121–131. "
            "https://doi.org/10.1002/joc.3413"
        ),
        bibtex=(
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
        ),
        homepage="https://www.climatologylab.org/gridmet.html",
        requires_extras=["hyriver"],
        common_pitfalls=["CONUS only.", "GridMET PET is penman-monteith alfalfa reference ET."],
        examples=["fetch('pet', gdf, '2010-01-01', '2020-12-31')  # auto → GridMET PET in CONUS"],
        next_steps=_ET_NEXT_STEPS,
        backend_config={"pygridmet_variable": "pet"},
    ),
]
