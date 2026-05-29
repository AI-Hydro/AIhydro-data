# First Fetch — Quick Start

`aihydro-data` unifies GEE, STAC, HyRiver, and direct APIs behind one `fetch()` call.

## Minimal example

```python
from aihydro_data import fetch

result = fetch(
    variable="precipitation",
    geometry=(40.0, -85.0),          # (lat, lon) — auto-detects CONUS → GridMET
    start="2015-01-01",
    end="2015-12-31",
)

print(result.product)               # 'GRIDMET_PRECIP'
print(result.source)                # 'hyriver'
print(result.data.head())           # pandas DataFrame: date | precipitation
print(result.citation)              # full citation string
```

## Via MCP

```
data_fetch(variable="precipitation", geometry=[40.0, -85.0], start="2015-01-01", end="2015-12-31")
```

## What happens inside

1. `geometry` is coerced to a shapely geometry.
2. The **region** is detected (CONUS, global, S_ASIA, …).
3. The **routing policy** picks the best product for that region.
4. The **backend** fetches and aggregates data.
5. The result is **disk-cached** so the next identical call is instant.

## Discover variables

```python
from aihydro_data import list_products
list_products()                      # all products
list_products(variable="et")        # ET products only
list_products(region="CONUS")       # CONUS-coverage products
```

## Next steps

- `data_help(topic="products")` — what variables + products exist
- `data_help(topic="auth")` — set up GEE credentials for global products
- `data_help(topic="batch")` — fetch over many watersheds in parallel
