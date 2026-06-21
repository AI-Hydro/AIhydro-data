"""
Digital elevation model product registry.

IDs:
    GLO30           – Copernicus GLO-30 global 30 m DEM, GEE
    SRTM            – USGS/NASA SRTM 30 m global DEM, GEE
    DEM3DEP_10M     – USGS 3DEP 10 m CONUS DEM, HyRiver (py3dep)
    MERIT_DEM       – MERIT-DEM 90 m global hydrologically-conditioned, GEE
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_DEM_NEXT_STEPS = [
    {"tool": "compute_twi", "rationale": "DEM ready — compute Topographic Wetness Index."},
    {"tool": "extract_geomorphic_parameters", "rationale": "DEM ready — extract slope, aspect, hypsometry."},
    {"tool": "delineate_watershed", "rationale": "DEM ready — delineate watershed boundaries."},
]

PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="GLO30",
        variable="dem",
        source="gee",
        source_dataset_id="COPERNICUS/DEM/GLO30",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=30,
        timestep="static",
        units="m",
        license="Copernicus DEM License (open use with attribution)",
        citation=(
            "European Space Agency and Airbus (2022). Copernicus Global Digital Elevation Model. "
            "https://doi.org/10.5270/ESA-c5d3d65"
        ),
        bibtex=(
            "@dataset{esa2022glo30,\n"
            "  author    = {{European Space Agency} and {Airbus}},\n"
            "  title     = {Copernicus Global Digital Elevation Model},\n"
            "  year      = {2022},\n"
            "  doi       = {10.5270/ESA-c5d3d65}\n"
            "}"
        ),
        homepage="https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Void filling over water bodies and some high-slope areas.",
            "GEE asset is tiled — large queries take longer.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('dem', gdf, '', '')  # auto → GLO-30 outside CONUS",
            "fetch('dem', gdf, '', '', mode='manual', product='GLO30')",
        ],
        next_steps=_DEM_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "COPERNICUS/DEM/GLO30",
            "band": "DEM",
            "scale_m": 30,
            "static": True,
            "gee_is_collection": True,   # mosaic of 1°×1° tiles
        },
    ),

    ProductSpec(
        id="SRTM",
        variable="dem",
        source="gee",
        source_dataset_id="USGS/SRTMGL1_003",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=30,
        timestep="static",
        units="m",
        license="public domain (NASA / USGS)",
        citation=(
            "Farr, T. G. et al. (2007). The Shuttle Radar Topography Mission. "
            "Reviews of Geophysics 45(2). https://doi.org/10.1029/2005RG000183"
        ),
        bibtex=(
            "@article{farr2007srtm,\n"
            "  author  = {Farr, Tom G. and others},\n"
            "  title   = {The Shuttle Radar Topography Mission},\n"
            "  journal = {Reviews of Geophysics},\n"
            "  year    = {2007},\n"
            "  volume  = {45},\n"
            "  number  = {2},\n"
            "  doi     = {10.1029/2005RG000183}\n"
            "}"
        ),
        homepage="https://www2.jpl.nasa.gov/srtm/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Void filling artifacts in steep or vegetated areas.",
            "Coverage: 60°N to 56°S only.",
            "GEE auth required.",
        ],
        examples=["fetch('dem', gdf, '', '', mode='manual', product='SRTM')"],
        next_steps=_DEM_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "USGS/SRTMGL1_003",
            "band": "elevation",
            "scale_m": 30,
            "static": True,
        },
    ),

    ProductSpec(
        id="DEM3DEP_10M",
        variable="dem",
        source="hyriver",
        source_dataset_id="DEM",
        coverage=["CONUS"],
        temporal_start="",
        temporal_end="",
        resolution_m=10,
        timestep="static",
        units="m",
        license="public domain (USGS)",
        citation=(
            "U.S. Geological Survey (2019). 1 Arc-second Digital Elevation Models (DEMs): "
            "USGS National Map 3DEP Downloadable Data Collection. "
            "https://doi.org/10.5066/F7DF6PQS"
        ),
        bibtex=(
            "@dataset{usgs20193dep,\n"
            "  author    = {{U.S. Geological Survey}},\n"
            "  title     = {1 Arc-second Digital Elevation Models: USGS National Map 3DEP},\n"
            "  year      = {2019},\n"
            "  doi       = {10.5066/F7DF6PQS}\n"
            "}"
        ),
        homepage="https://www.usgs.gov/3d-elevation-program",
        requires_extras=["hyriver"],
        common_pitfalls=[
            "CONUS only (partial Alaska / Hawaii coverage).",
            "py3dep required — `pip install aihydro-data[hyriver]`.",
        ],
        examples=["fetch('dem', gdf, '', '')  # auto → 3DEP in CONUS"],
        next_steps=_DEM_NEXT_STEPS,
        backend_config={
            "py3dep_resolution": 10,
            "py3dep_product": "DEM",
        },
    ),

    ProductSpec(
        id="MERIT_DEM",
        variable="dem",
        source="gee",
        source_dataset_id="MERIT/DEM/v1_0_3",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=90,
        timestep="static",
        units="m",
        license="CC BY-NC 4.0 (Yamazaki et al.)",
        citation=(
            "Yamazaki, D. et al. (2017). A high-accuracy map of global terrain elevations. "
            "Geophysical Research Letters 44(11), 5844–5853. "
            "https://doi.org/10.1002/2017GL072874"
        ),
        bibtex=(
            "@article{yamazaki2017merit,\n"
            "  author  = {Yamazaki, Dai and others},\n"
            "  title   = {A high-accuracy map of global terrain elevations},\n"
            "  journal = {Geophysical Research Letters},\n"
            "  year    = {2017},\n"
            "  volume  = {44},\n"
            "  number  = {11},\n"
            "  pages   = {5844--5853},\n"
            "  doi     = {10.1002/2017GL072874}\n"
            "}"
        ),
        homepage="http://hydro.iis.u-tokyo.ac.jp/~yamadai/MERIT_DEM/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Non-commercial use only — verify license before distribution.",
            "Hydrologically conditioned — stream-burned; better for flow routing than GLO-30.",
            "GEE auth required.",
        ],
        examples=["fetch('dem', gdf, '', '', mode='manual', product='MERIT_DEM')"],
        next_steps=_DEM_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "MERIT/DEM/v1_0_3",
            "band": "dem",
            "scale_m": 90,
            "static": True,
            "gee_is_collection": False,  # single Image asset (not a collection)
        },
    ),

    # Auth-free alternative to GLO30 via Planetary Computer STAC. Same dataset
    # (Copernicus DEM GLO-30) but served as COG, accessible without GEE.
    # The STAC backend automatically falls back to Element84 on PC timeout.
    ProductSpec(
        id="GLO30_STAC",
        variable="dem",
        source="stac",
        source_dataset_id="cop-dem-glo-30",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=30,
        timestep="static",
        units="m",
        license="ESA Copernicus License (free, attribution required)",
        citation=(
            "European Space Agency (2021). Copernicus Global Digital Elevation Model. "
            "Distributed by Microsoft Planetary Computer / OpenTopography. "
            "https://doi.org/10.5069/G9028PQB"
        ),
        bibtex=(
            "@dataset{esa2021copdem,\n"
            "  author    = {{European Space Agency}},\n"
            "  title     = {Copernicus Global Digital Elevation Model},\n"
            "  publisher = {Microsoft Planetary Computer},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5069/G9028PQB}\n"
            "}"
        ),
        homepage="https://planetarycomputer.microsoft.com/dataset/cop-dem-glo-30",
        requires_extras=["stac"],
        requires_auth=[],
        common_pitfalls=[
            "Primary endpoint is Planetary Computer; backend auto-falls-back to "
            "Element84 Earth Search on timeout.",
            "COG-backed: pulls only the tiles intersecting your geometry.",
            "Requires `pip install aihydro-data[stac]`.",
        ],
        examples=[
            "fetch('dem', gdf, '', '', mode='manual', product='GLO30_STAC')",
        ],
        next_steps=_DEM_NEXT_STEPS,
        backend_config={
            "stac_endpoint": "https://planetarycomputer.microsoft.com/api/stac/v1",
            "stac_collection": "cop-dem-glo-30",
            "stac_asset": "data",
            "stac_resolution": 30,
            "static": True,
            # Explicit fallback list — stac.py also injects Element84 automatically
            # when primary == PC, but listing it here is self-documenting.
            "stac_fallback_endpoints": [
                "https://earth-search.aws.element84.com/v1",
            ],
        },
    ),

    # Same Copernicus DEM GLO-30 data, served via Element84 Earth Search (AWS).
    # Completely independent infrastructure from Planetary Computer — useful when
    # PC times out and as an explicit manual override.
    ProductSpec(
        id="GLO30_ELEMENT84",
        variable="dem",
        source="stac",
        source_dataset_id="cop-dem-glo-30",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=30,
        timestep="static",
        units="m",
        license="ESA Copernicus License (free, attribution required)",
        citation=(
            "European Space Agency (2021). Copernicus Global Digital Elevation Model. "
            "Distributed via Element84 Earth Search (AWS). "
            "https://doi.org/10.5270/ESA-c5d3d65"
        ),
        bibtex=(
            "@dataset{esa2021copdem_es,\n"
            "  author    = {{European Space Agency}},\n"
            "  title     = {Copernicus Global Digital Elevation Model},\n"
            "  publisher = {Element84 Earth Search},\n"
            "  year      = {2021},\n"
            "  doi       = {10.5270/ESA-c5d3d65}\n"
            "}"
        ),
        homepage="https://earth-search.aws.element84.com/v1/collections/cop-dem-glo-30",
        requires_extras=["stac"],
        requires_auth=[],
        common_pitfalls=[
            "Independent infrastructure from Planetary Computer — use when PC is down.",
            "No token signing required; assets are public AWS S3 COGs.",
        ],
        examples=[
            "fetch('dem', gdf, '', '', mode='manual', product='GLO30_ELEMENT84')",
        ],
        next_steps=_DEM_NEXT_STEPS,
        backend_config={
            "stac_endpoint": "https://earth-search.aws.element84.com/v1",
            "stac_collection": "cop-dem-glo-30",
            "stac_asset": "data",
            "stac_resolution": 30,
            "static": True,
        },
    ),
]
