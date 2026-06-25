"""
GFM STAC live fetch tests (network optional).
"""
from __future__ import annotations

import pytest

from aihydro_data.flood.gfm_stac import search_gfm_items


@pytest.mark.live
def test_gfm_stac_search_returns_items():
    """Bangladesh AOI from EODC tutorials — should have GFM tiles."""
    items = search_gfm_items([63.0, 24.0, 73.0, 27.0], "2022-09-15", max_items=3)
    assert len(items) >= 1
    assert "ensemble_flood_extent" in (items[0].get("assets") or {})
