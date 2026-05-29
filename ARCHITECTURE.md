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

### GEE backend (`sources/gee.py`)

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

Cache key = `sha256(json({variable, start, end, aggregation, geom_wkt}))`. For manual mode, the `product` ID is also included in the key (different product = different cached result). For auto mode it is excluded (same data regardless of which product was selected).

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
├── test_scaffold.py          smoke — package imports, contract validation
├── test_routing.py           offline — region detection, policy resolution, registry
├── test_phase4_products.py   offline — ET/DEM/SM/vegetation product specs
├── test_live_backends.py     live   — targeted: GEE, HyRiver, NWIS, routing, cache
└── test_live_sweep.py        live   — parametrized sweep: all 32 products
```

- **Offline** (`pytest -m "not live"`): ~7 seconds. No network, no auth. Covers registry correctness, routing logic, spec field shapes.
- **Live** (`pytest tests/test_live_sweep.py`): ~13 minutes. Requires GEE auth + internet. Validates real backend responses, unit conversions, and plausibility checks.

The `conftest.py` auto-sets `AIHYDRO_DATA_NO_RETRY=1` for non-live runs, preventing retry delays from affecting the offline suite.

---

## Adding a new product: checklist

1. **Add `ProductSpec`** to `products/<variable>.py` (create the file if new variable).
   - Set `backend_config` with the keys your backend reads.
   - Fill `common_pitfalls`, `examples`, `citation`, `bibtex`.
2. **Register in policy** — add a row to `routing/policy.py`.
3. **Implement backend** — if `source` is new, add a `Backend` subclass to `sources/`. If reusing an existing backend, ensure `backend_config` keys are handled.
4. **Offline test** — add a spec-shape test to `test_phase4_products.py` (or the appropriate test file).
5. **Live test** — the parametrized sweep in `test_live_sweep.py` picks it up automatically if the product is in the registry.
