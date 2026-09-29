# Progress

## 2026-09-29 — Cache policy and outlet identity for publication pilots

- Manual strict pins now check the cached serving product against the current
  candidate list. A caller validator is applied to cached results, and a
  rejected cache result enters the decision trail before backend fetch.
- Outlet coordinates now enter the cache key, preventing two outlet choices
  for the same basin geometry from sharing a response.
- Added offline regressions for warm fallback/strict pin, cache validator and
  outlet identity. `/opt/miniconda3/bin/python -m pytest tests -q -m 'not live'`:
  417 passed, 55 deselected. After core hashing changes, 14 selected data cache
  tests passed. No live data retrieval, release or remote write.


## 2026-09-07 — IMERG precipitation semantics (local, uncommitted)

Added explicit daily integration and completeness checks, preserved maturity status, corrected collection citations/availability and invalidated old normal-fetch cache keys. Added PROJECT.md and execution plan. Existing pipeline/cache/contracts/routing/population work remains preserved; only the schema constant/comment was changed within the pre-existing dirty pipeline file.

Verification: 130 selected offline tests passed initially; the strengthened real cache-migration/roundtrip regression subsequently passed with all 19 precipitation tests. Final verification is recorded below. No authenticated GEE retrieval or satellite-accuracy validation performed. CHIRPS identity, spatial coverage/area weighting and synthetic flood fallback remain open.

Final verification: all 130 selected tests passed after the cache regression was strengthened; focused Ruff passed. Whole-repo diff checking identifies pre-existing trailing whitespace in contracts.py:240 (not changed by this slice); the precipitation slice passes a scoped diff check.


## 2026-09-07 — Flood reference honesty (R06, local/uncommitted)

Removed synthetic observational fallback from data and tools adapters. Network-disabled/missing-package/live failures do not fabricate polygons; fixtures require explicit opt-in and synthetic labels. STAC no-acquisition/nodata/dry/flood outcomes are distinct, asset failures and pagination fail explicitly, and tiles are reprojected before union with acquisition/asset provenance retained. Automatic GFM scoring is not assessed without a joint valid observation footprint; observed map geometry remains available.

Verification: 64 selected data tests passed (2 live tests deselected), 20 selected tools/inundation/layering tests passed. Real tiny raster fixtures cover nodata, dry/flood and differing CRSs; outage, missing-package and legacy synthetic responses covered. Focused Ruff, tools_analysis compilation and scoped diff checks passed. No live GFM request or installed-runtime test. Manual reference scoring, quality masks, support-aware validation and immutable asset lineage remain open. Canonical decision: MCP/aihydro-data/DECISIONS.md.


## 2026-09-08 — CHIRPS and served-product identity (local/uncommitted)

GEE CHIRPS v3 daily SAT and IRI v2 daily-improved are now distinguished in declarations, citations, discovery and fetched/cached identity. V3 IMERG Late daily-partition dependency is explicit. Stable route IDs retained. New cache manifests preserve configured identity, interpretation fields, fallback history and notes; legacy identity stays unknown. MCP now forwards unit/coverage metadata that was previously omitted. Schema4 bypasses old citation metadata in normal fetches.

Verification: 164 selected offline tests passed, including fresh/cache/fallback/MCP and registry-change regressions; focused new-test Ruff and scoped diff checks passed. No live precipitation retrieval or asset revision verification. Pre-existing contracts.py trailing whitespace remains outside the scoped diff check. Next: joint observation/model support, source-revision-aware caching and live scientific validation. See MCP/aihydro-data/DECISIONS.md and plans/product-identity-2026-09-08.md.


## 2026-09-29: Small CONUS DEMs from 3DEP; NLCD impervious layer fix (local commit, not pushed)

CONUS DEM requests with a bounding box under 500 km² route to 3DEP 10 m first; larger ones keep GLO30 first (3DEP WCS times out on large requests). NLCD_IMPERVIOUS now returns percent impervious (it returned land-cover codes because the layer setting was ignored). NLCD default epoch 2021. Per-variable cache revision invalidates only old `dem` and `impervious` entries.

Verification: 433 offline tests pass (+18 new, 5 routing stubs updated to the new signature); live: a 1 km² Indiana box now fetches DEM3DEP_10M at ~10 m, and impervious returns 0 to 100 percent. aihydro-watershed 71 and aihydro-tools 1101 offline tests pass against the change. See DECISIONS.md.
