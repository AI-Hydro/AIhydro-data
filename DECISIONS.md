# Decisions

## 2026-10-02 — Cache key uses the canonical geometry id, not WKT

WKT encodes ring start vertex and direction, so the same polygon delivered by
two delineators (or re-serialised) missed the cache, and two spellings of one
place carried no shared identity. The key now uses `aihydro.geom/1`
(`aihydro_core.records.place.geometry_id`), the same algorithm that identifies a
basin realisation in the watershed layer; aihydro-data only consumes it
(ADR-003 amendment). Distinct outlets still separate keys (the 2026-09-29
rule is unchanged).

Bump result schema 4->5 so pre-slice entries miss rather than being aliased;
nothing is rewritten, and orphaned entries are safe to delete. A geometry that
cannot be identified raises `GeometryInvalid` instead of falling back to WKT, so
the key can never silently mix two identity schemes. The algorithm quantises at
1e-6 deg; two polygons that differ by less than that share a key by design.
`GaugeID` gains a `scheme` so a non-USGS station id is no longer assumed to be
inside CONUS.

## 2026-09-29 — Cache hits obey the current scientific request

A cache key bound to a manually pinned primary product can hold a prior
fallback response. Therefore every cache read checks the actual serving
product against the *current* candidate chain, including a strict
`fallback=[]` request. A `validate` callback applies to cache hits too;
otherwise scientific acceptance changes with cache warmth. Outlet coordinates
join the cache identity because reach selection can differ within one basin
polygon. Older entries without outlet identity are not evidence of equivalence
across outlets.


## 2026-09-07 — Require complete temporal support for IMERG daily integration

IMERG's GEE precipitation band is a half-hourly rate in mm/hr. The previous path truncated timestamps and returned those rates as daily mm/day. The explicit `imerg_half_hourly_rate_to_daily_v1` contract integrates each rate over 0.5 h, yielding depth over each UTC day (reported under the existing mm/day convention). Date-only start/end are inclusive for this contract; other products retain their existing date handling.

Reject missing, duplicate, off-grid, masked, negative and nonfinite observations for the entire request. This avoids plausible but biased totals and lets the routing layer report failure/fallback. A complete day requires 48 distinct intervals; a wholly missing interior day also fails. No extrapolation, zero filling, duplicate selection or final-product substitution occurs.

Retain source status per day (including unknown/mixed states); a collection-wide Final citation was misleading. Temporal checks do not establish stable spatial masks, area weighting, native quality flags, error independence or accuracy. Large requests still retrieve native observations and can hit GEE limits; server-side scalable aggregation remains future work.

Bump result cache schema 2→3 to avoid returning old incorrectly labelled numerical values in normal fetches. Existing cache files remain untouched. Direct low-level reads of old cache keys are not certified by this migration. This small pipeline change preserves the pre-existing result-honesty implementation.

Source verified 2026-09-07: [Earth Engine IMERG V07 catalog](https://developers.google.com/earth-engine/datasets/catalog/NASA_GPM_L3_IMERG_V07), native cadence/units/status and current collection availability.

## 2026-09-07 — Flood observations must not degrade into synthetic references

R06: live GFM failures now raise typed errors. `allow_network=False` disables fetching; it does not authorize invented geometry. Only explicit `use_fixture=True` produces a synthetic polygon, marked at envelope and feature level and without an observational citation. The tools adapter rejects fixture responses from older installed data packages and cannot synthesize a fallback if the data package is missing.

The STAC path distinguishes no acquisitions, no valid classified pixels, no flood detected in valid pixels, and detected flood. Missing assets or any read failure fail the request with item details, rather than silently producing partial coverage or a no-flood claim. Searches with a next-page link fail explicitly until pagination is implemented. Each geometry is reprojected before union; per-item IDs, acquisition times, asset URLs and pixel counts are retained. Counts describe read windows and can overlap across scenes; they are not a unique basin-area coverage measure.

Automatic hindcast scoring is now `not_assessed`: extent polygons alone do not provide the joint valid observation footprint, grid alignment or event-time suitability. The observed map can remain useful while scoring is unavailable. The manual user-supplied-reference path is unchanged and still needs equivalent support-aware validation. Source quality/exclusion masks, spatial coverage, immutable asset checksums, pagination and live end-to-end validation remain open.

Encoding checked against [EODC's GFM processing tutorial](https://docs.eodc.eu/tutorials/gfm_maximum_flood_extent_dask.html) and [JRC's 2024 quality assessment](https://publications.jrc.ec.europa.eu/repository/bitstream/JRC142154/JRC142154_01.pdf): 0 no flood, 1 flood, 255 nodata. Source limitations and additional masks are documented in the [GFM product manual](https://extwiki.eodc.eu/GFM/PUM/Products). Accessed 2026-09-07. No live source retrieval was used as validation evidence.

## 2026-09-08 — Preserve scientific product identity through fallback and cache

Stable IDs and default routes remain unchanged: `CHIRPS` serves GEE CHIRPS v3 daily SAT; `CHIRPS_IRI` serves IRI v2 daily-improved. These are different scientific products. V3 SAT partitions pentad amounts using IMERG Late V07; this shared input must not be interpreted as independent evidence when comparing daily rainfall with IMERG. The v3 repository citation replaces the v2 paper citation, and provider/catalog licensing descriptions are explicitly distinguished rather than conflated.

ProductSpec now declares version, variant, temporal derivation and known input dependencies. FetchResult captures configured identity plus units, cadence, resolution and warnings; cache manifests preserve this snapshot, fallback history and notes. New-cache interpretation is not rewritten from the current registry. Legacy identity remains empty/unknown; legacy units still use the previous registry fallback. Schema v4 prevents normal requests from reusing previous misleading citation metadata. Existing cache files remain untouched.

MCP discovery and result serialization expose these fields, including previously omitted unit/coverage/fallback metadata. Empty dependency lists mean undeclared, not independent. Identity is explicitly `configured_product`, with `asset_revision_verified=False`: no claim of upstream byte-level identity, revision pinning, maturity verification or reproducibility across changing remote products. Cache hits report the historical snapshot; product-version-aware cache invalidation beyond this schema migration remains future work.

Sources checked 2026-09-08: [GEE v3 SAT catalog](https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHC_CHIRPS_V3_DAILY_SAT), [CHC v3 documentation](https://www.chc.ucsb.edu/data/chirps3), [IRI access notice](https://iridl.ldeo.columbia.edu/auth/notice). IRI web pages require sign-in; its notice explicitly exempts OPeNDAP download requests. The configured v2 URL identifies that variant; live precipitation retrieval was not validated in this slice.

## 2026-09-29: Size-aware CONUS DEM routing; NLCD layer and default year

CONUS DEM requests whose bounding box is under 500 km² now try USGS 3DEP 10 m (DEM3DEP_10M) first; larger requests keep GLO30 first. The static order put GLO30 first because 3DEP's WCS request timed out on a 2276 km² benchmark, but GLO-30 is a surface model that keeps canopy and buildings and is 30 m, which misroutes flow in small catchments (found delineating ~1 km² culvert catchments for INDOT SPR-4926). The rule lives in `routing/policy.py` (`_SMALL_REQUEST_FIRST`, `DEM3DEP_MAX_BBOX_KM2`) and uses the geodesic area of the request's bounding box, because the WCS request is sized by the box, not the polygon. Points, unknown sizes and NORTH_AMERICA (3DEP does not cover Canada or Mexico) keep the static order. The 500 km² cut-off comes from the timeout note, not a measured timeout curve; a small benchmark across sizes would pin it properly.

`resolve_product_ids` takes an optional geometry; every caller (pipeline, `resolve_product`, MCP routing helpers) passes it.

The HyRiver NLCD backend ignored the product's `nlcd_layer` and always requested `cover`, so NLCD_IMPERVIOUS returned land-cover class codes (live check: values 21 to 81 over an Indiana box) instead of percent impervious. It now requests the configured layer. The NLCD default epoch (used only when no start date is given) is 2021, the latest pygeohydro serves, instead of 2019.

Cache: the auto-mode key is product-agnostic, so old entries would keep being served. A per-variable revision (`_VARIABLE_CACHE_REVISION`, `impervious` and `dem`) changes only those keys; other variables keep their caches. Old files stay on disk untouched.
