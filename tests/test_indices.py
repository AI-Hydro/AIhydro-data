"""
Unit tests for aihydro_data.transforms.indices and .cloud_mask.

These are pure-numpy tests against hand-computed expected values — no live
backends.  Marked as offline (default pytest) so CI runs them on every push.
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from aihydro_data.transforms import (
    CLOUD_AWARE_SENSORS,
    INDEX_REGISTRY,
    SENSOR_BAND_MAPS,
    compute_index,
    evi,
    gndvi,
    list_indices,
    mask_clouds,
    mndwi,
    nbr,
    ndbi,
    ndre,
    ndsi,
    ndvi,
    ndwi,
    swi,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def band_arrays():
    """A consistent 4x4 set of band DataArrays with known values.

    All bands are float32 reflectances in [0, 1].  Pixel (0,0) is dense
    vegetation; (3,3) is open water; (3,0) is built-up; centre is bare soil.
    """
    rng = np.random.default_rng(42)
    base = rng.uniform(0.05, 0.5, size=(4, 4)).astype("float32")
    coords = {"y": np.arange(4), "x": np.arange(4)}

    def _da(arr, name):
        return xr.DataArray(arr.astype("float32"), dims=("y", "x"),
                            coords=coords, name=name)

    # Plausible Sentinel-2 reflectances at (0,0) vegetation, (3,3) water,
    # (3,0) built-up.
    blue   = base.copy(); blue[0, 0] = 0.03;  blue[3, 3] = 0.05;  blue[3, 0] = 0.10
    green  = base.copy(); green[0, 0] = 0.05; green[3, 3] = 0.07; green[3, 0] = 0.12
    red    = base.copy(); red[0, 0]   = 0.04; red[3, 3]   = 0.06; red[3, 0]   = 0.15
    re1    = base.copy(); re1[0, 0]   = 0.20; re1[3, 3]   = 0.04
    nir    = base.copy(); nir[0, 0]   = 0.55; nir[3, 3]   = 0.02; nir[3, 0]   = 0.20
    swir1  = base.copy(); swir1[0, 0] = 0.30; swir1[3, 3] = 0.01; swir1[3, 0] = 0.25
    swir2  = base.copy(); swir2[0, 0] = 0.20; swir2[3, 3] = 0.00; swir2[3, 0] = 0.20

    return {
        "blue":  _da(blue,  "B2"),
        "green": _da(green, "B3"),
        "red":   _da(red,   "B4"),
        "re1":   _da(re1,   "B5"),
        "nir":   _da(nir,   "B8"),
        "swir1": _da(swir1, "B11"),
        "swir2": _da(swir2, "B12"),
    }


@pytest.fixture
def sentinel2_dataset(band_arrays):
    """Same bands packaged as an xr.Dataset with Sentinel-2 native names."""
    return xr.Dataset(
        {
            "B2":  band_arrays["blue"],
            "B3":  band_arrays["green"],
            "B4":  band_arrays["red"],
            "B5":  band_arrays["re1"],
            "B8":  band_arrays["nir"],
            "B11": band_arrays["swir1"],
            "B12": band_arrays["swir2"],
        },
        attrs={"sensor": "sentinel2"},
    )


# ---------------------------------------------------------------------------
# Formula correctness
# ---------------------------------------------------------------------------


def test_ndvi_formula(band_arrays):
    out = ndvi(band_arrays["red"], band_arrays["nir"])
    # Hand-compute one pixel: nir=0.55, red=0.04 → (0.55-0.04)/(0.55+0.04) ≈ 0.8644
    assert out.values[0, 0] == pytest.approx(0.8644, abs=1e-3)
    # Water pixel: nir=0.02, red=0.06 → (0.02-0.06)/(0.02+0.06) = -0.5
    assert out.values[3, 3] == pytest.approx(-0.5, abs=1e-3)
    assert out.attrs["index"] == "NDVI"
    assert out.attrs["colormap"] == "RdYlGn"


def test_ndwi_formula(band_arrays):
    out = ndwi(band_arrays["green"], band_arrays["nir"])
    # Water pixel: green=0.07, nir=0.02 → (0.07-0.02)/(0.07+0.02) ≈ 0.5556
    assert out.values[3, 3] == pytest.approx(0.5556, abs=1e-3)
    # Vegetation: green=0.05, nir=0.55 → negative
    assert out.values[0, 0] < 0
    assert out.attrs["colormap"] == "Blues"


def test_mndwi_formula(band_arrays):
    out = mndwi(band_arrays["green"], band_arrays["swir1"])
    # Water: green=0.07, swir1=0.01 → (0.07-0.01)/(0.07+0.01) ≈ 0.75
    assert out.values[3, 3] == pytest.approx(0.75, abs=1e-3)
    assert out.attrs["citation"] == "Xu 2006"


def test_ndbi_formula(band_arrays):
    out = ndbi(band_arrays["swir1"], band_arrays["nir"])
    # Built-up: swir1=0.25, nir=0.20 → (0.25-0.20)/(0.25+0.20) ≈ 0.1111
    assert out.values[3, 0] == pytest.approx(0.1111, abs=1e-3)
    assert out.attrs["colormap"] == "Reds"


def test_nbr_formula(band_arrays):
    out = nbr(band_arrays["nir"], band_arrays["swir2"])
    # Vegetation: nir=0.55, swir2=0.20 → (0.55-0.20)/(0.55+0.20) ≈ 0.4667
    assert out.values[0, 0] == pytest.approx(0.4667, abs=1e-3)


def test_ndsi_formula(band_arrays):
    out = ndsi(band_arrays["green"], band_arrays["swir1"])
    assert out.attrs["use_case"] == "snow / ice cover"


def test_gndvi_formula(band_arrays):
    out = gndvi(band_arrays["green"], band_arrays["nir"])
    # Vegetation: nir=0.55, green=0.05 → (0.55-0.05)/(0.55+0.05) ≈ 0.8333
    assert out.values[0, 0] == pytest.approx(0.8333, abs=1e-3)


def test_ndre_formula(band_arrays):
    out = ndre(band_arrays["nir"], band_arrays["re1"])
    # nir=0.55, re1=0.20 → (0.55-0.20)/(0.55+0.20) ≈ 0.4667
    assert out.values[0, 0] == pytest.approx(0.4667, abs=1e-3)


def test_swi_formula(band_arrays):
    out = swi(band_arrays["re1"], band_arrays["swir2"])
    assert out.attrs["citation"] == "Fernandes et al. 2017"


def test_evi_formula(band_arrays):
    out = evi(band_arrays["blue"], band_arrays["red"], band_arrays["nir"])
    # Vegetation: blue=0.03, red=0.04, nir=0.55
    # 2.5*(0.55-0.04)/(0.55+6*0.04 - 7.5*0.03 + 1) = 2.5*0.51/(0.55+0.24-0.225+1)
    # = 1.275 / 1.565 ≈ 0.8147
    assert out.values[0, 0] == pytest.approx(0.8147, abs=1e-3)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_denominator_handled_by_eps(band_arrays):
    """All-zero pixels must not produce inf or nan."""
    z = xr.zeros_like(band_arrays["red"])
    out = ndvi(z, z)
    assert np.isfinite(out.values).all()
    # (0 - 0) / (0 + 0 + eps) = 0
    assert np.all(out.values == 0)


def test_dtype_promotion_from_uint16(band_arrays):
    """Raw Sentinel-2 DNs come as uint16 — output must promote to float."""
    red_u16 = (band_arrays["red"] * 10000).astype("uint16")
    nir_u16 = (band_arrays["nir"] * 10000).astype("uint16")
    out = ndvi(red_u16, nir_u16)
    assert out.dtype == np.float32
    # Formula scale-invariant — ratio of equally-scaled values is unchanged.
    expected = ndvi(band_arrays["red"], band_arrays["nir"]).values
    np.testing.assert_allclose(out.values, expected, atol=1e-3)


def test_nan_propagation(band_arrays):
    """NaN in input must propagate to NaN in output."""
    red = band_arrays["red"].copy()
    red.values[1, 1] = np.nan
    out = ndvi(red, band_arrays["nir"])
    assert np.isnan(out.values[1, 1])


# ---------------------------------------------------------------------------
# Registry + discovery
# ---------------------------------------------------------------------------


def test_index_registry_completeness():
    """Every registered index has the required metadata fields."""
    required_fields = {"fn", "bands", "range", "colormap",
                       "citation", "use_case", "threshold_hint"}
    for name, meta in INDEX_REGISTRY.items():
        assert required_fields.issubset(meta.keys()), \
            f"Index {name} missing fields: {required_fields - meta.keys()}"
        assert callable(meta["fn"])
        assert isinstance(meta["bands"], list) and len(meta["bands"]) >= 2
        lo, hi = meta["range"]
        assert lo < hi


def test_list_indices_filters_by_use_case():
    water_indices = list_indices(use_case="water")
    names = {e["name"] for e in water_indices}
    assert {"NDWI", "MNDWI", "SWI"}.issubset(names)
    # Vegetation indices should not match.
    assert "NDVI" not in names


def test_index_registry_count():
    """Sanity: exactly the 10 indices documented in the plan are registered."""
    expected = {"NDVI", "NDWI", "MNDWI", "NDBI", "NBR",
                "NDSI", "GNDVI", "NDRE", "SWI", "EVI"}
    assert set(INDEX_REGISTRY) == expected


# ---------------------------------------------------------------------------
# compute_index convenience wrapper
# ---------------------------------------------------------------------------


def test_compute_index_with_dataset(sentinel2_dataset):
    """Auto band resolution from Dataset + sensor attr."""
    out = compute_index("NDWI", sentinel2_dataset, mask_clouds_first=False)
    # Same result as calling ndwi() directly.
    expected = ndwi(sentinel2_dataset["B3"], sentinel2_dataset["B8"]).values
    np.testing.assert_allclose(out.values, expected)
    assert out.attrs["index"] == "NDWI"


def test_compute_index_with_band_map(band_arrays):
    """Explicit band_map overrides sensor lookup."""
    ds = xr.Dataset(
        {"my_green": band_arrays["green"], "my_nir": band_arrays["nir"]}
    )
    out = compute_index("NDWI", ds,
                        band_map={"green": "my_green", "nir": "my_nir"},
                        mask_clouds_first=False)
    expected = ndwi(band_arrays["green"], band_arrays["nir"]).values
    np.testing.assert_allclose(out.values, expected)


def test_compute_index_with_plain_dict(band_arrays):
    """Plain dict mode for ad-hoc inputs."""
    out = compute_index("NDVI", band_arrays, mask_clouds_first=False)
    expected = ndvi(band_arrays["red"], band_arrays["nir"]).values
    np.testing.assert_allclose(out.values, expected)


def test_compute_index_unknown_raises():
    with pytest.raises(KeyError, match="Unknown index"):
        compute_index("NOT_A_REAL_INDEX", {})


def test_compute_index_case_insensitive(sentinel2_dataset):
    """Index names matched case-insensitively."""
    a = compute_index("ndwi", sentinel2_dataset, mask_clouds_first=False)
    b = compute_index("NDWI", sentinel2_dataset, mask_clouds_first=False)
    np.testing.assert_allclose(a.values, b.values)


# ---------------------------------------------------------------------------
# Sensor band map sanity
# ---------------------------------------------------------------------------


def test_sensor_band_maps_have_required_bands():
    """Each sensor preset has at minimum {red, green, nir} for basic indices."""
    minimum = {"red", "green", "nir"}
    for sensor, m in SENSOR_BAND_MAPS.items():
        assert minimum.issubset(m.keys()), \
            f"Sensor {sensor} missing required canonical bands: {minimum - m.keys()}"


# ---------------------------------------------------------------------------
# Cloud masking
# ---------------------------------------------------------------------------


def test_sentinel2_cloud_mask_basics(sentinel2_dataset):
    """SCL classes 8, 9, 10 → cloud; data must become NaN."""
    scl = xr.DataArray(
        np.array([
            [4, 4, 4, 4],    # vegetation — keep
            [4, 8, 9, 10],   # row of clouds — mask
            [3, 4, 4, 6],    # shadow + water + veg
            [4, 4, 4, 4],
        ], dtype="uint8"),
        dims=("y", "x"),
        coords=sentinel2_dataset.coords,
    )
    ds = sentinel2_dataset.assign(SCL=scl)
    out = mask_clouds(ds, sensor="sentinel2")
    # B4 at (1,1), (1,2), (1,3), (2,0) should be NaN (cloud_med, cloud_high,
    # cirrus, cloud_shadow).
    assert np.isnan(out["B4"].values[1, 1])
    assert np.isnan(out["B4"].values[1, 2])
    assert np.isnan(out["B4"].values[1, 3])
    assert np.isnan(out["B4"].values[2, 0])
    # SCL itself preserved.
    np.testing.assert_array_equal(out["SCL"].values, scl.values)
    # Vegetation pixel untouched.
    assert not np.isnan(out["B4"].values[0, 0])


def test_cloud_mask_preserves_provenance(sentinel2_dataset):
    scl = xr.DataArray(np.full((4, 4), 4, dtype="uint8"), dims=("y", "x"))
    ds = sentinel2_dataset.assign(SCL=scl)
    out = mask_clouds(ds, sensor="sentinel2")
    assert out.attrs["cloud_masked"] is True
    assert out.attrs["cloud_masked_sensor"] == "sentinel2"
    assert out.attrs["cloud_masked_fraction"] == 0.0


def test_cloud_mask_skips_unsupported_sensor(sentinel2_dataset):
    """CHIRPS / GridMET / etc. have no QA — return ds unchanged."""
    out = mask_clouds(sentinel2_dataset, sensor="chirps")
    # No QA → returned as-is.
    assert out is sentinel2_dataset


def test_cloud_mask_skips_when_qa_band_missing(sentinel2_dataset):
    """Sentinel-2 attr but no SCL band present → log + return unchanged."""
    out = mask_clouds(sentinel2_dataset, sensor="sentinel2")  # no SCL added
    # Should have returned the original ds since QA band is missing.
    assert out is sentinel2_dataset


def test_cloud_aware_sensors_constant():
    """All sensors with cloud-mask logic have an entry in SENSOR_BAND_MAPS."""
    for s in CLOUD_AWARE_SENSORS:
        assert s in SENSOR_BAND_MAPS, \
            f"Cloud-aware sensor {s} has no SENSOR_BAND_MAPS entry"
        assert "qa" in SENSOR_BAND_MAPS[s]


def test_landsat8_cloud_mask_bits():
    """Landsat QA_PIXEL bitfield decoding."""
    # Bit 3 = cloud → 0b00001000 = 8
    # Bit 4 = shadow → 0b00010000 = 16
    # Bit 1 = dilated cloud → 0b00000010 = 2
    qa_values = np.array([
        [0,  8,  16, 2],     # clear, cloud, shadow, dilated cloud
        [0,  0,  0,  0],
        [0,  0,  0,  0],
        [0,  0,  0,  0],
    ], dtype="uint16")
    coords = {"y": np.arange(4), "x": np.arange(4)}
    qa = xr.DataArray(qa_values, dims=("y", "x"), coords=coords, name="QA_PIXEL")
    sr_b4 = xr.DataArray(np.full((4, 4), 0.1, dtype="float32"),
                         dims=("y", "x"), coords=coords)
    ds = xr.Dataset({"SR_B4": sr_b4, "QA_PIXEL": qa},
                    attrs={"sensor": "landsat8"})
    out = mask_clouds(ds)
    # Cloud pixel
    assert np.isnan(out["SR_B4"].values[0, 1])
    # Shadow pixel
    assert np.isnan(out["SR_B4"].values[0, 2])
    # Dilated cloud
    assert np.isnan(out["SR_B4"].values[0, 3])
    # Clear pixel preserved
    assert not np.isnan(out["SR_B4"].values[0, 0])


def test_aggressive_mode_masks_more(sentinel2_dataset):
    """Aggressive mode also masks low-confidence cloud (SCL=7)."""
    scl = xr.DataArray(
        np.array([
            [7, 4, 4, 4],   # cloud_low at (0,0)
            [4, 4, 4, 4],
            [4, 4, 4, 4],
            [4, 4, 4, 4],
        ], dtype="uint8"),
        dims=("y", "x"),
    )
    ds = sentinel2_dataset.assign(SCL=scl)
    out_default = mask_clouds(ds, sensor="sentinel2", aggressive=False)
    out_aggressive = mask_clouds(ds, sensor="sentinel2", aggressive=True)
    assert not np.isnan(out_default["B4"].values[0, 0])
    assert np.isnan(out_aggressive["B4"].values[0, 0])


def test_compute_index_auto_cloud_mask(sentinel2_dataset):
    """compute_index() with mask_clouds_first=True applies the mask."""
    scl = xr.DataArray(
        np.full((4, 4), 4, dtype="uint8"),  # all veg
        dims=("y", "x"),
    )
    scl.values[0, 0] = 9  # cloud_high
    ds = sentinel2_dataset.assign(SCL=scl)
    out = compute_index("NDWI", ds, mask_clouds_first=True)
    # Cloud pixel should be NaN.
    assert np.isnan(out.values[0, 0])
    # Other pixels untouched.
    assert not np.isnan(out.values[3, 3])
