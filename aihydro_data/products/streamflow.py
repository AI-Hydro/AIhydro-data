"""
Streamflow product registry.

IDs:
    NWIS_STREAMFLOW   – USGS NWIS daily streamflow, CONUS, direct_api backend
    GEOGLOWS_RETRO    – GEOGLOWS v2 modelled daily discharge, global, geoglows_retro
                        backend (anonymous AWS S3 Zarr — NO auth, NO queue, 1940→now)
    OPENMETEO_FLOOD   – Open-Meteo Global Flood (GloFAS v4) daily discharge, global,
                        openmeteo_flood backend (instant REST, no auth, ~1984→present)
    GLOFAS_STREAMFLOW – GloFAS modelled daily discharge, global, cds_glofas backend
                        (EWDS queue + auth — async last-resort)
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

    ProductSpec(
        id="GEOGLOWS_RETRO",
        variable="streamflow",
        source="geoglows_retro",
        source_dataset_id="geoglows_v2_retrospective",
        coverage=["global"],
        temporal_start="1940-01-01",
        temporal_end="present",
        resolution_m=0,                 # reach-level (vector river network), not gridded
        timestep="daily",
        units="m3/s",
        license=(
            "Open, free, anonymous AWS Open Data (GEOGLOWS v2 / ECMWF). "
            "No account, no token, no licence acceptance."
        ),
        citation=(
            "Ashby, K., Riley, S., Hales, R., Sanchez Lozano, J., Williams, G.P., "
            "Nelson, E.J., Ames, D.P., and Souffront, M. (2023). GEOGLOWS ECMWF "
            "Streamflow Model v2: global hydrologic forecasting and retrospective "
            "discharge on the TDX-Hydro river network. https://geoglows.ecmwf.int."
        ),
        bibtex=(
            "@misc{GEOGLOWS2023,\n"
            "  title        = {{GEOGLOWS} {ECMWF} Streamflow Model v2},\n"
            "  author       = {{GEOGLOWS} and {ECMWF}},\n"
            "  year         = {2023},\n"
            "  howpublished = {\\url{https://geoglows.ecmwf.int}}\n"
            "}"
        ),
        homepage="https://geoglows.ecmwf.int",
        requires_extras=["geoglows"],   # geoglows + s3fs + zarr
        requires_auth=[],               # anonymous AWS Open Data S3
        common_pitfalls=[
            "MODELLED, not observed — ECMWF IFS reanalysis routed through RAPID on the "
            "TDX-Hydro network. Never present it as gauge truth; provenance must say modelled.",
            "ALWAYS supply a delineated basin polygon, not a bare point. Validated against "
            "5 NWIS gauges (2019): bare-point snaps that landed on a tributary (snap area "
            "< 5 % of target) produced NSE < -1 and Pbias < -99 %. Correct snaps (ratio "
            "≈ 1.0) achieved NSE 0.37–0.47, KGE 0.38–0.50. Use delineate_watershed_from_point "
            "upstream of every global streamflow fetch.",
            "Snap-quality guard: if the snapped upstream area is < 5 % of the basin polygon "
            "area (ratio < 0.05), the backend raises GEOGLOWS_SNAP_MISMATCH and forces the "
            "fallback chain. Ratios 0.05–0.30 emit a warning; > 0.30 proceed silently.",
            "Typical performance on medium-large basins (1,000–1,000,000 km²) with correct "
            "snapping: NSE 0.37–0.47, KGE 0.38–0.50, r 0.63–0.76, dry bias −13 to −32 %. "
            "Published global median KGE ≈ 0.36 (Qiao et al. 2024, GEOGLOWS v2).",
            "Small basins (< 500 km²) are marginal even with correct snapping — the IFS-RAPID "
            "network may not resolve the channel. Do not expect NSE > 0.4 below 500 km².",
            "First call downloads the ~1M-reach model metadata table once (cached to disk); "
            "subsequent snaps are fast.",
        ],
        examples=[
            "# RECOMMENDED: delineate first, then fetch\n"
            "basin = delineate_watershed_from_point(lat, lon)\n"
            "fetch('streamflow', basin.geometry, '2010-01-01', '2020-12-31')",
            "fetch('streamflow', Point(-91.16, 32.31), '2020-01-01', '2020-12-31')  # bare point (risky — may snap to tributary)",
        ],
        next_steps=_SF_NEXT_STEPS,
        backend_config={
            "max_downstream_hops": 30,   # how far to walk DSLINKNO for the area-match
        },
    ),

    ProductSpec(
        id="OPENMETEO_FLOOD",
        variable="streamflow",
        source="openmeteo_flood",
        source_dataset_id="openmeteo_global_flood",
        coverage=["global"],
        temporal_start="1984-01-01",    # GloFAS v4 reanalysis via Open-Meteo
        temporal_end="present",
        resolution_m=5000,              # GloFAS v4 native 0.05° ≈ 5 km
        timestep="daily",
        units="m3/s",
        license=(
            "Free for non-commercial use, no API key (Open-Meteo). Underlying data is "
            "GloFAS v4 (Copernicus Emergency Management Service / CEMS-FLOODS)."
        ),
        citation=(
            "Zippenfenig, P. (2023). Open-Meteo.com Weather API / Global Flood API "
            "(GloFAS v4 river discharge). https://open-meteo.com. See also Harrigan et al. "
            "(2020), GloFAS-ERA5, ESSD 12(3), 2043–2060, doi:10.5194/essd-12-2043-2020."
        ),
        bibtex=(
            "@misc{OpenMeteoFlood2023,\n"
            "  title        = {Open-Meteo Global Flood {API} ({GloFAS} v4 river discharge)},\n"
            "  author       = {Zippenfenig, Patrick},\n"
            "  year         = {2023},\n"
            "  howpublished = {\\url{https://open-meteo.com/en/docs/flood-api}}\n"
            "}"
        ),
        homepage="https://open-meteo.com/en/docs/flood-api",
        requires_extras=[],             # only `requests`, a base dep
        requires_auth=[],               # no API key
        common_pitfalls=[
            "MODELLED, not observed — it IS GloFAS v4, served without the EWDS queue. "
            "Never present as gauge truth.",
            "Historical reanalysis ends ~July 2022 (GloFAS v4 Seamless); recent dates are "
            "forecast/forecast-record blends, not consolidated reanalysis.",
            "Exposes NO river topology — the 0.05° (≈5 km) GloFAS cell is picked by "
            "coordinate only, with no reach-network to walk. Validated against NWIS (2019): "
            "Potomac 30,000 km² → NSE=0.68, KGE=0.77 (✅); but Little Miami 3,116 km² and "
            "Missouri 1.36M km² returned 0.66 and 0.80 m³/s (actual: 62 and 5,923 m³/s) "
            "because the coordinate landed on a hillslope cell. cell_selection='land' does "
            "NOT help — the miss is positional, not parameter-level.",
            "Cell-miss guard: when a polygon is supplied, the backend computes specific "
            "discharge (mean Q / basin_area_km2). If < 1e-3 m³/s/km² (1 L/s/km²), "
            "OPENMETEO_CELL_MISS is raised and the fallback chain proceeds to GloFAS.",
            "Role is an availability cushion (position 3 in chain) — reliable for large "
            "dominant channels where the GloFAS cell coincides with the main stem. "
            "For reliable global coverage, prefer GEOGLOWS with a delineated polygon.",
        ],
        examples=[
            "fetch('streamflow', Point(6.96, 50.93), '2019-01-01', '2019-12-31')  # Rhine at Cologne (large dominant channel — works)",
            "fetch('streamflow', basin_gdf, '2010-01-01', '2020-12-31')  # polygon triggers cell-miss guard if Q too low",
        ],
        next_steps=_SF_NEXT_STEPS,
        backend_config={
            "cell_selection": "nearest",  # 'nearest' | 'land' | 'sea'
        },
    ),

    ProductSpec(
        id="GLOFAS_STREAMFLOW",
        variable="streamflow",
        source="cds_glofas",
        source_dataset_id="cems-glofas-historical",
        coverage=["global"],
        temporal_start="1979-01-01",
        temporal_end="present",
        resolution_m=5000,              # GloFAS v4 native 0.05° ≈ 5 km
        timestep="daily",
        units="m3/s",
        license=(
            "Free, full and open (Copernicus Emergency Management Service / CEMS-FLOODS). "
            "One-time licence acceptance required per Copernicus account."
        ),
        citation=(
            "Harrigan, S., Zsoter, E., Alfieri, L., Prudhomme, C., Salamon, P., "
            "Wetterhall, F., Barnard, C., Cloke, H., and Pappenberger, F. (2020). "
            "GloFAS-ERA5 operational global river discharge reanalysis 1979–present. "
            "Earth System Science Data, 12(3), 2043–2060. "
            "https://doi.org/10.5194/essd-12-2043-2020"
        ),
        bibtex=(
            "@article{Harrigan2020,\n"
            "  title   = {{GloFAS}-{ERA5} operational global river discharge reanalysis 1979--present},\n"
            "  author  = {Harrigan, Shaun and Zsoter, Ervin and Alfieri, Lorenzo and Prudhomme, "
            "Christel and Salamon, Peter and Wetterhall, Fredrik and Barnard, Christel and "
            "Cloke, Hannah and Pappenberger, Florian},\n"
            "  journal = {Earth System Science Data},\n"
            "  volume  = {12},\n  number  = {3},\n  pages   = {2043--2060},\n"
            "  year    = {2020},\n  doi     = {10.5194/essd-12-2043-2020}\n"
            "}"
        ),
        homepage="https://www.globalfloods.eu/",
        requires_extras=["glofas"],     # cdsapi + xarray + netCDF4
        requires_auth=["cds"],          # free EWDS personal access token
        common_pitfalls=[
            "MODELLED, not observed — LISFLOOD/ERA5 reanalysis, never present it as gauge truth. "
            "Provenance must distinguish it from NWIS observations.",
            "A point is snapped to the GloFAS main-channel cell (max-discharge) — the naive "
            "nearest cell is usually a hillslope, wrong by orders of magnitude. Supply a "
            "delineated basin polygon so the upstream-area validation can confirm the right river.",
            "Minimum resolvable basin ≈ 500–1000 km²; small headwaters have no channel pixel and "
            "will raise (basin too small for GloFAS).",
            "Auth required: a free EWDS token in ~/.cdsapirc + one-time CEMS-FLOODS licence acceptance.",
            "Multi-decade daily pulls are large/slow on EWDS — run long records through jobs.py async.",
        ],
        examples=[
            "fetch('streamflow', basin_gdf, '2010-01-01', '2020-12-31')  # snap basin → main channel",
            "fetch('streamflow', Point(87.93, 24.80), '2020-06-01', '2020-06-30')  # global point (Ganga)",
        ],
        next_steps=_SF_NEXT_STEPS,
        backend_config={
            "dataset": "cems-glofas-historical",
            "system_version": "version_4_0",
            "hydrological_model": "lisflood",
            "product_type": "consolidated",
            "search_half_deg": 0.25,     # ±0.25° window (~±5 cells) around the outlet
        },
    ),
]
