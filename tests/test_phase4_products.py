"""
Phase 4 product registry tests — ET, DEM, soil moisture, vegetation.

All offline: no network, no auth. Tests cover registry completeness,
spec field shape, policy routing for new variables, and backend stubs.
"""
from __future__ import annotations

import pytest


def _get(product_id: str):
    from aihydro_data.products import get_product
    return get_product(product_id)

def _policy(variable: str, region: str):
    from aihydro_data.routing.policy import resolve_product_ids
    return resolve_product_ids(variable, region)


# ── Evapotranspiration ────────────────────────────────────────────────────

class TestEtProducts:
    def test_et_products_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="et")}
        # SSEBOP was replaced with TERRACLIMATE_AET in v0.2.0 (the old
        # USGS/fews_net GEE asset was removed upstream).
        assert {"MOD16_ET", "TERRACLIMATE_AET"}.issubset(ids)

    def test_pet_products_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="pet")}
        assert {"MOD16_PET", "ERA5L_PET", "GRIDMET_PET"}.issubset(ids)

    def test_mod16_et_spec(self):
        spec = _get("MOD16_ET")
        assert spec.source == "gee"
        assert "global" in spec.coverage
        assert spec.resolution_m == 500
        assert spec.backend_config["band"] == "ET"

    def test_gridmet_pet_conus(self):
        spec = _get("GRIDMET_PET")
        assert "CONUS" in spec.coverage
        assert spec.source == "hyriver"
        assert spec.backend_config["pygridmet_variable"] == "pet"

    def test_era5l_pet_unit_conversion(self):
        spec = _get("ERA5L_PET")
        # ERA5-Land reports evaporation as a downward flux in metres.
        # PET (upward from surface) is the negative — so the conversion
        # is -1000.0 (sign flip + m → mm).  Verified live: this returns
        # positive ~5–11 mm/day for a CONUS summer point.
        assert spec.backend_config["unit_conversion"] == -1000.0

    def test_policy_et_global(self):
        ids = _policy("et", "global")
        assert ids[0] == "MOD16_ET"

    def test_policy_pet_conus(self):
        ids = _policy("pet", "CONUS")
        assert ids[0] == "GRIDMET_PET"

    def test_all_et_have_next_steps(self):
        from aihydro_data.products import list_products
        for spec in list_products(variable="et") + list_products(variable="pet"):
            assert spec.next_steps


# ── DEM ───────────────────────────────────────────────────────────────────

class TestDemProducts:
    def test_dem_products_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="dem")}
        assert {"GLO30", "SRTM", "DEM3DEP_10M", "MERIT_DEM"}.issubset(ids)

    def test_glo30_spec(self):
        spec = _get("GLO30")
        assert "global" in spec.coverage
        assert spec.source == "gee"
        assert spec.resolution_m == 30
        assert spec.timestep == "static"

    def test_dem3dep_conus(self):
        spec = _get("DEM3DEP_10M")
        assert "CONUS" in spec.coverage
        assert spec.source == "hyriver"
        assert spec.resolution_m == 10
        assert spec.backend_config["py3dep_resolution"] == 10

    def test_merit_dem_non_commercial(self):
        spec = _get("MERIT_DEM")
        assert "non-commercial" in spec.license.lower() or "nc" in spec.license.lower()
        assert spec.resolution_m == 90

    def test_policy_dem_conus(self):
        ids = _policy("dem", "CONUS")
        assert ids[0] == "DEM3DEP_10M"
        assert "GLO30" in ids

    def test_policy_dem_global(self):
        ids = _policy("dem", "global")
        assert ids[0] == "GLO30"
        assert "DEM3DEP_10M" not in ids

    def test_all_dem_have_delineation_next_step(self):
        from aihydro_data.products import list_products
        for spec in list_products(variable="dem"):
            tools = [s["tool"] for s in spec.next_steps]
            assert any("twi" in t or "geomorphic" in t or "delineate" in t for t in tools), \
                f"{spec.id}: no terrain-analysis next_step"


# ── Soil Moisture ─────────────────────────────────────────────────────────

class TestSoilMoistureProducts:
    def test_sm_products_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="soil_moisture")}
        # ESA_CCI_SM was removed in v0.2.0 (GEE Community asset deleted upstream).
        # SMAP is the sole global soil-moisture product until we add a CDS-backed
        # replacement.
        assert "SMAP_SM" in ids

    def test_smap_spec(self):
        spec = _get("SMAP_SM")
        assert "global" in spec.coverage
        assert spec.temporal_start == "2015-03-31"
        assert spec.resolution_m == 9000
        assert spec.units == "cm3/cm3"

    def test_policy_soil_moisture_global(self):
        ids = _policy("soil_moisture", "global")
        assert ids[0] == "SMAP_SM"

    def test_policy_soil_moisture_conus(self):
        ids = _policy("soil_moisture", "CONUS")
        assert len(ids) > 0


# ── Vegetation ────────────────────────────────────────────────────────────

class TestVegetationProducts:
    def test_veg_products_registered(self):
        from aihydro_data.products import list_products
        ndvi_ids = {p.id for p in list_products(variable="ndvi")}
        lai_ids  = {p.id for p in list_products(variable="lai")}
        assert {"MODIS_NDVI", "SENTINEL2_NDVI"}.issubset(ndvi_ids)
        assert "MODIS_LAI" in lai_ids

    def test_modis_ndvi_spec(self):
        spec = _get("MODIS_NDVI")
        assert spec.resolution_m == 250
        assert spec.backend_config["unit_conversion"] == 0.0001

    def test_sentinel2_ndvi_compute_flag(self):
        spec = _get("SENTINEL2_NDVI")
        assert spec.backend_config["compute_ndvi"] is True
        assert spec.resolution_m == 10

    def test_modis_lai_unit(self):
        spec = _get("MODIS_LAI")
        assert spec.backend_config["unit_conversion"] == 0.1
        assert spec.units == "m2/m2"

    def test_policy_ndvi_global(self):
        ids = _policy("ndvi", "global")
        assert ids[0] == "MODIS_NDVI"

    def test_policy_lai_global(self):
        ids = _policy("lai", "global")
        assert "MODIS_LAI" in ids


# ── Grand total registry check ────────────────────────────────────────────

class TestGrandTotal:
    def test_total_product_count_phase4(self):
        from aihydro_data.products import list_products
        all_prods = list_products()
        # Tier 2 live sweep removed broken products (MSWEP, SSEBOP_ET,
        # ESA_CCI_SM — assets removed from GEE) and added replacements
        # (IMERG_PRECIP, TERRACLIMATE_AET), netting -1.
        # Tier 3 added CHIRPS_IRI (auth-free fallback), netting +1 → ≥31.
        assert len(all_prods) >= 31, f"Expected ≥31 products, got {len(all_prods)}"

    def test_no_duplicates_phase4(self):
        from aihydro_data.products import list_products
        all_ids = [p.id for p in list_products()]
        assert len(all_ids) == len(set(all_ids))

    def test_all_gee_products_require_gee_extra(self):
        from aihydro_data.products import list_products
        for spec in list_products():
            if spec.source == "gee":
                assert "gee" in spec.requires_extras, \
                    f"{spec.id}: GEE source but 'gee' not in requires_extras"

    def test_all_hyriver_products_require_hyriver_extra(self):
        from aihydro_data.products import list_products
        for spec in list_products():
            if spec.source == "hyriver":
                assert "hyriver" in spec.requires_extras, \
                    f"{spec.id}: hyriver source but 'hyriver' not in requires_extras"

    def test_routing_resolves_for_all_phase4_variables(self):
        from aihydro_data.routing.policy import resolve_product_ids
        combos = [
            ("et",           "global"),
            ("et",           "CONUS"),
            ("pet",          "CONUS"),
            ("pet",          "global"),
            ("dem",          "CONUS"),
            ("dem",          "global"),
            ("soil_moisture","global"),
            ("ndvi",         "global"),
            ("lai",          "global"),
        ]
        for var, region in combos:
            ids = resolve_product_ids(var, region)
            assert len(ids) > 0, f"No policy for ({var!r}, {region!r})"
