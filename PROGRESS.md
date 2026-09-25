# Progress

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
