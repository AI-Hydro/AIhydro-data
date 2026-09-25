"""
Precipitation product registry.

IDs referenced in routing/policy.py:
    CHIRPS          – global daily, 5 km, GEE
    IMERG_PRECIP    – global half-hourly/daily, ~11 km, GEE (GPM)
    ERA5L_PRECIP    – global hourly/daily, ~11 km, GEE
    GRIDMET_PRECIP  – CONUS daily, ~4 km, HyRiver (pygridmet)
    DAYMET_PRECIP   – North America daily, 1 km, HyRiver (pydaymet)
    CHIRPS_IRI      – global daily, 5 km, IRI OPeNDAP (auth-free fallback)
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_PRECIP_NEXT_STEPS = [
    {
        "tool": "extract_hydrological_signatures",
        "rationale": "Precipitation time-series ready — derive flow signatures.",
    },
    {
        "tool": "data_describe_product",
        "rationale": "Show citation / provenance before writing up results.",
    },
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="CHIRPS",
        variable="precipitation",
        source="gee",
        source_dataset_id="UCSB-CHC/CHIRPS/V3/DAILY_SAT",
        dataset_version="3.0",
        variant="daily_sat",
        temporal_derivation="CHIRPS v3 pentad totals partitioned into daily amounts using IMERG Late V07",
        input_dependencies=["CHIRPS v3 pentad", "NASA IMERG Late V07"],
        coverage=["global"],
        temporal_start="1981-01-01",
        temporal_end="present",
        resolution_m=5566,
        timestep="daily",
        units="mm/day",
        license="CHC v3 page states CC BY 4.0; GEE catalog describes public domain (provider terms differ).",
        citation=(
            "Climate Hazards Center Infrared Precipitation with Stations version 3. "
            "CHIRPS3 Data Repository (2025). https://doi.org/10.15780/G2JQ0P. "
            "Daily SAT variant (IMERG-based temporal disaggregation)."
        ),
        bibtex=(
            "@dataset{chirps3,\n"
            "  author = {{Climate Hazards Center}},\n"
            "  title = {Climate Hazards Center Infrared Precipitation with Stations version 3},\n"
            "  year = {2025},\n"
            "  doi = {10.15780/G2JQ0P},\n"
            "  note = {Daily SAT variant, IMERG-based temporal disaggregation}\n"
            "}"
        ),
        homepage="https://www.chc.ucsb.edu/data/chirps3",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Dates before 1981-01-01 are not available.",
            "CHIRPS ID serves v3 daily SAT, not v2; CHIRPS_IRI is a different v2 daily-improved product.",
            "Nominal v3 coverage is land between 60°S and 60°N; global routing is not proof of coverage.",
            "Daily amounts partition pentad totals using IMERG Late V07; comparison with IMERG is not independent validation.",
            "Satellite-gauge product; uncertainty varies by region and gauge density. Per-observation maturity is not verified.",
            "GEE auth required — run `aihydro-data auth gee` first.",
        ],
        examples=[
            "fetch('precipitation', (34.0, 72.0), '2010-01-01', '2010-12-31')  # auto → CHIRPS outside CONUS",
            "fetch('precipitation', gdf, '2015-01-01', '2015-12-31', mode='manual', product='CHIRPS')",
        ],
        next_steps=_PRECIP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "UCSB-CHC/CHIRPS/V3/DAILY_SAT",
            "band": "precipitation",
            "scale_m": 5566,
            "unit_conversion": 1.0,  # already mm/day
        },
    ),

    # MSWEP's public GEE Community asset was removed in 2024. Until a stable
    # replacement appears we ship NASA's IMERG (GPM L3 V07) under the
    # same "alternative global precipitation reference" slot — both are
    # ~10 km global, sub-daily, well-validated.
    ProductSpec(
        id="IMERG_PRECIP",
        variable="precipitation",
        source="gee",
        source_dataset_id="NASA/GPM_L3/IMERG_V07",
        coverage=["global"],
        temporal_start="1998-01-01",
        temporal_end="present",
        resolution_m=11132,
        timestep="daily",   # native is half-hourly; backend aggregates to daily
        units="mm/day",
        license="public domain (NASA GES DISC)",
        citation=(
            "Huffman, G. J., E. F. Stocker, D. T. Bolvin, E. J. Nelkin, J. Tan (2023). "
            "GPM IMERG V07 half-hourly precipitation (GEE collection: permanent and provisional). "
            "NASA GES DISC. Permanent: https://doi.org/10.5067/GPM/IMERG/3B-HH/07; "
            "provisional: https://doi.org/10.5067/GPM/IMERG/3B-HH-L/07. "
            "Use the DOI matching the returned source_status."
        ),
        bibtex=(
            "@dataset{huffman2023imerg,\n"
            "  author    = {Huffman, George J. and Stocker, Eric F. and Bolvin, David T. and Nelkin, Eric J. and Tan, Jackson},\n"
            "  title     = {{GPM IMERG} V07 half-hourly precipitation collection},\n"
            "  publisher = {NASA GES DISC},\n"
            "  year      = {2023},\n"
            "  url       = {https://developers.google.com/earth-engine/datasets/catalog/NASA_GPM_L3_IMERG_V07},\n"
            "  note      = {Permanent DOI: 10.5067/GPM/IMERG/3B-HH/07; provisional DOI: 10.5067/GPM/IMERG/3B-HH-L/07. Select by source status.}\n"
            "}"
        ),
        homepage="https://gpm.nasa.gov/data/imerg",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Native cadence is half-hourly precipitation RATE (mm/hr).",
            "Daily totals integrate 48 complete UTC half-hours (rate × 0.5 h); start/end dates are inclusive.",
            "Missing, duplicate, masked or invalid intervals fail; no zero filling or extrapolation.",
            "GEE contains provisional and permanent products; inspect source_status for each day. "
            "Final DOI applies only to permanent data; provisional source DOI: 10.5067/GPM/IMERG/3B-HH-L/07.",
            "Temporal completeness does not verify stable pixel masks or basin-area weighting.",
        ],
        examples=[
            "fetch('precipitation', gdf, '2015-01-01', '2020-12-31', mode='manual', product='IMERG_PRECIP')",
        ],
        next_steps=_PRECIP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "NASA/GPM_L3/IMERG_V07",
            "temporal_contract": "imerg_half_hourly_rate_to_daily_v1",
            "band": "precipitation",
            "scale_m": 11132,
        },
    ),

    ProductSpec(
        id="ERA5L_PRECIP",
        variable="precipitation",
        source="gee",
        source_dataset_id="ECMWF/ERA5_LAND/DAILY_AGGR",
        coverage=["global"],
        temporal_start="1950-01-01",
        temporal_end="present",
        resolution_m=11132,       # ~0.1°
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
            "  publisher = {Copernicus Climate Change Service (C3S) Climate Data Store (CDS)},\n"
            "  year      = {2019},\n"
            "  doi       = {10.24381/cds.e2161bac}\n"
            "}"
        ),
        homepage="https://cds.climate.copernicus.eu/cdsapp#!/dataset/reanalysis-era5-land",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "ERA5-Land precipitation is total_precipitation_sum in metres — backend converts to mm/day.",
            "Model-reanalysis; may show biases in high-altitude or complex-terrain regions.",
            "GEE auth required — run `aihydro-data auth gee` first.",
        ],
        examples=[
            "fetch('precipitation', gdf, '1980-01-01', '2023-12-31', mode='manual', product='ERA5L_PRECIP')",
        ],
        next_steps=_PRECIP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "ECMWF/ERA5_LAND/DAILY_AGGR",
            "band": "total_precipitation_sum",
            "scale_m": 11132,
            "unit_conversion": 1000.0,   # m → mm/day
        },
    ),

    ProductSpec(
        id="GRIDMET_PRECIP",
        variable="precipitation",
        source="hyriver",
        source_dataset_id="pr",               # pygridmet variable key
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
            "  title   = {Development of gridded surface meteorological data for ecological applications},\n"
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
        requires_auth=[],
        common_pitfalls=[
            "CONUS only — use CHIRPS or ERA5L_PRECIP for non-CONUS geometries.",
            "Requires `pip install aihydro-data[hyriver]`.",
        ],
        examples=[
            "fetch('precipitation', gdf, '2015-01-01', '2015-12-31')  # auto → GridMET inside CONUS",
            "fetch('precipitation', gdf, '2015-01-01', '2015-12-31', mode='manual', product='GRIDMET_PRECIP')",
        ],
        next_steps=_PRECIP_NEXT_STEPS,
        backend_config={
            "pygridmet_variable": "pr",
        },
    ),

    ProductSpec(
        id="DAYMET_PRECIP",
        variable="precipitation",
        source="hyriver",
        source_dataset_id="prcp",             # pydaymet variable key
        coverage=["NORTH_AMERICA"],
        temporal_start="1980-01-01",
        temporal_end="present",
        resolution_m=1000,
        timestep="daily",
        units="mm/day",
        license="public domain (NASA / ORNL DAAC)",
        citation=(
            "Thornton, M. M. et al. (2022). Daymet: Daily Surface Weather Data on a "
            "1-km Grid for North America, Version 4 R1. ORNL DAAC. "
            "https://doi.org/10.3334/ORNLDAAC/2129"
        ),
        bibtex=(
            "@dataset{thornton2022daymet,\n"
            "  author    = {Thornton, Michele M. and others},\n"
            "  title     = {{Daymet}: Daily Surface Weather Data on a 1-km Grid for North America, Version 4 R1},\n"
            "  publisher = {ORNL DAAC},\n"
            "  year      = {2022},\n"
            "  doi       = {10.3334/ORNLDAAC/2129}\n"
            "}"
        ),
        homepage="https://daymet.ornl.gov/",
        requires_extras=["hyriver"],
        requires_auth=[],
        common_pitfalls=[
            "North America only (CONUS, Canada, Mexico, Hawaii, Puerto Rico).",
            "Requires `pip install aihydro-data[hyriver]`.",
            "Variable name is 'prcp' (not 'precip') in pydaymet.",
        ],
        examples=[
            "fetch('precipitation', gdf, '2010-01-01', '2010-12-31', mode='manual', product='DAYMET_PRECIP')",
        ],
        next_steps=_PRECIP_NEXT_STEPS,
        backend_config={
            "pydaymet_variable": "prcp",
        },
    ),

    # ── Auth-free fallback: CHIRPS via IRI OPeNDAP ───────────────────────────
    # Activated automatically when all GEE precipitation products fail (e.g.
    # GEE is not authenticated or its quota is exhausted).  Requires only
    # xarray + netCDF4 — no account, no token, no API key.
    #
    # Source: IRI/LDEO Data Library — Columbia University
    # URL   : https://iridl.ldeo.columbia.edu/SOURCES/.UCSB/.CHIRPS/.v2p0/
    #          .daily-improved/.global/.0p05/.prcp/dods
    # Distinct v2 daily-improved product; the GEE CHIRPS ID serves v3 daily SAT.
    ProductSpec(
        id="CHIRPS_IRI",
        variable="precipitation",
        source="direct_api",
        source_dataset_id="https://iridl.ldeo.columbia.edu/SOURCES/.UCSB/.CHIRPS/.v2p0/.daily-improved/.global/.0p05/.prcp/dods",
        dataset_version="2.0",
        variant="daily-improved",
        temporal_derivation="CHIRPS v2 daily-improved; exact temporal disaggregation not verified by this adapter",
        coverage=["global"],
        temporal_start="1981-01-01",
        temporal_end="present",
        resolution_m=5566,
        timestep="daily",
        units="mm/day",
        license="public domain (Creative Commons CC0)",
        citation=(
            "Funk, C. et al. (2015). The Climate Hazards Infrared Precipitation with "
            "Stations–a new environmental record for monitoring extremes. "
            "Scientific Data 2, 150066. https://doi.org/10.1038/sdata.2015.66"
        ),
        bibtex=(
            "@article{funk2015chirps,\n"
            "  author  = {Funk, Chris and others},\n"
            "  title   = {The Climate Hazards Infrared Precipitation with Stations},\n"
            "  journal = {Scientific Data},\n"
            "  year    = {2015},\n"
            "  volume  = {2},\n"
            "  pages   = {150066},\n"
            "  doi     = {10.1038/sdata.2015.66}\n"
            "}"
        ),
        homepage="https://www.chc.ucsb.edu/data/chirps",
        requires_extras=["opendap"],
        requires_auth=[],        # No authentication required — main advantage over GEE CHIRPS
        common_pitfalls=[
            "Serves CHIRPS v2 daily-improved, not the GEE CHIRPS v3 daily SAT product; fallback changes scientific product identity.",
            "Requires `pip install aihydro-data[opendap]` (xarray + netCDF4 with OPeNDAP support).",
            "Nominal coverage 50°S–50°N land; ocean pixels are fill-valued.",
            "Dates before 1981-01-01 are not available.",
            "OPeNDAP server may be slow for large spatial extents; prefer GEE CHIRPS when authed.",
            "Server-side subsetting: first request opens the dataset header (~1–2 s latency).",
        ],
        examples=[
            "# Automatic fallback — no action needed; fires when GEE is unavailable",
            "fetch('precipitation', gdf, '2010-01-01', '2020-12-31', mode='manual', product='CHIRPS_IRI')",
        ],
        next_steps=_PRECIP_NEXT_STEPS,
        backend_config={
            "service": "chirps_iri",
            "iri_url": (
                "https://iridl.ldeo.columbia.edu/SOURCES/.UCSB/.CHIRPS/.v2p0"
                "/.daily-improved/.global/.0p05/.prcp/dods"
            ),
            "variable": "prcp",
            "lon_dim": "X",
            "lat_dim": "Y",
            "time_dim": "T",
            # IRI CHIRPS T axis is "Julian days" (days since an internal epoch).
            # The epoch is resolved at fetch time by reading the first T value
            # (which corresponds to 1981-01-01) and computing offsets from there.
        },
    ),
]
