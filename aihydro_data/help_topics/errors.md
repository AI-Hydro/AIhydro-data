# Error Reference

All errors return a structured envelope:

```json
{
  "error": true,
  "code": "GEE_AUTH_MISSING",
  "message": "Google Earth Engine credentials not initialised.",
  "recovery": "Run `ee.Authenticate()` then `ee.Initialize()`. See data_help(topic='auth').",
  "next_tools": ["data_help", "data_list_products"],
  "docs_anchor": "auth#gee"
}
```

## Error codes

| Code                   | Cause                                           | Recovery                                                     |
|------------------------|-------------------------------------------------|--------------------------------------------------------------|
| `GEE_AUTH_MISSING`     | No GEE credentials on disk                      | `ee.Authenticate()` + `ee.Initialize()`                      |
| `GEE_QUOTA_EXCEEDED`   | GEE per-user/project rate limit hit             | Wait ~30 s; reduce `max_workers`                             |
| `ALL_BACKENDS_FAILED`  | Every product in the fallback chain failed      | Check network; try `mode='manual'` with a specific product   |
| `NO_PRODUCTS_FOR_REGION`| No policy entry for (variable, region) combo  | `data_list_products(variable=...)` to see supported regions  |
| `PRODUCT_NOT_FOUND`    | Unknown product ID                              | `data_list_products()` for valid IDs                         |
| `REGION_UNSUPPORTED`   | Region detected but no chain defined            | Add `product=` to force a specific product                   |
| `DATE_OUT_OF_RANGE`    | Dates outside product temporal coverage         | Adjust dates; see `data_describe_product(product_id).temporal_start` |
| `GEOMETRY_INVALID`     | Could not parse / coerce the geometry           | Use `{"lat":..,"lon":..}`, a `(lat, lon)` tuple (library) or valid GeoJSON dict; a gauge id is digits / `USGS-` + digits only                 |
| `MISSING_EXTRAS`       | Required package not installed                  | `pip install aihydro-data[<extra>]`                          |
| `FETCH_TOO_LARGE`      | Estimated request volume too large              | Use `aggregation="basin_mean"` or split time range           |
| `GAUGE_REQUIRED`       | Gauge not identifiable (polygon without outlet, or outlet off the network with no polygon) | Pass a USGS site number, a polygon plus `outlet=(lat, lon)`, or ask for a modelled product explicitly |
| `GAUGE_NOT_FOUND`      | No discharge gauge passes the A2 gates (5 km along the river, 10 % drainage area) | Pass the gauge id explicitly; `details.reason` and `details.candidates` say why |
| `GAUGE_AMBIGUOUS`      | More than one admissible (or unverifiable) gauge, or an id/alias conflict | Pass the gauge id explicitly; there is no "nearest wins" |
| `USGS_RATE_LIMITED`    | USGS returned HTTP 429 without an API key       | Register an api.data.gov key and export `API_USGS_PAT`       |
| `USGS_*` refusals (`USGS_NO_DATA`, `USGS_SITE_NOT_FOUND`, `USGS_MULTIPLE_SERIES`, `USGS_APPROVAL_UNKNOWN`, `USGS_UNIT_UNKNOWN`, `USGS_VALUE_UNPARSEABLE`, `USGS_VALUE_SENTINEL`, ...) | The record cannot be served as observed discharge | Read `message`/`details`; these are terminal and never fall back to modelled flow |
| `USGS_TIMEOUT` / `USGS_UNREACHABLE` / `USGS_SERVER_ERROR` | USGS could not be reached or failed | Transient; retry later (the chain may continue to another product) |
| `UNEXPECTED_ERROR`     | Unclassified exception in backend               | Report the full error message at github.com/AI-Hydro/aihydro-data/issues |

## In Python

All errors inherit from `aihydro_data.exceptions.AihydroDataError`:

```python
from aihydro_data.exceptions import AihydroDataError, SourceUnavailable
try:
    result = fetch(...)
except SourceUnavailable as exc:
    print(exc.code)       # 'ALL_BACKENDS_FAILED'
    print(exc.recovery)   # human-readable hint
    print(exc.to_dict())  # full structured envelope
except AihydroDataError as exc:
    print(exc.to_dict())
```

## In MCP

The `data_fetch()` MCP tool never raises — it returns the error dict directly.
Agents should check `result.get("error") is True` before assuming success.
