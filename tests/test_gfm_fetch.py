"""GFM fetch unit tests (offline + optional live STAC)."""
from __future__ import annotations

import pytest

from aihydro_data.flood.gfm import fetch_gfm_extent, fixture_gfm_geojson


def test_fixture_geojson():
    gj = fixture_gfm_geojson([-72.0, 44.0, -71.0, 45.0], "2023-07-15")
    assert gj["features"]


def test_fetch_offline_fixture():
    out = fetch_gfm_extent([-72.0, 44.0, -71.0, 45.0], "2023-07-15", use_fixture=True)
    assert out["live"] is False
    assert out["geojson"]["features"]


@pytest.mark.live
def test_fetch_live_stac_small_aoi():
    """Small Bangladesh bbox — live STAC (may take ~30s for COG window read)."""
    out = fetch_gfm_extent([88.3, 22.4, 88.6, 22.7], "2022-09-15", allow_network=True)
    assert out.get("live") is True
    assert out["source"].startswith("gfm_stac")
    assert out["geojson"]["type"] == "FeatureCollection"
