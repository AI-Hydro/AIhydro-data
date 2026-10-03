"""Provider-declared units are recorded; the product-spec unit is kept as ``units_spec``.

Synthetic payloads only. Before this change the pipeline labelled every series with
the product spec's unit and never read the payload's (GEOGLOWS: the zarr declares
ft3/s, the result said m3/s). Recording is neutral: no conversion, warning or refusal.
"""
from __future__ import annotations

import pandas as pd
import pytest

import aihydro_data._pipeline as pipeline
from aihydro_data.contracts import FetchRequest
from aihydro_data.products import get_product
from aihydro_data.sources._common import declare_units, payload_units


def _df(declared=None):
    df = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=5), "streamflow": [1.0, 2, 3, 4, 5]})
    if declared is not None:
        declare_units(df, declared)
    return df


class _Backend:
    def __init__(self, df):
        self.df = df

    def is_available(self, *a, **k):
        return True, None

    def fetch_timeseries(self, *a, **k):
        return self.df


def _serve(monkeypatch, product, df, cache=False):
    from shapely.geometry import Point

    monkeypatch.setattr("aihydro_data.sources.base.get_backend", lambda source: _Backend(df))
    spec = get_product(product)
    req = FetchRequest(variable="streamflow", geometry=(41.0, -71.0), start="2020-01-01", end="2020-01-05")
    return pipeline._fetch_one(spec, Point(-71.0, 41.0), "2020-01-01", "2020-01-05", "basin_mean", req)


def test_declared_unit_is_recorded_and_spec_unit_kept(monkeypatch):
    r = _serve(monkeypatch, "GEOGLOWS_RETRO", _df("ft3/s"))
    assert (r.units, r.units_spec, r.units_declared) == ("ft3/s", "m3/s", "ft3/s")
    assert r.product_identity["units"] == "ft3/s" and r.product_identity["units_spec"] == "m3/s"


def test_no_declared_unit_leaves_behaviour_unchanged(monkeypatch):
    r = _serve(monkeypatch, "GEOGLOWS_RETRO", _df())
    assert (r.units, r.units_spec, r.units_declared) == ("m3/s", "m3/s", "")


def test_matching_declared_unit_records_both_equal(monkeypatch):
    r = _serve(monkeypatch, "NWIS_STREAMFLOW", _df("m3/s"))
    assert (r.units, r.units_spec, r.units_declared) == ("m3/s", "m3/s", "m3/s")


def test_a_difference_is_recorded_without_notes_or_refusal(monkeypatch):
    r = _serve(monkeypatch, "GEOGLOWS_RETRO", _df("ft3/s"))
    assert not any("unit" in n.lower() for n in r.notes)


def test_cache_roundtrip_keeps_declared_and_spec_units(monkeypatch, tmp_path):
    import aihydro_data.cache as cache

    monkeypatch.setattr(cache, "cache_dir", lambda: tmp_path)
    r = _serve(monkeypatch, "GEOGLOWS_RETRO", _df("ft3/s")).model_copy(update={"cache_key": "abc"})
    cache.cache_write(r)
    back = cache.cache_read("abc")
    assert (back.units, back.units_spec, back.units_declared) == ("ft3/s", "m3/s", "ft3/s")


def test_result_dict_exposes_both_units(monkeypatch):
    from aihydro_data.mcp import _result_to_dict

    d = _result_to_dict(_serve(monkeypatch, "GEOGLOWS_RETRO", _df("ft3/s")))
    assert (d["units"], d["units_spec"], d["units_declared"]) == ("ft3/s", "m3/s", "ft3/s")


def test_streamflow_cache_key_changed():
    assert pipeline._result_schema_for("streamflow") != pipeline.RESULT_SCHEMA_VERSION


def test_helpers():
    assert payload_units({"units": " m3 s-1 "}) == "m3 s-1" and payload_units({}) == ""
    assert payload_units({"unit": "cfs"}) == "cfs"
    df = pd.DataFrame({"a": [1]})
    assert "aihydro_units" not in declare_units(df, "").attrs
    assert "aihydro_units" not in declare_units(df, None).attrs
    assert declare_units(df, "ft3/s").attrs["aihydro_units"] == "ft3/s"


def test_openmeteo_adapter_reads_daily_units(monkeypatch):
    from shapely.geometry import Point

    from aihydro_data.sources.openmeteo_flood import Backend as OpenMeteoFloodBackend

    class R:
        status_code = 200

        def json(self):
            return {"daily": {"time": ["2020-01-01", "2020-01-02"], "river_discharge": [1.0, 2.0]},
                    "daily_units": {"river_discharge": "ft3/s"}}

    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    df = OpenMeteoFloodBackend().fetch_timeseries(
        get_product("OPENMETEO_FLOOD"), Point(-71.0, 41.0), "2020-01-01", "2020-01-02", "basin_mean")
    assert df.attrs["aihydro_units"] == "ft3/s"


def test_geoglows_adapter_reads_the_zarr_variable_unit(monkeypatch):
    geoglows = pytest.importorskip("geoglows")
    import xarray as xr
    from shapely.geometry import Point

    from aihydro_data.sources.geoglows_retro import Backend as GeoglowsRetroBackend

    times = pd.date_range("2020-01-01", periods=3, tz=None)

    def retro_daily(river_id, format="df", **kw):
        if format == "xarray":
            q = xr.DataArray([[1.0], [2.0], [3.0]], dims=("time", "river_id"),
                             coords={"time": times, "river_id": [river_id]}, attrs={"units": "ft3/s"})
            return xr.Dataset({"Q": q})
        idx = pd.DatetimeIndex(times).tz_localize("UTC")
        return pd.DataFrame({river_id: [1.0, 2.0, 3.0]}, index=idx)

    monkeypatch.setattr(geoglows.data, "retro_daily", retro_daily)
    be = GeoglowsRetroBackend()
    monkeypatch.setattr(be, "_assert_available", lambda: None)
    monkeypatch.setattr(be, "_snap", lambda *a, **k: {"river_id": 7, "strategy": "t", "uparea_km2": None})
    df = be.fetch_timeseries(get_product("GEOGLOWS_RETRO"), Point(-71.0, 41.0), "2020-01-01", "2020-01-03",
                             "basin_mean", outlet=(41.0, -71.0))
    assert df.attrs["aihydro_units"] == "ft3/s" and len(df) == 3
