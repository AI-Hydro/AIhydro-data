"""
Routing tests — region detection + product policy.

All tests are offline (no network, no auth). They only exercise:
  - routing/regions.py    (bbox math)
  - routing/detect.py     (detect_region)
  - routing/policy.py     (resolve_product_ids)
  - routing/__init__.py   (resolve_product end-to-end)
  - products/precipitation.py (ProductSpec sanity)
"""
from __future__ import annotations

import pytest
from shapely.geometry import Point, box


# ── Region detection ──────────────────────────────────────────────────────

class TestDetectRegion:
    def test_conus_point(self):
        from aihydro_data.routing import detect_region
        # Kansas City, MO — well inside CONUS
        geom = Point(-94.5, 39.1)
        assert detect_region(geom) == "CONUS"

    def test_conus_bbox(self):
        from aihydro_data.routing import detect_region
        # A small watershed bbox clearly inside CONUS
        geom = box(-90.0, 35.0, -85.0, 40.0)
        assert detect_region(geom) == "CONUS"

    def test_global_point_india(self):
        from aihydro_data.routing import detect_region
        geom = Point(77.0, 20.0)   # India — S_ASIA or ASIA
        result = detect_region(geom)
        assert result in ("S_ASIA", "ASIA"), f"Expected S_ASIA or ASIA, got {result!r}"

    def test_global_point_amazon(self):
        from aihydro_data.routing import detect_region
        geom = Point(-60.0, -5.0)  # Brazil / Amazon
        result = detect_region(geom)
        assert result in ("SOUTH_AMERICA", "global"), f"Got {result!r}"

    def test_global_point_europe(self):
        from aihydro_data.routing import detect_region
        geom = Point(10.0, 51.0)   # Germany
        result = detect_region(geom)
        assert result == "EUROPE", f"Got {result!r}"

    def test_global_fallback_ocean(self):
        from aihydro_data.routing import detect_region
        geom = Point(170.0, 5.0)   # Pacific ocean — no region covers this well
        result = detect_region(geom)
        assert isinstance(result, str) and len(result) > 0

    def test_trans_conus_bbox_falls_back_to_centroid(self):
        from aihydro_data.routing import detect_region
        # A bbox that spans CONUS + Canada is NOT fully within CONUS bbox,
        # but centroid is still in CONUS → should return CONUS or NORTH_AMERICA
        geom = box(-100.0, 45.0, -80.0, 55.0)
        result = detect_region(geom)
        assert result in ("CONUS", "NORTH_AMERICA")


# ── Policy resolution ─────────────────────────────────────────────────────

class TestResolveProductIds:
    def test_precip_conus(self):
        from aihydro_data.routing.policy import resolve_product_ids
        ids = resolve_product_ids("precipitation", "CONUS")
        assert ids[0] == "GRIDMET_PRECIP"
        assert "CHIRPS" in ids

    def test_precip_global(self):
        from aihydro_data.routing.policy import resolve_product_ids
        ids = resolve_product_ids("precipitation", "global")
        assert ids[0] == "CHIRPS"

    def test_precip_s_asia_falls_through_to_asia_then_global(self):
        from aihydro_data.routing.policy import resolve_product_ids
        ids = resolve_product_ids("precipitation", "S_ASIA")
        assert len(ids) > 0
        assert "CHIRPS" in ids or "ERA5L_PRECIP" in ids

    def test_unknown_variable_returns_empty(self):
        from aihydro_data.routing.policy import resolve_product_ids
        ids = resolve_product_ids("unicorn_variable", "global")
        assert ids == []


# ── Product registry ──────────────────────────────────────────────────────

class TestProductRegistry:
    def test_precip_products_registered(self):
        from aihydro_data.products import list_products
        prods = list_products(variable="precipitation")
        ids = {p.id for p in prods}
        # Core GEE + HyRiver products
        assert {"CHIRPS", "IMERG_PRECIP", "ERA5L_PRECIP", "GRIDMET_PRECIP", "DAYMET_PRECIP"}.issubset(ids)
        # Auth-free fallback
        assert "CHIRPS_IRI" in ids

    def test_chirps_spec_shape(self):
        from aihydro_data.products import get_product
        spec = get_product("CHIRPS")
        assert spec.variable == "precipitation"
        assert spec.source == "gee"
        assert "global" in spec.coverage
        assert spec.temporal_start == "1981-01-01"
        assert spec.resolution_m == 5566
        assert spec.units == "mm/day"
        assert spec.requires_extras == ["gee"]
        assert len(spec.citation) > 50
        assert len(spec.bibtex) > 50

    def test_gridmet_spec_conus_only(self):
        from aihydro_data.products import get_product
        spec = get_product("GRIDMET_PRECIP")
        assert "CONUS" in spec.coverage
        assert "global" not in spec.coverage
        assert spec.source == "hyriver"
        assert spec.requires_auth == []

    def test_list_by_source(self):
        from aihydro_data.products import list_products
        gee_prods = list_products(source="gee")
        assert all(p.source == "gee" for p in gee_prods)
        assert any(p.id == "CHIRPS" for p in gee_prods)

    def test_list_by_region_conus(self):
        from aihydro_data.products import list_products
        conus_prods = list_products(variable="precipitation", region="CONUS")
        # GRIDMET and DAYMET cover CONUS/NORTH_AMERICA; CHIRPS/ERA5L cover global (global ⊇ CONUS)
        ids = {p.id for p in conus_prods}
        assert "GRIDMET_PRECIP" in ids
        assert "CHIRPS" in ids       # global covers all

    def test_get_missing_raises_key_error(self):
        from aihydro_data.products import get_product
        with pytest.raises(KeyError):
            get_product("NOT_A_REAL_PRODUCT")

    def test_frozen_spec_immutable(self):
        from aihydro_data.products import get_product
        spec = get_product("CHIRPS")
        with pytest.raises(Exception):
            spec.id = "CHANGED"   # type: ignore[misc]

    def test_all_specs_have_next_steps(self):
        from aihydro_data.products import list_products
        for spec in list_products(variable="precipitation"):
            assert len(spec.next_steps) >= 1, f"{spec.id} missing next_steps"

    def test_no_duplicate_ids(self):
        from aihydro_data.products import list_products
        all_ids = [p.id for p in list_products()]
        assert len(all_ids) == len(set(all_ids)), "Duplicate product IDs detected"

    def test_chirps_iri_spec(self):
        """CHIRPS_IRI is the auth-free OPeNDAP fallback — check its key fields."""
        from aihydro_data.products import get_product
        spec = get_product("CHIRPS_IRI")
        assert spec.variable == "precipitation"
        assert spec.source == "direct_api"          # NOT gee — no auth required
        assert "global" in spec.coverage
        assert spec.requires_auth == []              # key property: zero auth
        assert spec.requires_extras == ["opendap"]
        assert spec.temporal_start == "1981-01-01"
        assert spec.resolution_m == 5566             # same as GEE CHIRPS
        assert spec.units == "mm/day"
        assert spec.backend_config["service"] == "chirps_iri"
        assert "iridl.ldeo.columbia.edu" in spec.backend_config["iri_url"]

    def test_chirps_iri_last_in_global_policy(self):
        """CHIRPS_IRI should be the last fallback in all precipitation chains."""
        from aihydro_data.routing.policy import resolve_product_ids
        for region in ("global", "CONUS", "S_ASIA", "AFRICA", "EUROPE"):
            ids = resolve_product_ids("precipitation", region)
            assert "CHIRPS_IRI" in ids, f"CHIRPS_IRI missing from ({region!r},)"
            assert ids[-1] == "CHIRPS_IRI", (
                f"CHIRPS_IRI should be last in precipitation/{region} chain, "
                f"got {ids}"
            )


# ── End-to-end routing (no live fetch) ───────────────────────────────────

class TestResolveProductEndToEnd:
    def test_auto_conus_resolves_gridmet(self):
        from aihydro_data.contracts import FetchRequest
        from aihydro_data.routing import resolve_product

        req = FetchRequest(
            variable="precipitation",
            geometry=Point(-94.5, 39.1),  # already coerced — shapely Point
            start="2015-01-01",
            end="2015-12-31",
        )
        spec = resolve_product(req)
        # GridMET is first in CONUS policy and is registered
        assert spec.id == "GRIDMET_PRECIP"
        assert spec.source == "hyriver"

    def test_auto_global_resolves_chirps(self):
        from aihydro_data.contracts import FetchRequest
        from aihydro_data.routing import resolve_product

        req = FetchRequest(
            variable="precipitation",
            geometry=Point(77.0, 20.0),   # India
            start="2015-01-01",
            end="2015-12-31",
        )
        spec = resolve_product(req)
        # India → S_ASIA or ASIA → fallback → global → CHIRPS first
        assert spec.variable == "precipitation"
        assert spec.id in {"CHIRPS", "IMERG_PRECIP", "ERA5L_PRECIP"}

    def test_manual_mode_pins_product(self):
        from aihydro_data.contracts import FetchRequest
        from aihydro_data.routing import resolve_product

        req = FetchRequest(
            variable="precipitation",
            geometry=Point(-94.5, 39.1),
            start="2015-01-01",
            end="2015-12-31",
            mode="manual",
            product="CHIRPS",
        )
        spec = resolve_product(req)
        assert spec.id == "CHIRPS"

    def test_unknown_variable_raises(self):
        from aihydro_data.contracts import FetchRequest
        from aihydro_data.exceptions import RegionUnsupported
        from aihydro_data.routing import resolve_product

        req = FetchRequest(
            variable="unicorn_data",
            geometry=Point(-94.5, 39.1),
            start="2015-01-01",
            end="2015-12-31",
        )
        with pytest.raises(RegionUnsupported):
            resolve_product(req)
