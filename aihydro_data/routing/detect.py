"""
Region detection: given a shapely geometry, return the best-matching
CoverageTag string.

Strategy (Phase 2):
1. Get the geometry's bounding box (shapely .bounds → minx, miny, maxx, maxy).
2. Get the centroid lon/lat.
3. Walk REGION_DEFS in order:
   - If the full bbox is inside the region bbox → confident match, return tag.
   - Otherwise note if the centroid falls inside (candidate).
4. Return the first confident match, or the first centroid candidate, or "global".

This intentionally avoids importing geopandas / pyproj at module level so
`from aihydro_data.routing import detect_region` is always cheap.
"""
from __future__ import annotations

from typing import Any

from aihydro_data.routing.regions import REGION_DEFS, bbox_within_bbox, point_in_bbox


def detect_region(geometry: Any) -> str:
    """
    Infer the best CoverageTag for `geometry`.

    Accepts any shapely geometry (the caller is responsible for coercing
    user input via geometry.coerce_geometry first).

    Returns one of: "CONUS", "NORTH_AMERICA", "SOUTH_AMERICA", "EUROPE",
    "AFRICA", "S_ASIA", "ASIA", "OCEANIA", "global".
    """
    # Non-geometric identifiers (GaugeID for NWIS, etc.) bypass spatial
    # detection. NWIS is CONUS-only; GRDC (future) would route differently.
    if getattr(geometry, "geom_type", None) == "GaugeID":
        return "CONUS"

    try:
        # shapely .bounds → (minx, miny, maxx, maxy) = (min_lon, min_lat, max_lon, max_lat)
        bounds = geometry.bounds
        if bounds is None:
            return "global"
        min_lon, min_lat, max_lon, max_lat = bounds
        centroid = geometry.centroid
        clon, clat = centroid.x, centroid.y
    except Exception:
        return "global"

    centroid_candidate: str | None = None

    for r in REGION_DEFS:
        if bbox_within_bbox(min_lon, min_lat, max_lon, max_lat, r):
            return r.tag
        if centroid_candidate is None and point_in_bbox(clon, clat, r):
            centroid_candidate = r.tag

    return centroid_candidate or "global"
