# Fallback Chains

When `mode="auto"` (the default), `fetch()` resolves a priority-ordered chain of
products for `(variable, region)` and tries them in sequence. The first success
wins; failures are logged as warnings, not raised.

## Example

For `variable="precipitation"` in CONUS:

```
1. GRIDMET_PRECIP  (hyriver) — primary
2. DAYMET_PRECIP   (hyriver) — fallback if GridMET unavailable
3. CHIRPS          (gee)     — fallback if HyRiver unavailable
4. ERA5L_PRECIP    (gee)     — last resort
```

If GridMET is unreachable (network down, quota), the router tries Daymet, then CHIRPS.

## Manual mode with explicit fallback

```python
result = fetch(
    variable="precipitation",
    geometry=watershed,
    start="2015-01-01",
    end="2015-12-31",
    mode="manual",
    product="CHIRPS",
    fallback=["MSWEP", "ERA5L_PRECIP"],
)
```

`result.product` tells you which product actually served the data.

## No-fallback (strict)

```python
result = fetch(..., mode="manual", product="CHIRPS", fallback=None)
# raises SourceUnavailable if CHIRPS fails — no fallback attempted
```

## Reading the error when all backends fail

```python
from aihydro_data.exceptions import SourceUnavailable
try:
    result = fetch(...)
except SourceUnavailable as exc:
    print(exc.to_dict())
    # {"error": True, "code": "ALL_BACKENDS_FAILED", "recovery": "..."}
```

## Via MCP

The `data_fetch()` MCP tool returns the same structured error dict — no exception
is raised at the MCP layer. Agents read `result["error"]` and `result["recovery"]`.
