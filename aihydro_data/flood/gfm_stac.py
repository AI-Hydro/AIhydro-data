"""
GFM STAC live fetch — EODC Global Flood Monitoring catalogue.

STAC API: https://stac.eodc.eu/api/v1  collection: GFM
Primary asset: ensemble_flood_extent (uint8; 1 = flooded, 255 = nodata)
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

GFM_STAC_API = "https://stac.eodc.eu/api/v1"
GFM_COLLECTION = "GFM"
GFM_FLOOD_ASSET = "ensemble_flood_extent"
FLOOD_VALUE = 1


def search_gfm_items(
    bounds_wgs84: list[float],
    event_date: str,
    *,
    max_items: int = 20,
    timeout: int = 45,
) -> list[dict[str, Any]]:
    """Return STAC feature dicts for GFM on ``event_date`` intersecting ``bounds``."""
    if len(bounds_wgs84) < 4:
        raise ValueError("bounds_wgs84 must be [west, south, east, north]")
    west, south, east, north = [float(v) for v in bounds_wgs84[:4]]
    day = event_date.strip()[:10]
    dt_range = f"{day}T00:00:00Z/{day}T23:59:59Z"
    query = urlencode(
        {
            "collections": GFM_COLLECTION,
            "bbox": f"{west},{south},{east},{north}",
            "datetime": dt_range,
            "limit": str(int(max_items)),
        }
    )
    url = f"{GFM_STAC_API}/search?{query}"
    with urlopen(url, timeout=timeout) as resp:
        payload = json.load(resp)
    return list(payload.get("features") or [])


def _read_flood_mask_window(
    asset_href: str,
    bounds_wgs84: list[float],
) -> tuple[Any, Any, str] | None:
    """Read flooded (==1) boolean mask for bounds from one COG asset."""
    import numpy as np
    import rasterio
    from rasterio.crs import CRS
    from rasterio.features import shapes
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    west, south, east, north = [float(v) for v in bounds_wgs84[:4]]
    with rasterio.open(asset_href) as src:
        pb = transform_bounds(CRS.from_epsg(4326), src.crs, west, south, east, north)
        window = from_bounds(*pb, transform=src.transform)
        if window.width <= 0 or window.height <= 0:
            return None
        arr = src.read(1, window=window, boundless=True, fill_value=255)
        flooded = arr == FLOOD_VALUE
        if not np.any(flooded):
            return None
        transform = src.window_transform(window)
        geoms = []
        for geom, val in shapes(flooded.astype(np.uint8), mask=flooded, transform=transform):
            if int(val) != 1:
                continue
            geoms.append(shape(geom))
        if not geoms:
            return None
        merged = unary_union(geoms)
        if merged.is_empty:
            return None
        return merged, src.crs, asset_href


def _geom_to_wgs84_geojson(geom, src_crs) -> dict[str, Any]:
    from shapely.geometry import mapping
    from shapely.ops import transform as shp_transform
    import pyproj

    src = pyproj.CRS.from_user_input(src_crs)
    dst = pyproj.CRS.from_epsg(4326)
    if src == dst:
        out_geom = geom
    else:
        transformer = pyproj.Transformer.from_crs(src, dst, always_xy=True)
        out_geom = shp_transform(transformer.transform, geom)
    out_geom = out_geom.simplify(tolerance=0.0001, preserve_topology=True)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(out_geom),
                "properties": {"source": "gfm_stac", "flood_value": FLOOD_VALUE},
            }
        ],
    }


def fetch_gfm_stac_geojson(
    bounds_wgs84: list[float],
    event_date: str,
    *,
    timeout: int = 45,
) -> dict[str, Any]:
    """
    Live GFM fetch via EODC STAC + COG read.

    Returns geojson (possibly empty features if no flood in AOI) and metadata.
    Raises RuntimeError when STAC/network fails.
    """
    items = search_gfm_items(bounds_wgs84, event_date, max_items=5, timeout=timeout)
    if not items:
        return {
            "geojson": {"type": "FeatureCollection", "features": []},
            "source": "gfm_stac_empty",
            "event_date": event_date[:10],
            "live": True,
            "n_items": 0,
            "note": "No GFM STAC items for date/bbox.",
        }

    from shapely.ops import unary_union
    from shapely.geometry import mapping, shape
    import pyproj
    from shapely.ops import transform as shp_transform

    merged_geoms = []
    src_crs = None
    n_assets = 0
    for feat in items:
        assets = feat.get("assets") or {}
        asset = assets.get(GFM_FLOOD_ASSET)
        if not asset:
            continue
        href = asset.get("href")
        if not href:
            continue
        try:
            result = _read_flood_mask_window(href, bounds_wgs84)
        except Exception as exc:
            log.debug("Skip GFM asset %s: %s", href, exc)
            continue
        if result is None:
            continue
        geom, crs, _ = result
        merged_geoms.append(geom)
        src_crs = crs
        n_assets += 1

    if not merged_geoms:
        return {
            "geojson": {"type": "FeatureCollection", "features": []},
            "source": "gfm_stac_no_flood",
            "event_date": event_date[:10],
            "live": True,
            "n_items": len(items),
            "n_assets_read": 0,
            "note": "GFM tiles found but no flooded cells (value=1) in AOI.",
        }

    union = unary_union(merged_geoms)
    gj = _geom_to_wgs84_geojson(union, src_crs)
    return {
        "geojson": gj,
        "source": "gfm_stac",
        "event_date": event_date[:10],
        "live": True,
        "n_items": len(items),
        "n_assets_read": n_assets,
        "stac_api": GFM_STAC_API,
    }
