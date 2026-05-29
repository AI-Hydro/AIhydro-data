# Deprecations

## aihydro-tools v1.x fetcher tools → aihydro-data

The following tools in `aihydro-tools` ≤ v1.x are **deprecated** and will be removed in v2.0.
They continue to work as thin shims that forward to `aihydro-data`.

| Old tool (aihydro-tools)     | New tool (aihydro-data)                                      |
|------------------------------|--------------------------------------------------------------|
| `fetch_streamflow_data`      | `data_fetch(variable="streamflow", ...)`                     |
| `fetch_forcing_data`         | `data_fetch(variable="precipitation" / "tmax" / ..., ...)`   |
| `fetch_lulc_data`            | `data_fetch(variable="landcover", ...)`                      |
| `fetch_soil_data_polaris`    | `data_fetch(variable="soil", product="POLARIS", ...)`        |

Each deprecated call emits a deprecation notice in its response:

```json
{
  "deprecation_notice": "fetch_streamflow_data is deprecated. Use data_fetch(variable='streamflow', ...) instead.",
  "migration_guide": "See data_help(topic='deprecations').",
  ...
}
```

## Migration guide

**Before (aihydro-tools ≤ v1)**:
```
fetch_streamflow_data(gauge_id="01234567", start="2015-01-01", end="2020-12-31")
```

**After (aihydro-data)**:
```
data_fetch(variable="streamflow", geometry="01234567", start="2015-01-01", end="2020-12-31")
```

Note: `geometry` for streamflow accepts USGS gauge IDs as strings
(e.g. `"01234567"` or `"USGS-01234567"`).

## Timeline

| Milestone | Date      | Change                                               |
|-----------|-----------|------------------------------------------------------|
| v1.2.0    | 2026-Q2   | Shims added; deprecation notices emitted             |
| v2.0.0    | 2026-Q4   | Old tools removed from aihydro-tools                 |
