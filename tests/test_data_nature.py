"""data_nature (P1): every registered product declares what kind of knowledge
its values are, and the label travels into FetchResult, the MCP result dict
and the cache manifest. Offline."""
from __future__ import annotations

import typing

import pandas as pd
import pytest

from aihydro_data.contracts import (
    DataNature, FetchRequest, FetchResult, ProductSpec, is_observed_nature,
)

VALID = set(typing.get_args(DataNature))


def _all_products():
    from aihydro_data.products import list_products
    return list_products()


class TestRegistryDeclaresDataNature:
    def test_every_product_declares_a_valid_nature(self):
        missing = [p.id for p in _all_products() if not p.data_nature]
        assert not missing, f"products without data_nature: {missing}"
        bad = [p.id for p in _all_products() if p.data_nature not in VALID]
        assert not bad, bad

    def test_registry_is_not_empty(self):
        assert len(_all_products()) >= 57

    @pytest.mark.parametrize("pid,nature", [
        ("NWIS_STREAMFLOW", "gauge_observed"),
        ("GEOGLOWS_RETRO", "modelled"),
        ("OPENMETEO_FLOOD", "modelled"),
        ("GLOFAS_STREAMFLOW", "modelled"),
        ("ERA5L_PRECIP", "reanalysis"),
        ("ERA5L_TMAX", "reanalysis"),
        ("OPEN_METEO_TMAX", "reanalysis"),
        ("DAYMET_PRECIP", "gridded_obs_derived"),
        ("GRIDMET_PRECIP", "gridded_obs_derived"),
        ("MODIS_NDVI", "satellite_derived"),
        ("SENTINEL2_SR_STAC", "satellite_derived"),
        ("PYGEOGLIM_ALL", "mapped"),
    ])
    def test_pinned_classifications(self, pid, nature):
        from aihydro_data.products import get_product
        assert get_product(pid).data_nature == nature

    def test_only_nwis_is_observed_in_core_terms(self):
        obs = [p.id for p in _all_products() if is_observed_nature(p.data_nature)]
        assert obs == ["NWIS_STREAMFLOW"]

    def test_undeclared_nature_is_not_observed(self):
        assert is_observed_nature("") is False
        assert ProductSpec(id="X", variable="v", source="gee").data_nature is None

    def test_invalid_nature_rejected(self):
        with pytest.raises(Exception):
            ProductSpec(id="X", variable="v", source="gee", data_nature="observed")

    def test_independent_of_spatial_support(self):
        from aihydro_data.products import get_product
        # same support, different nature
        assert get_product("GEOGLOWS_RETRO").spatial_support == "reach"
        assert get_product("GEOGLOWS_RETRO").data_nature == "modelled"
        assert get_product("NWIS_STREAMFLOW").spatial_support == "gauge_point"


class _Stub:
    def is_available(self, spec=None):
        return True, None

    def fetch_timeseries(self, spec, geometry, start, end, aggregation):
        return pd.DataFrame({"date": pd.to_datetime(["2020-01-01", "2020-01-02"]),
                             spec.variable: [1.0, 2.0]})


class TestCarriedIntoResult:
    def test_fetch_one_copies_nature_from_spec(self, monkeypatch):
        import aihydro_data.sources.base as base
        from aihydro_data._pipeline import _fetch_one
        from shapely.geometry import Point
        monkeypatch.setattr(base, "get_backend", lambda src: _Stub())
        spec = ProductSpec(id="PT", variable="tmax", source="direct_api",
                           timestep="daily", spatial_support="point",
                           data_nature="reanalysis")
        req = FetchRequest(variable="tmax", geometry=(39.1, -94.5),
                           start="2020-01-01", end="2020-01-02")
        res = _fetch_one(spec, Point(-94.5, 39.1), "2020-01-01", "2020-01-02",
                         "basin_mean", req)
        assert res.data_nature == "reanalysis"
        assert res.spatial_support == "point"

    def test_result_default_is_empty(self):
        req = FetchRequest(variable="tmax", geometry=(39.1, -94.5),
                           start="2020-01-01", end="2020-01-02")
        res = FetchResult(variable="tmax", product="X", source="gee",
                          request=req, data=None)
        assert res.data_nature == ""

    def test_mcp_result_dict_carries_nature(self):
        from aihydro_data.mcp import _result_to_dict
        req = FetchRequest(variable="streamflow", geometry=(39.1, -94.5),
                           start="2020-01-01", end="2020-01-02")
        res = FetchResult(variable="streamflow", product="GEOGLOWS_RETRO",
                          source="geoglows_retro", request=req, data=None,
                          spatial_support="reach", data_nature="modelled")
        d = _result_to_dict(res)
        assert d["data_nature"] == "modelled" and d["spatial_support"] == "reach"

    def test_manifest_roundtrip_and_legacy_entry(self):
        from aihydro_data.cache.manifest import ManifestEntry
        e = ManifestEntry(cache_key="k", variable="streamflow", product="NWIS_STREAMFLOW",
                          source="direct_api", start="a", end="b", geom_wkt="",
                          aggregation="basin_mean", fetched_at="t",
                          data_nature="gauge_observed")
        d = e.to_dict()
        assert d["data_nature"] == "gauge_observed"
        assert ManifestEntry.from_dict(d).data_nature == "gauge_observed"
        d.pop("data_nature")  # entry written before this field existed
        assert ManifestEntry.from_dict(d).data_nature == ""


class TestDocs:
    def test_readme_tables_show_nature_for_every_product(self):
        from pathlib import Path
        readme = (Path(__file__).parent.parent / "README.md").read_text()
        for p in _all_products():
            row = next(l for l in readme.splitlines() if l.startswith(f"| `{p.id}` |"))
            assert f"nature: {p.data_nature}" in row, p.id
