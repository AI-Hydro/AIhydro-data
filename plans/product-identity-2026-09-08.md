# Product identity — 2026-09-08

R05 continuation: distinguish GEE CHIRPS v3 daily SAT from IRI v2 daily-improved without silently switching datasets or breaking stable product IDs. Reuse ProductSpec, FetchResult and cache manifests; add declared version/variant/temporal derivation/dependency metadata and persist an identity snapshot at fetch time. Legacy missing identity remains unknown, never reconstructed from today's registry. Correct v3 citation and record shared IMERG inputs without claiming error independence.

Acceptance: product discovery exposes identities; fresh and cached results preserve exact served identity even if registry changes; legacy manifests do not invent version; fallback reports served product identity; default route IDs remain unchanged. Normal cache schema advances to prevent reuse of old misleading citation metadata. Offline tests cover fetch, cache, fallback and discovery serialization. Verify selected regression suites and scoped lint/diff checks. Authenticated source data validation and product independence quantification remain open.

## Outcome
Implemented locally; 164 selected offline tests passed. Cache regression verifies preserved version/units/warnings after registry changes; fallback identity and MCP fields verified. No live retrieval or source revision verification. See DECISIONS.md for limitations.
