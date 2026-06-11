"""
Land cover product registry.

IDs:
    NLCD            – National Land Cover Database, CONUS 30 m, HyRiver (pygeohydro)
    ESA_WORLDCOVER  – ESA WorldCover 10 m global, GEE
    DYNAMIC_WORLD   – Google / WRI Dynamic World 10 m, GEE
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_LC_NEXT_STEPS = [
    {"tool": "create_cn_grid", "rationale": "Land cover ready — build a Curve Number grid."},
    {"tool": "data_describe_product", "rationale": "Include citation for the land cover product used."},
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="NLCD",
        variable="landcover",
        source="hyriver",
        source_dataset_id="NLCD",
        coverage=["CONUS"],
        temporal_start="2001-01-01",
        temporal_end="2021-01-01",       # discrete years; not continuous
        resolution_m=30,
        timestep="static",
        units="class",
        license="public domain (USGS)",
        citation=(
            "Jin, S. et al. (2019). Overall methodology design for the United States "
            "National Land Cover Database 2016 products. "
            "Remote Sensing 11(24), 2971. https://doi.org/10.3390/rs11242971"
        ),
        bibtex=(
            "@article{jin2019nlcd,\n"
            "  author  = {Jin, Suming and others},\n"
            "  title   = {Overall methodology design for the United States National Land Cover Database 2016 products},\n"
            "  journal = {Remote Sensing},\n"
            "  year    = {2019},\n"
            "  volume  = {11},\n"
            "  number  = {24},\n"
            "  pages   = {2971},\n"
            "  doi     = {10.3390/rs11242971}\n"
            "}"
        ),
        homepage="https://www.mrlc.gov/",
        requires_extras=["hyriver"],
        requires_auth=[],
        common_pitfalls=[
            "CONUS only.",
            "Available for discrete years (2001, 2004, 2006, 2008, 2011, 2013, 2016, 2019, 2021) — not continuous.",
            "Output is a categorical raster; never use numeric mean on class codes.",
            "Pass `year` in backend_config; defaults to 2019.",
        ],
        examples=[
            "fetch('landcover', gdf, '2019-01-01', '2019-12-31')  # auto → NLCD in CONUS",
            "fetch('landcover', gdf, '2016-01-01', '2016-12-31', mode='manual', product='NLCD')",
        ],
        next_steps=_LC_NEXT_STEPS,
        backend_config={
            "pygeohydro_product": "nlcd",
            "default_year": 2019,
        },
    ),

    ProductSpec(
        id="ESA_WORLDCOVER",
        variable="landcover",
        source="gee",
        source_dataset_id="ESA/WorldCover/v200",
        coverage=["global"],
        temporal_start="2021-01-01",
        temporal_end="2022-01-01",       # v200 covers 2020–2021
        resolution_m=10,
        timestep="static",
        units="class",
        license="CC BY 4.0 (ESA WorldCover)",
        citation=(
            "Zanaga, D. et al. (2022). ESA WorldCover 10 m 2021 v200. "
            "https://doi.org/10.5281/zenodo.7254221"
        ),
        bibtex=(
            "@dataset{zanaga2022worldcover,\n"
            "  author    = {Zanaga, D. and others},\n"
            "  title     = {{ESA WorldCover} 10 m 2021 v200},\n"
            "  year      = {2022},\n"
            "  doi       = {10.5281/zenodo.7254221}\n"
            "}"
        ),
        homepage="https://esa-worldcover.org/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Only covers 2020–2021 (v200). Annual updates expected.",
            "Global classification uncertainty varies by biome.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('landcover', gdf, '2021-01-01', '2021-12-31', mode='manual', product='ESA_WORLDCOVER')",
        ],
        next_steps=_LC_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "ESA/WorldCover/v200",
            "band": "Map",
            "scale_m": 10,
            "categorical": True,
            # WorldCover v200 is an ImageCollection of tiles (one per UTM zone).
            # Flag as static so gee.fetch_raster uses the mosaic() → select() path,
            # and as a collection so the driver calls ImageCollection.mosaic() first.
            "static": True,
            "gee_is_collection": True,
        },
    ),

    ProductSpec(
        id="DYNAMIC_WORLD",
        variable="landcover",
        source="gee",
        source_dataset_id="GOOGLE/DYNAMICWORLD/V1",
        coverage=["global"],
        temporal_start="2015-06-23",    # Sentinel-2 start
        temporal_end="present",
        resolution_m=10,
        timestep="static",               # treated as a categorical raster mosaic — fetch_raster only
        units="class",
        license="CC BY 4.0 (Google / WRI)",
        citation=(
            "Brown, C. F. et al. (2022). Dynamic World, Near real-time global 10 m "
            "land use land cover mapping. Scientific Data 9, 251. "
            "https://doi.org/10.1038/s41597-022-01307-4"
        ),
        bibtex=(
            "@article{brown2022dynamicworld,\n"
            "  author  = {Brown, Christopher F. and others},\n"
            "  title   = {Dynamic {World}, near real-time global 10 m land use land cover mapping},\n"
            "  journal = {Scientific Data},\n"
            "  year    = {2022},\n"
            "  volume  = {9},\n"
            "  pages   = {251},\n"
            "  doi     = {10.1038/s41597-022-01307-4}\n"
            "}"
        ),
        homepage="https://dynamicworld.app/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Individual scenes may be cloudy; composite a date range for a stable map.",
            "Label class is the argmax of per-pixel probability — use with caution in heterogeneous areas.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('landcover', gdf, '2022-01-01', '2022-12-31', mode='manual', product='DYNAMIC_WORLD')",
        ],
        next_steps=_LC_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "GOOGLE/DYNAMICWORLD/V1",
            "band": "label",
            "scale_m": 10,
            "categorical": True,
            "composite_method": "mode",   # mode composite over the date range
            # NOTE: fetch_raster for this product requires a temporal mode-composite
            # over the requested date range (not a static mosaic). That path is not
            # yet implemented in the GEE driver. DYNAMIC_WORLD is therefore NOT in
            # the default routing chain — it can only be called with mode='manual'.
            # When the driver is extended, remove this note and re-add to policy.py.
            "_not_routed": True,
        },
    ),

    # Auth-free alternative to ESA_WORLDCOVER via Planetary Computer STAC.
    # Same dataset, different access path — no GEE auth needed.
    ProductSpec(
        id="ESA_WORLDCOVER_STAC",
        variable="landcover",
        source="stac",
        source_dataset_id="esa-worldcover",
        coverage=["global"],
        temporal_start="2020-01-01",
        temporal_end="2021-12-31",
        resolution_m=10,
        timestep="static",
        units="class_code",
        license="CC BY 4.0 (ESA WorldCover)",
        citation=(
            "Zanaga, D. et al. (2022). ESA WorldCover 10 m 2021 v200. "
            "Distributed by Microsoft Planetary Computer. "
            "https://doi.org/10.5281/zenodo.7254221"
        ),
        bibtex=(
            "@dataset{zanaga2022worldcoverstac,\n"
            "  author    = {Zanaga, Daniele and others},\n"
            "  title     = {{ESA WorldCover} 10 m 2021 v200},\n"
            "  publisher = {Microsoft Planetary Computer},\n"
            "  year      = {2022},\n"
            "  doi       = {10.5281/zenodo.7254221}\n"
            "}"
        ),
        homepage="https://planetarycomputer.microsoft.com/dataset/esa-worldcover",
        requires_extras=["stac"],
        requires_auth=[],
        common_pitfalls=[
            "Auth-free alternative to ESA_WORLDCOVER (GEE) — same data, no Earth Engine needed.",
            "Two epochs available: 2020 (v100) and 2021 (v200). Backend picks the newest.",
            "Requires `pip install aihydro-data[stac]`.",
        ],
        examples=[
            "fetch('landcover', gdf, '2021-01-01', '2021-12-31', mode='manual', product='ESA_WORLDCOVER_STAC')",
        ],
        next_steps=_LC_NEXT_STEPS,
        backend_config={
            "stac_endpoint": "https://planetarycomputer.microsoft.com/api/stac/v1",
            "stac_collection": "esa-worldcover",
            "stac_asset": "map",
            "stac_resolution": 10,
            "static": True,
            "categorical": True,
        },
    ),
]
