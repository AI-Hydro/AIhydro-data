"""
Input coercion + batching.

Phase 1 deliverable: skeleton + the `coerce_geometry()` helper that turns
arbitrary user input (GDF, GeoJSON dict, shapely, (lat, lon) tuple, bbox)
into a single canonical shapely geometry in WGS84.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

import re

# Lazy imports to keep core lightweight.

_COORD_STRING = re.compile(r"[+-]?\d+(?:\.\d*)?(?:\s*,\s*|\s+)[+-]?\d+(?:\.\d*)?")
_GAUGE_STRING = re.compile(r"(?:USGS-)?\d+", re.IGNORECASE)


class GaugeID:
    """
    Non-geometric identifier wrapper for backends that route on station IDs
    (e.g. USGS NWIS, GRDC). Quacks-like-a-geometry enough that the fetch
    pipeline can pass it through routing/caching without special-casing
    every call site:

        - .wkt              → "GAUGE_ID(<id>)" for the default usgs scheme,
                              "GAUGE_ID(<scheme>:<id>)" otherwise
        - .bounds           → None (signals to detect_region() to fall back)
        - .geom_type        → "GaugeID"
        - .__repr__         → human-readable for logs
        - .id               → the raw string
        - .scheme           → id namespace ("usgs" by default). Only "usgs"
                              routes to CONUS; any other scheme routes global.

    Backends that want station IDs check ``isinstance(geom, GaugeID)`` or
    inspect ``.geom_type``; backends that need a real geometry refuse with
    a clear error.
    """
    __slots__ = ("id", "scheme")
    geom_type = "GaugeID"
    bounds = None

    def __init__(self, ident: str, scheme: str = "usgs") -> None:
        self.id = str(ident).strip()
        self.scheme = str(scheme).strip().lower()

    @property
    def wkt(self) -> str:
        if self.scheme == "usgs":
            return f"GAUGE_ID({self.id})"
        return f"GAUGE_ID({self.scheme}:{self.id})"

    @property
    def centroid(self):  # noqa: D401 — duck-type signal for detect_region
        return None

    def __repr__(self) -> str:
        if self.scheme == "usgs":
            return f"GaugeID({self.id!r})"
        return f"GaugeID({self.id!r}, scheme={self.scheme!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, GaugeID)
            and self.id == other.id
            and self.scheme == other.scheme
        )

    def __hash__(self) -> int:
        return hash(("GaugeID", self.scheme, self.id))


def geometry_id(geom: Any) -> str:
    """
    Canonical geometry identity (``aihydro.geom/1``) of a coerced geometry.

    Delegates to ``aihydro_core.records.place.geometry_id``; this function only
    converts the shapely object (or GaugeID) to the GeoJSON-like mapping that
    function takes. Ring start/direction and hole order do not change the id;
    distinct outlets/polygons do. Raises GeometryInvalid (never falls back to
    WKT) for geometries the algorithm cannot identify.
    """
    from aihydro_core.records.place import PlaceIdentityError
    from aihydro_core.records.place import geometry_id as _core_geometry_id

    from aihydro_data.exceptions import GeometryInvalid

    try:
        if isinstance(geom, GaugeID):
            mapping = {"type": "GaugeID", "scheme": geom.scheme, "id": geom.id}
        else:
            from shapely.geometry import mapping as _mapping
            mapping = _mapping(geom)
        return _core_geometry_id(mapping)
    except PlaceIdentityError as exc:
        raise GeometryInvalid(
            code=exc.code,
            message=f"Cannot derive a geometry identity: {exc}",
            recovery="Pass a Point, MultiPoint, Polygon, MultiPolygon or GaugeID with valid, non-degenerate coordinates.",
            next_tools=["data_help"],
        ) from exc
    except GeometryInvalid:
        raise
    except Exception as exc:
        raise GeometryInvalid(
            code="GEOMETRY_ID_FAILED",
            message=f"Cannot derive a geometry identity from {type(geom).__name__}: {exc}",
            recovery="Pass a valid shapely geometry or GaugeID.",
            next_tools=["data_help"],
        ) from exc


_POINT_KEYS = frozenset({"lat", "lon"})
_BBOX_KEYS = frozenset({"west", "south", "east", "north"})


def _is_number(v: Any) -> bool:
    """The one numeric predicate for coordinate values: a real number (numpy scalars included), not a bool."""
    import numbers

    return isinstance(v, numbers.Real) and not isinstance(v, bool)


def _bad_coords(message: str):
    from aihydro_data.exceptions import GeometryInvalid

    return GeometryInvalid(
        code="GEOMETRY_INVALID_COORDINATES",
        message=message,
        recovery=("Pass exactly {'lat': <-90..90>, 'lon': <-180..180>} for a point or exactly "
                  "{'west','south','east','north'} (west<east, south<north) for a box, with numeric values."),
        next_tools=["data_help"], docs_anchor="first_fetch",
    )


def coordinate_dict_kind(d: Any) -> Optional[str]:
    """``"point"`` / ``"bbox"`` for a valid coordinate dict, ``None`` when ``d`` is not a coordinate dict
    (e.g. a ``{label: geometry}`` mapping or GeoJSON); raises GEOMETRY_INVALID_COORDINATES for a dict that
    is clearly one but malformed: extra keys, a lat/lon + bbox mix, a missing half, non-numeric / non-finite
    / out-of-range values, west>=east or south>=north."""
    import math

    if not isinstance(d, dict) or "type" in d:
        return None
    named = [k for k in d if k in _POINT_KEYS | _BBOX_KEYS]
    # Label mappings may use any label, even "lat"; they are told apart by their values being geometries.
    if not named or not any(
        not (hasattr(d[k], "geom_type") or hasattr(d[k], "geometry") or isinstance(d[k], (dict, list, tuple)))
        for k in named
    ):
        return None
    keys = set(d)
    if keys == _POINT_KEYS:
        kind = "point"
    elif keys == _BBOX_KEYS:
        kind = "bbox"
    else:
        raise _bad_coords(
            f"geometry dict has keys {sorted(keys)}; expected exactly {sorted(_POINT_KEYS)} or {sorted(_BBOX_KEYS)} "
            f"(no extra keys, no mixing, no missing half).")
    for k in keys:
        v = d[k]
        if not _is_number(v) or not math.isfinite(float(v)):
            raise _bad_coords(f"{k}={v!r} ({type(v).__name__}) is not a finite number.")
    lat_keys, lon_keys = (("lat",), ("lon",)) if kind == "point" else (("south", "north"), ("west", "east"))
    for k in lat_keys:
        if abs(float(d[k])) > 90:
            raise _bad_coords(f"{k}={d[k]} is outside latitude range +/-90.")
    for k in lon_keys:
        if abs(float(d[k])) > 180:
            raise _bad_coords(f"{k}={d[k]} is outside longitude range +/-180.")
    if kind == "bbox" and not (float(d["west"]) < float(d["east"]) and float(d["south"]) < float(d["north"])):
        raise _bad_coords(f"bbox needs west<east and south<north, got {dict(d)}. A box crossing the antimeridian "
                          "(west>east) is not supported: split it in two or pass GeoJSON.")
    return kind


def coerce_geometry(geom: Any) -> Any:
    """
    Return a shapely geometry in EPSG:4326 from a variety of inputs:
        - geopandas.GeoDataFrame / GeoSeries  → union of all rows
        - dict with type/coordinates           → GeoJSON
        - shapely BaseGeometry                 → assumed already WGS84
        - (lat, lon) tuple                     → Point (library convention; agent-facing
                                                  tools take {"lat","lon"} instead)
        - WKT string                           → shapely geometry
        - USGS gauge id string ('01013500', 'USGS-01013500') → GaugeID
        - any other string, including a numeric coordinate string → GeometryInvalid
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

    _kind = coordinate_dict_kind(geom)
    if _kind == "point":
        return _Point(float(geom["lon"]), float(geom["lat"]))
    if _kind == "bbox":
        return _box(float(geom["west"]), float(geom["south"]), float(geom["east"]), float(geom["north"]))

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
            if abs(lat) > 90 or abs(lon) > 180:
                raise _bad_coords(f"(lat, lon)=({lat}, {lon}) is outside +/-90 / +/-180.")
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
        # A numeric coordinate string ("-68.58,47.24") is a position, never a gauge. Its axis
        # order cannot be known from a string, so it is refused rather than guessed.
        if _COORD_STRING.fullmatch(s):
            raise GeometryInvalid(
                code="GEOMETRY_COORDINATE_STRING",
                message=f"{s!r} is a coordinate string, not a gauge id or WKT; its axis order is ambiguous.",
                recovery="Pass {'lat': <lat>, 'lon': <lon>}, a GeoJSON Point, or 'POINT (<lon> <lat>)'.",
                next_tools=["data_help"],
                docs_anchor="first_fetch",
            )
        # Only a USGS-style station id is a gauge: digits, optionally 'USGS-' prefixed.
        if _GAUGE_STRING.fullmatch(s):
            return GaugeID(s)
        raise GeometryInvalid(
            code="GEOMETRY_UNPARSEABLE_STRING",
            message=f"{s[:60]!r} is neither WKT nor a USGS gauge id (digits, optionally 'USGS-' prefixed).",
            recovery="Pass WKT ('POINT (<lon> <lat>)'), a USGS site number such as '01013500', or {'lat':..,'lon':..}.",
            next_tools=["data_help"],
            docs_anchor="first_fetch",
        )

    raise GeometryInvalid(
        code="GEOMETRY_UNSUPPORTED_TYPE",
        message=f"Unsupported geometry input: {type(geom).__name__}",
        recovery="Pass a GeoDataFrame, GeoJSON dict, shapely geometry, (lat, lon), or (minx, miny, maxx, maxy).",
        next_tools=["data_help"],
        docs_anchor="first_fetch",
    )
