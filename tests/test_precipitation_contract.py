"""Offline contract fixtures; these do not validate live satellite accuracy."""
from types import SimpleNamespace

import pandas as pd
import pytest

from aihydro_data.sources._gee_vendored import timeseries
from aihydro_data.sources._gee_vendored.precipitation import CONTRACT, daily_window, integrate_daily


def native(days=1):
    return [{"time_start_ms": int(t.timestamp() * 1000), "value": 2.0, "status": "permanent"}
            for t in pd.date_range("2024-02-28", periods=48 * days, freq="30min", tz="UTC")]


def test_constant_rate_and_leap_day():
    result = integrate_daily(native(3), "2024-02-28", "2024-03-01")
    assert [r["value"] for r in result] == [48.0] * 3
    assert [r["date"] for r in result] == ["2024-02-28", "2024-02-29", "2024-03-01"]
    assert all(r["interval_count"] == 48 for r in result)


def test_varying_rates_and_maturity():
    rows = native()
    for n, row in enumerate(rows):
        row["value"] = n
    rows[0]["status"] = "provisional"
    rows[1]["status"] = None
    result = integrate_daily(rows[::-1], "2024-02-28", "2024-02-28")[0]
    assert result["value"] == sum(range(48)) / 2
    assert result["source_status"] == "permanent,provisional,unknown"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "offgrid", "outside", "null", "nan", "inf", "negative", "bool", "timestamp"])
def test_invalid_observations_fail(mutation):
    rows = native()
    if mutation == "missing":
        rows.pop(20)
    elif mutation == "duplicate":
        rows[-1] = rows[0].copy()
    elif mutation == "offgrid":
        rows[0]["time_start_ms"] += 1000
    elif mutation == "outside":
        rows[0]["time_start_ms"] -= 1800000
    elif mutation == "timestamp":
        rows[0]["time_start_ms"] = None
    else:
        rows[0]["value"] = {"null": None, "nan": float("nan"), "inf": float("inf"), "negative": -1, "bool": True}[mutation]
    with pytest.raises(ValueError, match="IMERG"):
        integrate_daily(rows, "2024-02-28", "2024-02-28")


def test_whole_missing_interior_day_and_empty():
    rows = native(3)
    with pytest.raises(ValueError, match="48 missing"):
        integrate_daily(rows[:48] + rows[96:], "2024-02-28", "2024-03-01")
    with pytest.raises(ValueError, match="48 missing"):
        integrate_daily([], "2024-02-28", "2024-02-28")


@pytest.mark.parametrize("start,end", [("2024-02-28T12:00:00", "2024-03-01"), ("2024-03-02", "2024-03-01"), ("20240228", "2024-03-01")])
def test_invalid_windows(start, end):
    with pytest.raises(ValueError):
        daily_window(start, end)


def fake_ee(monkeypatch, rows):
    calls = {}
    class Image:
        def __init__(self, row):
            self.row = row
        def get(self, key):
            return self.row.get("time_start_ms" if key == "system:time_start" else key)
        def reduceRegion(self, **kwargs):
            return {"precipitation": self.row["value"]}
    class Collection:
        def filterDate(self, start, end):
            calls["window"] = (start, end)
            return self
        def select(self, band):
            return self
        def map(self, function):
            return [function(Image(row)) for row in rows]
    ee = SimpleNamespace(
        Initialize=lambda **kw: None, Geometry=lambda value: value,
        Reducer=SimpleNamespace(mean=lambda: "mean"),
        ImageCollection=lambda dataset: Collection(),
        Date=lambda value: SimpleNamespace(format=lambda fmt: pd.to_datetime(value, unit="ms").strftime("%Y-%m-%d")),
        Feature=lambda geometry, properties: {"properties": properties},
        FeatureCollection=lambda features: SimpleNamespace(getInfo=lambda: {"features": features}),
    )
    monkeypatch.setattr(timeseries, "_import_ee", lambda: (True, ee, None))
    return calls


def test_extractor_and_backend_contract(monkeypatch):
    from shapely.geometry import box

    from aihydro_data.products.precipitation import PRODUCTS
    from aihydro_data.sources.gee import Backend
    spec = next(p for p in PRODUCTS if p.id == "IMERG_PRECIP")
    calls = fake_ee(monkeypatch, native())
    backend = Backend()
    monkeypatch.setattr(backend, "_assert_available", lambda: None)
    frame = backend.fetch_timeseries(spec, box(80, 25, 81, 26), "2024-02-28", "2024-02-28", "basin_mean")
    assert calls["window"] == ("2024-02-28", "2024-02-29")
    assert frame["precipitation"].tolist() == [48.0]
    assert frame["source_status"].tolist() == ["permanent"]
    assert "spatial masks" in frame.attrs["aihydro_notes"][0]
    assert spec.units == "mm/day" and spec.timestep == "daily"


def test_extractor_failure_and_ordinary_path(monkeypatch):
    calls = fake_ee(monkeypatch, native()[:47])
    args = dict(dataset_id="test", band="precipitation", start_date="2024-02-28", end_date="2024-02-29", roi_geojson={})
    result = timeseries.extract_timeseries(**args, temporal_contract=CONTRACT)
    assert result["ok"] is False and result["rows"] == []
    assert "missing half-hours" in result["message"]
    result = timeseries.extract_timeseries(**args)
    assert result["ok"] is True and len(result["rows"]) == 47
    assert calls["window"] == ("2024-02-28", "2024-02-29")


def test_old_native_rate_cache_is_not_reused(tmp_path, monkeypatch):
    from shapely.geometry import box

    from aihydro_data import _pipeline, cache
    from aihydro_data.contracts import FetchRequest, FetchResult
    from aihydro_data.sources.gee import Backend

    geometry = box(80, 25, 81, 26)
    request = FetchRequest(variable="precipitation", geometry=geometry,
                           start="2024-02-28", end="2024-02-28", mode="manual",
                           product="IMERG_PRECIP", aggregation="basin_mean")
    key = cache.cache_key(dict(result_schema=2, variable=request.variable,
                               start=request.start, end=request.end,
                               aggregation=request.aggregation, geom_wkt=geometry.wkt,
                               product="IMERG_PRECIP"))
    monkeypatch.setattr(cache, "cache_dir", lambda: tmp_path)
    old = FetchResult(variable="precipitation", product="IMERG_PRECIP", source="gee",
                      request=request, cache_key=key,
                      data=pd.DataFrame({"date": ["2024-02-28"], "precipitation": [2.0]}))
    cache.cache_write(old, geom_wkt=geometry.wkt)
    calls = fake_ee(monkeypatch, native())
    monkeypatch.setattr(Backend, "is_available", lambda self: (True, None))
    monkeypatch.setattr(Backend, "_assert_available", lambda self: None)
    result = _pipeline.fetch("precipitation", geometry, request.start, request.end,
                             mode="manual", product="IMERG_PRECIP", fallback=[],
                             aggregation="basin_mean", region="global")
    assert calls["window"] == ("2024-02-28", "2024-02-29")
    assert result.cache_key != key
    assert result.data["precipitation"].tolist() == [48.0]
    # New cache roundtrip retains the daily diagnostics and values.
    monkeypatch.setattr(Backend, "fetch_timeseries", lambda *args: pytest.fail("cache miss"))
    restored = _pipeline.fetch("precipitation", geometry, request.start, request.end,
                               mode="manual", product="IMERG_PRECIP", fallback=[],
                               aggregation="basin_mean", region="global")
    assert restored.data["precipitation"].tolist() == [48.0]
    assert restored.data["source_status"].tolist() == ["permanent"]
