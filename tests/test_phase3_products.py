"""
Phase 3 product registry tests — temperature, streamflow, landcover, soil.

All offline: no network, no auth. Tests cover:
  - Registry completeness (correct IDs, variables, sources)
  - ProductSpec field shape (citation, bibtex, next_steps)
  - Policy routing (correct primary product per region)
  - Backend availability stubs (is_available() returns bool, not exception)
  - direct_api gauge-ID helper
"""
from __future__ import annotations

import pytest
from shapely.geometry import Point


# ── Helpers ───────────────────────────────────────────────────────────────

def _get(product_id: str):
    from aihydro_data.products import get_product
    return get_product(product_id)

def _policy(variable: str, region: str):
    from aihydro_data.routing.policy import resolve_product_ids
    return resolve_product_ids(variable, region)


# ── Temperature ───────────────────────────────────────────────────────────

class TestTemperatureProducts:
    def test_all_temp_products_registered(self):
        from aihydro_data.products import list_products
        tmax_ids = {p.id for p in list_products(variable="tmax")}
        tmin_ids = {p.id for p in list_products(variable="tmin")}
        assert tmax_ids == {"GRIDMET_TMAX", "DAYMET_TMAX", "ERA5L_TMAX"}
        assert tmin_ids == {"GRIDMET_TMIN", "DAYMET_TMIN", "ERA5L_TMIN"}

    def test_gridmet_tmax_spec(self):
        spec = _get("GRIDMET_TMAX")
        assert spec.variable == "tmax"
        assert spec.source == "hyriver"
        assert "CONUS" in spec.coverage
        assert spec.units == "K"
        assert spec.backend_config["pygridmet_variable"] == "tmmx"
        assert len(spec.citation) > 50

    def test_era5l_tmax_global(self):
        spec = _get("ERA5L_TMAX")
        assert "global" in spec.coverage
        assert spec.source == "gee"
        assert spec.requires_auth == ["gee"]

    def test_daymet_tmin_north_america(self):
        spec = _get("DAYMET_TMIN")
        assert "NORTH_AMERICA" in spec.coverage
        assert spec.backend_config["pydaymet_variable"] == "tmin"

    def test_tmean_registered(self):
        spec = _get("ERA5L_TMEAN")
        assert spec.variable == "tmean"
        assert "global" in spec.coverage

    def test_policy_tmax_conus(self):
        ids = _policy("tmax", "CONUS")
        assert ids[0] == "GRIDMET_TMAX"
        assert "ERA5L_TMAX" in ids

    def test_policy_tmax_global(self):
        ids = _policy("tmax", "SOUTH_AMERICA")
        assert len(ids) > 0
        assert "ERA5L_TMAX" in ids

    def test_policy_tmin_conus(self):
        ids = _policy("tmin", "CONUS")
        assert ids[0] == "GRIDMET_TMIN"

    def test_all_temp_have_next_steps(self):
        from aihydro_data.products import list_products
        for spec in list_products(variable="tmax") + list_products(variable="tmin"):
            assert spec.next_steps, f"{spec.id} missing next_steps"


# ── Streamflow ────────────────────────────────────────────────────────────

class TestStreamflowProducts:
    def test_nwis_registered(self):
        spec = _get("NWIS_STREAMFLOW")
        assert spec.variable == "streamflow"
        assert spec.source == "direct_api"
        assert "CONUS" in spec.coverage
        assert spec.units == "m3/s"
        assert spec.backend_config["parameter_code"] == "00060"

    def test_nwis_citation_present(self):
        spec = _get("NWIS_STREAMFLOW")
        assert "Geological Survey" in spec.citation
        assert len(spec.bibtex) > 50

    def test_policy_streamflow_conus(self):
        ids = _policy("streamflow", "CONUS")
        assert "NWIS_STREAMFLOW" in ids
        assert ids[0] == "NWIS_STREAMFLOW"

    def test_nwis_next_steps_include_signatures(self):
        spec = _get("NWIS_STREAMFLOW")
        tools = [s["tool"] for s in spec.next_steps]
        assert "extract_hydrological_signatures" in tools

    def test_direct_api_backend_importable(self):
        from aihydro_data.sources.direct_api import Backend
        b = Backend()
        assert b.source_id == "direct_api"
        ok, reason = b.is_available()
        # Either installed or gives a clean message — never raises
        assert isinstance(ok, bool)
        if not ok:
            assert "dataretrieval" in reason or "hyriver" in reason.lower()

    def test_gauge_id_helper_string(self):
        from aihydro_data.sources.direct_api import _gauge_id_from_geometry
        assert _gauge_id_from_geometry("03245500") == "03245500"
        assert _gauge_id_from_geometry("USGS-03245500") == "03245500"

    def test_gauge_id_helper_geometry_returns_none(self):
        from aihydro_data.sources.direct_api import _gauge_id_from_geometry
        assert _gauge_id_from_geometry(Point(-85.0, 40.0)) is None


# ── Land Cover ────────────────────────────────────────────────────────────

class TestLandCoverProducts:
    def test_all_lc_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="landcover")}
        # v0.1.3 added ESA_WORLDCOVER_STAC as auth-free fallback.
        assert {"NLCD", "ESA_WORLDCOVER", "DYNAMIC_WORLD"}.issubset(ids)

    def test_nlcd_spec(self):
        spec = _get("NLCD")
        assert "CONUS" in spec.coverage
        assert spec.source == "hyriver"
        assert spec.timestep == "static"
        assert spec.backend_config["pygeohydro_product"] == "nlcd"

    def test_esa_worldcover_global(self):
        spec = _get("ESA_WORLDCOVER")
        assert "global" in spec.coverage
        assert spec.source == "gee"
        assert spec.resolution_m == 10

    def test_dynamic_world_near_realtime(self):
        spec = _get("DYNAMIC_WORLD")
        assert "global" in spec.coverage
        assert spec.temporal_end == "present"
        assert spec.backend_config["composite_method"] == "mode"

    def test_policy_lc_conus(self):
        ids = _policy("landcover", "CONUS")
        assert ids[0] == "NLCD"
        assert "ESA_WORLDCOVER" in ids

    def test_policy_lc_global(self):
        ids = _policy("landcover", "global")
        assert ids[0] == "ESA_WORLDCOVER"
        assert "NLCD" not in ids

    def test_all_lc_have_bibtex(self):
        from aihydro_data.products import list_products
        for spec in list_products(variable="landcover"):
            assert len(spec.bibtex) > 30, f"{spec.id} bibtex too short"


# ── Soil ──────────────────────────────────────────────────────────────────

class TestSoilProducts:
    def test_all_soil_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="soil")}
        assert {"POLARIS", "SOILGRIDS"}.issubset(ids)

    def test_polaris_spec(self):
        spec = _get("POLARIS")
        assert "CONUS" in spec.coverage
        assert spec.source == "hyriver"
        assert spec.resolution_m == 30
        assert spec.backend_config["pygeohydro_product"] == "polaris"
        assert "sand" in spec.backend_config["default_layers"]

    def test_soilgrids_global(self):
        spec = _get("SOILGRIDS")
        assert "global" in spec.coverage
        assert spec.source == "gee"
        assert spec.resolution_m == 250

    def test_policy_soil_conus(self):
        ids = _policy("soil", "CONUS")
        assert ids[0] == "POLARIS"
        assert "SOILGRIDS" in ids

    def test_policy_soil_global_no_polaris(self):
        ids = _policy("soil", "global")
        assert "POLARIS" not in ids
        assert "SOILGRIDS" in ids

    def test_soil_next_steps_include_cn(self):
        for pid in ["POLARIS", "SOILGRIDS"]:
            spec = _get(pid)
            tools = [s["tool"] for s in spec.next_steps]
            assert "create_cn_grid" in tools, f"{pid} missing create_cn_grid next_step"


# ── Full registry cross-checks ────────────────────────────────────────────

class TestFullRegistry:
    def test_total_product_count(self):
        from aihydro_data.products import list_products
        all_prods = list_products()
        # Phase 2: 5 precip; Phase 3: 7 temp (tmax×3+tmin×3+tmean) + 1 sf + 3 lc + 2 soil = 13
        assert len(all_prods) >= 18, f"Expected ≥18 products, got {len(all_prods)}"

    def test_no_duplicate_ids_across_all_variables(self):
        from aihydro_data.products import list_products
        all_ids = [p.id for p in list_products()]
        assert len(all_ids) == len(set(all_ids)), "Duplicate product IDs detected"

    def test_every_product_has_citation(self):
        from aihydro_data.products import list_products
        for spec in list_products():
            assert len(spec.citation) >= 20, f"{spec.id}: citation too short or missing"

    def test_every_product_has_bibtex(self):
        from aihydro_data.products import list_products
        for spec in list_products():
            assert "@" in spec.bibtex, f"{spec.id}: bibtex missing @ entry"

    def test_routing_resolves_for_all_phase3_variables(self):
        from aihydro_data.routing.policy import resolve_product_ids
        combos = [
            ("tmax",        "CONUS"),
            ("tmin",        "CONUS"),
            ("tmean",       "global"),
            ("streamflow",  "CONUS"),
            ("landcover",   "CONUS"),
            ("landcover",   "global"),
            ("soil",        "CONUS"),
            ("soil",        "global"),
        ]
        for var, region in combos:
            ids = resolve_product_ids(var, region)
            assert len(ids) > 0, f"No policy for ({var!r}, {region!r})"
