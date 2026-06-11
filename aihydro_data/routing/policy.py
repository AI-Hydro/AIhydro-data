"""
Declarative routing policy.

PRODUCT_POLICY maps (variable, region) → ordered list of product IDs.
The first entry is the primary source; subsequent entries are the fallback
chain. All IDs must match entries in products/<variable>.py PRODUCTS lists.

Rules:
- Adding a new product = adding a row here + a ProductSpec in products/.
- Editing routing logic = never. This table IS the logic.
- "global" entries are the ultimate fallback for every region not listed.

Phase 2: precipitation vertical.
Phase 3: temperature, streamflow, landcover, soil.
"""
from __future__ import annotations

# (variable, region) → ordered product IDs (primary first)
PRODUCT_POLICY: dict[tuple[str, str], list[str]] = {

    # ── Precipitation ────────────────────────────────────────────────────────
    # CHIRPS_IRI is the auth-free OPeNDAP fallback (last in every chain).
    # It activates automatically when all GEE and HyRiver sources fail, e.g.
    # because GEE credentials are absent or quota is exhausted.
    ("precipitation", "CONUS"):         ["GRIDMET_PRECIP", "DAYMET_PRECIP", "CHIRPS", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "NORTH_AMERICA"): ["DAYMET_PRECIP", "CHIRPS", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "SOUTH_AMERICA"): ["CHIRPS", "IMERG_PRECIP", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "EUROPE"):        ["ERA5L_PRECIP", "CHIRPS", "CHIRPS_IRI"],
    ("precipitation", "AFRICA"):        ["CHIRPS", "IMERG_PRECIP", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "S_ASIA"):        ["CHIRPS", "IMERG_PRECIP", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "ASIA"):          ["ERA5L_PRECIP", "CHIRPS", "IMERG_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "OCEANIA"):       ["CHIRPS", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "global"):        ["CHIRPS", "IMERG_PRECIP", "ERA5L_PRECIP", "CHIRPS_IRI"],

    # ── Temperature (max) ─────────────────────────────────────────────────
    # OPEN_METEO_TMAX is the auth-free non-GEE fallback (last in every chain).
    # It activates automatically when GEE is unavailable (no credentials, quota
    # exhausted, etc.). Centroid-based ~25 km ERA5 resolution — good enough for
    # basin-mean forcing even when GEE can't serve a finer-resolution product.
    ("tmax", "CONUS"):                  ["GRIDMET_TMAX", "DAYMET_TMAX", "ERA5L_TMAX", "OPEN_METEO_TMAX"],
    ("tmax", "NORTH_AMERICA"):          ["DAYMET_TMAX", "ERA5L_TMAX", "OPEN_METEO_TMAX"],
    ("tmax", "global"):                 ["ERA5L_TMAX", "OPEN_METEO_TMAX"],

    # ── Temperature (min) ─────────────────────────────────────────────────
    ("tmin", "CONUS"):                  ["GRIDMET_TMIN", "DAYMET_TMIN", "ERA5L_TMIN", "OPEN_METEO_TMIN"],
    ("tmin", "NORTH_AMERICA"):          ["DAYMET_TMIN", "ERA5L_TMIN", "OPEN_METEO_TMIN"],
    ("tmin", "global"):                 ["ERA5L_TMIN", "OPEN_METEO_TMIN"],

    # ── Temperature (mean) ────────────────────────────────────────────────
    ("tmean", "global"):                ["ERA5L_TMEAN"],

    # ── Streamflow ────────────────────────────────────────────────────────
    # CONUS prefers observed gauges (NWIS); then modelled global sources in
    # order of robustness: GEOGLOWS (open S3, no auth/queue, reach-level) →
    # Open-Meteo (instant REST GloFAS v4, availability cushion) → GloFAS/EWDS
    # (auth + async queue, last resort).
    ("streamflow", "CONUS"):            ["NWIS_STREAMFLOW", "GEOGLOWS_RETRO", "OPENMETEO_FLOOD", "GLOFAS_STREAMFLOW"],
    # Everywhere else: same modelled chain, no observed gauges.
    ("streamflow", "global"):           ["GEOGLOWS_RETRO", "OPENMETEO_FLOOD", "GLOFAS_STREAMFLOW"],

    # ── Land Cover ────────────────────────────────────────────────────────
    # DYNAMIC_WORLD removed from all chains: its GEE driver requires a temporal
    # mode-composite that is not yet implemented (fetch_raster only handles static
    # mosaics). ESA_WORLDCOVER (GEE ImageCollection mosaic) is the global primary;
    # ESA_WORLDCOVER_STAC (Planetary Computer) is the auth-free fallback.
    ("landcover", "CONUS"):             ["NLCD", "ESA_WORLDCOVER", "ESA_WORLDCOVER_STAC"],
    ("landcover", "NORTH_AMERICA"):     ["NLCD", "ESA_WORLDCOVER", "ESA_WORLDCOVER_STAC"],
    ("landcover", "global"):            ["ESA_WORLDCOVER", "ESA_WORLDCOVER_STAC"],

    # ── Soil ─────────────────────────────────────────────────────────────
    ("soil", "CONUS"):                  ["POLARIS", "SOILGRIDS"],
    ("soil", "global"):                 ["SOILGRIDS"],

    # ── Evapotranspiration ────────────────────────────────────────────────
    # OPEN_METEO_PET (FAO-56 ET0) is the auth-free non-GEE fallback for pet —
    # last in every chain so it only activates when ERA5L/MOD16 (GEE) fail.
    ("et",  "CONUS"):                   ["MOD16_ET",  "TERRACLIMATE_AET",  "ERA5L_PET"],
    ("et",  "global"):                  ["MOD16_ET",  "TERRACLIMATE_AET",  "ERA5L_PET"],
    ("pet", "CONUS"):                   ["GRIDMET_PET", "MOD16_PET", "ERA5L_PET", "OPEN_METEO_PET"],
    ("pet", "global"):                  ["ERA5L_PET", "MOD16_PET", "OPEN_METEO_PET"],

    # ── DEM ───────────────────────────────────────────────────────────────
    # DEM3DEP_10M (py3dep/HyRiver) moved to second position: its OGC WCS request
    # times out on polygon inputs > ~500 km² (benchmark: 2276 km² → TimeoutError).
    # GLO30 (GEE, 30 m) is reliable for any size and serves as the primary CONUS
    # source; DEM3DEP_10M remains in the chain for callers who pin it manually or
    # need 10 m resolution on small basins where it succeeds.
    ("dem", "CONUS"):                   ["GLO30", "DEM3DEP_10M", "SRTM", "GLO30_STAC"],
    ("dem", "NORTH_AMERICA"):           ["GLO30", "DEM3DEP_10M", "SRTM", "GLO30_STAC"],
    ("dem", "global"):                  ["GLO30", "SRTM", "MERIT_DEM", "GLO30_STAC"],

    # ── Soil Moisture ─────────────────────────────────────────────────────
    ("soil_moisture", "global"):        ["SMAP_SM"],
    ("soil_moisture", "CONUS"):         ["SMAP_SM"],

    # ── Vegetation ────────────────────────────────────────────────────────
    ("ndvi", "global"):                 ["MODIS_NDVI", "SENTINEL2_NDVI"],
    ("ndvi", "CONUS"):                  ["MODIS_NDVI", "SENTINEL2_NDVI"],
    ("lai",  "global"):                 ["MODIS_LAI"],

    # ── Optical surface reflectance (multi-band composites) ───────────────
    # Raw bands for local spectral-index computation (NDWI, MNDWI, NBR, …).
    # Sentinel-2 is primary (10 m); Landsat 9/8 fall back at 30 m with longer
    # historical record. The two *_STAC products are the no-size-limit escape
    # hatch: GEE's getDownloadURL caps synchronous exports at ~32 MB, so large
    # watersheds raise GEE_AREA_TOO_LARGE — which this chain catches and falls
    # through to stackstac (lazy COG reads, dask-chunked, no ceiling).
    ("optical", "global"):              ["SENTINEL2_SR", "LANDSAT9_SR", "LANDSAT8_SR", "SENTINEL2_SR_STAC", "LANDSAT_SR_STAC"],
    ("optical", "CONUS"):               ["SENTINEL2_SR", "LANDSAT9_SR", "LANDSAT8_SR", "SENTINEL2_SR_STAC", "LANDSAT_SR_STAC"],
}


def resolve_product_ids(variable: str, region: str) -> list[str]:
    """
    Return the ordered list of product IDs for (variable, region).

    Falls back through progressively broader region keys if the specific
    region isn't in the table:
      S_ASIA → ASIA → global
    """
    # Direct lookup
    key = (variable, region)
    if key in PRODUCT_POLICY:
        return list(PRODUCT_POLICY[key])

    # Try parent regions in a rough hierarchy
    parent: dict[str, str] = {
        "CONUS":         "NORTH_AMERICA",
        "NORTH_AMERICA": "global",
        "SOUTH_AMERICA": "global",
        "EUROPE":        "global",
        "AFRICA":        "global",
        "S_ASIA":        "ASIA",
        "ASIA":          "global",
        "OCEANIA":       "global",
    }
    current = region
    while current in parent:
        current = parent[current]
        key = (variable, current)
        if key in PRODUCT_POLICY:
            return list(PRODUCT_POLICY[key])

    # Final fallback: global
    global_key = (variable, "global")
    if global_key in PRODUCT_POLICY:
        return list(PRODUCT_POLICY[global_key])

    return []
