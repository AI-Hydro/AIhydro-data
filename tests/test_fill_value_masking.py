"""D4: provider fill values are missing data, never served as data.

Synthetic only: the IRI OPeNDAP dataset is an in-memory xarray Dataset; no
network, no netCDF C-library traffic.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from shapely.geometry import box

from aihydro_data._physical import (
    bounds_for, declared_fill_values, enforce_physical_range, mask_invalid,
)

N_DAYS = 40
T0 = 18000.0   # arbitrary "Julian day" epoch, as the backend only uses offsets


def _iri_dataset(fill_cells: dict | None = None, declared: str | None = "_FillValue",
                 fill: float = 1e33, base: float = 2.0, all_fill: bool = False):
    X = np.array([-77.50, -77.45, -77.40])
    Y = np.array([39.30, 39.25, 39.20])        # descending, as IRI
    T = T0 + np.arange(N_DAYS)
    arr = np.full((N_DAYS, 3, 3), base, dtype="float32")
    if all_fill:
        arr[:] = fill
    for (iy, ix) in (fill_cells or {}):
        arr[:, iy, ix] = fill
    da = xr.DataArray(arr, dims=("T", "Y", "X"), coords={"T": T, "Y": Y, "X": X},
                      name="prcp")
    if declared:
        da.attrs[declared] = np.float32(fill)
    return xr.Dataset({"prcp": da})


def _fetch_iri(monkeypatch, ds):
    from aihydro_data.products import get_product  # noqa: F401
    from aihydro_data.sources.direct_api import Backend
    monkeypatch.setattr(xr, "open_dataset", lambda *a, **k: ds)
    spec = SimpleNamespace(id="CHIRPS_IRI", backend_config={
        "iri_url": "synthetic://", "variable": "prcp",
        "lon_dim": "X", "lat_dim": "Y", "time_dim": "T"})
    geom = box(-77.5, 39.2, -77.4, 39.3)
    return Backend()._fetch_chirps_iri(spec, spec.backend_config, geom,
                                       "1981-01-01", f"1981-02-{N_DAYS - 31:02d}")


@pytest.mark.parametrize("declared", ["_FillValue", "missing_value", None])
def test_iri_masks_fill_cells_before_the_spatial_mean(monkeypatch, declared):
    # A third of the cells are fill; the mean must be that of the REAL cells.
    ds = _iri_dataset(fill_cells={(0, 0): 1, (1, 1): 1, (2, 2): 1}, declared=declared)
    df = _fetch_iri(monkeypatch, ds)
    assert len(df) and df["precipitation"].max() < 10
    assert df["precipitation"].mean() == pytest.approx(2.0, rel=1e-3)


def test_iri_fails_when_everything_is_fill_never_returns_it(monkeypatch):
    from aihydro_data.exceptions import SourceUnavailable
    with pytest.raises(SourceUnavailable) as e:
        _fetch_iri(monkeypatch, _iri_dataset(all_fill=True, declared="_FillValue"))
    assert "ALL_MASKED" in str(getattr(e.value, "code", "")) or "fill" in str(e.value).lower()
    with pytest.raises(SourceUnavailable):
        _fetch_iri(monkeypatch, _iri_dataset(all_fill=True, declared=None, fill=9.96921e36))


def test_mask_invalid_helper():
    out, n = mask_invalid([1.0, np.inf, np.nan, -5.0, 1e33, 3000.0, 9.96921e36],
                          fill_values=[9.96921e36], bounds=(0.0, 2000.0))
    assert n == 5                                  # nan is not "newly" masked
    assert np.isfinite(out).sum() == 1 and out[0] == 1.0


def test_declared_fill_values_reads_attrs_and_encoding():
    assert declared_fill_values({"_FillValue": -999.0}, {"missing_value": 1e33}) == [-999.0, 1e33]
    assert declared_fill_values(None, {}) == []


def test_bounds_table():
    assert bounds_for("precipitation", "mm/day") == (0.0, 2000.0)
    assert bounds_for("landcover", "") is None


def _spec(variable="precipitation", units="mm/day"):
    return SimpleNamespace(variable=variable, units=units, id="X")


def test_pipeline_backstop_masks_dataframe_and_notes():
    df = pd.DataFrame({"date": pd.date_range("2000-01-01", periods=4),
                       "precipitation": [1.0, 4.3e32, 2.0, -3.0]})
    out, n = enforce_physical_range(_spec(), df)
    assert n == 2 and out["precipitation"].isna().sum() == 2
    assert df["precipitation"].max() == 4.3e32     # input not mutated


def test_pipeline_backstop_temperature_in_kelvin_and_unbounded_products():
    t = pd.DataFrame({"tmax": [280.0, 1e33]})
    out, n = enforce_physical_range(_spec("tmax", "K"), t)
    assert n == 1 and out["tmax"].iloc[0] == 280.0
    lc = pd.DataFrame({"class": [11, 95, 255]})
    assert enforce_physical_range(_spec("landcover", ""), lc)[1] == 0   # categorical untouched


def test_pipeline_backstop_xarray_and_declared_fill():
    da = xr.DataArray(np.array([1.0, 1e33, 2.0]), dims="t", attrs={"_FillValue": 1e33})
    out, n = enforce_physical_range(_spec(), da)
    assert n == 1 and np.isnan(out.values[1])


def test_fetch_one_fails_candidate_when_all_values_are_fill(monkeypatch):
    """Whole pipeline: a backend that returns only fill must fail the candidate
    (so the chain continues) rather than be returned as data."""
    from aihydro_data import _pipeline
    from aihydro_data.products import get_product
    spec = get_product("CHIRPS_IRI")
    bad = pd.DataFrame({"date": pd.date_range("2000-01-01", periods=5),
                        "precipitation": np.full(5, 9.96921e36)})

    class FakeBackend:
        def is_available(self, **k): return True, ""
        def fetch_timeseries(self, *a, **k): return bad

    monkeypatch.setattr("aihydro_data.sources.base.get_backend", lambda s: FakeBackend())
    from aihydro_data.contracts import FetchRequest
    req = FetchRequest(variable="precipitation", geometry=(40.0, -77.0),
                       start="2000-01-01", end="2000-01-05")
    with pytest.raises(_pipeline._EmptyResult):
        _pipeline._fetch_one(spec, box(0, 0, 1, 1), "2000-01-01", "2000-01-05",
                             "basin_mean", req)

    ok = bad.copy(); ok.loc[0, "precipitation"] = 3.0
    FakeBackend.fetch_timeseries = lambda self, *a, **k: ok
    res = _pipeline._fetch_one(spec, box(0, 0, 1, 1), "2000-01-01", "2000-01-05",
                               "basin_mean", req)
    assert res.data["precipitation"].notna().sum() == 1
    assert any("masked as missing" in n for n in res.notes)
