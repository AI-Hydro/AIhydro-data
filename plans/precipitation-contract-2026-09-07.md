# Precipitation contract — 2026-09-07

Authorized continuation of the ecosystem research-grade remediation (R05).

Reuse the GEE extractor and declarative product configuration. Add an explicit half-hourly-rate integration contract for IMERG only. Date-only requests cover inclusive UTC calendar days; Earth Engine's exclusive stop is advanced one day. Require all 48 unique, aligned, finite nonnegative observations per day, including interior days. Reject incomplete or duplicate data without zero filling or extrapolation. Preserve source status per day; unknown status must remain unknown.

Acceptance: known constant and varying rates integrate correctly; missing, duplicate, malformed and off-grid intervals fail; backend passes the contract and exposes diagnostics; unrelated products keep their existing path; old cache keys cannot reuse incorrectly labelled values. Document that temporal completeness does not prove stable spatial masks or scientific accuracy.

Verification: offline pytest tests/test_precipitation_contract.py and existing result/product/routing tests; focused Ruff and git diff --check. No live-source accuracy claim. Update PROJECT/PROGRESS/DECISIONS and ecosystem continuation records.

## Outcome
Implemented locally. 130 selected offline tests passed, including 19 precipitation cases and a real old-cache bypass/new-cache roundtrip. Focused Ruff passed. No live GEE validation. See DECISIONS.md for date semantics, spatial limitations and cache migration scope.
