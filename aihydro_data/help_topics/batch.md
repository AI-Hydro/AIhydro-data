# Batch Fetching

`fetch_batch()` / `data_batch_fetch()` fetches the same variable over N geometries
in parallel using a thread pool.

## Python API

```python
from aihydro_data import fetch_batch
from shapely.geometry import Point

watersheds = {
    "ws_blue_river": Point(-85.0, 40.0),
    "ws_wabash":     Point(-87.4, 40.7),
    "ws_ohio":       Point(-83.9, 38.7),
}

out = fetch_batch(
    variable="precipitation",
    geometries=watersheds,
    start="2015-01-01",
    end="2015-12-31",
    max_workers=4,        # parallel threads
    on_error="warn",      # 'warn' | 'raise' | 'skip'
)

print(out["labels"])                           # ['ws_blue_river', 'ws_wabash', 'ws_ohio']
print(out["results"]["ws_blue_river"].product) # 'GRIDMET_PRECIP'
print(out["errors"])                           # {} if all succeeded
```

## Geometry input formats

| Input type               | Labels used            |
|--------------------------|------------------------|
| `dict[str, geom]`        | dict keys              |
| `list[geom]`             | "0", "1", "2", …       |
| `list[(label, geom)]`    | explicit labels        |
| `GeoDataFrame`           | index values           |
| single `(lat, lon)`      | "0"                    |

## Error handling

- `on_error="warn"` (default): logs each failure, collects it in `out["errors"]`, continues
- `on_error="raise"`: first failure raises immediately
- `on_error="skip"`: silently omits failures

## MCP

```
data_batch_fetch(
    variable="precipitation",
    geometries=[[40.0, -85.0], [40.7, -87.4]],
    labels=["ws_blue_river", "ws_wabash"],
    start="2015-01-01",
    end="2015-12-31"
)
```

## Performance tips

- `max_workers=4` is a safe default; GEE throttles to ~10 concurrent requests.
- Use `cache=True` (default) — identical requests across batch members are served from disk.
- For large batches, chunk into groups of 20 and process sequentially to avoid GEE quota.
