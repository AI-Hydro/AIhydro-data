# aihydro-data — Architecture

> A guided tour of how `fetch("precipitation", gdf, "2020-01-01", "2020-12-31")` actually works.

---

## The three-axis model

Every previous data fetcher hard-coded all three of these:

| Axis | Question | Hard-coded before | `aihydro-data` answer |
|---|---|---|---|
| **Variable** | *What* am I fetching? | One function per variable | `products/<variable>.py` registry |
| **Source / Product** | *Where* does it come from? | Single source per variable | Priority-ordered fallback chain |
| **Region** | *Where* on Earth? | CONUS only | Automatic region detection → routing |

Separating these three axes is the core design decision. It means adding a new data source requires only (1) a `ProductSpec` entry and (2) a row in `policy.py` — no changes to the pipeline or routing logic.

---

## Request lifecycle

```
User call
─────────
fetch("precipitation", watershed_gdf, "2020-01-01", "2020-12-31")
  │
  ▼
┌──────────────────────────────┐
│  geometry/coerce_geometry()  │  GeoDataFrame / GeoJSON / (lat,lon) / WKT / GaugeID
│                              │  → canonical shapely geometry
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│  routing/detect_region()     │  shapely.bounds centroid vs CONUS bbox
│                              │  → "CONUS" | "S_ASIA" | "EUROPE" | "global" | …
└──────────────┬───────────────┘
               │
               ▼  (auto mode)
┌──────────────────────────────┐
│  routing/policy.py           │  PRODUCT_POLICY[("precipitation", "CONUS")]
│  resolve_product_ids()       │  → ["GRIDMET_PRECIP", "DAYMET_PRECIP", "CHIRPS", ...]
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│  products/__init__.py        │  get_product("GRIDMET_PRECIP")
│  get_product()               │  → ProductSpec(id="GRIDMET_PRECIP",
│                              │      source="hyriver",
│                              │      backend_config={"pygridmet_variable": "pr"},
│                              │      ...)
└──────────────┬───────────────┘
               │
               ▼  (cache miss)
┌──────────────────────────────┐
│  sources/get_backend()       │  lazy-loads sources/hyriver.py
│                              │  → Backend instance (HyRiver)
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│  Backend.fetch_timeseries()  │  calls pygridmet with (lon, lat), date range
│                              │  applies unit_conversion
│                              │  → pd.DataFrame(columns=["date","precipitation"])
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│  cache/__init__.py           │  write to ~/.aihydro/cache/data/
│                              │  key = sha256(variable+geom_wkt+start+end+agg)
└──────────────┬───────────────┘
               │
               ▼
FetchResult(
    data       = pd.DataFrame,
    product    = "GRIDMET_PRECIP",
    source     = "hyriver",
    units      = "mm/day",
    citation   = "Abatzoglou 2013 ...",
    bibtex     = "@article{...}",
    license    = "public domain",
    cache_key  = "a3f7...",
    next_steps = [{"tool": "extract_hydrological_signatures", ...}]
)
```

---

## Product registry

Each variable gets its own file under `products/`. Every file exports a `PRODUCTS: list[ProductSpec]` list. The registry's `__init__.py` auto-discovers all these lists at import time.

### `ProductSpec` fields

```python
@dataclass(frozen=True)
class ProductSpec:
    id: str                      # "CHIRPS"
    variable: str                # "precipitation"
    source: str                  # "gee" | "hyriver" | "direct_api" | "stac"
    source_dataset_id: str       # "UCSB-CHC/CHIRPS/V3/DAILY_SAT"
    coverage: list[str]          # ["global"] | ["CONUS"] | ["NORTH_AMERICA"]
    temporal_start: str          # "1981-01-01"
    temporal_end: str            # "present"
    resolution_m: int            # 5566
    timestep: str                # "daily" | "monthly" | "8day" | "static"
    units: str                   # "mm/day"
    license: str                 # "public domain (Creative Commons CC0)"
    citation: str                # full bibliographic reference
    bibtex: str                  # BibTeX entry
    homepage: str                # dataset homepage URL
    requires_extras: list[str]   # ["gee"]
    requires_auth: list[str]     # ["gee"] | []
    common_pitfalls: list[str]   # edge cases to document
    examples: list[str]          # working call snippets
    next_steps: list[dict]       # agent chaining hints
    backend_config: dict         # backend-specific parameters
```

`ProductSpec` is frozen — immutable after construction. This makes it safe to cache and share across threads.

The `backend_config` dict is the bridge between the declarative registry and the imperative backend. Each backend knows which keys to read from it:

| Backend | Keys consumed |
|---|---|
| `gee` | `gee_dataset_id`, `band`, `scale_m`, `unit_conversion`, `agg_to_monthly`, `compute_ndvi` |
| `hyriver` | `pygridmet_variable`, `pydaymet_variable`, `py3dep_resolution` |
| `direct_api` | `service` (`nwis_dv` or `chirps_iri`), `iri_url`, `parameter_code` |

---

## Routing policy

`routing/policy.py` is a pure data table:

```python
PRODUCT_POLICY: dict[tuple[str, str], list[str]] = {
    ("precipitation", "CONUS"):  ["GRIDMET_PRECIP", "DAYMET_PRECIP", "CHIRPS", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("precipitation", "global"): ["CHIRPS", "IMERG_PRECIP", "ERA5L_PRECIP", "CHIRPS_IRI"],
    ("et",  "global"):           ["MOD16_ET", "TERRACLIMATE_AET", "ERA5L_PET"],
    ("dem", "CONUS"):            ["DEM3DEP_10M", "GLO30", "SRTM"],
    # ...
}
```

`resolve_product_ids(variable, region)` performs a direct lookup, then walks a parent-region hierarchy if no exact match exists:

```
S_ASIA → ASIA → global
CONUS → NORTH_AMERICA → global
EUROPE → global
```

The pipeline tries each product ID in order and stops at the first successful fetch. A product raises `SourceUnavailable` if its backend is unavailable (not installed, not authed, or the remote is down); the pipeline catches this and tries the next.

---

## Backend adapters

All backends implement `SourceBackend` from `sources/base.py`:

```python
class SourceBackend(ABC):
    source_id: str

    def capabilities(self) -> dict:   ...
    def is_available(self) -> tuple[bool, str | None]:   ...
    def fetch_timeseries(self, spec, geometry, start, end, aggregation) -> pd.DataFrame:   ...
    def fetch_raster(self, spec, geometry, start, end) -> xr.DataArray:   ...
```

All imports inside backend files are **lazy** — the file is safe to import without any extras installed. `is_available()` does the actual import check and returns a human-readable reason on failure.

### GEE backend (`sources/gee/` package)

Split across three files to keep each under ~600 lines while preserving the import path (`from aihydro_data.sources.gee import Backend`):

- `__init__.py` — `Backend` class, `fetch_timeseries`, `fetch_raster`, `_fetch_soilgrids_raster`, `_assert_available`, `is_available`.
- `_download.py` — `_DownloadMixin`: `_open_geotiff`, `_download_image_array`, `_clip_to_polygon`, `_download_tiled`, `_coarsen_scale_for_budget`.
- `_composite.py` — `_CompositeMixin`: `fetch_multiband_composite`, `fetch_index_composite`, `_masked_median_composite`, `_extract_computed_ndvi`.

Key flow:

1. Calls `_gee_vendored/auth.py` to ensure EE is initialised.
2. Converts geometry to GeoJSON for the EE API.
3. Calls vendored `extract_timeseries()` which runs a server-side reducer.
4. Reads the `"rows"` key from the response (not `"timeseries"` — a common confusion).
5. Applies `unit_conversion` from `backend_config`.
6. Special path for computed bands (e.g. NDVI from Sentinel-2) via `_extract_computed_ndvi()`.

**Key pitfall**: MODIS products mask urban/barren pixels. Use vegetated geometries for testing.

### HyRiver backend (`sources/hyriver.py`)

Thin wrappers around:
- `pygridmet` — GridMET variables (precipitation, tmax, tmin, pet)
- `pydaymet` — Daymet variables (precipitation, tmax, tmin)
- `py3dep` — USGS 3DEP elevation
- `pygeohydro` — NLCD land cover, POLARIS soil

Coordinate order note: `pygridmet` expects `(lon, lat)`, not `(lat, lon)`.

### Direct API backend (`sources/direct_api.py`)

Two services:

- **`nwis_dv`** — USGS daily values via `dataretrieval.nwis.get_dv()`. Converts cfs → m³/s. Accepts gauge IDs passed as geometry strings (wrapped in `GaugeID` by `coerce_geometry()`).
- **`chirps_iri`** — CHIRPS v2 via IRI Data Library OPeNDAP. No auth. Server-side spatial + temporal subsetting via `xarray.open_dataset(url, engine="netcdf4")`.

---

## Cache layer

```
cache/
└── __init__.py     ← diskcache.Cache at ~/.aihydro/cache/data/
```

Cache key = `aihydro_core.primitives.hashing.content_hash({variable, start, end, aggregation, geom_wkt}, length=24)` — i.e. `sha256(json(...))` truncated to 24 chars, computed by the shared `aihydro-core` substrate so every layer of the platform (jobs, features, this cache) hashes through **one** implementation and keys never drift. For manual mode, the `product` ID is also included in the payload (different product = different cached result). For auto mode it is excluded (same data regardless of which product was selected).

Cache entries also write a manifest row via `cache/manifest.py` with: `product_id`, `source`, `license`, `citation`, `fetched_at`, `entry_count_rows`. This lets `data_get_cache_status()` report provenance without loading the actual data.

---

## Geometry normalisation

`geometry/coerce_geometry()` accepts:

| Input type | Example |
|---|---|
| `geopandas.GeoDataFrame` | `gpd.read_file("watershed.geojson")` |
| `dict` (GeoJSON) | `{"type": "Polygon", "coordinates": [...]}` |
| `shapely` geometry | `Point(-94.5, 39.1)` |
| `(lat, lon)` tuple | `(39.1, -94.5)` |
| `[west, south, east, north]` bbox | `[-95, 38, -94, 40]` |
| WKT string | `"POINT (-94.5 39.1)"` |
| USGS gauge ID string | `"03245500"` → `GaugeID("03245500")` |

`GaugeID` is a sentinel that bypasses spatial operations and routes directly to the `direct_api` / NWIS backend. It carries a `.id` string and a synthetic `.wkt` for cache keying.

---

## Agent-facing contracts

### Error envelope

Every exception raised by the pipeline carries `.to_dict()`:

```python
{
    "error": True,
    "code": "GEE_AUTH_MISSING",          # machine-parseable
    "message": "Credentials not found.", # human-readable
    "recovery": "Run ee.Authenticate()", # what to do next
    "next_tools": ["data_doctor"],       # agent chain
    "docs_anchor": "auth#gee"            # docs link fragment
}
```

### `FetchResult.next_steps`

Each product's `ProductSpec.next_steps` becomes `FetchResult.next_steps`:

```python
[
    {"tool": "extract_hydrological_signatures",
     "rationale": "Precipitation series ready — derive flow signatures."},
    {"tool": "data_describe_product",
     "rationale": "Show citation before writing up results."},
]
```

Agents read this field to chain downstream work without re-planning.

---

## Testing strategy

```
tests/
├── test_scaffold.py              smoke — package imports, contract validation
├── test_routing.py               offline — region detection, policy resolution, registry
├── test_phase1_fixes.py          offline — B1/B2/B3/B5/B6/B7 bug-fix regression tests
├── test_phase2_integrity.py      offline — spatial_support, empty-result gate
├── test_phase3_routing_cache.py  offline — verify-on-read cache, region/outlet kwargs
├── test_phase3_products.py       offline — NLDI, streamflow routing, product specs
├── test_phase4_products.py       offline — ET/DEM/SM/vegetation/optical/streamflow specs
├── test_phase_d_robustness.py    offline — require_import, retry, MCP envelope
├── test_auto_batch.py            offline — batch kwargs, BatchResult, stop-flag
├── test_cache_and_batch.py       offline — threaded manifest writes, batch concurrency
├── test_open_meteo.py            offline — Open-Meteo temperature/PET/flood products
├── test_optical_products.py      offline — Sentinel-2 / Landsat optical spec validation
├── test_indices.py               offline — spectral index formulas
├── test_stac.py                  offline — STAC product spec validation
├── test_doc_consistency.py       offline — README/PAPER counts match live registry
├── test_live_backends.py         live   — targeted: GEE, HyRiver, NWIS, routing, cache
└── test_live_sweep.py            live   — parametrized sweep: all 45 products
```

- **Offline** (`pytest -m "not live"`): ~10 seconds, 341 tests. No network, no auth. Covers registry correctness, routing logic, spec field shapes, and doc consistency.
- **Live** (`pytest tests/test_live_sweep.py`): ~15 minutes. Requires GEE auth + internet. Validates real backend responses, unit conversions, and plausibility checks.

The `conftest.py` auto-sets `AIHYDRO_DATA_NO_RETRY=1` for non-live runs, preventing retry delays from affecting the offline suite.

---

## Spatial-support model

Every `ProductSpec` declares `spatial_support: Literal["areal", "point", "reach", "gauge_point"]` (default `"areal"`). This is the **invariant** of what the value represents, not how you asked for it:

| `spatial_support` | Meaning | Example products |
|---|---|---|
| `areal` | True spatial average over the geometry | GEE, HyRiver, STAC products |
| `point` | Centroid-based point value (Open-Meteo ERA5 archive) | `OPEN_METEO_TMAX`, `OPEN_METEO_PET` |
| `reach` | Model-snapped river-reach value | `GEOGLOWS_RETRO`, `OPENMETEO_FLOOD`, `GLOFAS_STREAMFLOW` |
| `gauge_point` | Observation at a specific gauge | `NWIS_STREAMFLOW` |

The pipeline enforces honesty:
- `aggregation="basin_sum"` on a non-areal product raises `AGGREGATION_UNSUPPORTED` immediately (a sum over a point value is physically meaningless).
- `aggregation="basin_mean"` on a point/reach product is silently downgraded to `{support}_value` with an explicit note attached to the result (`FetchResult.notes`).
- `FetchResult.spatial_support` and `FetchResult.aggregation_actual` surface the actual semantics so agents/users always know what the number means.

## Verify-on-read cache

Cache keys in auto mode exclude the product ID: the same (variable, geometry, start, end, aggregation) maps to the same key regardless of which product won. This is deliberate — the data are interchangeable for the same request.

However, a cached result served by product `A` should **not** be returned if the current routing policy would never select `A` (e.g. the product was removed, its priority changed, or a better product became available). The verify-on-read check handles this:

```python
# cache_read is given the current candidate chain
hit = cache_read(cache_key, request, allowed_products=["CHIRPS", "ERA5L_PRECIP"])
# A cache hit from GRIDMET_PRECIP would be a MISS here — GRIDMET_PRECIP is not in this chain.
```

The manifest's `serving_product` is checked against `allowed_products` before returning the cached data. This closes the staleness hole without migrating cache keys or requiring a cache flush on policy changes.

---

## Geology product (Wave B4)

`products/geology.py` declares three `ProductSpec` entries that wrap pygeoglim:

```
PYGEOGLIM_ALL   — auto-routes CONUS→CONUS gpkg, global→HF shards (pygeoglim 1.4.0+)
GLIM_TILES      — GLiM-only variable alias
GLHYMPS_TILES   — GLHYMPS-only variable alias
```

The backend in `sources/pygeoglim.py` calls:
```python
glim   = pygeoglim.glim_attributes(gdf,    region="auto")
glhymp = pygeoglim.glhymps_attributes(gdf, region="auto")
```
and returns a single-row `pd.DataFrame` with all 9 CAMELS geology attrs.

Since pygeoglim 1.4.0 has `CCGM_PERMISSION_GRANTED = True`, the global shard
path (`region="global"`) is now open — any watershed on Earth gets geology attrs
as soon as the global tile files are built and uploaded to HuggingFace.

```
fetch("geology", amazon_gdf, "1990-01-01", "2020-12-31")
  → PYGEOGLIM_ALL → region="global" → HF shards → 9-attr DataFrame
```

---

## Adding a new product: checklist

1. **Add `ProductSpec`** to `products/<variable>.py` (create the file if new variable).
   - Set `backend_config` with the keys your backend reads.
   - Fill `common_pitfalls`, `examples`, `citation`, `bibtex`.
2. **Register in policy** — add a row to `routing/policy.py`.
3. **Implement backend** — if `source` is new, add a `Backend` subclass to `sources/`. If reusing an existing backend, ensure `backend_config` keys are handled.
4. **Offline test** — add a spec-shape test to `test_phase4_products.py` (or the appropriate test file).
5. **Live test** — the parametrized sweep in `test_live_sweep.py` picks it up automatically if the product is in the registry.
