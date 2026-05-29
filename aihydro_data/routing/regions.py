"""
Region definitions used by the router.

A "region" is a geographic extent label that maps to a CoverageTag in
contracts.py. The router uses these to pick the best product for a
user's geometry.

Design: we keep this purposefully simple for Phase 2 — bounding-box
checks that cover the CONUS case and a global fallback. Later phases
can add Pfafstetter basin lookups for S_ASIA / AFRICA etc.

All bboxes are (min_lon, min_lat, max_lon, max_lat) in WGS84.
"""
from __future__ import annotations

from typing import NamedTuple


class RegionDef(NamedTuple):
    """A geographic region used by the router."""
    tag: str                          # matches CoverageTag
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    label: str = ""                   # human-readable name


# Ordered list — router tries each in sequence and returns the first match.
# More specific regions must come before broader ones.
REGION_DEFS: list[RegionDef] = [
    RegionDef(
        tag="CONUS",
        min_lon=-125.0, min_lat=24.0, max_lon=-66.0, max_lat=50.0,
        label="Contiguous United States",
    ),
    RegionDef(
        tag="NORTH_AMERICA",
        min_lon=-168.0, min_lat=7.0, max_lon=-52.0, max_lat=83.0,
        label="North America",
    ),
    RegionDef(
        tag="SOUTH_AMERICA",
        min_lon=-82.0, min_lat=-56.0, max_lon=-34.0, max_lat=13.0,
        label="South America",
    ),
    RegionDef(
        tag="EUROPE",
        min_lon=-25.0, min_lat=34.0, max_lon=45.0, max_lat=72.0,
        label="Europe",
    ),
    RegionDef(
        tag="AFRICA",
        min_lon=-18.0, min_lat=-35.0, max_lon=52.0, max_lat=38.0,
        label="Africa",
    ),
    RegionDef(
        tag="S_ASIA",
        min_lon=60.0, min_lat=5.0, max_lon=100.0, max_lat=40.0,
        label="South / Southeast Asia",
    ),
    RegionDef(
        tag="ASIA",
        min_lon=26.0, min_lat=-10.0, max_lon=180.0, max_lat=78.0,
        label="Asia",
    ),
    RegionDef(
        tag="OCEANIA",
        min_lon=110.0, min_lat=-50.0, max_lon=180.0, max_lat=-5.0,
        label="Oceania",
    ),
]


def point_in_bbox(lon: float, lat: float, r: RegionDef) -> bool:
    return r.min_lon <= lon <= r.max_lon and r.min_lat <= lat <= r.max_lat


def bbox_within_bbox(
    geom_min_lon: float, geom_min_lat: float,
    geom_max_lon: float, geom_max_lat: float,
    r: RegionDef,
) -> bool:
    """True only if the geometry bbox is fully inside the region bbox."""
    return (
        r.min_lon <= geom_min_lon
        and geom_max_lon <= r.max_lon
        and r.min_lat <= geom_min_lat
        and geom_max_lat <= r.max_lat
    )
