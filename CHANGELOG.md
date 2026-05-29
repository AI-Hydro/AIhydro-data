# Changelog

All notable changes to `aihydro-data` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.1.6] — 2026-05-26

### Added — `fetch()` auto-batch dispatch

- **`fetch()` now auto-detects collection inputs** (list / tuple / dict / multi-row GeoDataFrame) and forwards to `fetch_batch()` transparently. Users can write `fetch("streamflow", ["03245500", "01646500"], ...)` and get back a `list[FetchResult]` without learning about the dual API. Single Points, gauge ID strings, `(lat, lon)` tuples, bbox tuples, and GeoJSON dicts continue to take the single-shot path.
- `_looks_like_collection()` helper in `_pipeline.py` does the heuristic split — covered by 10 case tests in `tests/test_auto_batch.py`.
- 3 dispatch integration tests confirm the routing decision: list of strings → batch; single Point → single; `(lat,lon)` → single.

### Fixed

- **`coerce_geometry()` now passes `GaugeID` through idempotently.** Previously, `fetch_batch()` over a list of gauge IDs would coerce each string to a `GaugeID`, then re-call `coerce_geometry()` on the `GaugeID`, which rejected it as `GEOMETRY_UNSUPPORTED_TYPE`. Added a `isinstance(geom, GaugeID): return geom` short-circuit.
- **Thread-safety race in `products._load_registry()`.** Two threads in `fetch_batch()`'s `ThreadPoolExecutor` could both pass the `if _LOADED: return` check and concurrently populate the registry, tripping the duplicate-ID guard with errors like `Duplicate ProductSpec id: 'GLO30'`. Fixed with double-checked locking via `threading.Lock`. The fast path (already-loaded) takes no lock.

### Verified

- Live test of original cookbook Recipe 3 against real NWIS: `fetch("streamflow", ["03245500", "01646500", "06892350"], "2018-01-01", "2020-12-31")` returns 3 `FetchResult`s with 1096 rows each.
- Offline suite: 199 → 212 tests, all pass in 8 s.

---

## [0.1.5] — 2026-05-26

### Added — CI + future-work doc

- **`.github/workflows/test.yml`** — refreshed CI workflow:
  - 3-version × 2-OS matrix (Python 3.10/3.11/3.12 × Ubuntu/macOS)
  - Installs `[dev,viz]` so all 199 offline tests run (was `[dev]` only)
  - Sets `AIHYDRO_DATA_NO_RETRY=1` to skip transient retries in CI
  - Lints with ruff before the test suite
  - Verifies public API + product count after each run
  - Separate `build-wheel` job builds + `twine check`s the wheel/sdist
  - `smoke-from-wheel` job re-installs from the built wheel in a clean env and confirms `≥34 products` registered
- **`ROADMAP.md`** — explicit list of near-term, mid-term, and far-term enhancement ideas, plus things deliberately out of scope. Lives next to README so contributors can see what's planned without grep-archaeology.

### Notes

This release is documentation + infra only — no library code changes. v0.1.5 is the natural stopping point before testing the full v0.1.x line end-to-end.

---

## [0.1.4] — 2026-05-26

### Added — Cookbook notebook

- **`examples/cookbook.ipynb`** — 10 working recipes covering the canonical workflows:
  1. Auto-mode fetch (global watershed)
  2. Manual mode with explicit fallback chain
  3. Batch fetch over multiple gauges
  4. `result.plot()` — auto-dispatched plots
  5. `compare()` — multi-source side-by-side
  6. Budyko diagram (P + PET + ET in one figure)
  7. Discovery: `list_products` / `get_product` / `data_validate_request`
  8. Cache inspection and invalidation
  9. Auth-free DEM via STAC (no GEE needed)
  10. `data_doctor()` health check
- 22 cells total (10 code + 12 markdown). Built via `nbformat`, valid Jupyter notebook structure.

---

## [0.1.3] — 2026-05-26

### Added — STAC backend

- **`sources/stac.py`** — real implementation of `fetch_timeseries()` and `fetch_raster()` via `pystac-client` + `stackstac`. Uses Microsoft Planetary Computer as the primary catalog (auto-signs assets when `planetary-computer` is installed), Earth Search as fallback.
- **`GLO30_STAC`** — Copernicus GLO-30 DEM via Planetary Computer. Auth-free alternative to the `GLO30` GEE product; same dataset, COG-backed.
- **`ESA_WORLDCOVER_STAC`** — ESA WorldCover landcover via Planetary Computer. Auth-free alternative to the `ESA_WORLDCOVER` GEE product.
- Both new products wired into `routing/policy.py` as the last fallback for DEM and landcover chains (after GEE-based primaries fail).
- 13 new tests in `tests/test_stac.py` covering backend structure, geometry-to-bbox conversion, product registry shape, and the `[stac]` extras availability check.

### Notes

- **Product count**: 32 → 34.
- The STAC backend signature is generic — any COG-backed Planetary Computer collection can be added as a product by writing only a `ProductSpec` entry with `source="stac"` and the right `backend_config` keys (`stac_collection`, `stac_asset`, `stac_resolution`). No backend code changes needed.
- Live STAC fetches are intentionally NOT in `test_live_sweep.py` — they need a different harness (cloud cost budget, asset signing flow). Live validation lives with the cookbook notebook.
- The `aihydro_data/sources/stac.py` previous `NotImplementedError` stubs are now removed.

### Fixed

- `tests/test_phase3_products.py::test_all_lc_registered` — relaxed strict-equality check on landcover IDs to `issubset` so new STAC products don't break the test.

---

## [0.1.2] — 2026-05-26

### Added — Visualization layer (3 tiers)

- **`aihydro_data/viz/` subpackage** — research-grade plotting layer. All matplotlib + folium imports are lazy; offline tests run without `[viz]` installed.

#### Tier 1 — auto-dispatch
- `viz.auto_plot(result)` — dispatches on data shape: DataFrame → time series (line for streamflow, bar for short precip windows, line for everything else), xarray.DataArray → imshow with variable-aware colormap, GeoDataFrame → polygon plot.
- **`FetchResult.plot()`** method wired onto the Pydantic model — `r.plot()` and `r.plot(logy=True)` now work natively.
- **`FetchResult.map()`** method — folium interactive map preview with geometry + optional raster overlay.

#### Tier 2 — hydrology plots (`viz.hydrology`)
- `flow_duration_curve(series)` — exceedance probability curve with optional log-y.
- `climatology(series, show_iqr=True)` — monthly mean ± IQR; the standard QA plot for "does this look like Iowa precip?".
- `double_mass(a, b)` — cumulative A vs cumulative B for cross-source consistency checking.
- `budyko(precip, pet, et)` — Budyko diagram with energy/water limit lines + Choudhury n=2.6 curve.
- `aridity_index_plot(precip, pet)` — monthly P/PET ratio time series.

#### Tier 3 — multi-source comparison (`viz._compare`)
- `compare(products, geometry, start, end, plots=[...])` — fetches each product and renders multi-panel side-by-side: `timeseries`, `climatology`, `scatter`, `fdc`, `double_mass`. Per-product colour palette is fixed and consistent across panels.

#### Infrastructure
- `viz/_common.py` — `PRODUCT_COLORS` table (print-friendly), `_resolve_color`, `_detect_value_column`, citation overlay helpers.
- `viz/spatial.py` — `map_preview()` standalone API, used by `FetchResult.map()`.
- 24 new tests in `tests/test_viz.py` covering auto-dispatch, FetchResult method wiring, all hydrology plots, compare API signature, and import safety. Headless via matplotlib `Agg`.

### Fixed

- **Submodule shadowing**: renamed `viz/compare.py` → `viz/_compare.py`. The previous name collided with the `compare()` function exported by `viz/__init__.py` — `from viz import compare` would return the function until the first call, after which Python's submodule machinery would re-bind `viz.compare` to the module, breaking subsequent imports.

---

## [0.1.1] — 2026-05-26

### Added

- **`CHIRPS_IRI`** — auth-free CHIRPS v2 precipitation via IRI Data Library OPeNDAP (`direct_api` backend). Requires `pip install aihydro-data[opendap]`. No GEE account needed. Added as last-resort fallback in all precipitation routing chains.
- **`opendap` extra** — `xarray + netCDF4` minimal dependency set for the IRI backend.
- **`conftest.py`** — sets `AIHYDRO_DATA_NO_RETRY=1` automatically for all non-live test runs; offline suite now completes in ~7 seconds with no retry delays.
- **`tests/test_live_backends.py`** — 6 targeted live tests: GEE CHIRPS, HyRiver GridMET, NWIS, auto-mode routing, cross-source sanity, cache round-trip.
- **`tests/test_live_sweep.py`** — parametrized sweep over all 32 non-static products; smart xfail logic distinguishes config errors (hard FAIL) from transient server issues (xfail).
- **`GaugeID` sentinel class** in `geometry/__init__.py` — wraps USGS site number strings so they flow through the geometry pipeline without triggering WKT parsing.
- **`sources/_retry.py`** — `call_with_retry()` with exponential backoff, transient-error detection, and `AIHYDRO_DATA_NO_RETRY` escape hatch.

### Fixed

- **GEE backend silent empty DataFrames** — vendored `extract_timeseries()` writes results under `"rows"` key, not `"timeseries"`. Backend now reads both keys and raises `RuntimeError` when the result is unexpectedly empty.
- **MOD16 ET/PET 10× too high** — LP DAAC stores MOD16A2GF values as integers × 10. Applied `unit_conversion: 0.1` to both `MOD16_ET` and `MOD16_PET`.
- **ERA5L_PET negative values** — ERA5-Land `potential_evaporation_sum` is a downward flux (negative convention). Fixed `unit_conversion: -1000.0` (sign flip + m → mm/day).
- **MODIS LAI wrong band name** — `Lai_500m` does not exist; actual band is `Lai`. Fixed in `products/vegetation.py`.
- **Sentinel-2 NDVI empty result** — `band: ""` failed silently. NDVI is now computed server-side via `image.normalizedDifference(['B8','B4'])` using the new `_extract_computed_ndvi()` path in `sources/gee.py`.
- **pygridmet coordinate order** — `pygridmet` expects `(lon, lat)` not `(lat, lon)`. Fixed `(c.y, c.x)` → `(c.x, c.y)` in `sources/hyriver.py`.
- **NWIS gauge ID rejected by geometry coercer** — strings representing gauge IDs (e.g. `"03245500"`) were rejected by `coerce_geometry()`. Now wraps non-WKT strings in `GaugeID` and routes to `direct_api` backend.
- **Auto-mode cache key included product** — cache key in `_pipeline.py` now excludes the product ID for auto-mode fetches; includes it only for manual mode.
- **Dynamic World >5000 GEE elements error** — fixed by marking `DYNAMIC_WORLD.timestep = "static"` (raster-only; daily collection is too large for FeatureCollection mapping).

### Removed / Replaced

- **`MSWEP`** — replaced by `IMERG_PRECIP` (`NASA/GPM_L3/IMERG_V07`). The MSWEP GEE Community asset (`projects/sat-io/...`) was removed from GEE upstream in 2024.
- **`SSEBOP_ET`** — replaced by `TERRACLIMATE_AET` (`IDAHO_EPSCOR/TERRACLIMATE`, band `aet`). The USGS/fews_net GEE ImageCollection was deprecated upstream.
- **`ESA_CCI_SM`** — removed. The `projects/sat-io/open-datasets/ESA_CCI/ESA_CCI_SM_COMBINED` GEE Community asset was deleted upstream.
- All three replacements are registered in `routing/policy.py` and tested in the live sweep.

---

## [0.1.0] — 2026-05-25

### Added — Phases 2–7: Full implementation

#### Phase 2: Precipitation reference vertical

- `products/precipitation.py` — 5 products: `CHIRPS`, `IMERG_PRECIP`, `ERA5L_PRECIP`, `GRIDMET_PRECIP`, `DAYMET_PRECIP`.
- `sources/gee.py` — GEE backend: auth init, geometry → GeoJSON, `extract_timeseries()` dispatch, unit conversion.
- `sources/hyriver.py` — HyRiver backend: `pygridmet` (GridMET) and `pydaymet` (Daymet) adapters.
- `routing/regions.py` — CONUS bounding box, Pfafstetter level-2 region table.
- `routing/detect.py` — `detect_region(geometry)` → region string.
- `routing/policy.py` — `PRODUCT_POLICY` declarative table + `resolve_product_ids()` with parent-region fallback.
- `_pipeline.py` — end-to-end pipeline: geometry coercion → region detection → product resolution → backend dispatch → cache write → `FetchResult`.
- Disk cache in `cache/__init__.py` using `diskcache`; key = `sha256(variable + geom_wkt + start + end + aggregation)`.

#### Phase 3: CONUS migration

- `products/streamflow.py` — `NWIS_STREAMFLOW` via `dataretrieval`.
- `products/temperature.py` — 7 products: `GRIDMET_TMAX/TMIN`, `DAYMET_TMAX/TMIN`, `ERA5L_TMAX/TMIN/TMEAN`.
- `products/landcover.py` — 3 products: `NLCD`, `ESA_WORLDCOVER`, `DYNAMIC_WORLD`.
- `products/soil.py` — 2 products: `POLARIS`, `SOILGRIDS`.
- `sources/direct_api.py` — NWIS daily values backend using `dataretrieval.nwis.get_dv()`; CFS → m³/s conversion.

#### Phase 4: Global gap-filling

- `products/et.py` — 5 products: `MOD16_ET`, `MOD16_PET`, `TERRACLIMATE_AET`, `ERA5L_PET`, `GRIDMET_PET`.
- `products/dem.py` — 4 products: `GLO30`, `SRTM`, `MERIT_DEM`, `DEM3DEP_10M`.
- `products/soil_moisture.py` — 1 product: `SMAP_SM`.
- `products/vegetation.py` — 3 products: `MODIS_NDVI`, `SENTINEL2_NDVI`, `MODIS_LAI`.
- All routing policy entries for `et`, `pet`, `dem`, `soil_moisture`, `ndvi`, `lai`.

#### Phase 5: Cache + provenance

- `cache/manifest.py` — per-entry provenance tracking: origin, fetched_at, source, product_id, license.
- `FetchResult` carries `.cache_key`, `.fetched_at`, `.license`, `.citation`, `.bibtex`.

#### Phase 6: Batch fetching

- `geometry/batch.py` — `batch_fetch()` for N-geometry parallel dispatch using `concurrent.futures`.
- `fetch()` API extended: `geometry` may be a list; returns a list of `FetchResult`.

#### Phase 7: MCP tools + help surface

- `mcp/__init__.py` — 9 MCP tools registered via `aihydro.tools` entry-point:
  - `data_fetch`, `data_batch_fetch`, `data_list_products`, `data_describe_product`
  - `data_validate_request`, `data_get_cache_status`, `data_invalidate_cache`
  - `data_doctor`, `data_help`
- `help_topics/` — 8 bundled markdown files: `first_fetch`, `auth`, `fallback`, `batch`, `products`, `caching`, `deprecations`, `errors`. Version-pinned to the installed package.
- `data_validate_request()` — pre-flight check: date range coverage, geography coverage, estimated size; returns candidate products if the requested one fails.
- `data_doctor()` — structured environment report: backend availability, GEE auth path, cache stats, missing extras.

---

## [0.0.1] — 2026-05-25

### Added — Phase 1: Scaffold

Initial package skeleton.

- `pyproject.toml` with extras: `[gee]`, `[stac]`, `[hyriver]`, `[direct]`, `[cache]`, `[mcp]`, `[viz]`, `[all]`, `[dev]`.
- Public API stubs: `fetch()`, `list_products()`, `get_product()` (bodies in Phase 2+).
- `contracts.py` — `ProductSpec`, `FetchRequest`, `FetchResult`, `FetchError` Pydantic models.
- `exceptions.py` — typed exceptions with `.to_dict()` envelope (`code` / `recovery` / `next_tools` / `docs_anchor`) for agent-friendly error handling.
- `products/` — declarative variable-centric registry; lazy module discovery.
- `sources/base.py` — `SourceBackend` ABC + `get_backend(source_id)` lazy loader.
- `routing/`, `geometry/`, `cache/` — package skeletons. `coerce_geometry()` handles GDF / GeoJSON / shapely / `(lat, lon)` / bbox inputs.
- `cli.py` — `aihydro-data list-products` and `aihydro-data describe <id>` subcommands.
- GEE modules vendored under `sources/_gee_vendored/` from `aihydro-tools` v1.7.0.
- `tests/test_scaffold.py` — smoke tests: imports, contract validation, registry discovery.

[0.1.5]: https://github.com/AI-Hydro/aihydro-data/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/AI-Hydro/aihydro-data/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/AI-Hydro/aihydro-data/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/AI-Hydro/aihydro-data/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/AI-Hydro/aihydro-data/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/AI-Hydro/aihydro-data/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/AI-Hydro/aihydro-data/releases/tag/v0.0.1
