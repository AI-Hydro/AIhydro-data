"""
Optical surface-reflectance products + multi-band composite dispatch.

All offline (no GEE auth, no network) — the GEE backend is monkeypatched to
return a synthetic reflectance Dataset so we exercise the routing + pipeline
dispatch path without live Earth Engine.

Covers:
  - the 3 optical ProductSpecs are registered with multiband band maps
  - routing resolves the optical fallback chain (incl. S_ASIA → global)
  - fetch('optical', ...) dispatches to fetch_multiband_composite and returns
    the xr.Dataset of bands
  - compute_index resolves friendly band names from that Dataset
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box


def _synthetic_reflectance(sensor: str = "sentinel2") -> xr.Dataset:
    y = np.arange(4)
    x = np.arange(5)

    def band(v):
        return xr.DataArray(np.full((4, 5), v, dtype="float32"),
                            dims=["y", "x"], coords={"y": y, "x": x})

    ds = xr.Dataset({
        "blue": band(0.25), "green": band(0.3), "red": band(0.2),
        "nir": band(0.5), "swir1": band(0.1), "swir2": band(0.08),
    })
    ds.attrs["sensor"] = sensor
    return ds


class TestOpticalProductSpecs:
    def test_optical_products_registered(self):
        from aihydro_data.products import list_products
        ids = {p.id for p in list_products(variable="optical")}
        assert ids == {
            "SENTINEL2_SR", "LANDSAT9_SR", "LANDSAT8_SR",
            "SENTINEL2_SR_STAC", "LANDSAT_SR_STAC",
        }

    def test_multiband_flag_and_bandmap(self):
        from aihydro_data.products import get_product
        for pid in ("SENTINEL2_SR", "LANDSAT9_SR", "LANDSAT8_SR"):
            spec = get_product(pid)
            assert spec.variable == "optical"
            assert spec.backend_config.get("multiband") is True
            bm = spec.backend_config["band_map"]
            # Every product must expose at least green + nir for NDWI/NDVI
            assert "green" in bm and "nir" in bm
            assert spec.source == "gee"

    def test_stac_products_are_no_auth_multiband(self):
        from aihydro_data.products import get_product
        for pid in ("SENTINEL2_SR_STAC", "LANDSAT_SR_STAC"):
            spec = get_product(pid)
            assert spec.variable == "optical"
            assert spec.source == "stac"
            assert spec.backend_config.get("multiband") is True
            assert spec.requires_auth == []          # PC anonymous read
            assert "stac" in spec.requires_extras
            bm = spec.backend_config["band_map"]
            assert "green" in bm and "nir" in bm
            assert spec.backend_config.get("qa_asset")


_OPTICAL_CHAIN = [
    "SENTINEL2_SR", "LANDSAT9_SR", "LANDSAT8_SR",
    "SENTINEL2_SR_STAC", "LANDSAT_SR_STAC",
]


class TestOpticalRouting:
    def test_global_chain(self):
        from aihydro_data.routing import resolve_product_ids
        assert resolve_product_ids("optical", "global") == _OPTICAL_CHAIN

    def test_s_asia_falls_through_to_chain(self):
        # The original bug: S_ASIA had no optical policy → REGION_NO_POLICY.
        from aihydro_data.routing import resolve_product_ids
        assert resolve_product_ids("optical", "S_ASIA") == _OPTICAL_CHAIN

    def test_stac_products_are_fallbacks_after_gee(self):
        # GEE products lead (synchronous, fast); STAC streams large AOIs that
        # would trip GEE_AREA_TOO_LARGE. STAC must come strictly after GEE.
        from aihydro_data.routing import resolve_product_ids
        chain = resolve_product_ids("optical", "global")
        gee_last = max(chain.index(p) for p in ("SENTINEL2_SR", "LANDSAT9_SR", "LANDSAT8_SR"))
        stac_first = min(chain.index(p) for p in ("SENTINEL2_SR_STAC", "LANDSAT_SR_STAC"))
        assert gee_last < stac_first


class TestMultibandDispatch:
    def test_fetch_optical_returns_band_dataset(self, monkeypatch):
        """fetch('optical', ...) must dispatch to fetch_multiband_composite."""
        from aihydro_data.sources.gee import Backend

        captured = {}

        def fake_available(self):
            return True, None

        def fake_composite(self, spec, geometry, start, end):
            captured["product"] = spec.id
            return _synthetic_reflectance(spec.backend_config.get("sensor", "sentinel2"))

        monkeypatch.setattr(Backend, "is_available", fake_available)
        monkeypatch.setattr(Backend, "fetch_multiband_composite", fake_composite)

        import aihydro_data
        roi = box(76.5, 28.0, 76.8, 28.3)  # S_ASIA → exercises the fixed routing
        result = aihydro_data.fetch("optical", roi, "2022-01-01", "2022-03-31", cache=False)

        assert captured["product"] == "SENTINEL2_SR"   # primary in chain
        ds = result.data
        assert isinstance(ds, xr.Dataset)
        assert {"green", "nir"} <= set(ds.data_vars)

    def test_gee_area_too_large_falls_through_to_stac(self, monkeypatch):
        """The real Request-B scenario: GEE raises GEE_AREA_TOO_LARGE on a big
        watershed, and the fallback loop streams the bands from STAC instead."""
        from aihydro_data.sources.gee import Backend as GeeBackend
        from aihydro_data.sources.stac import Backend as StacBackend
        from aihydro_data.exceptions import SourceUnavailable

        captured = {}

        def gee_too_large(self, spec, geometry, start, end):
            raise SourceUnavailable(
                code="GEE_AREA_TOO_LARGE",
                message="composite over ~11891 km² exceeds GEE's ~32 MB cap",
            )

        def stac_ok(self, spec, geometry, start, end):
            captured["product"] = spec.id
            return _synthetic_reflectance(spec.backend_config.get("sensor", "sentinel2"))

        monkeypatch.setattr(GeeBackend, "is_available", lambda self: (True, None))
        monkeypatch.setattr(StacBackend, "is_available", lambda self: (True, None))
        monkeypatch.setattr(GeeBackend, "fetch_multiband_composite", gee_too_large)
        monkeypatch.setattr(StacBackend, "fetch_multiband_composite", stac_ok)

        import aihydro_data
        roi = box(76.0, 27.5, 77.5, 29.0)  # large S_ASIA basin
        result = aihydro_data.fetch("optical", roi, "2022-01-01", "2022-03-31", cache=False)

        # All 3 GEE products raised → first STAC product served the bands.
        assert captured["product"] == "SENTINEL2_SR_STAC"
        ds = result.data
        assert isinstance(ds, xr.Dataset)
        assert {"green", "nir"} <= set(ds.data_vars)

    def test_server_side_index_preferred_when_index_passed(self, monkeypatch):
        """fetch('optical', ..., index='NDWI') must route to the GEE backend's
        server-side fetch_index_composite and return a single-band DataArray —
        NOT download all raw bands."""
        from aihydro_data.sources.gee import Backend

        captured = {}

        def fake_index(self, spec, geometry, start, end, index_name, *, mask_clouds=True, native_resolution=False):
            captured["index"] = index_name
            captured["product"] = spec.id
            captured["mask_clouds"] = mask_clouds
            da = xr.DataArray(
                np.full((4, 5), -0.25, dtype="float32"),
                dims=["y", "x"],
                coords={"y": np.arange(4), "x": np.arange(5)},
                name=index_name.lower(),
            )
            da.attrs["computed"] = "server-side (GEE)"
            return da

        def fake_multiband(self, spec, geometry, start, end):
            captured["multiband_called"] = True
            return _synthetic_reflectance()

        monkeypatch.setattr(Backend, "is_available", lambda self: (True, None))
        monkeypatch.setattr(Backend, "fetch_index_composite", fake_index)
        monkeypatch.setattr(Backend, "fetch_multiband_composite", fake_multiband)

        import aihydro_data
        roi = box(76.5, 28.0, 76.8, 28.3)
        result = aihydro_data.fetch(
            "optical", roi, "2022-01-01", "2022-03-31", index="NDWI", cache=False,
        )
        assert captured["index"] == "NDWI"
        assert captured["product"] == "SENTINEL2_SR"
        assert "multiband_called" not in captured   # raw-band path skipped
        da = result.data
        assert isinstance(da, xr.DataArray)
        assert da.attrs.get("computed") == "server-side (GEE)"

    def test_server_side_index_falls_back_to_raw_bands(self, monkeypatch):
        """If the backend can't compute an index server-side (ValueError), the
        pipeline must fall through to fetch_multiband_composite (raw bands)."""
        from aihydro_data.sources.gee import Backend

        captured = {}

        def fake_index(self, spec, geometry, start, end, index_name, *, mask_clouds=True, native_resolution=False):
            raise ValueError("no server-side formula")

        def fake_multiband(self, spec, geometry, start, end):
            captured["multiband_called"] = True
            return _synthetic_reflectance()

        monkeypatch.setattr(Backend, "is_available", lambda self: (True, None))
        monkeypatch.setattr(Backend, "fetch_index_composite", fake_index)
        monkeypatch.setattr(Backend, "fetch_multiband_composite", fake_multiband)

        import aihydro_data
        roi = box(76.5, 28.0, 76.8, 28.3)
        result = aihydro_data.fetch(
            "optical", roi, "2022-01-01", "2022-03-31", index="NDWI", cache=False,
        )
        assert captured.get("multiband_called") is True
        assert isinstance(result.data, xr.Dataset)

    def test_index_computed_from_fetched_bands(self, monkeypatch):
        """End-to-end: fetched band Dataset → compute_index NDWI."""
        from aihydro_data.sources.gee import Backend
        from aihydro_data.transforms.indices import compute_index

        monkeypatch.setattr(Backend, "is_available", lambda self: (True, None))
        monkeypatch.setattr(
            Backend, "fetch_multiband_composite",
            lambda self, spec, geometry, start, end: _synthetic_reflectance(),
        )

        import aihydro_data
        roi = box(76.5, 28.0, 76.8, 28.3)
        ds = aihydro_data.fetch("optical", roi, "2022-01-01", "2022-03-31", cache=False).data
        ndwi = compute_index("NDWI", ds=ds, sensor="sentinel2", mask_clouds_first=False)
        # NDWI = (green - nir)/(green + nir) = (0.3 - 0.5)/0.8 = -0.25
        assert float(ndwi.mean()) == pytest.approx(-0.25, abs=1e-4)
