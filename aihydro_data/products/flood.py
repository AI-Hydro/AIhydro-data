"""
Observed flood inundation product registry (GFM SAR).
"""
from __future__ import annotations

from aihydro_data.contracts import ProductSpec

_FLOOD_NEXT = [
    {
        "tool": "map_flood_inundation",
        "rationale": "Compare modeled HAND extent to GFM observed flood mask.",
    },
]

PRODUCTS: list[ProductSpec] = [
    ProductSpec(
        id="GFM_S1_INUNDATION",
        variable="flood_inundation",
        source="direct_api",
        source_dataset_id="gfm_sentinel1_stac",
        coverage=["global"],
        temporal_start="2014-01-01",
        temporal_end="present",
        resolution_m=20,
        timestep="event",
        units="binary",
        spatial_support="areal",
        license="Copernicus EMS — free for research; check redistribution terms",
        citation=(
            "Copernicus Emergency Management Service Global Flood Monitoring (GFM). "
            "Sentinel-1 SAR inundation mapping."
        ),
        bibtex=(
            "@misc{gfm2023,\n"
            "  title = {Global Flood Monitoring},\n"
            "  author = {{Copernicus EMS}},\n"
            "  year = {2023},\n"
            "  url = {https://gfm.eodc.eu/}\n"
            "}"
        ),
        homepage="https://gfm.eodc.eu/",
        requires_extras=[],
        requires_auth=[],
        common_pitfalls=[
            "Event-date specific — pass start=end=event date.",
            "Live API integration pending; tools use fixture fallback offline.",
        ],
        examples=[
            "from aihydro_data.flood.gfm import fetch_gfm_extent",
            "fetch_gfm_extent([-72,44,-71,45], '2023-07-15')",
        ],
        next_steps=_FLOOD_NEXT,
        backend_config={
            "service": "gfm_extent",
            "stac_endpoint": "https://stac.eodc.eu/api/v1",
            "stac_collection": "GFM",
            "stac_asset": "ensemble_flood_extent",
        },
    ),
]
