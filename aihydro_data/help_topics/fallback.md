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

`result.product` tells you which product actually served the data. When it is not the
pinned one, `result.product_substituted` is `True`, `result.requested_product` is `"CHIRPS"`,
and a `SUBSTITUTED PRODUCT` note is added: a fallback is a different scientific product.

In Python, `mode="manual"` with `fallback=None` (the default) pins the product as the
**first choice** and still falls back along the policy chain.

## No-fallback (strict)

```python
result = fetch(..., mode="manual", product="CHIRPS", fallback=[])
# raises SourceUnavailable if CHIRPS fails — no fallback attempted
```

The MCP `data_fetch` tool is strict by default: `mode="manual"` (or any `product=`) serves
the pinned product or fails with its error. Pass `fallback=[...]` there to opt in to named
fallbacks.

## Refusals are terminal

An identity or integrity refusal (`GAUGE_REQUIRED`, `GAUGE_NOT_FOUND`, `GAUGE_AMBIGUOUS`, and the
`USGS_*` data refusals) raises `TerminalSourceError` and is **never** answered by walking on to a
modelled product: `fallback_history` ends with `outcome="refused"`. Transport failures (timeouts,
5xx) are different — they mean "could not look" and the chain may continue.

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
