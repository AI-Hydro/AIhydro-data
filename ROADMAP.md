# Roadmap & Future Enhancements

This file tracks ideas that are scoped but not yet shipped. Items move out of here into the CHANGELOG when they land.

---

## Near-term (next 2–3 minor releases)

### Additional data products

| Product | Variable | Source | Why | Effort |
|---|---|---|---|---|
| **GRDC streamflow** | streamflow | direct_api | Global gauge network (extends NWIS beyond CONUS) | Medium |
| **IMD gridded precipitation** | precipitation | direct_api | South Asia primary — best for Indian subcontinent | Medium |
| **ASCAT soil moisture** | soil_moisture | stac (ESA) | Adds SM coverage 2007+ (SMAP starts 2015) | Medium |
| **CHELSA climatologies** | precipitation, tmax, tmin | stac | High-res (1km) climatology baselines for mountain hydrology | Medium |
| **Sentinel-1 backscatter** | soil_moisture (derived) | stac | SAR-based SM; complements optical NDVI | High |
| **HLS NDVI** | ndvi | stac | Harmonized Landsat-Sentinel-2; 30m, ~3-day cadence | Medium |
| **Landsat surface temperature** | lst | stac | Long archive (1984+), complements MOD11_LST | Medium |
| **MODIS MOD11 LST** | lst | gee | Already a planned variable, just needs a ProductSpec | Low |
| **MERIT-Hydro flow accumulation** | flow_acc | local_cache | Global flow routing inputs | Low (rehoused from monolith) |

### Backend / infra

- **`local_cache` backend** — formalise the `merit_manager` / `wbd_layers` patterns from the monolith into a clean backend. Useful for any pre-downloaded archive (Daymet zarr mirrors, NetCDF dumps, MERIT-Hydro tiles).
- **Async batch fetcher** — current `batch_fetch()` uses `concurrent.futures.ThreadPoolExecutor`; rewrite with `asyncio` for cleaner cancellation and progress tracking. Especially valuable for 100+ catchment LSH workflows.
- **Server-side polygon reducer for STAC** — currently STAC backend stacks → reduces locally. For large basins this transfers more bytes than needed. Switch to `odc-stac` with a server-side mean reducer where supported.
- **Cache compression** — Parquet (via diskcache) or netCDF for raster cubes. Currently uses pickle which is fast but bulky.

### Agent / MCP

- **`data_explain_failure(error_dict)`** MCP tool — takes an error envelope and returns plain-English explanation + ranked recovery actions. Saves the agent from re-grepping error codes.
- **`data_estimate_cost(request)`** — for paid backends (future Element84 paid tiers, NASA Earthdata fee tiers) return $ estimate before fetch.
- **Session-scoped cache** — cache key includes a session ID so a long-running agent session doesn't accidentally cross-contaminate. Useful for paper-replicability.

### Visualization

- **`viz.gee_natural_color`** — RGB Sentinel-2 / Landsat snapshot as a folium overlay. Closes the loop on "show me what this basin looks like".
- **`viz.hydrograph_with_precip`** — twin-axis hydrograph + precip bars, the classic flood-event plot.
- **Interactive `compare_dashboard()`** — Plotly version of `viz.compare` with hover details, range slider.

---

## Mid-term (3–6 months)

### Wave 3: split monolith into siblings

`aihydro-data` is the first of the 5-package split. Once stable, the remaining 4 follow:

- **`aihydro-core`** — sessions, projects, discovery, `run_python`. No geo deps. ~25 tools.
- **`aihydro-watershed`** — delineation, signatures, TWI, curve number, geomorphics. Depends on `aihydro-data`. ~10 tools.
- **`aihydro-modelling`** — HBV, LSTM, metrics, training runner. Opt-in PyTorch. ~6 tools.
- **`aihydro-lsh`** — wraps `camels-attrs` v1.1.0, adds batch + global + regionalization. ~8 tools.
- **`aihydro-tools`** — becomes meta-package that does `pip install aihydro-{core,data,watershed,modelling,lsh}`. Zero breaking change for existing users.

### `aihydro-data` deprecation shims

Once Wave 3 lands, the current monolith fetchers (`fetch_streamflow_data`, `fetch_forcing_data`, `fetch_lulc_data`, `fetch_soil_data_polaris`) become thin shims in `aihydro-tools` that:

1. Forward to `aihydro_data.fetch(...)` with translated args
2. Emit a one-time deprecation note in the response
3. Stay working until at least Wave 5

Tracked separately so the monolith never breaks for downstream users.

---

## Far-term (research / aspirational)

- **Provenance ledger** — every `FetchResult` writes to a per-project ledger (`.aihydro/provenance.jsonl`) with full request hash + license + citation. The ledger feeds a one-click "data availability statement" for papers.
- **Reproducibility hash** — a single SHA that summarises a full analysis (data versions + code git ref). Citing the hash means anyone can reproduce.
- **Smart fallback learning** — track which fallbacks are taken per region/variable. Reorder the policy table dynamically based on real success rates.
- **Climate scenario fetching** — `fetch_scenario("precipitation", basin, "2050-2080", scenario="ssp585")` for downscaled CMIP6 (NEX-GDDP, LOCA2). Same contract, different time horizons.
- **Native xarray-DataTree interface** — return a single `DataTree` for multi-variable, multi-time fetches. The whole library becomes one node-tree the user can index naturally.

---

## Won't ship (deliberately out of scope)

- **HydroMT integration** — studied its plugin model; too heavy for our needs.
- **Sphinx docs site** — README + ARCHITECTURE + cookbook cover what users need; full Sphinx would be doc-fan-fiction.
- **Web UI** — `aihydro-data` is a library. UI work belongs in the AI-Hydro VS Code extension.
- **Built-in geocoder** — users pass geometries directly; geocoding belongs upstream.

---

## How to propose an enhancement

1. Open an issue with the use case (not the implementation).
2. If it's a new data product: link the dataset homepage + check the licence + note auth requirements.
3. If it's a new backend: sketch the `SourceBackend.fetch_timeseries()` signature in the issue body.
4. PRs welcome — see `ARCHITECTURE.md` § "Adding a new product: checklist".
