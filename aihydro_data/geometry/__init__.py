"""
Input coercion + batching.

Phase 1 deliverable: skeleton + the `coerce_geometry()` helper that turns
arbitrary user input (GDF, GeoJSON dict, shapely, (lat, lon) tuple, bbox)
into a single canonical shapely geometry in WGS84.
"""
from __future__ import annotations

from typing import Any, Tuple

# Lazy imports to keep core lightweight.


class GaugeID:
    """
    Non-geometric identifier wrapper for backends that route on station IDs
    (e.g. USGS NWIS, GRDC). Quacks-like-a-geometry enough that the fetch
    pipeline can pass it through routing/caching without special-casing
    every call site:

        - .wkt              → "GAUGE_ID(<id>)"  (stable cache-key fragment)
        - .bounds           → None (signals to detect_region() to fall back)
        - .geom_type        → "GaugeID"
        - .__repr__         → human-readable for logs
        - .id               → the raw string

    Backends that want station IDs check ``isinstance(geom, GaugeID)`` or
    inspect ``.geom_type``; backends that need a real geometry refuse with
    a clear error.
    """
    __slots__ = ("id",)
    geom_type = "GaugeID"
    bounds = None

    def __init__(self, ident: str) -> None:
        self.id = str(ident).strip()

    @property
    def wkt(self) -> str:
        return f"GAUGE_ID({self.id})"

    @property
    def centroid(self):  # noqa: D401 — duck-type signal for detect_region
        return None

    def __repr__(self) -> str:
        return f"GaugeID({self.id!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GaugeID) and self.id == other.id

    def __hash__(self) -> int:
        return hash(("GaugeID", self.id))


def coerce_geometry(geom: Any) -> Any:
    """
    Return a shapely geometry in EPSG:4326 from a variety of inputs:
        - geopandas.GeoDataFrame / GeoSeries  → union of all rows
        - dict with type/coordinates           → GeoJSON
        - shapely BaseGeometry                 → assumed already WGS84
        - (lat, lon) tuple                     → Point
        - (minx, miny, maxx, maxy) tuple/list  → Polygon (bbox)

    Raises aihydro_data.exceptions.GeometryInvalid on failure.
    """
    from shapely import wkt as _wkt  # noqa: F401  — ensures shapely is available
    from shapely.geometry import Point as _Point
    from shapely.geometry import box as _box
    from shapely.geometry import shape as _shape
    from shapely.geometry.base import BaseGeometry

    from aihydro_data.exceptions import GeometryInvalid

    if geom is None:
        raise GeometryInvalid(
            code="GEOMETRY_NULL",
            message="geometry argument is None.",
            recovery="Pass a (lat, lon) tuple, a GeoJSON dict, a shapely geometry, or a GeoDataFrame.",
            next_tools=["data_help"],
            docs_anchor="first_fetch",
        )

    # geopandas GeoDataFrame / GeoSeries — duck-type check to avoid hard
    # import dependency at module level
    if hasattr(geom, "crs") and hasattr(geom, "geometry"):
        try:
            gdf = geom if hasattr(geom, "to_crs") else geom.to_frame()
            if gdf.crs is None:
                gdf = gdf.set_crs(epsg=4326)
            elif gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)
            return gdf.geometry.unary_union
        except Exception as exc:
            raise GeometryInvalid(
                code="GEOMETRY_COERCION_FAILED",
                message=f"Could not project GeoDataFrame to WGS84: {exc}",
                recovery="Ensure the GDF has a valid CRS set.",
            )

    if isinstance(geom, BaseGeometry):
        return geom

    # Idempotency: a GaugeID is already a coerced "geometry" wrapper.
    # Without this, batch fetches re-coerce items and fail with
    # GEOMETRY_UNSUPPORTED_TYPE.
    if isinstance(geom, GaugeID):
        return geom

    if isinstance(geom, dict) and "type" in geom:
        try:
            if geom["type"] == "FeatureCollection":
                from shapely.ops import unary_union
                return unary_union([_shape(f["geometry"]) for f in geom["features"]])
            if geom["type"] == "Feature":
                return _shape(geom["geometry"])
            return _shape(geom)
        except Exception as exc:
            raise GeometryInvalid(
                code="GEOMETRY_INVALID_GEOJSON",
                message=f"GeoJSON parse failed: {exc}",
            )

    if isinstance(geom, (tuple, list)):
        if len(geom) == 2 and all(isinstance(v, (int, float)) for v in geom):
            # (lat, lon) tuple — order intentional: human-readable
            lat, lon = geom
            return _Point(lon, lat)
        if len(geom) == 4 and all(isinstance(v, (int, float)) for v in geom):
            return _box(*geom)  # (minx, miny, maxx, maxy)

    # String input: try WKT first (POINT (...), POLYGON (...)), otherwise
    # treat as a non-geometric identifier (e.g. USGS gauge ID '03353000'
    # for the direct_api streamflow backend). Backends that don't accept
    # identifier-style geometries will reject it explicitly with a clear
    # error.
    if isinstance(geom, str):
        s = geom.strip()
        if not s:
            raise GeometryInvalid(
                code="GEOMETRY_EMPTY_STRING",
                message="geometry string is empty.",
                recovery="Pass a non-empty WKT string, a gauge ID, or use (lat, lon).",
            )
        # Try WKT
        try:
            return _wkt.loads(s)
        except Exception:
            pass
        # Treat as identifier (gauge ID). Wrap so downstream code can
        # introspect it consistently.
        return GaugeID(s)

    raise GeometryInvalid(
        code="GEOMETRY_UNSUPPORTED_TYPE",
        message=f"Unsupported geometry input: {type(geom).__name__}",
        recovery="Pass a GeoDataFrame, GeoJSON dict, shapely geometry, (lat, lon), or (minx, miny, maxx, maxy).",
        next_tools=["data_help"],
        docs_anchor="first_fetch",
    )
