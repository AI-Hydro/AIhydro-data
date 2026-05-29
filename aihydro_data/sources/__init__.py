"""
Backend adapters. One module per data origin.

Every backend implements the SourceBackend ABC from sources.base:
    .capabilities() -> dict          # what variables/regions this backend can serve
    .fetch_timeseries(spec, geom, start, end, agg) -> pd.DataFrame
    .fetch_raster(spec, geom, start, end) -> xarray.DataArray

The fetch() pipeline picks a ProductSpec (via the router), looks up its
.source field, dispatches to the matching backend module.
"""
