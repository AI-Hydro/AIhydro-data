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

57 products across 19 variables (v0.2.0).

### Precipitation (6 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `CHIRPS` | gee | global | 5 km | daily | GEE auth required |
| `IMERG_PRECIP` | gee | global | 11 km | daily | GEE auth required |
| `ERA5L_PRECIP` | gee | global | 11 km | daily | GEE auth required |
| `GRIDMET_PRECIP` | hyriver | CONUS | 4 km | daily | auth-free |
| `DAYMET_PRECIP` | hyriver | NORTH_AMERICA | 1 km | daily | auth-free |
| `CHIRPS_IRI` | direct_api | global | 5 km | daily | auth-free |

### Temperature — Tmax (4 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `GRIDMET_TMAX` | hyriver | CONUS | 4 km | daily | auth-free |
| `DAYMET_TMAX` | hyriver | NORTH_AMERICA | 1 km | daily | auth-free |
| `ERA5L_TMAX` | gee | global | 11 km | daily | GEE auth required |
| `OPEN_METEO_TMAX` | direct_api | global | 25 km | daily | auth-free; spatial support: point |

### Temperature — Tmin (4 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `GRIDMET_TMIN` | hyriver | CONUS | 4 km | daily | auth-free |
| `DAYMET_TMIN` | hyriver | NORTH_AMERICA | 1 km | daily | auth-free |
| `ERA5L_TMIN` | gee | global | 11 km | daily | GEE auth required |
| `OPEN_METEO_TMIN` | direct_api | global | 25 km | daily | auth-free; spatial support: point |

### Temperature — Tmean (1 product)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `ERA5L_TMEAN` | gee | global | 11 km | daily | GEE auth required |

### Potential Evapotranspiration (PET) (4 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `MOD16_PET` | gee | global | 500 m | monthly | GEE auth required |
| `ERA5L_PET` | gee | global | 11 km | daily | GEE auth required |
| `GRIDMET_PET` | hyriver | CONUS | 4 km | daily | auth-free |
| `OPEN_METEO_PET` | direct_api | global | 25 km | daily | auth-free; spatial support: point |

### Actual Evapotranspiration (ET) (3 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `MOD16_ET` | gee | global | 500 m | monthly | GEE auth required |
| `TERRACLIMATE_AET` | gee | global | 4 km | monthly | GEE auth required |
| `OPENET_ENSEMBLE` | gee | CONUS | 30 m | monthly | GEE auth required |

### DEM (6 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `GLO30` | gee | global | 30 m | static | GEE auth required |
| `SRTM` | gee | global | 30 m | static | GEE auth required |
| `DEM3DEP_10M` | hyriver | CONUS | 10 m | static | auth-free |
| `MERIT_DEM` | gee | global | 90 m | static | GEE auth required |
| `GLO30_STAC` | stac | global | 30 m | static | auth-free |
| `GLO30_ELEMENT84` | stac | global | 30 m | static | auth-free |

### Soil Moisture (1 product)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `SMAP_SM` | gee | global | 9 km | daily | GEE auth required |

### Land Cover (4 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `NLCD` | hyriver | CONUS | 30 m | static | auth-free |
| `ESA_WORLDCOVER` | gee | global | 10 m | static | GEE auth required |
| `DYNAMIC_WORLD` | gee | global | 10 m | static | GEE auth required |
| `ESA_WORLDCOVER_STAC` | stac | global | 10 m | static | auth-free |

### Soil Properties (2 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `POLARIS` | hyriver | CONUS | 30 m | static | auth-free |
| `SOILGRIDS` | gee | global | 250 m | static | GEE auth required |

### NDVI (2 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `MODIS_NDVI` | gee | global | 250 m | monthly | GEE auth required |
| `SENTINEL2_NDVI` | gee | global | 10 m | monthly | GEE auth required |

### LAI (1 product)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `MODIS_LAI` | gee | global | 500 m | monthly | GEE auth required |

### Optical (5 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `SENTINEL2_SR` | gee | global | 10 m | composite | GEE auth required |
| `LANDSAT9_SR` | gee | global | 30 m | composite | GEE auth required |
| `LANDSAT8_SR` | gee | global | 30 m | composite | GEE auth required |
| `SENTINEL2_SR_STAC` | stac | global | 10 m | composite | auth-free |
| `LANDSAT_SR_STAC` | stac | global | 30 m | composite | auth-free |

### Streamflow (4 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `NWIS_STREAMFLOW` | direct_api | CONUS | 0 m | daily | auth-free; spatial support: gauge_point |
| `GEOGLOWS_RETRO` | geoglows_retro | global | 0 m | daily | auth-free; spatial support: reach |
| `OPENMETEO_FLOOD` | openmeteo_flood | global | 5 km | daily | auth-free; spatial support: reach |
| `GLOFAS_STREAMFLOW` | cds_glofas | global | 5 km | daily | spatial support: reach |

### Bedrock Depth (1 product)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `OPENLANDMAP_BEDROCK` | gee | global | 250 m | static | GEE auth required |

### Flood Inundation (1 product)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `GFM_S1_INUNDATION` | direct_api | global | 20 m | event | auth-free |

### Geology (3 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `PYGEOGLIM_ALL` | pygeoglim | global | 0 m | static | auth-free |
| `GLIM_TILES` | pygeoglim | global | 0 m | static | auth-free |
| `GLHYMPS_TILES` | pygeoglim | global | 0 m | static | auth-free |

### Impervious (2 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `NLCD_IMPERVIOUS` | hyriver | CONUS | 30 m | static | auth-free |
| `GHSL_BUILT_UP` | gee | global | 100 m | static | GEE auth required |

### Population (3 products)

| ID | Source | Coverage | Resolution | Timestep | Notes |
|---|---|---|---|---|---|
| `GHSL_POP` | gee | global | 100 m | 5-yearly | — |
| `WORLDPOP` | gee | global | 100 m | annual | — |
| `GPW_V411` | gee | global | 927 m | 5-yearly | — |

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

# Live sweep — tests all 57 products against real backends (~15 minutes)
# Requires GEE auth + internet
pytest tests/test_live_sweep.py -v
```

---

## Status

**v0.2.1** — STAC robustness + metadata repair: retry+backoff in STAC backend; `GLO30_ELEMENT84`; impervious + bedrock_depth variables; and corrected `aihydro-core` dependency metadata so downstream `aihydro-watershed`/`aihydro-tools` resolve with `aihydro-core>=0.2`. Shipped with 54 products in 18 variables and 369 offline tests (current counts: see [Products](#products)).

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

### IMERG daily precipitation contract

`IMERG_PRECIP` integrates the native half-hourly rates into complete UTC daily totals. Date-only start/end are inclusive for this product. Missing, duplicate or invalid intervals fail instead of producing partial totals. Returned `interval_count`, `expected_interval_count` and `source_status` columns expose temporal support and product maturity. Provisional or unknown status must not be cited as verified Final data. This check does not establish spatial completeness or observational accuracy. See [the decision and limitations](DECISIONS.md) and [implementation plan](plans/precipitation-contract-2026-09-07.md).

### GFM observational failures and synthetic fixtures

`fetch_gfm_extent` raises on network/asset failures; it never substitutes synthetic polygons. `use_fixture=True` explicitly selects synthetic test data, without an observational citation. Inspect `status`: no acquisitions and no valid pixels are distinct from no flood detected. Per-item acquisition times and assets remain available for review. Automatic model-validation scores are withheld until a jointly valid comparison footprint is established; an extent alone is insufficient. See [the flood-reference decision](DECISIONS.md).

### CHIRPS versions and cached identity

`CHIRPS` currently routes to **v3 daily SAT** on GEE. `CHIRPS_IRI` serves **v2 daily-improved**; fallback between them changes the scientific product. V3 SAT uses IMERG Late V07 for daily partitioning of pentad totals, so comparison with IMERG must account for shared inputs.

Inspect `result.product_identity` (also returned by MCP) for version, variant, derivation, dependencies and interpretation metadata. New caches retain the identity and warnings captured at fetch time. Empty legacy identity means unknown. These are configured-product declarations, not verification of upstream asset bytes or revision. See [the identity decision](DECISIONS.md).
