"""
Offline tests for the Open-Meteo direct-API backend.

All tests are offline — no network calls are made.  The Open-Meteo HTTP
request is patched with unittest.mock so these run in CI without connectivity.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_payload(variable: str, values: list, dates: list[str]) -> dict:
    return {
        "latitude": 47.5,
        "longitude": 8.7,
        "daily_units": {variable: "°C" if "temperature" in variable else "mm"},
        "daily": {
            "time": dates,
            variable: values,
        },
    }


def _mock_response(payload: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    return resp


def _backend():
    from aihydro_data.sources.direct_api import Backend
    return Backend()


def _spec(product_id: str):
    from aihydro_data.products import get_product
    return get_product(product_id)


def _fake_geom(lat: float = 47.5, lon: float = 8.7):
    """Minimal fake geometry with centroid."""
    from unittest.mock import MagicMock
    pt = MagicMock()
    pt.y = lat
    pt.x = lon
    geom = MagicMock()
    geom.centroid = pt
    return geom


# ── Product registry ──────────────────────────────────────────────────────

class TestOpenMeteoRegistry:
    def test_open_meteo_tmax_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="tmax")}
        assert "OPEN_METEO_TMAX" in ids

    def test_open_meteo_tmin_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="tmin")}
        assert "OPEN_METEO_TMIN" in ids

    def test_open_meteo_pet_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="pet")}
        assert "OPEN_METEO_PET" in ids

    def test_tmax_spec_fields(self):
        spec = _spec("OPEN_METEO_TMAX")
        assert spec.source == "direct_api"
        assert "global" in spec.coverage
        assert spec.units == "degC"
        assert spec.requires_auth == []
        assert spec.requires_extras == []
        assert spec.backend_config["om_variable"] == "temperature_2m_max"
        assert spec.backend_config["result_column"] == "tmax"

    def test_tmin_spec_fields(self):
        spec = _spec("OPEN_METEO_TMIN")
        assert spec.backend_config["om_variable"] == "temperature_2m_min"
        assert spec.backend_config["result_column"] == "tmin"

    def test_pet_spec_fields(self):
        spec = _spec("OPEN_METEO_PET")
        assert spec.backend_config["om_variable"] == "et0_fao_evapotranspiration"
        assert spec.backend_config["result_column"] == "pet"
        assert spec.units == "mm/day"

    def test_open_meteo_products_have_next_steps(self):
        for pid in ("OPEN_METEO_TMAX", "OPEN_METEO_TMIN", "OPEN_METEO_PET"):
            spec = _spec(pid)
            assert spec.next_steps, f"{pid}: missing next_steps"

    def test_open_meteo_not_in_gee_extra_check(self):
        """Open-Meteo products must not require gee extra (they're auth-free)."""
        from aihydro_data.products import list_products
        for spec in list_products():
            if spec.id.startswith("OPEN_METEO_"):
                assert "gee" not in spec.requires_extras, \
                    f"{spec.id}: should not require gee extra"
                assert "gee" not in spec.requires_auth, \
                    f"{spec.id}: should not require gee auth"


# ── Routing policy ────────────────────────────────────────────────────────

class TestOpenMeteoRouting:
    def _policy(self, variable: str, region: str) -> list[str]:
        from aihydro_data.routing.policy import resolve_product_ids
        return resolve_product_ids(variable, region)

    def test_tmax_global_has_open_meteo_fallback(self):
        ids = self._policy("tmax", "global")
        assert "OPEN_METEO_TMAX" in ids
        assert ids[-1] == "OPEN_METEO_TMAX", "OPEN_METEO_TMAX should be last in global chain"

    def test_tmin_global_has_open_meteo_fallback(self):
        ids = self._policy("tmin", "global")
        assert "OPEN_METEO_TMIN" in ids
        assert ids[-1] == "OPEN_METEO_TMIN"

    def test_pet_global_has_open_meteo_fallback(self):
        ids = self._policy("pet", "global")
        assert "OPEN_METEO_PET" in ids
        assert ids[-1] == "OPEN_METEO_PET"

    def test_tmax_conus_has_open_meteo_fallback(self):
        ids = self._policy("tmax", "CONUS")
        assert "OPEN_METEO_TMAX" in ids
        # Primary should still be GRIDMET
        assert ids[0] == "GRIDMET_TMAX"

    def test_tmin_conus_has_open_meteo_fallback(self):
        ids = self._policy("tmin", "CONUS")
        assert "OPEN_METEO_TMIN" in ids
        assert ids[0] == "GRIDMET_TMIN"

    def test_pet_conus_has_open_meteo_fallback(self):
        ids = self._policy("pet", "CONUS")
        assert "OPEN_METEO_PET" in ids
        assert ids[0] == "GRIDMET_PET"

    def test_era5l_still_before_open_meteo_in_global_chains(self):
        """ERA5L (GEE) must come before Open-Meteo in all global chains."""
        for var, era5_id, om_id in [
            ("tmax", "ERA5L_TMAX", "OPEN_METEO_TMAX"),
            ("tmin", "ERA5L_TMIN", "OPEN_METEO_TMIN"),
            ("pet",  "ERA5L_PET",  "OPEN_METEO_PET"),
        ]:
            ids = self._policy(var, "global")
            assert era5_id in ids, f"{era5_id} missing from global {var} chain"
            assert ids.index(era5_id) < ids.index(om_id), \
                f"{era5_id} should come before {om_id} in global {var} chain"


# ── Backend fetch (mocked) ────────────────────────────────────────────────

class TestOpenMeteoBackend:
    """Test _fetch_open_meteo directly with mocked HTTP."""

    DATES = ["2021-06-01", "2021-06-02", "2021-06-03"]
    TMAX_VALS = [22.3, 24.1, 19.8]
    TMIN_VALS = [12.1, 13.5, 10.2]
    PET_VALS  = [4.2, 5.1, 3.8]

    def _call(self, product_id: str, om_variable: str, result_col: str, values: list) -> pd.DataFrame:
        spec   = _spec(product_id)
        geom   = _fake_geom()
        payload = _make_payload(om_variable, values, self.DATES)
        with patch("requests.get", return_value=_mock_response(payload)):
            return _backend().fetch_timeseries(
                spec, geom, "2021-06-01", "2021-06-03", aggregation="basin_mean"
            )

    def test_tmax_returns_dataframe(self):
        df = self._call("OPEN_METEO_TMAX", "temperature_2m_max", "tmax", self.TMAX_VALS)
        assert isinstance(df, pd.DataFrame)
        assert "tmax" in df.columns
        assert len(df) == 3
        assert list(df["tmax"]) == self.TMAX_VALS

    def test_tmin_returns_dataframe(self):
        df = self._call("OPEN_METEO_TMIN", "temperature_2m_min", "tmin", self.TMIN_VALS)
        assert "tmin" in df.columns
        assert len(df) == 3

    def test_pet_returns_dataframe(self):
        df = self._call("OPEN_METEO_PET", "et0_fao_evapotranspiration", "pet", self.PET_VALS)
        assert "pet" in df.columns
        assert len(df) == 3
        assert abs(df["pet"].mean() - sum(self.PET_VALS) / 3) < 0.01

    def test_date_column_is_datetime(self):
        df = self._call("OPEN_METEO_TMAX", "temperature_2m_max", "tmax", self.TMAX_VALS)
        assert pd.api.types.is_datetime64_any_dtype(df["date"])

    def test_none_values_dropped(self):
        """NaN values (from API returning None) should be dropped."""
        vals_with_none = [22.3, None, 19.8]
        df = self._call("OPEN_METEO_TMAX", "temperature_2m_max", "tmax", vals_with_none)
        assert len(df) == 2
        assert df["tmax"].notna().all()

    def test_http_error_raises_source_unavailable(self):
        from aihydro_data.exceptions import SourceUnavailable
        spec = _spec("OPEN_METEO_TMAX")
        geom = _fake_geom()
        err_resp = _mock_response({}, status_code=429)
        err_resp.text = "Too Many Requests"
        with patch("requests.get", return_value=err_resp):
            with pytest.raises(SourceUnavailable, match="HTTP 429"):
                _backend().fetch_timeseries(spec, geom, "2021-06-01", "2021-06-03", "basin_mean")

    def test_connection_error_raises_source_unavailable(self):
        from aihydro_data.exceptions import SourceUnavailable
        spec = _spec("OPEN_METEO_TMAX")
        geom = _fake_geom()
        import requests as req_lib
        with patch("requests.get", side_effect=req_lib.ConnectionError("unreachable")):
            with pytest.raises(SourceUnavailable, match="OPEN_METEO_CONNECT_FAILED"):
                _backend().fetch_timeseries(spec, geom, "2021-06-01", "2021-06-03", "basin_mean")

    def test_date_before_1940_raises(self):
        from aihydro_data.exceptions import DateOutOfRange
        spec = _spec("OPEN_METEO_TMAX")
        geom = _fake_geom()
        with pytest.raises(DateOutOfRange, match="1940"):
            # end date before 1940-01-01
            _backend().fetch_timeseries(spec, geom, "1930-01-01", "1939-12-31", "basin_mean")

    def test_unit_conversion_applied(self):
        """If backend_config has unit_conversion, it must be applied."""
        spec   = _spec("OPEN_METEO_TMAX")
        # Inject a conversion factor
        spec.backend_config["unit_conversion"] = 2.0
        geom   = _fake_geom()
        payload = _make_payload("temperature_2m_max", [10.0, 20.0, 30.0], self.DATES)
        with patch("requests.get", return_value=_mock_response(payload)):
            df = _backend().fetch_timeseries(spec, geom, "2021-06-01", "2021-06-03", "basin_mean")
        assert list(df["tmax"]) == [20.0, 40.0, 60.0]
        # Clean up to avoid cross-test contamination
        del spec.backend_config["unit_conversion"]

    def test_empty_response_returns_empty_df(self):
        spec = _spec("OPEN_METEO_TMAX")
        geom = _fake_geom()
        payload = {"daily": {"time": [], "temperature_2m_max": []}}
        with patch("requests.get", return_value=_mock_response(payload)):
            df = _backend().fetch_timeseries(spec, geom, "2021-06-01", "2021-06-03", "basin_mean")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0
        assert "tmax" in df.columns


# ── Grand total check ─────────────────────────────────────────────────────

class TestGrandTotalWithOpenMeteo:
    def test_total_product_count_includes_open_meteo(self):
        from aihydro_data.products import list_products
        all_prods = list_products()
        # Phase 4 guaranteed ≥31; Open-Meteo adds 3 more → ≥34
        assert len(all_prods) >= 34, f"Expected ≥34 products, got {len(all_prods)}"

    def test_open_meteo_products_not_in_gee_check(self):
        """Grand-total test: all GEE products need gee extra — Open-Meteo must NOT trigger this."""
        from aihydro_data.products import list_products
        for spec in list_products():
            if spec.source == "gee":
                assert "gee" in spec.requires_extras
            if spec.id.startswith("OPEN_METEO_"):
                assert spec.source == "direct_api"
                assert "gee" not in spec.requires_extras
