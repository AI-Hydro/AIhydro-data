# Decisions

## 2026-09-07 — Require complete temporal support for IMERG daily integration

IMERG's GEE precipitation band is a half-hourly rate in mm/hr. The previous path truncated timestamps and returned those rates as daily mm/day. The explicit `imerg_half_hourly_rate_to_daily_v1` contract integrates each rate over 0.5 h, yielding depth over each UTC day (reported under the existing mm/day convention). Date-only start/end are inclusive for this contract; other products retain their existing date handling.

Reject missing, duplicate, off-grid, masked, negative and nonfinite observations for the entire request. This avoids plausible but biased totals and lets the routing layer report failure/fallback. A complete day requires 48 distinct intervals; a wholly missing interior day also fails. No extrapolation, zero filling, duplicate selection or final-product substitution occurs.

Retain source status per day (including unknown/mixed states); a collection-wide Final citation was misleading. Temporal checks do not establish stable spatial masks, area weighting, native quality flags, error independence or accuracy. Large requests still retrieve native observations and can hit GEE limits; server-side scalable aggregation remains future work.

Bump result cache schema 2→3 to avoid returning old incorrectly labelled numerical values in normal fetches. Existing cache files remain untouched. Direct low-level reads of old cache keys are not certified by this migration. This small pipeline change preserves the pre-existing result-honesty implementation.

Source verified 2026-09-07: [Earth Engine IMERG V07 catalog](https://developers.google.com/earth-engine/datasets/catalog/NASA_GPM_L3_IMERG_V07), native cadence/units/status and current collection availability.

## 2026-09-08 — Preserve scientific product identity through fallback and cache

Stable IDs and default routes remain unchanged: `CHIRPS` serves GEE CHIRPS v3 daily SAT; `CHIRPS_IRI` serves IRI v2 daily-improved. These are different scientific products. V3 SAT partitions pentad amounts using IMERG Late V07; this shared input must not be interpreted as independent evidence when comparing daily rainfall with IMERG. The v3 repository citation replaces the v2 paper citation, and provider/catalog licensing descriptions are explicitly distinguished rather than conflated.

ProductSpec now declares version, variant, temporal derivation and known input dependencies. FetchResult captures configured identity plus units, cadence, resolution and warnings; cache manifests preserve this snapshot, fallback history and notes. New-cache interpretation is not rewritten from the current registry. Legacy identity remains empty/unknown; legacy units still use the previous registry fallback. Schema v4 prevents normal requests from reusing previous misleading citation metadata. Existing cache files remain untouched.

MCP discovery and result serialization expose these fields, including previously omitted unit/coverage/fallback metadata. Empty dependency lists mean undeclared, not independent. Identity is explicitly `configured_product`, with `asset_revision_verified=False`: no claim of upstream byte-level identity, revision pinning, maturity verification or reproducibility across changing remote products. Cache hits report the historical snapshot; product-version-aware cache invalidation beyond this schema migration remains future work.

Sources checked 2026-09-08: [GEE v3 SAT catalog](https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHC_CHIRPS_V3_DAILY_SAT), [CHC v3 documentation](https://www.chc.ucsb.edu/data/chirps3), [IRI access notice](https://iridl.ldeo.columbia.edu/auth/notice). IRI web pages require sign-in; its notice explicitly exempts OPeNDAP download requests. The configured v2 URL identifies that variant; live precipitation retrieval was not validated in this slice.
