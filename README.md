# aihydro-data

**Global hydrology dataverse — fetch any variable, anywhere, from the best available source.**

`aihydro-data` is a variable-centric, multi-source, region-aware Python library that unifies Google Earth Engine (GEE), HyRiver, and direct HTTP APIs behind a single `fetch()` call. It is the data backbone of the AI-Hydro toolchain.

```python
from aihydro_data import fetch

# Auto mode — router picks the best product for the geometry's region
result = fetch(
    variable="precipitation",
    geometry=watershed_gdf,       # GeoDataFrame, GeoJSON, shapely, (lat, lon), or bbox
    start="2010-01-01",
    end="2020-12-31",
)
print(result.product)    # "CHIRPS"  (auto-selected)
print(result.source)     # "gee"
print(result.citation)   # full bibliographic reference
result.data              # pd.DataFrame or xr.DataArray
result.next_steps        # agent-facing hints: what to do with this data
```

[![PyPI](https://img.shields.io/pypi/v/aihydro-data)](https://pypi.org/project/aihydro-data/)
[![Python](https://img.shields.io/pypi/pyversions/aihydro-data)](https://pypi.org/project/aihydro-data/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20823443.svg)](https://doi.org/10.5281/zenodo.20823443)

---

## Table of Contents

- [Why](#why)
- [Install](#install)
- [Quick Start](#quick-start)
- [Products (49 total)](#products)
- [Routing System](#routing-system)
- [Auth Setup](#auth-setup)
- [MCP Tools](#mcp-tools)
- [Architecture](#architecture)
- [Contributing](#contributing)

---

## Why

Before `aihydro-data`, fetching hydrology data meant hard-coding a single CONUS-only source per variable (GridMET for precip, NWIS for streamflow, POLARIS for soil). Anything outside CONUS meant writing a new fetcher from scratch.

`aihydro-data` turns this into a single declarative routing problem: **one call, every region, every variable, documented fallbacks**.

| Variable | CONUS (primary) | Global (primary) | Fallback chain |
|---|---|---|---|
| Precipitation | GridMET | CHIRPS (GEE) | IMERG, ERA5-Land, CHIRPS-IRI* |
| Tmax / Tmin | GridMET, Daymet | ERA5-Land | — |
| ET (actual) | MOD16 | MOD16 | TerraClimate, ERA5-Land |
| ET (potential) | GridMET | ERA5-Land | MOD16 |
| Soil moisture | SMAP | SMAP | — |
| Land cover | NLCD | ESA WorldCover | Dynamic World |
| Soil properties | POLARIS | SoilGrids | — |
| DEM | 3DEP (10 m) | Copernicus GLO-30 | SRTM, MERIT-DEM |
| Streamflow | USGS NWIS | GEOGLOWS, Open-Meteo, GloFAS | GEOGLOWS, Open-Meteo |
| NDVI | MODIS (250 m) | MODIS (250 m) | Sentinel-2 (10 m) |
| LAI | MODIS | MODIS | — |

\* `CHIRPS_IRI` — auth-free OPeNDAP fallback, no GEE account required.

---

## Install

```bash
# Full install (all backends)
pip install aihydro-data[all]

# Per-backend
pip install aihydro-data[gee]        # Google Earth Engine (23 products)
pip install aihydro-data[hyriver]    # CONUS via HyRiver (10 products)
pip install aihydro-data[stac]       # STAC catalogues (Planetary Computer)
pip install aihydro-data[opendap]    # IRI OPeNDAP (CHIRPS auth-free fallback)
```

> **Python**: 3.10+ &nbsp;|&nbsp; **GEE auth**: required for 23 GEE products (see [Auth Setup](#auth-setup))

---

## Quick Start

### 1. Auto mode — global watershed

```python
from aihydro_data import fetch
import geopandas as gpd

# Any geometry: GeoDataFrame, shapely Point/Polygon, (lat, lon) tuple, or bbox
gdf = gpd.read_file("ganges_basin.geojson")

result = fetch("precipitation", gdf, "2015-01-01", "2015-12-31")
print(result.product)   # "CHIRPS"  (auto-selected for South Asia)
print(result.data)
#           date  precipitation
# 0   2015-01-01           1.23
# 1   2015-01-02           0.00
# ...
```

### 2. Auto mode — CONUS watershed

```python
from aihydro_data import fetch
from shapely.geometry import Point

# Kansas City — routes to GridMET (no GEE auth needed)
result = fetch("precipitation", Point(-94.5, 39.1), "2020-01-01", "2020-12-31")
print(result.product)   # "GRIDMET_PRECIP"
print(result.source)    # "hyriver"
```

### 3. Manual mode — pin a specific product

```python
result = fetch(
    "et",
    gdf,
    "2010-01-01", "2020-12-31",
    mode="manual",
    product="MOD16_ET",
)
print(result.units)     # "mm/month"
print(result.citation)  # Running et al. 2019 ...
print(result.bibtex)    # @dataset{...}
```

### 4. Plot the result (v0.1.2+)

```python
result = fetch("streamflow", "03245500", "2010-01-01", "2020-12-31")

# Auto-dispatched plot — picks line/bar/imshow based on data shape
result.plot(logy=True)

# Interactive folium map preview
result.map()

# Multi-source comparison in one call
from aihydro_data.viz import compare
fig = compare(
    ["GRIDMET_PRECIP", "CHIRPS", "ERA5L_PRECIP"],
    watershed_gdf, "2015-01-01", "2020-12-31",
    plots=["timeseries", "climatology", "scatter", "double_mass"],
)

# Research-grade hydrology plots
from aihydro_data.viz import flow_duration_curve, climatology, budyko
flow_duration_curve(streamflow_result)
budyko(precip_result, pet_result, et_result, label="My catchment")
```

Install with `pip install aihydro-data[viz]` (matplotlib + folium).

### 5. Discover available products

```python
from aihydro_data import list_products, get_product

# List all precipitation products
for p in list_products(variable="precipitation"):
    print(f"{p.id:20s}  {p.coverage}  {p.resolution_m}m  {p.source}")

# Inspect one product's full spec
spec = get_product("CHIRPS")
print(spec.common_pitfalls)
print(spec.examples)
```

### 6. Validate before fetching

```python
from aihydro_data.mcp import data_validate_request

check = data_validate_request(
    variable="et",
    geometry={"type": "Point", "coordinates": [-94.5, 39.1]},
    start="1990-01-01",
    end="2000-12-31",
)
# {"ok": False, "issues": [{"code": "DATE_OUT_OF_RANGE",
#   "product": "MOD16_ET", "message": "MOD16 starts 2000-01-01."}], ...}
```

---

## Products

54 products across 18 variables, live-tested against real backends (v0.2.0).

### Precipitation (6 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `CHIRPS` | GEE | Global | 5 km | Daily | Primary global; GEE auth required |
| `IMERG_PRECIP` | GEE | Global | 11 km | Daily | NASA GPM V07; GEE auth required |
| `ERA5L_PRECIP` | GEE | Global | 11 km | Daily | Reanalysis; 1950–present |
| `GRIDMET_PRECIP` | HyRiver | CONUS | 4 km | Daily | Primary CONUS; no auth |
| `DAYMET_PRECIP` | HyRiver | N. America | 1 km | Daily | High-res; no auth |
| `CHIRPS_IRI` | Direct API | Global | 5 km | Daily | **Auth-free fallback** via IRI OPeNDAP |

### Temperature (9 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `ERA5L_TMAX` | GEE | Global | 11 km | Daily | GEE auth required |
| `ERA5L_TMIN` | GEE | Global | 11 km | Daily | GEE auth required |
| `ERA5L_TMEAN` | GEE | Global | 11 km | Daily | GEE auth required |
| `GRIDMET_TMAX` | HyRiver | CONUS | 4 km | Daily | auth-free |
| `GRIDMET_TMIN` | HyRiver | CONUS | 4 km | Daily | auth-free |
| `DAYMET_TMAX` | HyRiver | N. America | 1 km | Daily | auth-free |
| `DAYMET_TMIN` | HyRiver | N. America | 1 km | Daily | auth-free |
| `OPEN_METEO_TMAX` | Direct API | Global | 25 km | Daily | **auth-free** centroid-based; Open-Meteo ERA5 archive |
| `OPEN_METEO_TMIN` | Direct API | Global | 25 km | Daily | **auth-free** centroid-based; Open-Meteo ERA5 archive |

### Evapotranspiration (7 products)

| ID | Variable | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|---|
| `OPENET_ENSEMBLE` | ET (actual) | GEE | CONUS | 30 m | Monthly | OpenET ensemble; field-validated; 2016–present |
| `MOD16_ET` | ET (actual) | GEE | Global | 500 m | 8-day → monthly | GEE auth required |
| `TERRACLIMATE_AET` | ET (actual) | GEE | Global | 4.6 km | Monthly | GEE auth required |
| `MOD16_PET` | PET | GEE | Global | 500 m | 8-day → monthly | GEE auth required |
| `ERA5L_PET` | PET | GEE | Global | 11 km | Daily | GEE auth required |
| `GRIDMET_PET` | PET | HyRiver | CONUS | 4 km | Daily | auth-free |
| `OPEN_METEO_PET` | PET | Direct API | Global | 25 km | Daily | **auth-free** centroid-based |

### DEM (6 products)

| ID | Source | Coverage | Resolution | Notes |
|---|---|---|---|---|
| `GLO30` | GEE | Global | 30 m | Copernicus; primary global DEM; GEE auth required |
| `SRTM` | GEE | 60°S–60°N | 30 m | NASA SRTM v3; GEE auth required |
| `MERIT_DEM` | GEE | Global | 90 m | Hydrologically conditioned; GEE auth required |
| `DEM3DEP_10M` | HyRiver | CONUS | 10 m | USGS 3DEP; highest CONUS resolution; auth-free |
| `GLO30_STAC` | STAC | Global | 30 m | **auth-free** Copernicus GLO-30 via Planetary Computer; auto-falls-back to Element84 on timeout |
| `GLO30_ELEMENT84` | STAC | Global | 30 m | **auth-free** Copernicus GLO-30 via Element84 Earth Search (AWS); independent infrastructure |

### Soil Moisture (1 product)

| ID | Source | Coverage | Resolution | Timestep |
|---|---|---|---|---|
| `SMAP_SM` | GEE | Global | 9 km | Daily (2015–present) |

### Land Cover (4 products)

| ID | Source | Coverage | Notes |
|---|---|---|---|
| `NLCD` | HyRiver | CONUS | NLCD 2021; 30 m; auth-free |
| `ESA_WORLDCOVER` | GEE | Global | 10 m; 2020 & 2021; GEE auth required |
| `DYNAMIC_WORLD` | GEE | Global | 10 m; Sentinel-2 derived; GEE auth required |
| `ESA_WORLDCOVER_STAC` | STAC | Global | 10 m; **auth-free** via Planetary Computer |

### Soil Properties (3 products)

| ID | Source | Coverage | Notes |
|---|---|---|---|
| `POLARIS` | HyRiver | CONUS | 30 m; 9 properties |
| `SOILGRIDS` | GEE | Global | 250 m; ISRIC |
| `OPENLANDMAP_BEDROCK` | GEE | Global | 250 m; depth to bedrock (USDA-Simard); GEE auth required |

### Impervious Surface (2 products)

| ID | Source | Coverage | Resolution | Notes |
|---|---|---|---|---|
| `NLCD_IMPERVIOUS` | HyRiver | CONUS | 30 m | NLCD 2021 impervious surface fraction; auth-free |
| `GHSL_BUILT_UP` | GEE | Global | 100 m | JRC Global Human Settlement Layer built-up surface; GEE auth required |

### Vegetation (3 products)

| ID | Variable | Source | Coverage | Resolution | Timestep |
|---|---|---|---|---|---|
| `MODIS_NDVI` | NDVI | GEE | Global | 250 m | 16-day composite |
| `SENTINEL2_NDVI` | NDVI | GEE | Global | 10 m | ~5 day revisit |
| `MODIS_LAI` | LAI | GEE | Global | 500 m | 8-day composite |

### Optical (5 products)

| ID | Source | Coverage | Resolution | Notes |
|---|---|---|---|---|
| `SENTINEL2_SR` | GEE | Global | 10 m | Surface reflectance; GEE auth required |
| `LANDSAT9_SR` | GEE | Global | 30 m | Landsat 9 L2 SR; GEE auth required |
| `LANDSAT8_SR` | GEE | Global | 30 m | Landsat 8 L2 SR; GEE auth required |
| `SENTINEL2_SR_STAC` | STAC | Global | 10 m | **auth-free** via Planetary Computer |
| `LANDSAT_SR_STAC` | STAC | Global | 30 m | **auth-free** Landsat C2 L2 via Planetary Computer |

### Geology (3 products)

Area-weighted lithology and hydrogeology attributes from GLiM and GLHYMPS, returned as a
single-row DataFrame. `result.data.iloc[0].to_dict()` gives all 9 CAMELS-geology attributes.

> **License gate:** GLiM redistribution requires CCGM written permission. Tiles are available
> for private research via `PYGEOGLIM_HF_TOKEN`. Public release is fails-closed pending permission.

| ID | Source | Coverage | Notes |
|---|---|---|---|
| `PYGEOGLIM_ALL` | pygeoglim | Global | **Default.** Combined GLiM + GLHYMPS → 9 attributes: 5 lithology + 4 hydrogeology |
| `GLIM_TILES` | pygeoglim | Global | GLiM lithology only: `geol_1st_class`, `glim_1st_class_frac`, `geol_2nd_class`, `glim_2nd_class_frac`, `carbonate_rocks_frac` |
| `GLHYMPS_TILES` | pygeoglim | Global | GLHYMPS hydrogeology only: `geol_porosity`, `geol_permeability` (log₁₀ m²), `geol_permeability_linear`, `hydraulic_conductivity` |

```python
# All geology attributes in one call
result = fetch("geology", watershed_gdf, "2020-01-01", "2020-12-31")
attrs = result.data.iloc[0].to_dict()
# → {'geol_1st_class': 'Siliciclastic...', 'carbonate_rocks_frac': 0.18,
#    'geol_porosity': 0.099, 'geol_permeability': -11.1, ...}

# Aliases also work
fetch("lithology", ...)     # → geology
fetch("hydrogeology", ...)  # → geology
fetch("permeability", ...)  # → geology
```

### Flood Inundation (1 product)

| ID | Source | Coverage | Notes |
|---|---|---|---|
| `GFM_S1_INUNDATION` | Direct API | Global | Copernicus GFM SAR-derived flood extent; event-based, not operational forecast |

### Streamflow (4 products)

| ID | Source | Coverage | Notes |
|---|---|---|---|
| `NWIS_STREAMFLOW` | Direct API | CONUS | USGS daily values; pass gauge ID as geometry; auth-free |
| `GEOGLOWS_RETRO` | GEOGLOWS | Global | Modelled 1940–present via AWS Open Data Zarr; TDX-Hydro reach network; **auth-free** |
| `OPENMETEO_FLOOD` | Direct API | Global | Open-Meteo river discharge model; centroid-snapped; **auth-free** |
| `GLOFAS_STREAMFLOW` | CDS / GloFAS | Global | GloFAS v4 modelled discharge; requires free Copernicus CDS account + `~/.cdsapirc` |

---

## Routing System

The router is a **declarative policy table** in `routing/policy.py` — no if/else chains, no source-specific logic. Adding a new product means adding one row.

```
fetch(variable, geometry, start, end)
        │
        ▼
detect_region(geometry)          ← CONUS? S_ASIA? EUROPE? global?
        │
        ▼
PRODUCT_POLICY[(variable, region)]   ← ordered list [primary, fallback1, fallback2, ...]
        │
        ▼
resolve_product(spec)            ← ProductSpec with backend_config
        │
        ▼
Backend.fetch_timeseries()       ← gee / hyriver / direct_api
        │
        ▼
FetchResult(data, product, source, citation, units, next_steps)
```

**Region detection** uses bounding-box math: if the geometry's centroid is inside the CONUS rectangle (−125° to −66°W, 24° to 50°N), the region is `"CONUS"`. Otherwise a Pfafstetter level-2 table resolves to `"S_ASIA"`, `"EUROPE"`, `"AFRICA"`, etc. Unknown regions fall to `"global"`.

**Fallback chain**: if the primary product raises `SourceUnavailable` or times out, the pipeline walks down the policy list automatically.

---

## Auth Setup

### Google Earth Engine (23 products)

```bash
# 1. Install
pip install aihydro-data[gee]

# 2. Authenticate (one time — writes ~/.config/earthengine/credentials)
python -c "import ee; ee.Authenticate()"

# 3. Verify
python -c "from aihydro_data.mcp import data_doctor; print(data_doctor())"
```

> GEE requires a [Google account registered for Earth Engine](https://earthengine.google.com/signup/). Academic / research use is free.

### HyRiver (10 products)

No auth required. Just install:

```bash
pip install aihydro-data[hyriver]
```

### Auth-free global products

No auth required:

```bash
pip install aihydro-data[opendap]    # CHIRPS_IRI — needs xarray + netCDF4
pip install aihydro-data[geoglows]   # GEOGLOWS_RETRO — AWS Zarr; needs s3fs + zarr
# NWIS_STREAMFLOW, OPEN_METEO_*, *_STAC all work with their respective extras; no auth
```

### GloFAS (modelled global streamflow)

```bash
pip install aihydro-data[glofas]

# One-time: create a free Copernicus CDS account at cds.climate.copernicus.eu
# then add your token to ~/.cdsapirc:
# url: https://cds-beta.climate.copernicus.eu
# key: <your-api-key>
```

---

## MCP Tools

`aihydro-data` ships 9 MCP tools that expose the full API to AI agents (Claude, etc.) via the AI-Hydro MCP server:

| Tool | Description |
|---|---|
| `data_fetch` | Fetch a variable for a geometry and date range |
| `data_batch_fetch` | Fetch multiple geometries in one call |
| `data_list_products` | Discover products by variable / region / source |
| `data_describe_product` | Full spec for one product (citation, pitfalls, examples) |
| `data_validate_request` | Dry-run validation before fetching (size estimate, date checks) |
| `data_get_cache_status` | Inspect the disk cache |
| `data_invalidate_cache` | Clear cached entries |
| `data_doctor` | Environment check: auth, backends, cache, missing extras |
| `data_help` | Built-in onboarding guide (topics: auth, first_fetch, caching, …) |

The tools are auto-registered when `aihydro-data[mcp]` is installed, via the `aihydro.tools` entry-point group.

---

## Architecture

```
aihydro_data/
├── __init__.py          ← Public API: fetch(), list_products(), get_product()
├── fetch.py             ← Unified entry point
├── _pipeline.py         ← Routing → product resolution → backend dispatch → cache
├── contracts.py         ← ProductSpec, FetchRequest, FetchResult (Pydantic)
├── exceptions.py        ← Typed exceptions with agent-friendly .to_dict()
│
├── products/            ← Declarative variable registry (one file per variable)
│   ├── precipitation.py     6 products
│   ├── temperature.py       9 products (tmax/tmin/tmean + Open-Meteo)
│   ├── et.py                6 products (pet 4 + et 2)
│   ├── dem.py               5 products
│   ├── soil_moisture.py     1 product
│   ├── landcover.py         4 products
│   ├── soil.py              2 products
│   ├── vegetation.py        3 products (ndvi 2 + lai 1)
│   ├── optical.py           5 products
│   └── streamflow.py        4 products (NWIS + GEOGLOWS + Open-Meteo + GloFAS)
│
├── sources/             ← Backend adapters (lazy imports — safe without extras)
│   ├── base.py              SourceBackend ABC
│   ├── gee/                 GEE backend package (23 products)
│   │   ├── __init__.py          Backend class + fetch_timeseries/fetch_raster
│   │   ├── _download.py         raster download helpers
│   │   └── _composite.py        optical composite + spectral index helpers
│   ├── hyriver.py           HyRiver backend (10 products)
│   ├── direct_api.py        NWIS + CHIRPS IRI OPeNDAP + Open-Meteo (5 products)
│   ├── stac.py              STAC/Planetary Computer (4 products)
│   ├── geoglows_retro.py    GEOGLOWS v2 retrospective via AWS Zarr (1 product)
│   ├── openmeteo_flood.py   Open-Meteo river discharge (1 product)
│   ├── cds_glofas.py        GloFAS via Copernicus CDS (1 product)
│   ├── _common.py           require_import + assert_backend_available helpers
│   ├── _retry.py            call_with_retry for transient HTTP errors
│   └── _gee_vendored/       GEE auth + timeseries helpers
│
├── routing/
│   ├── regions.py           CONUS bbox, Pfafstetter region table
│   ├── detect.py            detect_region(geometry) → str
│   └── policy.py            PRODUCT_POLICY: (variable, region) → [product_ids]
│
├── geometry/
│   └── __init__.py          coerce_geometry() — normalises all input types
│
├── cache/
│   └── __init__.py          Disk cache at ~/.aihydro/cache/data/
│
├── mcp/
│   └── __init__.py          9 MCP tools (data_fetch, data_doctor, …)
│
└── help_topics/             Bundled markdown help (version-pinned to install)
    ├── first_fetch.md
    ├── auth.md
    ├── caching.md
    └── ...
```

### Three orthogonal axes

| Axis | What it is | Where it lives |
|---|---|---|
| **Variable** | *what* — precipitation, tmax, et, ndvi, dem, … | `products/<variable>.py` |
| **Source/Product** | *where from* — GridMET, CHIRPS, ERA5-Land, MOD16, … | `ProductSpec.backend_config` |
| **Region** | *where to* — CONUS, S_ASIA, EUROPE, global, … | `routing/policy.py` |

A user asks for "precipitation over this watershed" — the router resolves all three axes automatically, or the user pins any/all of them manually.

### Agent-friendly design

Every failure returns a structured envelope:

```python
{
    "error": True,
    "code": "GEE_AUTH_MISSING",
    "message": "Google Earth Engine credentials not found.",
    "recovery": "Run `python -c \"import ee; ee.Authenticate()\"`",
    "next_tools": ["data_doctor", "data_help"],
    "docs_anchor": "auth#gee"
}
```

Every success carries `citation`, `bibtex`, `units`, `license`, and `next_steps` so agents can chain downstream tools without re-planning.

---

## Contributing

### Adding a new product

1. Add a `ProductSpec` to the relevant `products/<variable>.py` (or create a new file).
2. Add a row to `routing/policy.py`.
3. If it's a new backend, add a `Backend` subclass to `sources/`.
4. Run `pytest -m "not live"` — no live credentials needed for the offline suite.

### Running tests

```bash
# Offline suite (no network, no auth — ~7 seconds)
pytest -m "not live"

# Live sweep — tests all 54 products against real backends (~15 minutes)
# Requires GEE auth + internet
pytest tests/test_live_sweep.py -v
```

---

## Status

**v0.2.1** — STAC robustness: retry+backoff in STAC backend; `GLO30_ELEMENT84` (Element84 Earth Search fallback for Copernicus DEM); impervious + bedrock_depth variables added. 54 products across 18 variables; 369 offline tests.

**v0.2.0** — First public PyPI release. Global streamflow tri-source chain (GEOGLOWS/Open-Meteo/GloFAS); spatial-support honesty (point vs areal vs reach products declared and enforced); verify-on-read cache; `region` and `outlet` kwargs; structural refactor (gee/ package, MCP `@_tool_envelope`); 341 offline tests.

See **[examples/cookbook.ipynb](examples/cookbook.ipynb)** for working recipes.

| Phase | Status | Description |
|---|---|---|
| 1: Scaffold | ✅ | Package structure, contracts, registry |
| 2: Precipitation vertical | ✅ | 6 products, routing, fallback chain |
| 3: CONUS migration | ✅ | Streamflow, temperature, landcover, soil |
| 4: Global gap-filling | ✅ | ET, DEM, soil moisture, vegetation |
| 5: Cache + provenance | ✅ | Disk cache, manifest, license tracking |
| 6: Batch fetching | ✅ | Multi-geometry parallel dispatch |
| 7: MCP tools | ✅ | 9 tools, help topics, doctor |
| 8: PyPI publish | ✅ | v0.2.0 on PyPI (`pip install aihydro-data`) |

---


---

## Citation

If you use `aihydro-data` in your research, please cite it:

```bibtex
@software{galib2025aihydrodata,
  author    = {Galib, Mohammad},
  title     = {aihydro-data: Global Hydrology Dataverse for the AI-Hydro Platform},
  year      = {2025},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.20823443},
  url       = {https://doi.org/10.5281/zenodo.20823443}
}
```

---

## License

Apache-2.0. Data products carry their own licenses — always check `result.license` or `data_describe_product(id)`.
