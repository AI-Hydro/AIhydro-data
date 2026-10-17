# Disk Cache

## Location

`~/.aihydro/cache/data/`

Each cache entry consists of two files:

```
<cache_key>.parquet          ← time-series (pandas DataFrame)
<cache_key>.nc               ← raster (xarray DataArray)
<cache_key>.manifest.json    ← provenance sidecar (list of ManifestEntry)
```

The cache key is a 24-character SHA-256 hash of the normalised request parameters
(variable, geometry WKT, start, end, product, aggregation).

## Cache status

```python
from aihydro_data import cache_status
print(cache_status())
# {"cache_dir": "/Users/…/.aihydro/cache/data",
#  "entry_count": 7,
#  "total_size_mb": 4.3,
#  "entries": [{"cache_key": "…", "variable": "precipitation", …}, …]}
```

Or via MCP: `data_get_cache_status()`

## Invalidation

```python
from aihydro_data import cache_invalidate
cache_invalidate("abc123def456789012345678")   # returns True if deleted
```

Or via MCP: `data_invalidate_cache(cache_key="abc123def456789012345678")`

## Disable caching per call

```python
result = fetch("precipitation", geom, "2015-01-01", "2015-12-31", cache=False)
```

## Manifest

The `.manifest.json` sidecar is an append-only JSON list. Each write adds a new
entry so re-fetches with different backends are tracked. The most-recent entry
wins for display in `cache_status()`.

Observation provenance (NWIS): the entry also records `site_resolution` (how the gauge was
identified, ADR-003 amendment A2), `quality` (approval/qualifier counts, `provisional_fraction`)
and `upstream` (request URLs without any key, response sha256s, retrieval time, Last-Modified/ETag
when the server sent them) plus `data_sha256` of the cached data file.

Integrity on read: the data file is re-hashed and a mismatch is a miss. An entry holding
provisional values expires after `AIHYDRO_DATA_PROVISIONAL_TTL_DAYS` (default 7) and the miss is
recorded in `fallback_history` as `provisional entry expired`; fully approved entries never expire.

## Performance notes

- Parquet files compress well with Snappy (~10× vs CSV for typical CONUS daily data).
- On a cache hit, `fetch()` reconstructs the full `FetchResult` in <5 ms.
- The manifest read is sequential over `*.manifest.json` glob — expect <100 ms for
  up to ~1000 entries.
