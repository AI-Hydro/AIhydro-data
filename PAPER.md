# aihydro-data: A Variable-Centric, Region-Aware Data Acquisition Engine for Globally-Robust Hydrological Modelling

> **Status:** working draft / paper-notes. This document consolidates the design,
> contributions, and evaluation of `aihydro-data` in a form that can be converted
> into a manuscript (candidate venues: *Journal of Open Source Software* (JOSS),
> *Environmental Modelling & Software*, *Geoscientific Model Development*).
> `[TODO]` markers flag where external citations, additional experiments, or
> author/affiliation metadata are still needed.

---

## Abstract

Hydrological analysis is bottlenecked by data acquisition. Existing tools hard-code
a single data source per variable and are almost universally scoped to the
contiguous United States (CONUS): GridMET for precipitation, USGS NWIS for
streamflow, POLARIS for soil. Applying the same workflow outside CONUS — or
surviving a transient outage of the primary source — requires rewriting the
fetcher from scratch. We present `aihydro-data`, a Python library that reframes
data acquisition as a *declarative routing problem* over three orthogonal axes —
**variable** (*what*), **product/source** (*where from*), and **region**
(*where on Earth*) — unifying Google Earth Engine (GEE), the HyRiver stack,
STAC catalogues (Microsoft Planetary Computer), and direct HTTP/OPeNDAP APIs
behind a single `fetch()` call. The engine automatically (i) detects the region
of an input geometry, (ii) resolves an ordered, region-specific list of candidate
products from a pure policy table, (iii) walks that fallback chain until one
backend succeeds, and (iv) returns a provenance-rich result envelope carrying
the served product, license, citation, decision trail, and machine-readable
next-step hints. The current release registers **42 products across 14 variables
and 4 backends**, of which **19 are auth-free** and **31 offer global coverage**.
A continental benchmark across **7 watersheds spanning 6 regions** (0.4 km² alpine
headwater to 3.6 million km² Congo basin) succeeds on **64 of 74** variable×basin
requests; every failure is either an architecturally-known gap (no global
streamflow product is registered — deferred) or an upstream provider limit on
an extreme input, with **zero engine defects**. The library additionally exposes
its full capability through nine Model Context Protocol (MCP) tools, making it
directly callable by large-language-model agents.

`[TODO: tighten abstract to target-venue word limit once venue chosen.]`

---

## 1. Introduction & Statement of Need

### 1.1 The problem

A hydrologist who wants "daily precipitation over this watershed for 2010–2020"
faces three coupled decisions that current tooling forces them to resolve by hand:

1. **Which variable** — and what canonical name/units does each provider use?
2. **Which product/source** — GridMET, CHIRPS, ERA5-Land, IMERG…? Each has its
   own API, coordinate conventions, unit quirks, temporal coverage, and auth.
3. **Which region** — most authoritative products are geographically locked
   (CONUS, North America, 60°S–60°N), so the right answer depends on *where on
   Earth* the watershed is.

Conventional libraries collapse all three decisions into a single hard-coded
function per variable. This has two consequences:

- **No global portability.** A workflow validated in Kansas fails in the Ganges
  or the Congo because the hard-coded source does not cover the region, and there
  is no defined fallback. The user must locate, install, learn, and wire in a new
  provider — re-deriving the same routing logic informally each time.
- **No robustness.** Upstream geoscience services exhibit frequent transient 5xx
  spikes, quota exhaustion, and auth lapses. A single failed request breaks the
  workflow even when an equivalent alternative product would have succeeded.

### 1.2 The contribution

`aihydro-data` turns acquisition into *one call, every region, every variable,
with documented fallbacks*. The core design decision is to **separate the three
axes** and make routing a declarative table rather than imperative code:

```python
from aihydro_data import fetch

result = fetch("precipitation", watershed_gdf, "2010-01-01", "2020-12-31")
result.product   # "CHIRPS"        — auto-selected for the geometry's region
result.source    # "gee"
result.data      # pd.DataFrame | xr.DataArray
result.citation  # full bibliographic reference for write-up
result.fallback_history  # the ordered decision trail the router followed
```

Adding a new data source requires **no change to the pipeline or routing logic** —
only (1) a declarative `ProductSpec` entry and (2) one row in the policy table.
This is what makes global robustness a *maintainable* property rather than an
ever-growing thicket of conditionals.

### 1.3 Why an agent-facing design

`aihydro-data` is the data backbone of the AI-Hydro toolchain, an LLM-agent system
for hydrological analysis. Every result carries `units`, `license`, `citation`,
`bibtex`, and `next_steps` (machine-readable follow-on tool hints); every failure
returns a structured envelope with a `code`, a plain-English `recovery` action,
and `next_tools` to try. This lets an agent chain downstream work and self-recover
from failures without re-planning — and equally benefits human users, who get
citations and recovery guidance for free. `[TODO: cite MCP spec; cite AI-Hydro
system paper if/when it exists.]`

---

## 2. Related Work

`[TODO: expand with proper citations.]`

- **HyRiver** (Chegini et al.) — an excellent, well-engineered family of Python
  clients (`pygridmet`, `pydaymet`, `py3dep`, `pygeohydro`, `pynhd`) for
  *primarily US* hydrology web services. `aihydro-data` *wraps* HyRiver as one of
  its four backends rather than competing with it, and adds the cross-source
  routing/fallback layer that HyRiver does not aim to provide.
- **Google Earth Engine** (Gorelick et al., 2017) — planetary-scale analysis, but
  requires authentication, has a steep API, and exposes no notion of
  variable-level fallback across assets. `aihydro-data` vendors a minimal GEE
  time-series reducer and presents GEE assets as ordinary products in the registry.
- **STAC / `stackstac` / `odc-stac` / Planetary Computer** — standardised cloud
  catalogue access; used here as the auth-free escape hatch for large rasters that
  exceed GEE's synchronous export ceiling.
- **Caravan** (Kratzert et al., 2023) and **CAMELS** family — *curated, static*
  large-sample datasets. Complementary: `aihydro-data` is a *live, on-demand*
  acquisition engine for arbitrary geometries, not a fixed catchment set. (Caravan
  is a candidate future backend for global observed streamflow; see §8.)
- **Single-source fetchers** (e.g. bespoke GridMET/NWIS scripts) — the status quo
  this work replaces.

**Gap addressed:** to our knowledge no existing open library treats
(variable × source × region) as orthogonal axes with a declarative,
region-aware fallback policy and a provenance-and-recovery-rich result contract
designed for both human reproducibility and autonomous agent use.

---

## 3. Architecture & Methods

### 3.1 The three-axis model

| Axis | Question | Hard-coded before | `aihydro-data` mechanism |
|---|---|---|---|
| **Variable** | *What* am I fetching? | one function per variable | `products/<variable>.py` registry |
| **Source / Product** | *Where* from? | single source per variable | priority-ordered fallback chain |
| **Region** | *Where* on Earth? | CONUS only | automatic detection → routing table |

### 3.2 Request lifecycle

```
fetch("precipitation", watershed_gdf, "2010-01-01", "2020-12-31")
   │
   ├─ geometry.coerce_geometry()    GeoDataFrame | GeoJSON | shapely | (lat,lon) | bbox | WKT | GaugeID
   │                                → canonical shapely geometry
   ├─ routing.detect_region()       centroid/bbox vs region table → "CONUS"|"S_ASIA"|…|"global"
   ├─ routing.resolve_product_ids() PRODUCT_POLICY[(variable, region)] → ordered [primary, …fallbacks]
   │                                (walks parent-region hierarchy on miss)
   ├─ for product_id in chain:      get_product() → ProductSpec
   │     get_backend(spec.source)   lazy-load gee|hyriver|stac|direct_api
   │     backend.fetch_*()          → DataFrame (timeseries) | DataArray (raster)
   │     on transient error: retry; on SourceUnavailable: next candidate
   ├─ cache write                   key = sha256(variable+geom_wkt+start+end+agg[+product])
   └─ FetchResult(data, product, source, units, license, citation, bibtex,
                  cache_key, next_steps, fallback_history)
```

### 3.3 Product registry (the *variable* axis)

Each variable is a file under `products/` exporting `PRODUCTS: list[ProductSpec]`;
`products/__init__.py` auto-discovers them at import. `ProductSpec` is a **frozen
Pydantic model** — immutable (safe to cache/share across threads), self-validating
at construction, and JSON-schema-serialisable (the same schema becomes the MCP tool
parameter schema and the structured discovery/`describe` responses). Key fields:

- *Identity*: `id`, `variable`, `source`, `source_dataset_id`
- *Capabilities*: `coverage[]`, `temporal_start/end`, `resolution_m`, `timestep`, `units`
- *Provenance*: `license`, `citation`, `bibtex`, `homepage`
- *Requirements*: `requires_extras[]`, `requires_auth[]`
- *Agent affordances*: `common_pitfalls[]`, `examples[]`, `next_steps[]`
- *Bridge to backend*: `backend_config{}` — the free-form dict each backend reads
  (e.g. `{"gee_dataset_id": …, "band": …, "scale_m": …, "unit_conversion": …}`)

`backend_config` is the seam between the *declarative* registry and the
*imperative* backend: the registry says **what** to fetch and **how to normalise
it**; the backend knows **how** to talk to the provider.

### 3.4 Routing policy (the *region* axis)

`routing/policy.py` is a pure data table mapping `(variable, region) → ordered
product IDs` — *no `if/else`, no source-specific code*:

```python
PRODUCT_POLICY = {
  ("precipitation","CONUS"):  ["GRIDMET_PRECIP","DAYMET_PRECIP","CHIRPS","ERA5L_PRECIP","CHIRPS_IRI"],
  ("precipitation","global"): ["CHIRPS","IMERG_PRECIP","ERA5L_PRECIP","CHIRPS_IRI"],
  ("tmax","global"):          ["ERA5L_TMAX","OPEN_METEO_TMAX"],
  ("dem","CONUS"):            ["GLO30","DEM3DEP_10M","SRTM","GLO30_STAC"],
  # …
}
```

`resolve_product_ids(variable, region)` does a direct lookup, then walks a
parent-region hierarchy on a miss (`CONUS → NORTH_AMERICA → global`;
`S_ASIA → ASIA → global`), with `global` as the universal fallback. **The table
*is* the logic** — editing routing means editing data, which is reviewable,
diffable, and testable in isolation.

A recurring design pattern in the table is the **auth-free tail**: each chain
ends with a product that needs no credentials (e.g. `CHIRPS_IRI` via IRI OPeNDAP
for precipitation; `OPEN_METEO_*` via the Open-Meteo ERA5 archive for
temperature/PET; `*_STAC` via Planetary Computer for DEM/landcover). This
guarantees a usable answer even when GEE credentials are absent or quota is
exhausted — the single most common real-world failure mode.

### 3.5 Backend adapters (the *source* axis)

All backends implement the `SourceBackend` ABC (`capabilities`, `is_available`,
`fetch_timeseries`, `fetch_raster`). **All provider imports are lazy**, so the
package imports cleanly with no extras installed; `is_available()` performs the
real dependency/auth probe and returns a human-readable reason on failure.

| Backend | Providers wrapped | Auth | Products |
|---|---|---|---|
| `gee` | Google Earth Engine assets (CHIRPS, ERA5-Land, IMERG, MODIS, GLO-30, SMAP, SoilGrids, Sentinel-2/Landsat, …) | GEE | 23 |
| `hyriver` | `pygridmet`, `pydaymet`, `py3dep`, `pygeohydro` | none | 10 |
| `direct_api` | USGS NWIS; CHIRPS-IRI OPeNDAP; **Open-Meteo ERA5 archive** | none | 5 |
| `stac` | Microsoft Planetary Computer (WorldCover, GLO-30, …) | none | 4 |

### 3.6 Result, provenance, and the decision trail (Phase D)

`FetchResult` always carries provenance (`product`, `source`, `license`,
`citation`, `bibtex`) plus two agent/reproducibility affordances:

- **`fallback_history`** — an ordered list of every candidate the router
  considered: `{"product", "source", "outcome": "served"|"failed"|"rejected",
  "reason"}`. The final entry's outcome is `"served"`. This exposes *why* a
  backend was chosen, not merely which — the same provenance idea proven in the
  watershed-delineation router, lifted into the shared engine.
- **A `validate=` quality-gate callback on `fetch()`** — lets a caller reject a
  low-quality result (e.g. too few valid days, degenerate area) and force the
  next fallback, mirroring the delineation router's escalation logic.

### 3.7 Agent-facing error contract

Every exception subclasses `AihydroDataError` and serialises via `.to_dict()` to:

```json
{"error": true, "code": "GEE_AUTH_MISSING", "message": "...",
 "recovery": "Run `python -c \"import ee; ee.Authenticate()\"`",
 "next_tools": ["data_doctor"], "docs_anchor": "auth#gee"}
```

The taxonomy is deliberately small and *actionable*: `SourceUnavailable`,
`RegionUnsupported`, `AuthRequired`, `DateOutOfRange`, `GeometryInvalid`,
`FetchTooLarge`. Critically, only *transient* upstream errors are retried
(connection resets, 5xx, timeouts); structured/permanent errors (4xx, auth,
unsupported region) re-raise immediately so no backoff is wasted on something
that will not improve (`sources/_retry.py`).

### 3.8 Caching & geometry normalisation

A `diskcache` store at `~/.aihydro/cache/data/` keys on
`sha256(variable, start, end, aggregation, geom_wkt[, product])` — `product` is
included only in manual mode (a pinned product is a distinct result; in auto mode
the served data is keyed independently of which candidate won). A companion
manifest records `product_id, source, license, citation, fetched_at, rows` so
provenance is inspectable without loading the data. `coerce_geometry()` accepts
GeoDataFrame, GeoJSON dict, shapely geometry, `(lat,lon)`, bbox, WKT, and a
`GaugeID` sentinel that routes gauge-ID strings straight to the NWIS backend.

---

## 4. Implementation Summary (current release)

- **Version:** 0.1.7 (pre-PyPI)  `[TODO: pin to release tag for camera-ready.]`
- **Products:** 42 across 14 variables — precipitation (6), tmax (4), tmin (4),
  tmean (1), pet (4), et (2), dem (5), landcover (4), soil (2), soil_moisture (1),
  ndvi (2), lai (1), optical (5), streamflow (1).
- **Backends:** gee (23), hyriver (10), direct_api (5), stac (4).
- **Auth-free products:** 19 of 42. **Global-coverage products:** 31 of 42.
- **Public API:** `fetch`, `list_products`, `get_product` + 9 MCP tools
  (`data_fetch`, `data_batch_fetch`, `data_list_products`, `data_describe_product`,
  `data_validate_request`, `data_get_cache_status`, `data_invalidate_cache`,
  `data_doctor`, `data_help`).
- **Tests:** ~300 offline tests (`pytest -m "not live"`, no network/auth, ~10 s) +
  a parametrized live sweep over the full registry.

---

## 5. Evaluation

### 5.1 Continental robustness benchmark

To test the central claim — *globally robust acquisition* — we delineated 7
watersheds spanning 6 regions and 4 orders of magnitude in area, then ran
`fetch(mode="auto")` for every applicable variable on each. This is an
end-to-end test of detection → routing → fallback → backend → result.

| Basin | Region | Area (km²) | Delineation method | Vars OK |
|---|---|---|---|---|
| africa_congo_full | AFRICA | 3,604,054 | merit_basins_hybrid | 5/8 |
| conus_boulder | CONUS | 2,276 | (NLDI/3DEP) | 10/11 |
| africa_congo_trib | AFRICA | 31.7 | — | 10/11 |
| australia_se | OCEANIA | 3.9 | merit_gee | 10/11 |
| samerica_andes | SOUTH_AMERICA | 3.8 | merit_gee | 10/11 |
| europe_alps | EUROPE | 2.8 | merit_gee | 9/11 |
| sasia_nepal | S_ASIA | 1.2 | merit_gee | 10/11 |

**Aggregate: 64 / 74 variable×basin requests succeeded (86%).**

### 5.2 Failure analysis (zero engine defects)

All 10 failures fall into two architecturally-understood classes:

- **7 × streamflow** — no global streamflow *product* is registered
  (`RegionUnsupported: REGION_NO_POLICY` outside CONUS; the one CONUS case used a
  polygon rather than a gauge ID, `ALL_BACKENDS_FAILED`). This is the deliberately
  **deferred Phase E**: GRDC, the obvious global gauge network, has *no open
  programmatic API* (its download is a Terms-of-Use-gated web form under WMO data
  rules), so a compliant backend cannot be written as originally scoped. Candidate
  replacements (Caravan-via-GEE = observed/basin-keyed; GloFAS-via-CDS =
  modelled/any-location) are under evaluation. **This is a missing product, not an
  engine fault.**
- **3 × GEE vegetation (ndvi/lai) on extreme inputs** — two are the 3.6 M km²
  Congo basin, where GEE's server-side reducer times out even after progressive
  geometry simplification; one is a 9-day Alpine window in which >60% cloud cover
  filtered all Sentinel-2 scenes. Both are **upstream provider limits on
  pathological inputs**, surfaced with a clear, actionable error message rather
  than a silent failure.

**Excluding the unregistered streamflow variable, robustness is 64/67 = 95.5%**,
and the core hydrological-forcing/terrain/landcover/soil stack
(precipitation, tmax, tmin, pet, et, dem, landcover, soil, soil_moisture)
succeeded on **every basin in every region**, frequently via a documented
fallback (`n_fallbacks ≥ 1`) — i.e. the robustness mechanism *fired and worked*.

### 5.3 Bugs found and fixed by the sweep

The benchmark doubled as an integration test and surfaced four real defects, all
fixed: (1) the ESA WorldCover GEE driver missed a `static`/`is_collection` flag
and always fell through to STAC; (2) DEM routing ordered a frequently-timing-out
HyRiver source ahead of a reliable GEE one; (3) a misleading "outside coverage"
NDVI error masked a too-short-window cause; (4) GEE's 10 MB `reduceRegion` payload
limit broke large-basin reanalysis fetches, fixed via progressive Shapely
simplification. `[TODO: add before/after latency table — e.g. DEM CONUS 127 s → 8.2 s.]`

`[TODO: add (a) a fallback-activation rate table per variable; (b) cache
hit/miss latency; (c) a head-to-head "single-source baseline fails / aihydro-data
succeeds" count for the non-CONUS basins — the cleanest quantitative argument.]`

### 5.4 Reproducibility

Benchmark generator, sweep, and reporter live in `tests/benchmarks/`
(`_gen_basins.py`, `_sweep.py`, `_report.py`); basin geometries and per-cell
results (`basins.json`, `sweep_results.json`) are committed as artifacts. Each
result records the served `product`, `source`, fallback count, and wall-clock
seconds. `[TODO: pin provider asset versions + capture run date/environment for
camera-ready reproducibility; some GEE assets are mutable.]`

---

## 6. Design Discussion / Lessons

- **Declarative-table routing scales; conditional routing does not.** The
  reference watershed-delineation router (~976 lines, ~60 `nonlocal` variables)
  has excellent *behaviour* but un-reusable *shape*. Encoding the same idea as a
  data table made fallback a property of every variable for the cost of one row.
- **The auth-free tail is the highest-leverage robustness pattern.** Most
  real-world failures are credential/quota problems on GEE; ending every chain
  with a no-auth product converts a hard failure into a graceful degradation.
- **Provenance must be a first-class return value, not a log line.** Carrying
  `citation`/`license`/`fallback_history` on the result is what makes both
  agent self-recovery and human paper-write-up trivial.
- **"No open API" is a real, recurring blocker** (GRDC). Verifying the *access
  path* before promising a backend is now a design rule (§8).

---

## 7. Limitations

- **Region detection is bounding-box based**, not a true Pfafstetter basin lookup;
  geometries straddling region boundaries resolve by centroid. `[TODO: quantify
  edge-case rate; consider polygon-in-region containment.]`
- **`basin_mean` for centroid-based direct-API products (Open-Meteo)** is a
  point approximation, not a true spatial mean — acceptable at ERA5's ~25 km
  resolution for small/medium basins, weaker for very large ones.
- **No global streamflow** (Phase E deferred — see §8).
- **GEE synchronous reducers cap out on very large basins**; the STAC tail
  mitigates for rasters but vegetation indices can still time out on
  continental-scale polygons.
- **Some GEE assets are mutable**, complicating exact long-term reproducibility.

---

## 8. Future Work

- **Global streamflow** (resume Phase E): register Caravan-via-GEE (observed,
  GRDC-derived, ~6,800 basins) and/or GloFAS-via-CDS (modelled, any location).
- **National/observed products with trust-gated, country-aware routing**: extend
  the region hierarchy down to ISO country codes (e.g. `IN → S_ASIA → ASIA →
  global`) and admit a national product (e.g. IMD gridded for India) into the
  *auto* chain only once it clears access, license, and reliability bars;
  otherwise keep it manual-pinnable. The deciding axis is trust/access/license,
  not auto-vs-manual. `[Design captured; not yet built.]`
- **Roadmap products**: ASCAT/Sentinel-1 soil moisture, CHELSA climatologies,
  HLS NDVI, MODIS/Landsat LST, MERIT-Hydro flow accumulation.
- **Infra**: async batch fetcher; server-side STAC reducers; compressed raster cache.

---

## 9. Availability

- **Repository:** `aihydro-data` (Apache-2.0). Data products carry their own
  licenses — always check `result.license` / `data_describe_product(id)`.
- **Install:** `pip install aihydro-data[all]` (or per-backend extras `[gee]`,
  `[hyriver]`, `[stac]`, `[opendap]`, `[viz]`, `[mcp]`).
- **Companion docs:** `README.md` (user guide), `ARCHITECTURE.md` (developer
  tour), `ROADMAP.md`, `examples/cookbook.ipynb` (10 recipes).

---

## 10. Author Notes / Manuscript TODO

- `[TODO]` Authors, affiliations, ORCID, funding/acknowledgements.
- `[TODO]` Pick venue and reformat (JOSS = short + paper.md/.bib; EMS/GMD = full
  IMRaD with figures).
- `[TODO]` Figures: (F1) three-axis schematic; (F2) request-lifecycle flow;
  (F3) benchmark map of the 7 basins; (F4) success matrix heatmap
  (variable × basin); (F5) fallback-activation bar chart.
- `[TODO]` Convert §2 + ProductSpec `bibtex` fields into a real `.bib`
  (every product already ships a BibTeX entry — assemble them).
- `[TODO]` Strengthen §5 with the single-source-baseline comparison (§5.3).
</content>
</invoke>
