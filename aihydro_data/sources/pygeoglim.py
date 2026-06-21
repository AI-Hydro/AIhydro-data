"""
pygeoglim backend — GLiM lithology + GLHYMPS hydrogeology.

Geology is inherently static vector data: the output is a flat dict of
scalar attributes (area-weighted statistics over the ROI), not a timeseries
or raster.  This backend adapts that shape to the aihydro-data contract by:

  fetch_timeseries  → not used (static products are auto-promoted to raw_raster
                       by _pipeline._fetch_one before reaching this backend)
  fetch_raster      → calls glim_attributes and/or glhymps_attributes, returns
                       a pd.DataFrame[attribute_name → value] (one row)

The caller receives ``result.data`` as a single-row DataFrame.  To convert to
a plain dict:  ``result.data.iloc[0].to_dict()``

Install: pip install aihydro-data[pygeoglim]
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)


def _require_pygeoglim():
    """Import guard — lazy so the module is importable without pygeoglim."""
    try:
        import pygeoglim  # noqa: F401
        return True
    except ImportError:
        return False


def _shapely_to_geom(geometry: Any):
    """Coerce the aihydro_data geometry (shapely/GDF/GeoJSON) to a form
    pygeoglim accepts (shapely Polygon or GeoDataFrame)."""
    try:
        import geopandas as gpd
        from shapely.geometry import mapping, shape
    except ImportError as exc:
        raise ImportError("geopandas is required for the pygeoglim backend.") from exc

    # Already GeoDataFrame
    if hasattr(geometry, "geometry") and hasattr(geometry, "crs"):
        if geometry.crs is None or geometry.crs.to_epsg() != 4326:
            geometry = geometry.to_crs(epsg=4326)
        return geometry

    # shapely geometry → single-row GDF
    if hasattr(geometry, "geom_type"):
        return gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326")

    # GeoJSON dict
    if isinstance(geometry, dict) and "type" in geometry:
        return gpd.GeoDataFrame(geometry=[shape(geometry)], crs="EPSG:4326")

    return geometry


class Backend(SourceBackend):
    """pygeoglim backend — wraps GLiM + GLHYMPS attribute extraction."""

    source_id = "pygeoglim"

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": ["geology"],
            "coverage": ["global"],
            "requires_auth": [],
            "notes": (
                "GLiM redistribution requires CCGM written permission. "
                "Set PYGEOGLIM_HF_TOKEN env var or prefetch tiles locally."
            ),
        }

    def is_available(self, spec: Optional[ProductSpec] = None) -> tuple[bool, Optional[str]]:
        if not _require_pygeoglim():
            return (
                False,
                "pygeoglim is not installed. Run: pip install aihydro-data[pygeoglim]",
            )
        return True, None

    # ── Static products bypass fetch_timeseries — only fetch_raster is called ──

    def fetch_timeseries(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
    ) -> Any:
        # Geology is always static → _pipeline auto-promotes to raw_raster before
        # calling this backend.  This path is a safety fallback.
        return self.fetch_raster(spec, geometry, start, end)

    def fetch_raster(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        """
        Return geology attributes as a single-row pd.DataFrame.

        Columns depend on the product:
          PYGEOGLIM_ALL (default)
            geol_1st_class, glim_1st_class_frac, geol_2nd_class,
            glim_2nd_class_frac, carbonate_rocks_frac,
            geol_porosity, geol_permeability, geol_permeability_linear,
            hydraulic_conductivity
          GLIM_TILES
            first 5 columns above (lithology only)
          GLHYMPS_TILES
            last 4 columns above (hydrogeology only)
        """
        import pandas as pd

        ok, reason = self.is_available(spec)
        if not ok:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="PYGEOGLIM_UNAVAILABLE",
                message=reason or "pygeoglim is not available.",
                recovery="pip install aihydro-data[pygeoglim]",
                next_tools=["data_doctor"],
                docs_anchor="install",
            )

        fetch_glim = spec.backend_config.get("fetch_glim", True)
        fetch_glhymps = spec.backend_config.get("fetch_glhymps", True)

        token = os.environ.get("PYGEOGLIM_HF_TOKEN")

        geom = _shapely_to_geom(geometry)
        combined: dict[str, Any] = {}

        if fetch_glim:
            try:
                from pygeoglim import glim_attributes
                log.info("pygeoglim: fetching GLiM lithology attributes")
                glim_attrs = glim_attributes(
                    geom, crs="EPSG:4326", decode_names=True,
                    token=token,
                )
                combined.update(glim_attrs)
                log.info("pygeoglim: GLiM attributes fetched: %s", list(glim_attrs))
            except Exception as exc:
                log.warning("pygeoglim: GLiM fetch failed: %s", exc)
                if not fetch_glhymps:
                    raise

        if fetch_glhymps:
            try:
                from pygeoglim import glhymps_attributes
                log.info("pygeoglim: fetching GLHYMPS hydrogeology attributes")
                glhymps_attrs = glhymps_attributes(
                    geom, crs="EPSG:4326",
                    token=token,
                )
                combined.update(glhymps_attrs)
                log.info("pygeoglim: GLHYMPS attributes fetched: %s", list(glhymps_attrs))
            except Exception as exc:
                log.warning("pygeoglim: GLHYMPS fetch failed: %s", exc)
                if not combined:
                    raise

        if not combined:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="PYGEOGLIM_NO_DATA",
                message=(
                    "pygeoglim returned no geology attributes for the given geometry. "
                    "The region may not be covered by the current tile set."
                ),
                recovery=(
                    "Ensure pygeoglim tiles are available for this region. "
                    "Run: python -m pygeoglim.cli prefetch --region CONUS"
                ),
                next_tools=["data_list_products", "data_doctor"],
                docs_anchor="troubleshooting",
            )

        return pd.DataFrame([combined])
