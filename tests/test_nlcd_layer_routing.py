"""Regression test: NLCD products must route to their OWN layer.

Guards the bug fixed 2026-07-09: `_fetch_pygeohydro_raster` hardcoded
`years={"cover": [year]}`, so NLCD_IMPERVIOUS (and NLCD_CANOPY) silently
returned the land-COVER layer (discrete class codes 11/21/.../95) instead of
their own continuous 0-100 % raster. The band `cover_YYYY` came back where
`impervious_YYYY` was expected, making the impervious product unusable.

Network-free: the pygeohydro import is monkeypatched with a spy that records
the `years=` kwarg and returns a minimal Dataset named after the requested
layer, so we assert the correct layer key is passed through from
`backend_config["nlcd_layer"]`.
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box

from aihydro_data import get_product
from aihydro_data.sources import hyriver as hymod


class _SpyPygeohydro:
    """Stand-in for the pygeohydro module: records calls, returns a fake raster."""

    def __init__(self):
        self.calls = []

    def nlcd_bygeom(self, gdf, years=None, resolution=None):
        self.calls.append({"years": years, "resolution": resolution})
        layer = list(years)[0]
        year = years[layer][0]
        da = xr.DataArray(
            np.zeros((2, 2), dtype="float32"),
            dims=("y", "x"),
            name=f"{layer}_{year}",
        )
        return {0: da.to_dataset()}


GEOM = box(-86.20, 39.70, -86.10, 39.80)  # small CONUS box, Indianapolis


def _run(monkeypatch, spec):
    spy = _SpyPygeohydro()
    monkeypatch.setattr(hymod, "require_import", lambda *a, **k: spy)
    ds = hymod.Backend()._fetch_pygeohydro_raster(
        spec, spec.backend_config, GEOM, "2019-01-01", "2019-12-31"
    )
    return spy, ds


def test_nlcd_impervious_routes_to_impervious_layer(monkeypatch):
    spy, ds = _run(monkeypatch, get_product("NLCD_IMPERVIOUS"))
    assert spy.calls, "nlcd_bygeom was never called"
    requested_layer = list(spy.calls[0]["years"])[0]
    assert requested_layer == "impervious", (
        f"NLCD_IMPERVIOUS must request the 'impervious' layer, got "
        f"{requested_layer!r} — the hardcoded-'cover' regression is back."
    )
    band = list(ds.data_vars)[0]
    assert band.startswith("impervious_"), (
        f"returned band {band!r} is not an impervious band (land-cover leaked through)"
    )


def test_nlcd_layer_defaults_to_cover_when_unset(monkeypatch):
    """A product without an explicit nlcd_layer still gets 'cover' (back-compat)."""
    spec = get_product("NLCD_IMPERVIOUS").model_copy(
        update={"backend_config": {"pygeohydro_product": "nlcd", "default_year": 2019}}
    )
    spy, _ = _run(monkeypatch, spec)
    assert list(spy.calls[0]["years"])[0] == "cover"
