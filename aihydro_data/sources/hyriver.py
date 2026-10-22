"""
HyRiver backend.

Wraps pygridmet (GridMET) and pydaymet (Daymet) for CONUS / North America
precipitation and other meteorological variables. All HyRiver imports are
lazy so this module is importable without the extra installed.

Install: pip install aihydro-data[hyriver]
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import threading
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources._common import require_import
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)

# Map product backend_config keys → HyRiver library + function
_GRIDMET_VAR_MAP = {
    "pr": "precipitation",   # pygridmet variable key → our canonical name
    "tmmx": "tmax",
    "tmmn": "tmin",
    "pet": "pet",
    "srad": "srad",
    "vs": "wind_speed",
    "rmax": "rmax",
    "rmin": "rmin",
    "sph": "specific_humidity",
    "th": "wind_direction",
    "bi": "burn_index",
    "fm100": "fm100",
    "fm1000": "fm1000",
    "etr": "etr",
    "eto": "eto",
}


# Discrete NLCD epochs (cover and impervious). Caller-requested years snap to
# the nearest available epoch so a request for e.g. 2019 hits the real product.
_NLCD_YEARS = (2001, 2004, 2006, 2008, 2011, 2013, 2016, 2019, 2021)
# Latest epoch pygeohydro.nlcd_bygeom serves; used when no year is requested.
NLCD_LATEST_YEAR = 2021


def _nearest_nlcd_year(start: str, default: int = NLCD_LATEST_YEAR) -> int:
    """Snap a requested year (from an ISO start date) to the nearest NLCD epoch."""
    try:
        requested = int(str(start)[:4])
    except (ValueError, TypeError):
        return default
    return min(_NLCD_YEARS, key=lambda y: abs(y - requested))


def _find_time_dim(data: Any) -> str:
    """Identify the time dimension of an xarray object.

    Prefers conventionally-named dims, then any dim whose coordinate is
    datetime64, then falls back to the first dim.
    """
    import numpy as np

    for cand in ("time", "date", "T", "day", "Time"):
        if cand in data.dims:
            return cand
    for d in data.dims:
        if d in data.coords:
            vals = np.asarray(data[d].values)
            if np.issubdtype(vals.dtype, np.datetime64):
                return d
    return list(data.dims)[0]


def _basin_aggregate_to_frame(data: Any, var_key: str, how: str = "mean") -> Any:
    """Reduce a gridded fetch to a tidy basin-aggregated ``[date, var_key]`` frame.

    ``pygridmet`` / ``pydaymet`` ``get_bygeom`` return an :class:`xarray.Dataset`
    (dims ``time × y × x``), NOT a pixel-columned DataFrame — so a pandas-style
    ``.mean(axis=1)`` raises ``ValueError: passing 'axis' to Dataset reduce
    methods is ambiguous``. This helper reduces over the SPATIAL dims with
    ``dim=[...]`` (the xarray-correct call) and returns a tidy DataFrame the
    downstream normaliser can consume. A legacy pandas DataFrame input still
    works (per-pixel columns → ``axis=1`` reduction) for forward-compatibility.
    """
    import pandas as pd

    # Legacy / defensive: a pixel-columned DataFrame.
    if isinstance(data, pd.DataFrame):
        reduced = data.mean(axis=1) if how == "mean" else data.sum(axis=1)
        out = reduced.to_frame(name=var_key).reset_index()
        out = out.rename(columns={out.columns[0]: "date"})
        return out[["date", var_key]]

    import xarray as xr

    if isinstance(data, (xr.Dataset, xr.DataArray)):
        time_dim = _find_time_dim(data)
        spatial_dims = [d for d in data.dims if d != time_dim]
        if spatial_dims:
            reduced = (
                data.mean(dim=spatial_dims) if how == "mean"
                else data.sum(dim=spatial_dims)
            )
        else:
            reduced = data
        if isinstance(reduced, xr.Dataset):
            name = var_key if var_key in reduced.data_vars else list(reduced.data_vars)[0]
            reduced = reduced[name]
        series = reduced.to_series().rename(var_key)
        out = series.reset_index()
        out = out.rename(columns={time_dim: "date"})
        return out[["date", var_key]]

    raise TypeError(
        f"_basin_aggregate_to_frame: unexpected input type {type(data).__name__}"
    )


# ── Chunked, validated GridMET fetch ─────────────────────────────────────
#
# pygridmet 0.19.4 downloads one file per calendar year into a shared on-disk
# cache named by sha256(url), merges them with xr.merge and retries only on
# NaNs. A cache file holding the WRONG year (observed: the 1999 file contains
# 2006) merges silently, dropping a whole year. We therefore fetch per calendar
# year, check each chunk's time coordinate against its own request, retry a bad
# chunk once with a fresh private cache directory, and never merge a chunk that
# still fails. Evidence: docs/vision-2040/evidence/gridmet-chunk-defect-1.
#
# Retry isolation: pygridmet reads HYRIVER_CACHE_NAME at call time
# (`_utils.download_files`), and that env var is process-global inside the
# long-running MCP server. We serialise GridMET calls under one process lock and
# set/restore the variable inside it, rather than spawning a subprocess: a
# subprocess would have to re-import the geospatial stack and pickle the
# geometry for a rare path, and could not be exercised with an in-process
# monkeypatched client. Cost: GridMET fetches in one process run one at a time.
# Other HyRiver libraries called concurrently could write their cache into the
# temporary directory during a retry; that only loses their cache entry.
_HYRIVER_ENV_LOCK = threading.RLock()
_HYRIVER_CACHE_ENV = "HYRIVER_CACHE_NAME"


@contextlib.contextmanager
def _private_hyriver_cache():
    """Point HYRIVER_CACHE_NAME at a fresh temp dir; restore and delete after.
    Caller must hold ``_HYRIVER_ENV_LOCK``."""
    tmp = tempfile.mkdtemp(prefix="aihydro_hyriver_")
    prev = os.environ.get(_HYRIVER_CACHE_ENV)
    os.environ[_HYRIVER_CACHE_ENV] = os.path.join(tmp, "aiohttp_cache.sqlite")
    try:
        yield tmp
    finally:
        if prev is None:
            os.environ.pop(_HYRIVER_CACHE_ENV, None)
        else:
            os.environ[_HYRIVER_CACHE_ENV] = prev
        shutil.rmtree(tmp, ignore_errors=True)


def _times_of(obj: Any) -> Any:
    """Time coordinate of a fetched chunk (DataFrame or xarray)."""
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        for c in ("date", "time"):
            if c in obj.columns:
                return obj[c]
        return obj.index
    return obj[_find_time_dim(obj)].values


def _chunked_daily(fetch: Any, start: str, end: str, label: str
                   ) -> tuple[Any, list[dict[str, Any]]]:
    """Fetch ``[start, end]`` per calendar year, validate each chunk, retry a bad
    one once with a private cache, drop it if it still fails. Returns
    ``(merged, failures)``; raises if no chunk survives or the merge would
    contain duplicate days."""
    import pandas as pd
    import xarray as xr

    from aihydro_data import _daily_validation as dv

    parts: list[Any] = []
    failures: list[dict[str, Any]] = []
    for cs, ce in dv.year_chunks(start, end):
        def _ok(chunk: Any) -> dict[str, Any]:
            return dv.validate_daily_series(
                _times_of(chunk), str(cs.date()), str(ce.date()),
                chunks=[(cs, ce, _times_of(chunk))], skip_leap_dec31=True)

        with _HYRIVER_ENV_LOCK:
            chunk = fetch(str(cs.date()), str(ce.date()))
            verdict = _ok(chunk)
            if not verdict["ok"]:
                log.warning("%s chunk %s..%s failed validation (%s); retrying once "
                            "with a private cache", label, cs.date(), ce.date(),
                            "; ".join(verdict["problems"]))
                with _private_hyriver_cache():
                    chunk = fetch(str(cs.date()), str(ce.date()))
                verdict = _ok(chunk)
        if verdict["ok"]:
            parts.append(chunk)
        else:
            failures.append({"start": str(cs.date()), "end": str(ce.date()),
                             "reason": dv.CHUNK_MISMATCH,
                             "problems": verdict["problems"], "retried": True})

    if not parts:
        raise RuntimeError(
            f"{label}: every yearly chunk failed validation after one retry with a "
            f"private cache ({failures}); nothing was merged.")
    first = parts[0]
    if len(parts) == 1:
        merged = first
    elif isinstance(first, pd.DataFrame):
        merged = pd.concat(parts)
    else:
        merged = xr.concat(parts, dim=_find_time_dim(first))
    final = dv.validate_daily_series(_times_of(merged), start, end, skip_leap_dec31=True)
    if final["duplicates"]:
        raise RuntimeError(f"{label}: merged series has {final['duplicates']} duplicate "
                           f"timestamp(s); refusing to return it.")
    return merged, failures


def _attach_chunk_failures(out: Any, failures: list[dict[str, Any]]) -> Any:
    if failures:
        out.attrs["aihydro_chunk_validation"] = {"failures": failures}
        out.attrs["aihydro_notes"] = list(out.attrs.get("aihydro_notes", [])) + [
            "GridMET yearly chunk(s) failed validation and were NOT merged: "
            + "; ".join(f"{f['start']}..{f['end']} ({'; '.join(f['problems'])})"
                        for f in failures)
            + ". See missing_ranges (reason chunk_mismatch)."
        ]
    return out


class Backend(SourceBackend):
    """HyRiver backend (pygridmet + pydaymet)."""

    source_id = "hyriver"

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": [
                "precipitation", "tmax", "tmin", "tmean", "pet",
                "wind_speed", "solar_radiation", "humidity",
                "landcover", "soil",
            ],
            "coverage": ["CONUS", "NORTH_AMERICA"],
            "requires_auth": [],
            "requires_extras": ["hyriver"],
        }

    # backend_config key → the one HyRiver library that product actually needs
    _CFG_KEY_TO_LIB = {
        "pygridmet_variable": "pygridmet",
        "pydaymet_variable": "pydaymet",
        "pygeohydro_product": "pygeohydro",
        "py3dep_resolution": "py3dep",
    }

    def is_available(self, spec: Optional[ProductSpec] = None) -> tuple[bool, Optional[str]]:
        # Product-specific check: only the library that product routes through
        # matters. Without it, a machine with only py3dep installed would
        # report GridMET products as available and fail mid-fetch.
        if spec is not None:
            libs = [lib for key, lib in self._CFG_KEY_TO_LIB.items()
                    if key in spec.backend_config]
            for lib in libs:
                try:
                    __import__(lib)
                    return True, None
                except ImportError:
                    return False, (
                        f"Product {spec.id!r} needs {lib!r}, which is not installed. "
                        "Run `pip install aihydro-data[hyriver]`."
                    )
            # Unknown config shape — fall through to the any-lib probe.

        for lib in ("pygridmet", "pydaymet", "pygeohydro", "py3dep"):
            try:
                __import__(lib)
                return True, None
            except ImportError:
                continue
        return False, (
            "No HyRiver libraries found (pygridmet, pydaymet, pygeohydro, py3dep). "
            "Run `pip install aihydro-data[hyriver]`."
        )

    def fetch_timeseries(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
    ) -> Any:
        """Return a pd.DataFrame with columns ['date', spec.variable]."""
        cfg = spec.backend_config

        if "pygridmet_variable" in cfg:
            return self._fetch_gridmet(spec, cfg, geometry, start, end, aggregation)
        if "pydaymet_variable" in cfg:
            return self._fetch_daymet(spec, cfg, geometry, start, end, aggregation)
        if "pygeohydro_product" in cfg or "py3dep_resolution" in cfg:
            # Static raster products — route through fetch_raster
            raise ValueError(
                f"Product {spec.id!r} is a static raster — call "
                "fetch(..., aggregation='raw_raster') or use fetch_raster() directly."
            )

        raise ValueError(
            f"HyRiver backend does not know how to fetch product {spec.id!r}. "
            "backend_config must contain 'pygridmet_variable', 'pydaymet_variable', "
            "'pygeohydro_product', or 'py3dep_resolution'."
        )

    def fetch_raster(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        """Return an xarray.DataArray / Dataset clipped to geometry."""
        cfg = spec.backend_config
        if "pygridmet_variable" in cfg:
            return self._fetch_gridmet_raster(spec, cfg, geometry, start, end)
        if "pygeohydro_product" in cfg:
            return self._fetch_pygeohydro_raster(spec, cfg, geometry, start, end)
        if "py3dep_resolution" in cfg:
            return self._fetch_3dep_raster(spec, cfg, geometry)
        raise NotImplementedError(
            f"HyRiver raster fetch not implemented for product {spec.id!r}."
        )

    # ── GridMET ──────────────────────────────────────────────────────────

    def _fetch_gridmet(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
    ) -> Any:
        pygridmet = require_import("pygridmet", extra="hyriver", backend="hyriver")

        import pandas as pd

        from aihydro_data.sources._retry import call_with_retry
        var_key = cfg["pygridmet_variable"]
        _failures: list[dict[str, Any]] = []

        if aggregation == "centroid":
            # Point-mode fetch: extract at geometry centroid
            c = geometry.centroid
            coords = (c.x, c.y)   # pygridmet expects (lon, lat) — verified live
            df, _failures = _chunked_daily(
                lambda a, b: call_with_retry(
                    lambda: pygridmet.get_bycoords(coords, dates=(a, b), variables=[var_key]),
                    label="pygridmet.get_bycoords",
                ),
                start, end, "pygridmet.get_bycoords",
            )
        else:
            # Polygon-mode: spatial mean over the geometry
            import geopandas as gpd
            gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
            df, _failures = _chunked_daily(
                lambda a, b: call_with_retry(
                    lambda: pygridmet.get_bygeom(
                        gdf.geometry.iloc[0], dates=(a, b), variables=[var_key],
                    ),
                    label="pygridmet.get_bygeom",
                ),
                start, end, "pygridmet.get_bygeom",
            )
            # Spatial aggregation. get_bygeom returns an xarray.Dataset, so
            # reduce over the spatial dims (dim=[...]) rather than axis=1.
            if aggregation == "basin_mean":
                df = _basin_aggregate_to_frame(df, var_key, how="mean")
            elif aggregation == "basin_sum":
                df = _basin_aggregate_to_frame(df, var_key, how="sum")

        # Normalise to (date, variable) shape
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index().rename(columns={"index": "date", "time": "date"})
        if "date" not in df.columns and df.index.name:
            df = df.reset_index()

        col = var_key
        out_col = spec.variable
        if col in df.columns:
            df = df[["date", col]].rename(columns={col: out_col})
        elif out_col not in df.columns:
            # Take whatever numeric column is first
            num_cols = df.select_dtypes("number").columns.tolist()
            df = df[["date", num_cols[0]]].rename(columns={num_cols[0]: out_col})

        df["date"] = pd.to_datetime(df["date"])
        return _attach_chunk_failures(df, _failures)

    def _fetch_gridmet_raster(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        pygridmet = require_import("pygridmet", extra="hyriver", backend="hyriver")

        import geopandas as gpd
        var_key = cfg["pygridmet_variable"]
        gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
        from aihydro_data.sources._retry import call_with_retry
        ds, failures = _chunked_daily(
            lambda a, b: call_with_retry(
                lambda: pygridmet.get_bygeom(gdf.geometry.iloc[0], dates=(a, b),
                                             variables=[var_key]),
                label="pygridmet.get_bygeom",
            ),
            start, end, "pygridmet.get_bygeom",
        )
        return _attach_chunk_failures(ds, failures)

    # ── Daymet ───────────────────────────────────────────────────────────

    def _fetch_daymet(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
    ) -> Any:
        pydaymet = require_import("pydaymet", extra="hyriver", backend="hyriver")

        import pandas as pd

        from aihydro_data.sources._retry import call_with_retry
        var_key = cfg["pydaymet_variable"]

        if aggregation == "centroid":
            c = geometry.centroid
            coords = (c.x, c.y)   # pydaymet uses (lon, lat)
            df = call_with_retry(
                lambda: pydaymet.get_bycoords(coords, dates=(start, end), variables=[var_key]),
                label="pydaymet.get_bycoords",
            )
        else:
            import geopandas as gpd
            gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
            df = call_with_retry(
                lambda: pydaymet.get_bygeom(
                    gdf.geometry.iloc[0], dates=(start, end), variables=[var_key],
                ),
                label="pydaymet.get_bygeom",
            )
            # get_bygeom returns an xarray.Dataset → reduce over spatial dims.
            if aggregation == "basin_mean":
                df = _basin_aggregate_to_frame(df, var_key, how="mean")
            elif aggregation == "basin_sum":
                df = _basin_aggregate_to_frame(df, var_key, how="sum")

        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index().rename(columns={"index": "date", "time": "date"})

        out_col = spec.variable
        if var_key in df.columns:
            df = df[["date", var_key]].rename(columns={var_key: out_col})

        df["date"] = pd.to_datetime(df["date"])
        return df

    # ── pygeohydro (NLCD, POLARIS) ────────────────────────────────────────

    def _fetch_pygeohydro_raster(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        """Fetch NLCD or POLARIS via pygeohydro. Returns xarray.Dataset."""
        gh = require_import("pygeohydro", extra="hyriver", backend="hyriver")

        import geopandas as gpd
        product = cfg.get("pygeohydro_product", "")

        if product == "nlcd":
            # Honour the caller's requested year via the start date, snapping to
            # the nearest available NLCD epoch; fall back to the configured
            # default if the date can't be parsed.
            year = _nearest_nlcd_year(start, default=cfg.get("default_year", NLCD_LATEST_YEAR))
            # The product's layer ("cover", "impervious", ...). This used to be
            # hard-coded to "cover", so NLCD_IMPERVIOUS returned land-cover
            # class codes instead of percent impervious.
            layer = cfg.get("nlcd_layer", "cover")
            gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
            ds = gh.nlcd_bygeom(gdf, years={layer: [year]}, resolution=cfg.get("resolution_m", 30))
            # nlcd_bygeom keyed by GeoDataFrame index → unwrap to a single Dataset.
            if isinstance(ds, dict):
                ds = next(iter(ds.values()))
            return ds

        if product == "polaris":
            # POLARIS layers are named "<property>_<depth-index>" (0–5 cm → "_5").
            # Use the proven pygeohydro.soil_polaris API which returns an
            # xr.Dataset with vars like sand_5/silt_5/clay_5/ksat_5 — exactly
            # what the Curve-Number classifier expects.
            layers = cfg.get("default_layers", ["sand_5", "silt_5", "clay_5", "ksat_5"])
            return gh.soil_polaris(layers=layers, geometry=geometry, geo_crs=4326)

        raise NotImplementedError(
            f"pygeohydro product {product!r} not yet implemented in HyRiver backend."
        )

    # ── py3dep (3DEP DEM) ─────────────────────────────────────────────────

    def _fetch_3dep_raster(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
    ) -> Any:
        """Fetch USGS 3DEP DEM via py3dep. Returns xarray.DataArray."""
        py3dep = require_import("py3dep", extra="hyriver", backend="hyriver")

        resolution = cfg.get("py3dep_resolution", 10)
        product = cfg.get("py3dep_product", "DEM")

        import geopandas as gpd
        gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
        dem = py3dep.get_map(product, gdf.geometry.iloc[0], resolution=resolution, crs="EPSG:4326")
        return dem
