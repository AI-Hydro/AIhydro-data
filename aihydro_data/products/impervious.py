"""
Impervious surface product registry.

IDs:
    NLCD_IMPERVIOUS     – NLCD % impervious, CONUS 30 m, HyRiver
    GHSL_BUILT_UP       – Global Human Settlement Layer built-up fraction, global 100 m, GEE
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_IMP_NEXT_STEPS = [
    {"tool": "extract_hydrological_signatures", "rationale": "Impervious cover ready — compute urban runoff ratio or effective CN."},
    {"tool": "data_describe_product", "rationale": "Include citation before using impervious data in publications."},
]

PRODUCTS: list[ProductSpec] = [

    # ── NLCD % impervious (CONUS, 30 m, HyRiver pygeohydro) ──────────────
    # Fractional impervious cover (0–100 %) at 30 m from NLCD. Critical for the
    # urban HRU story in HYDRO-ATOMS: % impervious is the continuous urbanisation
    # attribute that drives the crop→urban FHV gap (R5 impervious fast-path).
    # Available for discrete NLCD epochs (2001, 2004, 2006, 2008, 2011, 2013,
    # 2016, 2019, 2021) — backend picks the epoch closest to the request window.
    ProductSpec(
        id="NLCD_IMPERVIOUS",
        variable="impervious",
        source="hyriver",
        source_dataset_id="nlcd_impervious",
        coverage=["CONUS"],
        temporal_start="2001-01-01",
        temporal_end="2021-01-01",
        resolution_m=30,
        timestep="static",
        units="percent",
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
            "Discrete years only (2001, 2004, 2006, 2008, 2011, 2013, 2016, 2019, 2021).",
            "Values are 0–100 (percent, NOT fraction). Divide by 100 for fractional impervious.",
            "Urban/suburban pixels can be 80–95 %; undeveloped forest/agriculture near 0 %.",
            "Pass year explicitly via backend_config if you need a specific epoch.",
        ],
        examples=[
            "fetch('impervious', gdf, '2019-01-01', '2019-12-31')  # CONUS → NLCD 2019",
            "fetch('impervious', gdf, '2019-01-01', '2019-12-31', mode='manual', product='NLCD_IMPERVIOUS')",
        ],
        next_steps=_IMP_NEXT_STEPS,
        backend_config={
            "pygeohydro_product": "nlcd",
            "nlcd_layer": "impervious",
            "default_year": 2019,
        },
    ),

    # ── GHSL Built-Up Fraction (global, 100 m, GEE) ───────────────────────
    # European Commission JRC Global Human Settlement Layer (GHSL) built-up
    # surface fraction. Derived from Sentinel-2 and Landsat mosaics at 10 m and
    # downscaled to 100 m. Covers the full globe and multiple epochs (1975–2030
    # in 5-yr intervals). Use when NLCD is unavailable (non-CONUS basins).
    ProductSpec(
        id="GHSL_BUILT_UP",
        variable="impervious",
        source="gee",
        source_dataset_id="JRC/GHSL/P2023A/GHS_BUILT_S",
        coverage=["global"],
        temporal_start="1975-01-01",
        temporal_end="2030-01-01",
        resolution_m=100,
        timestep="static",
        units="m2/100m2",
        license="CC BY 4.0 (European Commission JRC)",
        citation=(
            "Pesaresi, M. et al. (2023). GHS-BUILT-S R2023A — GHS built-up surface grid, "
            "derived from Sentinel-2 composite and Landsat, multitemporal (1975-2030). "
            "European Commission Joint Research Centre. https://doi.org/10.2905/9A1B0013-70E6-4223-BE44-C1291F1B4B05"
        ),
        bibtex=(
            "@dataset{pesaresi2023ghsl,\n"
            "  author    = {Pesaresi, Martino and others},\n"
            "  title     = {{GHS-BUILT-S R2023A} — GHS built-up surface grid, multitemporal 1975-2030},\n"
            "  publisher = {European Commission JRC},\n"
            "  year      = {2023},\n"
            "  doi       = {10.2905/9A1B0013-70E6-4223-BE44-C1291F1B4B05}\n"
            "}"
        ),
        homepage="https://ghsl.jrc.ec.europa.eu/",
        requires_extras=["gee"],
        requires_auth=["gee"],
        common_pitfalls=[
            "Units are m² built-up per 100 m² cell — divide by 100 for fraction (0–1).",
            "Epochs in 5-year steps: 1975, 1980, 1985, 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2025, 2030.",
            "GEE auth required.",
        ],
        examples=[
            "fetch('impervious', gdf, '2020-01-01', '2020-12-31', mode='manual', product='GHSL_BUILT_UP')",
        ],
        next_steps=_IMP_NEXT_STEPS,
        backend_config={
            "gee_dataset_id": "JRC/GHSL/P2023A/GHS_BUILT_S",
            "band": "built_surface",
            "scale_m": 100,
            "unit_conversion": 1.0,   # m²/100m² — caller divides by 100 for fraction
            "static": True,
        },
    ),
]
