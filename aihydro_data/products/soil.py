"""
Soil properties product registry.

IDs:
    POLARIS         – 30 m CONUS probabilistic soil, HyRiver (pygeohydro)
    SOILGRIDS       – 250 m global soil, GEE (ISRIC SoilGrids 2.0)
    OPENLANDMAP_SOIL – OpenLandMap soil, GEE (global)
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_SOIL_NEXT_STEPS = [
    {"tool": "create_cn_grid", "rationale": "Soil texture ready — build a Curve Number grid."},
    {"tool": "extract_hydrological_signatures", "rationale": "Soil attributes feed catchment characteristics."},
    {"tool": "data_describe_product", "rationale": "Include citation for the soil dataset used."},
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="POLARIS",
        variable="soil",
        source="hyriver",
        source_dataset_id="polaris",
        coverage=["CONUS"],
        temporal_start="",              # static
        temporal_end="",
        resolution_m=30,
        timestep="static",
        units="varies",                 # sand/silt/clay % ; ksat cm/hr ; etc.
        license="CC BY 4.0 (Chaney et al.)",
        citation=(
            "Chaney, N. W. et al. (2019). POLARIS Soil Properties: 30-m Probabilistic Maps "
            "of Soil Properties Over the Contiguous United States. "
            "Water Resources Research 55(4), 2916–2938. "
            "https://doi.org/10.1029/2018WR022797"
        ),
        bibtex=(
            "@article{chaney2019polaris,\n"
            "  author  = {Chaney, Nathaniel W. and others},\n"
            "  title   = {{POLARIS} Soil Properties: 30-m Probabilistic Maps Over the Contiguous United States},\n"
            "  journal = {Water Resources Research},\n"
            "  year    = {2019},\n"
            "  volume  = {55},\n"
            "  number  = {4},\n"
            "  pages   = {2916--2938},\n"
            "  doi     = {10.1029/2018WR022797}\n"
            "}"
        ),
        homepage="http://hydrology.cee.duke.edu/POLARIS/",
        requires_extras=["hyriver"],
        requires_auth=[],
        common_pitfalls=[
            "CONUS only — use SOILGRIDS for global coverage.",
            "Multiple depth layers (0–5, 5–15, 15–30, 30–60, 60–100, 100–200 cm); specify `layers` in backend_config.",
            "Static product — start/end dates are ignored.",
        ],
        examples=[
            "fetch('soil', gdf, '2020-01-01', '2020-12-31')  # auto → POLARIS in CONUS",
            "fetch('soil', gdf, '2020-01-01', '2020-12-31', mode='manual', product='POLARIS')",
        ],
        next_steps=_SOIL_NEXT_STEPS,
        backend_config={
            "pygeohydro_product": "polaris",
            # POLARIS layer names: "<property>_<depth-index>"; "_5" = 0–5 cm.
            "default_layers": ["sand_5", "silt_5", "clay_5", "ksat_5"],
        },
    ),

    # ── OpenLandMap Depth-to-Bedrock (global, 250 m, GEE) ────────────────
    # USDA/Simard depth-to-bedrock in metres (0–340 m). Static product derived
    # from MODIS, SRTM, climatic indices and soil surveys. Critical for the
    # water-storage capacity of a basin (T2.4): shallow bedrock → limited
    # groundwater storage; deep sediment → large TWS buffer.
    ProductSpec(
        id="OPENLANDMAP_BEDROCK",
        variable="bedrock_depth",
        source="gee",
        source_dataset_id="OpenLandMap/SOL/SOL_DEPTH-TO-BEDROCK_USDA-SIMARD_M/v01",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=250,
        timestep="static",
        units="m",
        license="CC BY 4.0 (OpenLandMap / USDA)",
        citation=(
            "Shangguan, W. et al. (2017). Mapping the global depth to bedrock for land "
            "surface modelling. Journal of Advances in Modeling Earth Systems 9(1), 65-88. "
            "https://doi.org/10.1002/2016MS000686"
        ),
        bibtex=(
            "@article{shangguan2017bedrock,\n"
            "  author  = {Shangguan, Wei and others},\n"
            "  title   = {Mapping the global depth to bedrock for land surface modelling},\n"
            "  journal = {Journal of Advances in Modeling Earth Systems},\n"
            "  year    = {2017},\n"
            "  volume  = {9},\n"
            "  number  = {1},\n"
            "  pages   = {65--88},\n"
            "  doi     = {10.1002/2016MS000686}\n"
            "}"
        ),
        homepage="https://openlandmap.org/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Values are integer centimetres — backend converts to metres (÷100).",
            "Range 0–3400 cm (0–34 m); capped at 34 m in some regions.",
            "Static product — start/end dates are ignored.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('bedrock_depth', gdf, '2020-01-01', '2020-12-31')  # static → depth in metres",
            "fetch('bedrock_depth', gdf, '2020-01-01', '2020-12-31', mode='manual', product='OPENLANDMAP_BEDROCK')",
        ],
        next_steps=_SOIL_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "OpenLandMap/SOL/SOL_DEPTH-TO-BEDROCK_USDA-SIMARD_M/v01",
            "band": "b0",
            "scale_m": 250,
            "unit_conversion": 0.01,   # cm → m
            "static": True,
        },
    ),

    ProductSpec(
        id="SOILGRIDS",
        variable="soil",
        source="gee",
        source_dataset_id="projects/soilgrids-isric/clay_mean",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=250,
        timestep="static",
        units="varies",
        license="CC BY 4.0 (ISRIC)",
        citation=(
            "Poggio, L. et al. (2021). SoilGrids 2.0: producing soil information for the globe "
            "with quantified spatial uncertainty. SOIL 7(1), 217–240. "
            "https://doi.org/10.5194/soil-7-217-2021"
        ),
        bibtex=(
            "@article{poggio2021soilgrids,\n"
            "  author  = {Poggio, Laura and others},\n"
            "  title   = {{SoilGrids} 2.0: producing soil information for the globe with quantified spatial uncertainty},\n"
            "  journal = {SOIL},\n"
            "  year    = {2021},\n"
            "  volume  = {7},\n"
            "  number  = {1},\n"
            "  pages   = {217--240},\n"
            "  doi     = {10.5194/soil-7-217-2021}\n"
            "}"
        ),
        homepage="https://www.isric.org/explore/soilgrids",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Each property/depth combination is a separate GEE asset.",
            "Units vary by property (g/kg for texture, cm/day for Ks, etc.).",
            "GEE auth required.",
        ],
        examples=[
            "fetch('soil', gdf, '2020-01-01', '2020-12-31', mode='manual', product='SOILGRIDS')",
        ],
        next_steps=_SOIL_NEXT_STEPS,
        backend_config={
            "gee_collection": "projects/soilgrids-isric",
            # Multi-property texture fetch → xr.Dataset of sand/silt/clay (and
            # ksat where available) at the 0–5 cm depth, named POLARIS-style
            # (sand_5, silt_5, clay_5) so the CN classifier works unchanged.
            "soil_properties": ["sand", "silt", "clay"],
            "soil_depth": "0-5cm",
            "soil_depth_suffix": "5",
            "unit_conversion": 0.1,        # g/kg → %
            "scale_m": 250,
        },
    ),
]
