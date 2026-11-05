"""The raster cache keeps the CRS across the netCDF round trip (a cache-served DEM used to lose it).

A legacy .nc entry (no stored CRS) is a MISS with a stated reason, never silently given EPSG:4326.
Offline; cache_dir is patched to tmp_path.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import Point

rioxarray = pytest.importorskip("rioxarray")

from aihydro_data.cache import CRS_ATTR, cache_read, cache_write  # noqa: E402
from aihydro_data.contracts import FetchRequest, FetchResult  # noqa: E402


def _raster(crs):
    x = np.arange(12, dtype=float) * 30.0 + 500000.0 if crs != "EPSG:4326" else -100.0 + np.arange(12) / 3600.0
    y = (4000000.0 - np.arange(10) * 30.0) if crs != "EPSG:4326" else (35.0 + np.arange(10) / 3600.0)[::-1]
    da = xr.DataArray(np.random.default_rng(1).random((10, 12)).astype("float32"), dims=("y", "x"),
                      coords={"y": y, "x": x}, name="dem")
    return da if crs is None else da.rio.write_crs(crs)


def _result(data, ck):
    return FetchResult(
        variable="dem", product="GLO30", source="gee",
        request=FetchRequest(variable="dem", geometry=Point(-100.0, 35.0), start="1990-01-01", end="2020-12-31"),
        cache_key=ck, data=data, license="x", citation="c", bibtex="@misc{x}",
    )


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:32614", "EPSG:5070"])
def test_crs_survives_round_trip(tmp_path, crs):
    da = _raster(crs)
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        assert cache_write(_result(da, "crskey0000000000000000"), geom_wkt="POINT (-100 35)")
        got = cache_read("crskey0000000000000000")
    assert got is not None and got.cache_hit
    assert got.data.rio.crs is not None
    assert got.data.rio.crs == da.rio.crs
    assert got.data.rio.resolution() == pytest.approx(da.rio.resolution())
    np.testing.assert_allclose(got.data.values, da.values)


def test_raster_without_crs_round_trips_as_explicitly_no_crs(tmp_path):
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        cache_write(_result(_raster(None), "nocrs000000000000000000"), geom_wkt="POINT (0 0)")
        got = cache_read("nocrs000000000000000000")
    assert got is not None and got.data.rio.crs is None   # honest, not assumed


def test_legacy_entry_without_stored_crs_is_a_miss_with_a_reason(tmp_path):
    ck = "legacy00000000000000000"
    da = _raster("EPSG:4326")
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        cache_write(_result(da, ck), geom_wkt="POINT (-100 35)")
        # Rewrite the .nc the way the old code did: no CRS attribute, no spatial_ref; refresh the sha256.
        nc = tmp_path / f"{ck}.nc"
        legacy = da.drop_vars("spatial_ref")
        legacy.attrs = {k: v for k, v in legacy.attrs.items() if k != CRS_ATTR}
        nc.unlink()
        legacy.to_netcdf(nc)
        import json
        from aihydro_data.cache import file_sha256
        man = tmp_path / f"{ck}.manifest.json"
        entries = json.loads(man.read_text())
        entries = entries if isinstance(entries, list) else [entries]
        for e in entries:
            e["data_sha256"] = file_sha256(nc)
        man.write_text(json.dumps(entries))
        reasons: list[str] = []
        assert cache_read(ck, miss_reasons=reasons) is None
    assert any("no stored CRS" in r for r in reasons)


def test_attr_is_wkt_not_an_assumed_epsg(tmp_path):
    da = _raster("EPSG:32614")
    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        cache_write(_result(da, "wkt0000000000000000000"), geom_wkt="POINT (0 0)")
    stored = xr.open_dataset(tmp_path / "wkt0000000000000000000.nc")["dem"].attrs[CRS_ATTR]
    assert "UTM zone 14N" in stored


def test_legacy_miss_closes_the_dataset_so_the_file_can_be_replaced(tmp_path, monkeypatch):
    """An open handle on the legacy .nc would block the rewrite on Windows."""
    ck = "close0000000000000000000"
    da = _raster("EPSG:4326")
    closed = []
    real_close = xr.Dataset.close

    def _spy(self):
        closed.append(1)
        return real_close(self)

    with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
        cache_write(_result(da, ck), geom_wkt="POINT (-100 35)")
        nc = tmp_path / f"{ck}.nc"
        legacy = da.drop_vars("spatial_ref")
        legacy.attrs = {k: v for k, v in legacy.attrs.items() if k != CRS_ATTR}
        nc.unlink()
        legacy.to_netcdf(nc)
        import json
        from aihydro_data.cache import file_sha256
        man = tmp_path / f"{ck}.manifest.json"
        entries = json.loads(man.read_text())
        entries = entries if isinstance(entries, list) else [entries]
        for e in entries:
            e["data_sha256"] = file_sha256(nc)
        man.write_text(json.dumps(entries))
        monkeypatch.setattr(xr.Dataset, "close", _spy)
        assert cache_read(ck) is None
    assert closed, "legacy miss path did not close the dataset"
