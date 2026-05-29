# Products Reference

## Variables

| Variable        | ID keyword        | Notes                              |
|-----------------|-------------------|------------------------------------|
| Precipitation   | `precipitation`   | Daily / monthly mm                 |
| Max temperature | `tmax`            | Daily °C                           |
| Min temperature | `tmin`            | Daily °C                           |
| Mean temperature| `tmean`           | Daily °C                           |
| Evapotranspiration | `et`           | 8-day / monthly mm                 |
| PET             | `pet`             | Potential ET, mm/day               |
| Soil moisture   | `soil_moisture`   | cm³/cm³                            |
| DEM             | `dem`             | Metres above sea level (static)    |
| Land cover      | `landcover`       | Class codes (static)               |
| Soil properties | `soil`            | % sand/silt/clay (static)          |
| Streamflow      | `streamflow`      | m³/s (gauge-based)                 |
| NDVI            | `ndvi`            | Dimensionless 0–1                  |
| LAI             | `lai`             | m²/m²                              |

## All 32 products

### CONUS-first (via HyRiver — no auth)

| Product           | Variable          | Resolution | Timestep  | Extra     |
|-------------------|-------------------|------------|-----------|-----------|
| GRIDMET_PRECIP    | precipitation     | 4 km       | daily     | hyriver   |
| GRIDMET_TMAX      | tmax              | 4 km       | daily     | hyriver   |
| GRIDMET_TMIN      | tmin              | 4 km       | daily     | hyriver   |
| GRIDMET_PET       | pet               | 4 km       | daily     | hyriver   |
| DAYMET_PRECIP     | precipitation     | 1 km       | daily     | hyriver   |
| DAYMET_TMAX       | tmax              | 1 km       | daily     | hyriver   |
| DAYMET_TMIN       | tmin              | 1 km       | daily     | hyriver   |
| NLCD              | landcover         | 30 m       | static    | hyriver   |
| POLARIS           | soil              | 30 m       | static    | hyriver   |
| DEM3DEP_10M       | dem               | 10 m       | static    | hyriver   |

### Global (via GEE — auth required)

| Product           | Variable          | Resolution | Timestep      | Notes                        |
|-------------------|-------------------|------------|---------------|------------------------------|
| CHIRPS            | precipitation     | 5.5 km     | daily         | Primary global precip        |
| IMERG_PRECIP      | precipitation     | 11 km      | daily         | NASA GPM V07; from 2000      |
| ERA5L_PRECIP      | precipitation     | 11 km      | daily         | Reanalysis; 1950–present     |
| ERA5L_TMAX        | tmax              | 11 km      | daily         |                              |
| ERA5L_TMIN        | tmin              | 11 km      | daily         |                              |
| ERA5L_TMEAN       | tmean             | 11 km      | daily         |                              |
| MOD16_ET          | et                | 500 m      | 8-day/monthly | MODIS; masks urban pixels    |
| TERRACLIMATE_AET  | et                | 4.6 km     | monthly       | Replaces SSEBOP (removed)    |
| MOD16_PET         | pet               | 500 m      | 8-day/monthly | MODIS                        |
| ERA5L_PET         | pet               | 11 km      | daily         |                              |
| SMAP_SM           | soil_moisture     | 9 km       | daily         | From 2015-03-31              |
| GLO30             | dem               | 30 m       | static        | Copernicus; primary global   |
| SRTM              | dem               | 30 m       | static        | 60°S–60°N                    |
| MERIT_DEM         | dem               | 90 m       | static        | Hydrologically conditioned   |
| ESA_WORLDCOVER    | landcover         | 10 m       | static        | 2020 & 2021 epochs           |
| DYNAMIC_WORLD     | landcover         | 10 m       | static        | Sentinel-2; use raster mode  |
| SOILGRIDS         | soil              | 250 m      | static        | ISRIC; global                |
| MODIS_NDVI        | ndvi              | 250 m      | 16-day        |                              |
| SENTINEL2_NDVI    | ndvi              | 10 m       | ~5-day        | Computed B8/B4               |
| MODIS_LAI         | lai               | 500 m      | 8-day         |                              |

### Direct API (no auth)

| Product           | Variable          | Resolution | Notes                                  |
|-------------------|-------------------|------------|----------------------------------------|
| NWIS_STREAMFLOW   | streamflow        | gauge      | Pass USGS gauge ID as geometry         |
| CHIRPS_IRI        | precipitation     | 5 km       | Auth-free fallback; 50°S–50°N; [opendap] |

## Routing policy summary

`fetch(..., mode="auto")` auto-selects based on geometry region:

| Region   | Variable        | Priority order                                              |
|----------|-----------------|-------------------------------------------------------------|
| CONUS    | precipitation   | GRIDMET_PRECIP → DAYMET_PRECIP → CHIRPS → ERA5L_PRECIP → CHIRPS_IRI |
| global   | precipitation   | CHIRPS → IMERG_PRECIP → ERA5L_PRECIP → CHIRPS_IRI          |
| CONUS    | tmax            | GRIDMET_TMAX → DAYMET_TMAX → ERA5L_TMAX                    |
| global   | tmax            | ERA5L_TMAX                                                  |
| global   | et              | MOD16_ET → TERRACLIMATE_AET → ERA5L_PET                    |
| CONUS    | dem             | DEM3DEP_10M → GLO30 → SRTM                                 |
| global   | dem             | GLO30 → SRTM → MERIT_DEM                                   |
| global   | soil_moisture   | SMAP_SM                                                     |
| global   | ndvi            | MODIS_NDVI → SENTINEL2_NDVI                                 |

Call `data_list_products(variable=..., region=...)` for the live, up-to-date policy table.

## Removed products (v0.2.0)

| Old ID     | Reason                                                   | Replacement       |
|------------|----------------------------------------------------------|-------------------|
| MSWEP      | GEE Community asset removed upstream (2024)              | IMERG_PRECIP      |
| SSEBOP_ET  | USGS/fews_net GEE ImageCollection deprecated             | TERRACLIMATE_AET  |
| ESA_CCI_SM | GEE Community asset deleted upstream                     | (none currently)  |
