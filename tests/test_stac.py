"""
STAC backend tests — offline only.

Live STAC fetches (Planetary Computer) are deliberately NOT included in
test_live_sweep.py because they need a different test harness (cloud-cost
budget, asset signing). Live coverage will land in a separate suite once
the cookbook notebook validates the path end-to-end.
"""
from __future__ import annotations

import pytest
from shapely.geometry import Point, box


# ── Backend structural tests ────────────────────────────────────────────────

class TestStacBackend:
    def test_backend_importable(self):
        from aihydro_data.sources.stac import Backend
        b = Backend()
        assert b.source_id == "stac"

    def test_capabilities_structure(self):
        from aihydro_data.sources.stac import Backend
        caps = Backend().capabilities()
        assert "variables" in caps
        assert "dem" in caps["variables"]
        assert "landcover" in caps["variables"]
        assert caps["requires_auth"] == []
        assert caps["requires_extras"] == ["stac"]

    def test_geometry_to_bbox_polygon(self):
        from aihydro_data.sources.stac import Backend
        bbox = Backend()._geometry_to_bbox(box(-95, 38, -94, 40))
        assert bbox == [-95.0, 38.0, -94.0, 40.0]

    def test_geometry_to_bbox_point_expanded(self):
        from aihydro_data.sources.stac import Backend
        bbox = Backend()._geometry_to_bbox(Point(-94.5, 39.1))
        # Point bbox should be expanded (non-zero width/height)
        assert bbox[2] > bbox[0]
        assert bbox[3] > bbox[1]

    def test_gauge_id_rejected(self):
        from aihydro_data.sources.stac import Backend
        from aihydro_data.geometry import GaugeID
        from aihydro_data.exceptions import GeometryInvalid
        with pytest.raises(GeometryInvalid):
            Backend()._geometry_to_bbox(GaugeID("03245500"))


# ── STAC product registry tests ─────────────────────────────────────────────

class TestStacProducts:
    def test_glo30_stac_registered(self):
        from aihydro_data.products import get_product
        spec = get_product("GLO30_STAC")
        assert spec.source == "stac"
        assert spec.timestep == "static"
        assert spec.resolution_m == 30
        assert spec.requires_auth == []
        assert spec.requires_extras == ["stac"]
        assert spec.backend_config["stac_collection"] == "cop-dem-glo-30"

    def test_esa_worldcover_stac_registered(self):
        from aihydro_data.products import get_product
        spec = get_product("ESA_WORLDCOVER_STAC")
        assert spec.source == "stac"
        assert spec.timestep == "static"
        assert spec.resolution_m == 10
        assert spec.backend_config["stac_collection"] == "esa-worldcover"

    def test_stac_products_have_planetary_computer_endpoint(self):
        from aihydro_data.products import list_products
        stac_prods = list_products(source="stac")
        assert len(stac_prods) >= 2
        for p in stac_prods:
            endpoint = p.backend_config.get("stac_endpoint", "")
            assert "planetarycomputer" in endpoint or "earth-search" in endpoint

    def test_stac_products_in_routing_policy(self):
        from aihydro_data.routing.policy import resolve_product_ids

        # DEM global chain should include GLO30_STAC as last fallback
        dem_global = resolve_product_ids("dem", "global")
        assert "GLO30_STAC" in dem_global

        # Landcover global chain should include ESA_WORLDCOVER_STAC
        lc_global = resolve_product_ids("landcover", "global")
        assert "ESA_WORLDCOVER_STAC" in lc_global

    def test_no_duplicate_product_ids_after_stac(self):
        from aihydro_data.products import list_products
        all_ids = [p.id for p in list_products()]
        assert len(all_ids) == len(set(all_ids))

    def test_grand_total_now_34_products(self):
        # 32 before STAC + 2 STAC additions
        from aihydro_data.products import list_products
        all_prods = list_products()
        assert len(all_prods) >= 34, f"Got {len(all_prods)}"


# ── Availability check (works without stac extras installed) ───────────────

class TestStacAvailability:
    def test_is_available_reports_clearly(self):
        from aihydro_data.sources.stac import Backend
        ok, reason = Backend().is_available()
        # Either ok=True (stac extras installed) or ok=False with clear hint
        if not ok:
            assert reason is not None
            assert "stac" in reason.lower()

    def test_fetch_raises_clean_error_if_missing_extras(self, monkeypatch):
        """If stac packages aren't importable, fetch should raise SourceUnavailable."""
        from aihydro_data.sources.stac import Backend
        from aihydro_data.exceptions import SourceUnavailable

        b = Backend()
        # Force is_available to report False
        monkeypatch.setattr(b, "is_available",
                            lambda: (False, "stac not installed (forced)"))
        with pytest.raises(SourceUnavailable):
            b._assert_available()
