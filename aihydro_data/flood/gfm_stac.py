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
from urllib.request import urlopen

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
    from aihydro_data.exceptions import SourceUnavailable

    if any(link.get("rel") == "next" for link in payload.get("links", [])):
        raise SourceUnavailable(code="GFM_SEARCH_TRUNCATED",
                                message="GFM search has additional pages; the result is incomplete.",
                                recovery="Narrow the area/date request; paginated retrieval is not yet supported.")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError("GFM search did not return a feature list")
    return features


def _read_flood_mask_window(
    asset_href: str,
    bounds_wgs84: list[float],
) -> dict[str, Any]:
    """Read flooded (==1) boolean mask for bounds from one COG asset."""
    import numpy as np
    import rasterio
    from rasterio.crs import CRS
    from rasterio.features import shapes
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds
    from shapely.geometry import shape
    from shapely.ops import unary_union

    west, south, east, north = [float(v) for v in bounds_wgs84[:4]]
    with rasterio.open(asset_href) as src:
        pb = transform_bounds(CRS.from_epsg(4326), src.crs, west, south, east, north)
        window = from_bounds(*pb, transform=src.transform)
        if window.width <= 0 or window.height <= 0:
            raise ValueError("GFM window has no pixels")
        # Integer windows keep the pixel transform consistent with the read array.
        import math

        from rasterio.windows import Window
        left, top = math.floor(window.col_off), math.floor(window.row_off)
        right = math.ceil(window.col_off + window.width)
        bottom = math.ceil(window.row_off + window.height)
        window = Window(left, top, right - left, bottom - top)
        arr = src.read(1, window=window, boundless=True, masked=True, fill_value=255)
        valid = ~np.ma.getmaskarray(arr) & np.isin(arr.data, [0, FLOOD_VALUE])
        flooded = valid & (arr.data == FLOOD_VALUE)
        support = {"valid_pixel_count": int(valid.sum()),
                   "window_pixel_count": int(arr.size), "geometry": None,
                   "crs": src.crs, "asset_href": asset_href}
        if not np.any(flooded):
            return support
        transform = src.window_transform(window)
        geoms = []
        for geom, val in shapes(flooded.astype(np.uint8), mask=flooded, transform=transform):
            if int(val) != 1:
                continue
            geoms.append(shape(geom))
        if not geoms:
            return support
        merged = unary_union(geoms)
        if not merged.is_empty:
            support["geometry"] = merged
        return support


def _geom_to_wgs84_geojson(geom, src_crs) -> dict[str, Any]:
    import pyproj
    from shapely.geometry import mapping
    from shapely.ops import transform as shp_transform

    src = pyproj.CRS.from_user_input(src_crs)
    dst = pyproj.CRS.from_epsg(4326)
    if src == dst:
        out_geom = geom
    else:
        transformer = pyproj.Transformer.from_crs(src, dst, always_xy=True)
        out_geom = shp_transform(transformer.transform, geom)
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
    from shapely.geometry import box, mapping, shape
    from shapely.ops import unary_union

    from aihydro_data.exceptions import SourceUnavailable

    items = search_gfm_items(bounds_wgs84, event_date, max_items=20, timeout=timeout)
    base = {"geojson": {"type": "FeatureCollection", "features": []},
            "source": "gfm_stac", "event_date": event_date[:10], "live": True,
            "synthetic": False, "evidence_kind": "satellite_derived",
            "n_items": len(items), "n_assets_read": 0, "stac_api": GFM_STAC_API,
            "validation_ready": False,
            "validation_limitations": ["Joint valid observation footprint and model grid are not established.",
                                       "Acquisition-time suitability and source quality masks require review."],
            "spatial_coverage": "unverified", "items": []}
    if not items:
        return {**base, "status": "no_observations", "note": "No acquisitions found; this does not establish absence of flooding."}
    geometries = []
    failures = []
    valid_pixels = 0
    for feat in items:
        href = (feat.get("assets", {}).get(GFM_FLOOD_ASSET) or {}).get("href")
        record = {"id": feat.get("id"), "datetime": feat.get("properties", {}).get("datetime"),
                  "asset_href": href}
        base["items"].append(record)
        if not href:
            failures.append({**record, "error": "Flood asset missing"})
            continue
        try:
            result = _read_flood_mask_window(href, bounds_wgs84)
            record.update({key: result[key] for key in ("valid_pixel_count", "window_pixel_count")})
            if result["geometry"] is not None:
                # Transform each tile before union: tiles may use different CRSs.
                gj = _geom_to_wgs84_geojson(result["geometry"], result["crs"])
                geom = shape(gj["features"][0]["geometry"]).intersection(box(*bounds_wgs84))
                if not geom.is_empty:
                    geometries.append(geom)
            valid_pixels += result["valid_pixel_count"]
            base["n_assets_read"] += 1
        except Exception as exc:
            failures.append({**record, "error": str(exc)})
    if failures:
        raise SourceUnavailable(code="GFM_ASSET_READ_FAILED",
                                message="GFM reference is incomplete because one or more assets could not be read.",
                                recovery="Retry failed assets or supply a verified observed reference.",
                                details={"failures": failures, "n_assets_read": base["n_assets_read"]})
    base["valid_pixel_count"] = valid_pixels
    if not valid_pixels:
        return {**base, "status": "no_valid_pixels", "note": "Acquisitions contain no valid flood classification in the requested window."}
    if not geometries:
        return {**base, "status": "no_flood_detected",
                "note": "No flood detected in valid pixels; whole-area coverage and event-time suitability remain unverified."}
    base["geojson"]["features"] = [{"type": "Feature", "geometry": mapping(unary_union(geometries)),
                                    "properties": {"source": "gfm_stac", "synthetic": False}}]
    return {**base, "status": "flood_detected"}
