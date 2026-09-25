"""
Global Flood Monitoring (GFM) Sentinel-1 inundation extent fetch.

Live path: EODC STAC (``gfm_stac``) → ensemble_flood_extent COG → GeoJSON.
Fixtures require explicit opt-in and are never observational evidence.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

GFM_CITATION = (
    "Copernicus Emergency Management Service Global Flood Monitoring (GFM), "
    "Sentinel-1 SAR inundation product. https://gfm.eodc.eu/"
)

log = logging.getLogger(__name__)

__all__ = ["GFM_CITATION", "fixture_gfm_geojson", "fetch_gfm_extent"]


def _parse_date(event_date: str) -> str:
    raw = str(event_date).strip()[:10]
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


def fixture_gfm_geojson(bounds: list[float], event_date: str) -> dict[str, Any]:
    """Synthetic GFM-like polygon inside WGS84 bounds."""
    if len(bounds) < 4:
        raise ValueError("bounds must be [west, south, east, north]")
    west, south, east, north = [float(v) for v in bounds[:4]]
    _parse_date(event_date)
    inset = 0.15
    cx = (west + east) / 2.0
    cy = (south + north) / 2.0
    hw = (east - west) * (0.5 - inset)
    hh = (north - south) * (0.5 - inset)
    ring = [
        [cx - hw, cy - hh],
        [cx + hw, cy - hh],
        [cx + hw, cy + hh],
        [cx - hw, cy + hh],
        [cx - hw, cy - hh],
    ]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"source": "gfm_fixture", "event_date": event_date,
                               "synthetic": True, "evidence_kind": "synthetic_fixture"},
            }
        ],
    }


def fetch_gfm_extent(
    bounds: list[float],
    event_date: str,
    *,
    allow_network: bool = True,
    use_fixture: bool = False,
) -> dict[str, Any]:
    """
    Return GFM inundation extent GeoJSON for an event date and bbox.

    Live failures raise; only explicit ``use_fixture=True`` generates fixture geometry.
    """
    _parse_date(event_date)
    from aihydro_data.exceptions import SourceUnavailable

    if use_fixture:
        gj = fixture_gfm_geojson(bounds, event_date)
        return {
            "geojson": gj,
            "source": "gfm_fixture",
            "event_date": event_date,
            "live": False,
            "citation": "",
            "synthetic": True,
            "evidence_kind": "synthetic_fixture",
            "status": "synthetic",
            "validation_ready": False,
        }

    if not allow_network:
        raise SourceUnavailable(code="GFM_NETWORK_DISABLED",
                                message="GFM observations require network access.",
                                recovery="Enable network access or supply an observed reference export.")

    try:
        from aihydro_data.flood.gfm_stac import fetch_gfm_stac_geojson

        out = fetch_gfm_stac_geojson(bounds, event_date)
        out["citation"] = GFM_CITATION
        return out
    except SourceUnavailable:
        raise
    except Exception as exc:
        raise SourceUnavailable(
            code="GFM_FETCH_FAILED", message=f"GFM observations unavailable: {exc}",
            recovery="Retry the source or supply an observed reference export; no synthetic substitute was generated.",
        ) from exc
