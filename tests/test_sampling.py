"""
Unit tests for aihydro_data.sampling.catchment + .chunked.

Pure-numpy tests with synthetic rasters and shapely polygons — no live
backends.  Marked offline (default pytest).
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from rasterio.transform import Affine
from shapely.geometry import Point, Polygon, box

from aihydro_data.sampling import (
    CatchmentGridSampler,
    CatchmentRandomSampler,
    ChipInfo,
    chunked_raster_apply,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_raster(height: int, width: int, dx: float = 1.0, dy: float = 1.0,
                 x0: float = 0.0, y0: float | None = None,
                 fill: float | np.ndarray = 0.0) -> xr.DataArray:
    """Synthetic raster with explicit coords + affine via rio accessor."""
    if y0 is None:
        # raster top edge at y = height * dy so y values DECREASE with row
        # (standard image convention).
        y0 = height * dy
    x = x0 + dx / 2 + np.arange(width) * dx     # pixel centers
    y = y0 - dy / 2 - np.arange(height) * dy

    if isinstance(fill, np.ndarray):
        data = fill.astype("float32")
    else:
        data = np.full((height, width), fill, dtype="float32")

    da = xr.DataArray(data, dims=("y", "x"),
                      coords={"y": y, "x": x},
                      name="synthetic")
    return da


@pytest.fixture
def circular_basin():
    """A 1000×1000 raster + circular watershed centered at (500, 500), r=300."""
    raster = _make_raster(1000, 1000, dx=1.0, dy=1.0, x0=0.0, y0=1000.0,
                          fill=np.arange(1_000_000).reshape(1000, 1000))
    basin = Point(500, 500).buffer(300)
    return raster, basin


@pytest.fixture
def donut_basin():
    """Annular polygon (outer r=300, inner r=100) — tests irregular mask."""
    raster = _make_raster(1000, 1000, dx=1.0, dy=1.0, x0=0.0, y0=1000.0,
                          fill=42.0)
    outer = Point(500, 500).buffer(300)
    inner = Point(500, 500).buffer(100)
    basin = outer.difference(inner)
    return raster, basin


@pytest.fixture
def small_basin():
    """100×100 raster + a basin that fits in one chip — tests fast-path."""
    raster = _make_raster(100, 100, fill=1.0)
    basin = box(10, 10, 90, 90)
    return raster, basin


# ---------------------------------------------------------------------------
# CatchmentGridSampler
# ---------------------------------------------------------------------------


def test_grid_sampler_yields_chip_info(circular_basin):
    raster, basin = circular_basin
    sampler = CatchmentGridSampler(raster, basin, chip_size=256, stride=256)
    chips = list(sampler)
    assert len(chips) > 0
    for chip in chips:
        assert isinstance(chip, ChipInfo)
        assert chip.window.height > 0 and chip.window.width > 0
        assert chip.mask.shape == (chip.window.height, chip.window.width)
        assert chip.mask.dtype == np.bool_
        # At least one True pixel — otherwise sampler should have skipped.
        assert chip.mask.any()


def test_grid_sampler_prunes_outside_chips(circular_basin):
    """Chips whose bbox doesn't intersect the watershed are dropped."""
    raster, basin = circular_basin
    # 256×256 chips on a 1000×1000 raster → 16 candidate chips.  Circle is
    # in the center, so corner chips should be pruned.
    sampler = CatchmentGridSampler(raster, basin, chip_size=256, stride=256)
    chips = list(sampler)
    assert len(chips) < 16, "Expected some corner chips to be pruned"


def test_grid_sampler_donut_mask(donut_basin):
    """Annular polygon → some center pixels are False inside the chip mask."""
    raster, basin = donut_basin
    sampler = CatchmentGridSampler(raster, basin, chip_size=256, stride=256)
    chips = list(sampler)
    # The chip covering the center of the raster should have a hole.
    center_chip = next(
        (c for c in chips
         if c.window.row_off <= 500 < c.window.row_off + c.window.height
         and c.window.col_off <= 500 < c.window.col_off + c.window.width),
        None,
    )
    assert center_chip is not None
    # Mask should be False at the very center of the chip (inside the hole).
    cy = 500 - center_chip.window.row_off
    cx = 500 - center_chip.window.col_off
    assert center_chip.mask[cy, cx] == False


def test_grid_sampler_chip_mask_matches_single_geometry_mask(circular_basin):
    """Per-chip mask must equal the full-raster mask sliced to the chip."""
    from rasterio.features import geometry_mask
    raster, basin = circular_basin
    sampler = CatchmentGridSampler(raster, basin, chip_size=256, stride=256)

    # Full mask via single call.
    full_mask = geometry_mask(
        [basin], out_shape=(1000, 1000),
        transform=Affine(1, 0, 0, 0, -1, 1000),  # matches _make_raster fixture
        invert=True, all_touched=False,
    )

    chips = list(sampler)
    assert len(chips) > 0
    for chip in chips:
        w = chip.window
        full_slice = full_mask[
            w.row_off:w.row_off + w.height,
            w.col_off:w.col_off + w.width,
        ]
        np.testing.assert_array_equal(chip.mask, full_slice)


def test_grid_sampler_kernel_pad_core_slice():
    """kernel_pad=1 → core_slice drops 1-pixel border."""
    raster = _make_raster(500, 500, fill=1.0)
    basin = box(50, 50, 450, 450)
    sampler = CatchmentGridSampler(raster, basin, chip_size=128, kernel_pad=1)
    chips = list(sampler)
    assert len(chips) > 0
    assert chips[0].core_slice == (slice(1, -1), slice(1, -1))


def test_grid_sampler_validates_kernel_pad():
    """kernel_pad too large for chip_size → ValueError."""
    raster = _make_raster(100, 100)
    basin = box(0, 0, 100, 100)
    with pytest.raises(ValueError, match="kernel_pad"):
        CatchmentGridSampler(raster, basin, chip_size=4, kernel_pad=2)


def test_grid_sampler_len_is_upper_bound(circular_basin):
    """__len__ is the grid size (before pruning)."""
    raster, basin = circular_basin
    sampler = CatchmentGridSampler(raster, basin, chip_size=256, stride=256)
    n_emitted = len(list(sampler))
    assert n_emitted <= len(sampler)


# ---------------------------------------------------------------------------
# CatchmentRandomSampler
# ---------------------------------------------------------------------------


def test_random_sampler_yields_n_samples(circular_basin):
    raster, basin = circular_basin
    sampler = CatchmentRandomSampler(raster, basin, n_samples=10,
                                     chip_size=64, seed=42)
    chips = list(sampler)
    assert len(chips) <= 10  # may be fewer if rejection sampling fails
    assert len(chips) > 0


def test_random_sampler_reproducible(circular_basin):
    """Same seed → identical chip sequence."""
    raster, basin = circular_basin
    s1 = list(CatchmentRandomSampler(raster, basin, n_samples=5,
                                     chip_size=64, seed=42))
    s2 = list(CatchmentRandomSampler(raster, basin, n_samples=5,
                                     chip_size=64, seed=42))
    assert len(s1) == len(s2)
    for c1, c2 in zip(s1, s2):
        assert c1.window.row_off == c2.window.row_off
        assert c1.window.col_off == c2.window.col_off


def test_random_sampler_chips_in_basin(circular_basin):
    """Every emitted chip must touch the basin."""
    raster, basin = circular_basin
    sampler = CatchmentRandomSampler(raster, basin, n_samples=20,
                                     chip_size=64, seed=7)
    for chip in sampler:
        assert chip.mask.any()


# ---------------------------------------------------------------------------
# chunked_raster_apply
# ---------------------------------------------------------------------------


def test_chunked_apply_fastpath_small_raster(small_basin):
    """Raster smaller than auto_trigger → single-pass path (no chunking)."""
    raster, basin = small_basin

    def double(arr, mask):
        return arr * 2

    out = chunked_raster_apply(raster, basin, double,
                                auto_trigger_size=10_000_000)
    assert out.attrs["chunked_applied"] is False
    # Pixels inside the basin doubled; outside → NaN.
    inside_value = float(out.sel(y=50, x=50, method="nearest"))
    assert inside_value == pytest.approx(2.0)
    outside_value = float(out.sel(y=5, x=5, method="nearest"))
    assert np.isnan(outside_value)


def test_chunked_apply_pure_pixel_op_exact(circular_basin):
    """For kernel_pad=0, chunked output equals single-pass exactly."""
    raster, basin = circular_basin

    def double(arr, mask):
        return arr * 2

    single = chunked_raster_apply(raster, basin, double,
                                    auto_trigger_size=10_000_000)
    chunked = chunked_raster_apply(raster, basin, double,
                                     chip_size=256, kernel_pad=0,
                                     auto_trigger_size=0)  # force chunked
    assert chunked.attrs["chunked_applied"] is True
    # Inside the basin, both should be identical (NaN-aware compare).
    np.testing.assert_allclose(
        np.where(np.isnan(single.values), 0, single.values),
        np.where(np.isnan(chunked.values), 0, chunked.values),
        atol=1e-6,
    )


def test_chunked_apply_3x3_kernel_matches_single_pass(circular_basin):
    """Local 3×3 op (mean filter) with kernel_pad=1 matches single-pass within tolerance."""
    raster, basin = circular_basin

    def mean_3x3(arr, mask):
        """Simple separable 3x3 mean filter."""
        from scipy.ndimage import uniform_filter
        return uniform_filter(arr.astype("float32"), size=3, mode="reflect")

    pytest.importorskip("scipy")

    single = chunked_raster_apply(raster, basin, mean_3x3,
                                    auto_trigger_size=10_000_000)
    chunked = chunked_raster_apply(raster, basin, mean_3x3,
                                     chip_size=256, kernel_pad=1,
                                     auto_trigger_size=0)

    # Compare interior pixels (avoiding NaN edges).
    # Inside the basin: chunked must match single-pass within tolerance.
    finite_both = np.isfinite(single.values) & np.isfinite(chunked.values)
    assert finite_both.sum() > 100  # enough sample points
    diff = single.values[finite_both] - chunked.values[finite_both]
    rmse = float(np.sqrt(np.mean(diff ** 2)))
    assert rmse < 1.0, f"RMSE {rmse} too high — chunked 3×3 not equivalent"


def test_chunked_apply_irregular_polygon(donut_basin):
    """Donut polygon → output should be NaN in the hole."""
    raster, basin = donut_basin

    def identity(arr, mask):
        return arr.astype("float32")

    out = chunked_raster_apply(raster, basin, identity,
                                 chip_size=128, kernel_pad=0,
                                 auto_trigger_size=0)
    # Hole at center: y=500, x=500 → NaN.
    center = float(out.sel(y=500.5, x=500.5, method="nearest"))
    assert np.isnan(center)
    # Donut ring: y=700, x=500 → 42 (the fill value).
    ring = float(out.sel(y=700.5, x=500.5, method="nearest"))
    assert ring == pytest.approx(42.0)


def test_chunked_apply_auto_trigger(circular_basin):
    """Heuristic correctly picks fast vs chunked path."""
    raster, basin = circular_basin

    def add_one(arr, mask):
        return arr + 1

    # 1000×1000 = 1M cells; default 10M threshold → fast path.
    out_fast = chunked_raster_apply(raster, basin, add_one)
    assert out_fast.attrs["chunked_applied"] is False

    # Lower threshold → chunked.
    out_chunked = chunked_raster_apply(raster, basin, add_one,
                                         auto_trigger_size=100, chip_size=256)
    assert out_chunked.attrs["chunked_applied"] is True
    assert "chunked_n_chips" in out_chunked.attrs


def test_chunked_apply_progress_callback(circular_basin):
    """Progress callback fires once per chip."""
    raster, basin = circular_basin
    calls: list[tuple[int, int]] = []

    def add_one(arr, mask):
        return arr + 1

    chunked_raster_apply(
        raster, basin, add_one,
        chip_size=256, auto_trigger_size=0,
        progress_cb=lambda i, n: calls.append((i, n)),
    )
    assert len(calls) > 0
    assert calls[-1][0] == len(calls)


def test_chunked_apply_validates_fn_shape(circular_basin):
    """fn returning wrong shape → ValueError."""
    raster, basin = circular_basin

    def bad_fn(arr, mask):
        return arr[:-1, :]  # shrinks the array

    with pytest.raises(ValueError, match="must match"):
        chunked_raster_apply(raster, basin, bad_fn,
                              chip_size=256, auto_trigger_size=0)


def test_chunked_apply_preserves_attrs(circular_basin):
    """Caller's attrs survive — provenance unbroken."""
    raster, basin = circular_basin
    raster.attrs["source"] = "test"

    def double(arr, mask):
        return arr * 2

    out = chunked_raster_apply(raster, basin, double,
                                 chip_size=256, auto_trigger_size=0)
    assert out.attrs["source"] == "test"
    assert out.attrs["chunked_applied"] is True
