"""
HyRiver backend.

Wraps pygridmet (GridMET) and pydaymet (Daymet) for CONUS / North America
precipitation and other meteorological variables. All HyRiver imports are
lazy so this module is importable without the extra installed.

Install: pip install aihydro-data[hyriver]
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
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

    def is_available(self) -> tuple[bool, Optional[str]]:
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
        try:
            import pygridmet
        except ImportError as exc:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="HYRIVER_GRIDMET_NOT_INSTALLED",
                message=f"pygridmet not installed: {exc}",
                recovery="pip install aihydro-data[hyriver]",
                next_tools=["data_doctor"],
                docs_anchor="install",
            ) from exc

        import pandas as pd
        from aihydro_data.sources._retry import call_with_retry
        var_key = cfg["pygridmet_variable"]

        if aggregation == "centroid":
            # Point-mode fetch: extract at geometry centroid
            c = geometry.centroid
            coords = (c.x, c.y)   # pygridmet expects (lon, lat) — verified live
            df = call_with_retry(
                lambda: pygridmet.get_bycoords(coords, dates=(start, end), variables=[var_key]),
                label="pygridmet.get_bycoords",
            )
        else:
            # Polygon-mode: spatial mean over the geometry
            import geopandas as gpd
            gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
            df = call_with_retry(
                lambda: pygridmet.get_bygeom(
                    gdf.geometry.iloc[0], dates=(start, end), variables=[var_key],
                ),
                label="pygridmet.get_bygeom",
            )
            # Spatial aggregation
            if aggregation == "basin_mean":
                df = df.mean(axis=1).to_frame(name=var_key)
            elif aggregation == "basin_sum":
                df = df.sum(axis=1).to_frame(name=var_key)

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
        return df

    def _fetch_gridmet_raster(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        try:
            import pygridmet
        except ImportError as exc:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="HYRIVER_GRIDMET_NOT_INSTALLED",
                message=f"pygridmet not installed: {exc}",
                recovery="pip install aihydro-data[hyriver]",
            ) from exc

        import geopandas as gpd
        var_key = cfg["pygridmet_variable"]
        gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
        ds = pygridmet.get_bygeom(gdf.geometry.iloc[0], dates=(start, end), variables=[var_key])
        return ds

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
        try:
            import pydaymet
        except ImportError as exc:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="HYRIVER_DAYMET_NOT_INSTALLED",
                message=f"pydaymet not installed: {exc}",
                recovery="pip install aihydro-data[hyriver]",
                next_tools=["data_doctor"],
                docs_anchor="install",
            ) from exc

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
            if aggregation in ("basin_mean",):
                df = df.mean(axis=1).to_frame(name=var_key)
            elif aggregation == "basin_sum":
                df = df.sum(axis=1).to_frame(name=var_key)

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
        try:
            import pygeohydro as gh
        except ImportError as exc:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="HYRIVER_PYGEOHYDRO_NOT_INSTALLED",
                message=f"pygeohydro not installed: {exc}",
                recovery="pip install aihydro-data[hyriver]",
                next_tools=["data_doctor"],
                docs_anchor="install",
            ) from exc

        import geopandas as gpd
        product = cfg.get("pygeohydro_product", "")

        if product == "nlcd":
            year = cfg.get("default_year", 2019)
            gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
            ds = gh.nlcd_bygeom(gdf, years={"cover": [year]}, resolution=cfg.get("resolution_m", 30))
            return ds

        if product == "polaris":
            layers = cfg.get("default_layers", ["sand", "silt", "clay", "ksat"])
            depth = cfg.get("default_depth", "0_5")
            gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
            ds = gh.soil_properties(layers, depths=[depth], geometry=gdf.geometry.iloc[0])
            return ds

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
        try:
            import py3dep
        except ImportError as exc:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="HYRIVER_PY3DEP_NOT_INSTALLED",
                message=f"py3dep not installed: {exc}",
                recovery="pip install aihydro-data[hyriver]",
                next_tools=["data_doctor"],
                docs_anchor="install",
            ) from exc

        resolution = cfg.get("py3dep_resolution", 10)
        product = cfg.get("py3dep_product", "DEM")

        import geopandas as gpd
        gdf = gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")
        dem = py3dep.get_map(product, gdf.geometry.iloc[0], resolution=resolution, crs="EPSG:4326")
        return dem
