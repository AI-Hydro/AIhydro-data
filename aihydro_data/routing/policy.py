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
    ("tmax", "CONUS"):                  ["GRIDMET_TMAX", "DAYMET_TMAX", "ERA5L_TMAX"],
    ("tmax", "NORTH_AMERICA"):          ["DAYMET_TMAX", "ERA5L_TMAX"],
    ("tmax", "global"):                 ["ERA5L_TMAX"],

    # ── Temperature (min) ─────────────────────────────────────────────────
    ("tmin", "CONUS"):                  ["GRIDMET_TMIN", "DAYMET_TMIN", "ERA5L_TMIN"],
    ("tmin", "NORTH_AMERICA"):          ["DAYMET_TMIN", "ERA5L_TMIN"],
    ("tmin", "global"):                 ["ERA5L_TMIN"],

    # ── Temperature (mean) ────────────────────────────────────────────────
    ("tmean", "global"):                ["ERA5L_TMEAN"],

    # ── Streamflow ────────────────────────────────────────────────────────
    ("streamflow", "CONUS"):            ["NWIS_STREAMFLOW"],
    # Global streamflow (GRDC) lands in Phase 4
    # ("streamflow", "global"):         ["GRDC_STREAMFLOW"],

    # ── Land Cover ────────────────────────────────────────────────────────
    ("landcover", "CONUS"):             ["NLCD", "ESA_WORLDCOVER", "DYNAMIC_WORLD", "ESA_WORLDCOVER_STAC"],
    ("landcover", "NORTH_AMERICA"):     ["NLCD", "ESA_WORLDCOVER", "DYNAMIC_WORLD", "ESA_WORLDCOVER_STAC"],
    ("landcover", "global"):            ["ESA_WORLDCOVER", "DYNAMIC_WORLD", "ESA_WORLDCOVER_STAC"],

    # ── Soil ─────────────────────────────────────────────────────────────
    ("soil", "CONUS"):                  ["POLARIS", "SOILGRIDS"],
    ("soil", "global"):                 ["SOILGRIDS"],

    # ── Evapotranspiration ────────────────────────────────────────────────
    ("et",  "CONUS"):                   ["MOD16_ET",  "TERRACLIMATE_AET",  "ERA5L_PET"],
    ("et",  "global"):                  ["MOD16_ET",  "TERRACLIMATE_AET",  "ERA5L_PET"],
    ("pet", "CONUS"):                   ["GRIDMET_PET", "MOD16_PET", "ERA5L_PET"],
    ("pet", "global"):                  ["ERA5L_PET", "MOD16_PET"],

    # ── DEM ───────────────────────────────────────────────────────────────
    ("dem", "CONUS"):                   ["DEM3DEP_10M", "GLO30", "SRTM", "GLO30_STAC"],
    ("dem", "NORTH_AMERICA"):           ["DEM3DEP_10M", "GLO30", "SRTM", "GLO30_STAC"],
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
