# aihydro-data

Agent-facing environmental data discovery, routing, fetching and provenance, built on aihydro-core. Product identity, units, temporal support and failures must remain visible to research consumers.

## Status — 2026-09-08
Research-grade remediation in progress; local changes are not a released validation claim.

## Where to read next
- Continue: ROADMAP.md, PROGRESS.md, plans/precipitation-contract-2026-09-07.md , plans/flood-reference-honesty-2026-09-07.md and plans/product-identity-2026-09-08.md.
- Design: ARCHITECTURE.md; decisions: DECISIONS.md.
- Install and usage: README.md.

## Current state
2026-09-29 research-pilot continuation: strict manual requests validate cached
serving products, cache hits run caller validators, and outlet changes affect
cache identity. Local non-live suite: 417 passed, 55 deselected. See latest
PROGRESS.md. Uncommitted, not independently validated against remote sources.

IMERG rate-to-daily integration, strict temporal completeness and status reporting are implemented locally with offline regression coverage. Existing pipeline/cache/contracts/routing and population changes predate this slice and must be preserved. Baseline HEAD: 1ba148a.
GFM synthetic fallback and misleading no-flood success paths are repaired locally; automatic scoring awaits joint validity support. CHIRPS version identity and cache/MCP propagation are implemented locally (164 selected tests). Next: joint observation/model masks, source-revision-aware cache invalidation and authenticated source validation.

## Scope
Data semantics and traceability; not proof that satellite estimates are ground truth. No independent model execution or alternate platform runtime.

## Verification
Use offline pytest fixtures; live GEE validation requires a separately reported authenticated run.
