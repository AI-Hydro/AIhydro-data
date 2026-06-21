"""
Geology product registry.

IDs:
    PYGEOGLIM_ALL   – GLiM lithology + GLHYMPS hydrogeology (combined; default auto-mode)
    GLIM_TILES      – GLiM lithology only  (5 attributes)
    GLHYMPS_TILES   – GLHYMPS hydrogeology only  (4 attributes)

Public-release gate: pygeoglim enforces a machine-readable permission gate
(``public_release_allowed=false`` in the tile manifest) that fails-closed
until CCGM grants redistribution permission for GLiM.  Engineering can fetch
from private/local tiles by passing ``PYGEOGLIM_HF_TOKEN`` in the environment
or setting ``offline=True`` after a manual prefetch.
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_GEOLOGY_NEXT_STEPS = [
    {"tool": "extract_hydrological_signatures", "rationale": "Geology drives baseflow index and recession behaviour."},
    {"tool": "fetch", "rationale": "Pair with soil, landcover and streamflow for a CAMELS-style attribute vector."},
    {"tool": "data_describe_product", "rationale": "Surface GLiM / GLHYMPS citations for the provenance record."},
]

_GLIM_CITATION = (
    "Hartmann, J. & Moosdorf, N. (2012). The new global lithological map database "
    "GLiM: A representation of rock properties at the Earth surface. "
    "Geochemistry, Geophysics, Geosystems 13(12). "
    "https://doi.org/10.1029/2012GC004370"
)
_GLHYMPS_CITATION = (
    "Gleeson, T., Moosdorf, N., Hartmann, J. & van Beek, L.P.H. (2014). "
    "A glimpse beneath earth's surface: GLobal HYdrogeology MaPS (GLHYMPS) of "
    "permeability and porosity. Geophysical Research Letters 41(11), 3891–3898. "
    "https://doi.org/10.1002/2014GL059856"
)
_COMBINED_CITATION = f"{_GLIM_CITATION}\n{_GLHYMPS_CITATION}"

_GLIM_BIBTEX = (
    "@article{hartmann2012glim,\n"
    "  author  = {Hartmann, J{\\\"o}rg and Moosdorf, Nils},\n"
    "  title   = {The new global lithological map database {GLiM}: A representation of rock properties at the {E}arth surface},\n"
    "  journal = {Geochemistry, Geophysics, Geosystems},\n"
    "  year    = {2012},\n"
    "  volume  = {13},\n"
    "  number  = {12},\n"
    "  doi     = {10.1029/2012GC004370}\n"
    "}"
)
_GLHYMPS_BIBTEX = (
    "@article{gleeson2014glhymps,\n"
    "  author  = {Gleeson, Tom and Moosdorf, Nils and Hartmann, J{\\\"o}rg and van Beek, Ludovicus P. H.},\n"
    "  title   = {A glimpse beneath earth's surface: {GLobal HYdrogeology MaPS} ({GLHYMPS}) of permeability and porosity},\n"
    "  journal = {Geophysical Research Letters},\n"
    "  year    = {2014},\n"
    "  volume  = {41},\n"
    "  number  = {11},\n"
    "  pages   = {3891--3898},\n"
    "  doi     = {10.1002/2014GL059856}\n"
    "}"
)

_COMMON_PITFALLS = [
    "GLiM redistribution requires CCGM written permission. The tile manifest "
    "enforces a public_release_allowed gate — tiles are available for private "
    "research but cannot be served publicly until CCGM grants permission.",
    "Set PYGEOGLIM_HF_TOKEN env var (HuggingFace token) for private-repo tiles, "
    "or run `python -m pygeoglim.cli prefetch --region CONUS` to cache locally.",
    "Global tiles are placeholders in pygeoglim 1.x — CONUS tiles are the only "
    "production-grade data; other regions raise GeologyError until Wave B3 tiles ship.",
    "Geometry must be in EPSG:4326 (WGS84). Pass a shapely Polygon or GeoDataFrame.",
    "result.data is a pd.DataFrame of scalar attributes (one row), NOT a timeseries.",
]


PRODUCTS: list[ProductSpec] = [

    ProductSpec(
        id="PYGEOGLIM_ALL",
        variable="geology",
        source="pygeoglim",
        source_dataset_id="glim+glhymps",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=0,         # vector polygons, not a grid
        timestep="static",
        units="varies",         # fractions, log10(m²), m/s
        spatial_support="areal",
        license=(
            "GLiM: CC BY 4.0 (Hartmann & Moosdorf 2012). "
            "GLHYMPS: CC BY 4.0 (Gleeson et al. 2014). "
            "Redistribution of raw tiles requires CCGM written permission."
        ),
        citation=_COMBINED_CITATION,
        bibtex=_GLIM_BIBTEX + "\n" + _GLHYMPS_BIBTEX,
        homepage="https://www.clisap.de/research/a:-climate-feedback-and-sensitivity/crg-southern-ocean-carbon-and-climate/glim/",
        requires_extras=["pygeoglim"],
        requires_auth=[],
        common_pitfalls=_COMMON_PITFALLS,
        examples=[
            "fetch('geology', gdf, '2020-01-01', '2020-12-31')  # → all 9 geology attributes",
            "fetch('geology', gdf, '2020-01-01', '2020-12-31', mode='manual', product='PYGEOGLIM_ALL')",
        ],
        next_steps=_GEOLOGY_NEXT_STEPS,
        backend_config={
            "fetch_glim": True,
            "fetch_glhymps": True,
        },
    ),

    ProductSpec(
        id="GLIM_TILES",
        variable="geology",
        source="pygeoglim",
        source_dataset_id="glim",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=0,
        timestep="static",
        units="fraction / lithology class",
        spatial_support="areal",
        license="CC BY 4.0 (Hartmann & Moosdorf 2012). Redistribution requires CCGM written permission.",
        citation=_GLIM_CITATION,
        bibtex=_GLIM_BIBTEX,
        homepage="https://www.clisap.de/research/a:-climate-feedback-and-sensitivity/crg-southern-ocean-carbon-and-climate/glim/",
        requires_extras=["pygeoglim"],
        requires_auth=[],
        common_pitfalls=_COMMON_PITFALLS,
        examples=[
            "fetch('geology', gdf, '2020-01-01', '2020-12-31', mode='manual', product='GLIM_TILES')",
            "# Returns: geol_1st_class, glim_1st_class_frac, geol_2nd_class, glim_2nd_class_frac, carbonate_rocks_frac",
        ],
        next_steps=_GEOLOGY_NEXT_STEPS,
        backend_config={
            "fetch_glim": True,
            "fetch_glhymps": False,
        },
    ),

    ProductSpec(
        id="GLHYMPS_TILES",
        variable="geology",
        source="pygeoglim",
        source_dataset_id="glhymps",
        coverage=["global"],
        temporal_start="",
        temporal_end="",
        resolution_m=0,
        timestep="static",
        units="fraction / log10(m²) / m/s",
        spatial_support="areal",
        license="CC BY 4.0 (Gleeson et al. 2014). Redistribution requires CCGM written permission.",
        citation=_GLHYMPS_CITATION,
        bibtex=_GLHYMPS_BIBTEX,
        homepage="https://www.hydroshare.org/resource/4bbf8eed2f4b4f7a9e5a3d0d3e3f5c9e/",
        requires_extras=["pygeoglim"],
        requires_auth=[],
        common_pitfalls=_COMMON_PITFALLS,
        examples=[
            "fetch('geology', gdf, '2020-01-01', '2020-12-31', mode='manual', product='GLHYMPS_TILES')",
            "# Returns: geol_porosity, geol_permeability, geol_permeability_linear, hydraulic_conductivity",
        ],
        next_steps=_GEOLOGY_NEXT_STEPS,
        backend_config={
            "fetch_glim": False,
            "fetch_glhymps": True,
        },
    ),
]
