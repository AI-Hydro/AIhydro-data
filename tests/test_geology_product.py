"""
Tests for the geology product (Wave B4).

All offline tests cover:
  - Product registration (3 specs in registry)
  - Routing policy (geology → PYGEOGLIM_ALL for any region)
  - Variable alias resolution
  - Backend is_available check
  - fetch() end-to-end with CONUS geometry (live, marked live)

The live test requires pygeoglim + CONUS tile data to be available locally.
"""
from __future__ import annotations

import pytest
from shapely.geometry import box, Point
import geopandas as gpd
import pandas as pd


# ── Product registry ───────────────────────────────────────────────────────

class TestGeologyProductRegistry:
    def test_three_products_registered(self):
        from aihydro_data.products import list_products
        geo = [p for p in list_products() if p.variable == "geology"]
        ids = {p.id for p in geo}
        assert ids == {"PYGEOGLIM_ALL", "GLIM_TILES", "GLHYMPS_TILES"}

    def test_pygeoglim_all_has_global_coverage(self):
        from aihydro_data.products import get_product
        spec = get_product("PYGEOGLIM_ALL")
        assert "global" in spec.coverage
        assert spec.source == "pygeoglim"
        assert spec.timestep == "static"

    def test_glim_tiles_fetch_flags(self):
        from aihydro_data.products import get_product
        spec = get_product("GLIM_TILES")
        assert spec.backend_config["fetch_glim"] is True
        assert spec.backend_config["fetch_glhymps"] is False

    def test_glhymps_tiles_fetch_flags(self):
        from aihydro_data.products import get_product
        spec = get_product("GLHYMPS_TILES")
        assert spec.backend_config["fetch_glim"] is False
        assert spec.backend_config["fetch_glhymps"] is True

    def test_all_products_have_citation(self):
        from aihydro_data.products import list_products
        for p in list_products(variable="geology"):
            assert p.citation, f"{p.id} missing citation"
            assert p.bibtex, f"{p.id} missing bibtex"


# ── Routing policy ─────────────────────────────────────────────────────────

class TestGeologyRouting:
    @pytest.mark.parametrize("region", [
        "CONUS", "EUROPE", "ASIA", "S_ASIA", "AFRICA",
        "SOUTH_AMERICA", "OCEANIA", "global",
    ])
    def test_all_regions_resolve_to_pygeoglim_all(self, region):
        from aihydro_data.routing.policy import resolve_product_ids
        ids = resolve_product_ids("geology", region)
        assert ids == ["PYGEOGLIM_ALL"], (
            f"Expected ['PYGEOGLIM_ALL'] for region={region!r}, got {ids!r}"
        )

    @pytest.mark.parametrize("alias", [
        "lithology", "hydrogeology", "permeability", "porosity", "glim", "glhymps",
    ])
    def test_variable_aliases(self, alias):
        from aihydro_data._pipeline import _normalise_variable
        assert _normalise_variable(alias) == "geology", (
            f"Alias {alias!r} did not resolve to 'geology'"
        )


# ── Backend availability ───────────────────────────────────────────────────

class TestPygeoglimBackend:
    def test_backend_is_available_when_pygeoglim_installed(self):
        pytest.importorskip("pygeoglim")
        from aihydro_data.sources.pygeoglim import Backend
        b = Backend()
        ok, reason = b.is_available()
        assert ok is True
        assert reason is None

    def test_backend_source_id(self):
        from aihydro_data.sources.pygeoglim import Backend
        assert Backend.source_id == "pygeoglim"


# ── End-to-end fetch (live) ────────────────────────────────────────────────

@pytest.mark.live
class TestGeologyFetchLive:
    """Live tests — require network + pygeoglim CONUS tile data."""

    @pytest.fixture
    def potomac_gdf(self):
        """Small polygon inside the Potomac headwaters — CONUS."""
        geom = box(-79.5, 38.5, -77.5, 39.5)
        return gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")

    def test_fetch_geology_auto_mode(self, potomac_gdf):
        import aihydro_data
        result = aihydro_data.fetch("geology", potomac_gdf, "2020-01-01", "2020-12-31")
        assert result.product == "PYGEOGLIM_ALL"
        assert result.source == "pygeoglim"
        assert isinstance(result.data, pd.DataFrame)
        assert len(result.data) == 1

    def test_fetch_returns_all_nine_attributes(self, potomac_gdf):
        import aihydro_data
        result = aihydro_data.fetch("geology", potomac_gdf, "2020-01-01", "2020-12-31")
        cols = set(result.data.columns)
        expected_glim = {"geol_1st_class", "glim_1st_class_frac", "geol_2nd_class",
                         "glim_2nd_class_frac", "carbonate_rocks_frac"}
        expected_glhymps = {"geol_porosity", "geol_permeability",
                            "geol_permeability_linear", "hydraulic_conductivity"}
        assert expected_glim <= cols, f"Missing GLiM attrs: {expected_glim - cols}"
        assert expected_glhymps <= cols, f"Missing GLHYMPS attrs: {expected_glhymps - cols}"

    def test_fetch_numeric_values_in_range(self, potomac_gdf):
        import aihydro_data
        result = aihydro_data.fetch("geology", potomac_gdf, "2020-01-01", "2020-12-31")
        row = result.data.iloc[0]
        assert 0.0 <= row["carbonate_rocks_frac"] <= 1.0
        assert 0.0 <= row["geol_porosity"] <= 1.0
        assert -20 <= row["geol_permeability"] <= 0  # log10(m²) — typical range
        assert 0.0 <= row["glim_1st_class_frac"] <= 1.0

    def test_fetch_lithology_alias(self, potomac_gdf):
        """Variable alias 'lithology' → 'geology' → same result."""
        import aihydro_data
        r1 = aihydro_data.fetch("geology", potomac_gdf, "2020-01-01", "2020-12-31")
        r2 = aihydro_data.fetch("lithology", potomac_gdf, "2020-01-01", "2020-12-31")
        assert r1.product == r2.product
        assert set(r1.data.columns) == set(r2.data.columns)

    def test_manual_pin_glim_tiles(self, potomac_gdf):
        """Manual pin to GLIM_TILES returns only lithology attributes."""
        import aihydro_data
        result = aihydro_data.fetch(
            "geology", potomac_gdf, "2020-01-01", "2020-12-31",
            mode="manual", product="GLIM_TILES",
        )
        assert result.product == "GLIM_TILES"
        cols = set(result.data.columns)
        assert "carbonate_rocks_frac" in cols
        assert "geol_porosity" not in cols  # GLHYMPS not fetched

    def test_manual_pin_glhymps_tiles(self, potomac_gdf):
        """Manual pin to GLHYMPS_TILES returns only hydrogeology attributes."""
        import aihydro_data
        result = aihydro_data.fetch(
            "geology", potomac_gdf, "2020-01-01", "2020-12-31",
            mode="manual", product="GLHYMPS_TILES",
        )
        assert result.product == "GLHYMPS_TILES"
        cols = set(result.data.columns)
        assert "geol_porosity" in cols
        assert "carbonate_rocks_frac" not in cols  # GLiM not fetched

    def test_fetch_result_has_citation(self, potomac_gdf):
        import aihydro_data
        result = aihydro_data.fetch("geology", potomac_gdf, "2020-01-01", "2020-12-31")
        assert "Hartmann" in result.citation
        assert "Gleeson" in result.citation

    def test_to_dict_convenience(self, potomac_gdf):
        """result.data.iloc[0].to_dict() → plain dict for downstream use."""
        import aihydro_data
        result = aihydro_data.fetch("geology", potomac_gdf, "2020-01-01", "2020-12-31")
        attrs = result.data.iloc[0].to_dict()
        assert isinstance(attrs, dict)
        assert len(attrs) == 9
