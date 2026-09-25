# Flood reference honesty — 2026-09-07

Continue R06 remediation across aihydro-data and the tools compatibility adapter.

Acceptance: network/import/read failures never yield synthetic observational geometry; fixtures require explicit use_fixture=True and carry synthetic labels without observational citation. Distinguish no acquisitions, no valid pixels, partial reads and valid pixels with no flood detected. Preserve per-item acquisition/asset provenance; reproject each geometry before combining different tile CRSs. Never silently truncate paginated search results. Tests use mocked outages/catalogs and actual tiny local raster files; no live accuracy claim.

Reuse existing typed SourceUnavailable errors and public fetch API. Update compatibility wrapper so missing optional data package cannot resurrect fixture fallback. Verify relevant data and tools tests, Ruff on changed files, and scoped diff checks. Full spatial completeness, scene-quality validation and event-time suitability remain separate research gates.

## Outcome
Implemented locally; 64 data and 20 tools tests passed. Automatic GFM scoring is explicitly not assessed pending joint validity/grid support. No live retrieval or installed-runtime test. See DECISIONS.md for limits.
