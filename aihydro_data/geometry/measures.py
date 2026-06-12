"""Shared geometry measurements for snapping backends.

The reach/gauge backends (GEOGLOWS, GloFAS, Open-Meteo Flood) each need the
same two operations: turn a geometry into a single (lat, lon) snap point, and
measure a polygon's geodesic area to validate the snap. These lived as
near-identical private helpers in three backend files; centralising them here
keeps the behaviour identical and the snap logic in one place.

All imports are lazy so importing this module costs nothing without pyproj.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


def geodesic_area_km2(geometry: Any) -> Optional[float]:
    """Geodesic area (km²) of a Polygon/MultiPolygon; None for non-polygons
    or if pyproj is unavailable / the calculation fails."""
    if getattr(geometry, "geom_type", None) not in ("Polygon", "MultiPolygon"):
        return None
    try:
        from pyproj import Geod
        area_m2, _ = Geod(ellps="WGS84").geometry_area_perimeter(geometry)
        return abs(area_m2) / 1e6
    except Exception as exc:
        log.debug("geodesic_area_km2 failed: %s", exc)
        return None


def outlet_and_area(
    geometry: Any,
    outlet: Optional[tuple[float, float]] = None,
) -> tuple[float, float, Optional[float]]:
    """Return ``(lat, lon, area_km2 | None)`` for a snap.

    - ``outlet`` (lat, lon), if given, wins as the snap point (a delineated pour
      point beats the centroid for main-channel snapping).
    - Otherwise: polygon → centroid; point → its own coords.
    - ``area_km2`` is the polygon's geodesic area (None for point inputs),
      regardless of which snap point was used — it validates the snap.
    """
    is_polygon = getattr(geometry, "geom_type", None) in ("Polygon", "MultiPolygon")
    area = geodesic_area_km2(geometry) if is_polygon else None

    if outlet is not None:
        return float(outlet[0]), float(outlet[1]), area

    if is_polygon:
        c = geometry.centroid
        return float(c.y), float(c.x), area
    try:
        return float(geometry.y), float(geometry.x), area
    except Exception:
        c = geometry.centroid
        return float(c.y), float(c.x), area
