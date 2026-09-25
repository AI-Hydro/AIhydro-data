"""
Population product registry.

IDs:
    GHSL_POP       – Global Human Settlement Layer population, global 100 m, GEE
    WORLDPOP       – WorldPop unconstrained population count, global 100 m, GEE
    GPW_V411       – Gridded Population of the World v4.11, global ~1 km, GEE

Added 2026-08-27 while screening Himalayan debris-flow corridors for downstream
exposure. Every product here is a *count per pixel*, so basin totals require
``aggregation="basin_sum"``; a basin_mean returns people per pixel, which is
almost never what a caller wants. The specs say so in ``common_pitfalls``, and
those now travel to the caller on FetchResult.
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_POP_NEXT_STEPS = [
    {"tool": "data_describe_product",
     "rationale": "Check the year and the constrained/unconstrained distinction before publishing exposure numbers."},
]

_COUNT_PITFALLS = [
    "Values are people PER PIXEL, not a density. Use aggregation='basin_sum' for a "
    "catchment total; basin_mean returns average people per pixel and is meaningless "
    "as an exposure figure.",
    "Modelled estimates disaggregated from census units, not observations. Errors in "
    "mountainous and sparsely settled terrain are large and are not reported per pixel.",
    "Totals are sensitive to the polygon edge. A corridor buffer that clips a settlement "
    "in half will halve its population.",
]

PRODUCTS: list[ProductSpec] = [

    # ── GHSL population (global, 100 m, 5-yearly 1975–2030) ──────────────
    # Preferred default: consistent 1975–2030 series, epochs beyond the present
    # are projections. Built by JRC from census and built-up surface.
    ProductSpec(
        id="GHSL_POP",
        variable="population",
        source="gee",
        source_dataset_id="JRC/GHSL/P2023A/GHS_POP",
        coverage=["global"],
        temporal_start="1975-01-01",
        temporal_end="2030-01-01",
        resolution_m=100,
        timestep="5-yearly",
        units="people/pixel",
        license="CC BY 4.0 (European Commission, JRC)",
        citation=(
            "Schiavina, M., Freire, S., Carioli, A., MacManus, K. (2023). "
            "GHS-POP R2023A - GHS population grid multitemporal (1975-2030). "
            "European Commission, Joint Research Centre. "
            "https://doi.org/10.2905/2FF68A52-5B5B-4A22-8F40-C41DA8332CFE"
        ),
        bibtex=(
            "@misc{schiavina2023ghspop,\n"
            "  author = {Schiavina, Marcello and Freire, Sergio and Carioli, Alessandra and MacManus, Kytt},\n"
            "  title  = {GHS-POP R2023A: GHS population grid multitemporal (1975-2030)},\n"
            "  year   = {2023},\n"
            "  publisher = {European Commission, Joint Research Centre},\n"
            "  doi    = {10.2905/2FF68A52-5B5B-4A22-8F40-C41DA8332CFE}\n"
            "}"
        ),
        homepage="https://human-settlement.emergency.copernicus.eu/",
        requires_extras=["gee"],
        requires_auth=["earthengine"],
        common_pitfalls=_COUNT_PITFALLS + [
            "Epochs are 5-yearly. Requests are served by the nearest epoch, not interpolated.",
            "Epochs after the present are projections, not estimates of observed population.",
        ],
        examples=[
            "fetch('population', gdf, '2025-01-01', '2025-12-31', aggregation='basin_sum')",
        ],
        next_steps=_POP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "JRC/GHSL/P2023A/GHS_POP",
            "band": "population_count",
            "scale_m": 100,
            "epoch_years": [1975, 1980, 1985, 1990, 1995, 2000, 2005,
                            2010, 2015, 2020, 2025, 2030],
            "static": False,
        },
    ),

    # ── WorldPop unconstrained count (global, 100 m, annual 2000–2020) ───
    ProductSpec(
        id="WORLDPOP",
        variable="population",
        source="gee",
        source_dataset_id="WorldPop/GP/100m/pop",
        coverage=["global"],
        temporal_start="2000-01-01",
        temporal_end="2020-12-31",
        resolution_m=100,
        timestep="annual",
        units="people/pixel",
        license="CC BY 4.0 (WorldPop, University of Southampton)",
        citation=(
            "Tatem, A.J. (2017). WorldPop, open data for spatial demography. "
            "Scientific Data 4, 170004. https://doi.org/10.1038/sdata.2017.4"
        ),
        bibtex=(
            "@article{tatem2017worldpop,\n"
            "  author  = {Tatem, Andrew J.},\n"
            "  title   = {WorldPop, open data for spatial demography},\n"
            "  journal = {Scientific Data},\n"
            "  year    = {2017},\n"
            "  volume  = {4},\n"
            "  pages   = {170004},\n"
            "  doi     = {10.1038/sdata.2017.4}\n"
            "}"
        ),
        homepage="https://www.worldpop.org/",
        requires_extras=["gee"],
        requires_auth=["earthengine"],
        common_pitfalls=_COUNT_PITFALLS + [
            "This is the UNCONSTRAINED product: it can place population where built-up "
            "area does not exist. The constrained product is more conservative in "
            "sparsely settled terrain but has a shorter record.",
            "Record ends in 2020; later requests are served by the 2020 layer.",
        ],
        examples=[
            "fetch('population', gdf, '2020-01-01', '2020-12-31', aggregation='basin_sum', "
            "mode='manual', product='WORLDPOP')",
        ],
        next_steps=_POP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "WorldPop/GP/100m/pop",
            "band": "population",
            "scale_m": 100,
            "static": False,
        },
    ),

    # ── GPW v4.11 (global, ~927 m, 5-yearly 2000–2020) ───────────────────
    # Last-resort fallback: coarse, but a straightforward census disaggregation
    # with no built-up modelling, so it fails differently from the other two.
    ProductSpec(
        id="GPW_V411",
        variable="population",
        source="gee",
        source_dataset_id="CIESIN/GPWv411/GPW_Population_Count",
        coverage=["global"],
        temporal_start="2000-01-01",
        temporal_end="2020-12-31",
        resolution_m=927,
        timestep="5-yearly",
        units="people/pixel",
        license="CC BY 4.0 (CIESIN, Columbia University)",
        citation=(
            "Center for International Earth Science Information Network (CIESIN), "
            "Columbia University (2018). Gridded Population of the World, Version 4 "
            "(GPWv4): Population Count, Revision 11. NASA SEDAC. "
            "https://doi.org/10.7927/H4JW8BX5"
        ),
        bibtex=(
            "@misc{ciesin2018gpw411,\n"
            "  author = {{Center for International Earth Science Information Network}},\n"
            "  title  = {Gridded Population of the World, Version 4 (GPWv4): Population Count, Revision 11},\n"
            "  year   = {2018},\n"
            "  publisher = {NASA Socioeconomic Data and Applications Center},\n"
            "  doi    = {10.7927/H4JW8BX5}\n"
            "}"
        ),
        homepage="https://sedac.ciesin.columbia.edu/data/collection/gpw-v4",
        requires_extras=["gee"],
        requires_auth=["earthengine"],
        common_pitfalls=_COUNT_PITFALLS + [
            "About 927 m at the equator. Too coarse to resolve individual villages; a "
            "small catchment may fall inside a single cell.",
        ],
        examples=[
            "fetch('population', gdf, '2020-01-01', '2020-12-31', aggregation='basin_sum', "
            "mode='manual', product='GPW_V411')",
        ],
        next_steps=_POP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "CIESIN/GPWv411/GPW_Population_Count",
            "band": "population_count",
            "scale_m": 927,
            "static": False,
        },
    ),
]
