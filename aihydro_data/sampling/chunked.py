"""
Generalized chunked raster applier.

For pixel-local raster operations (band math, slope/aspect/curvature in a 3×3
neighborhood, LULC × soil overlay, reclassification), this is the bridge that
turns a single-pass function into one that scales to continent-sized inputs
without blowing up memory.

Architecture
~~~~~~~~~~~~
The applier:

  1. Heuristic short-circuit: if ``raster.size < auto_trigger_size``, run
     ``fn`` once on the whole array (existing behavior — zero overhead for
     small basins).
  2. Otherwise, instantiate a :class:`CatchmentGridSampler` with the requested
     ``chip_size`` / ``stride`` / ``kernel_pad``.
  3. For each chip: slice the source raster, run ``fn(chip_array, chip_mask)``,
     drop the kernel-pad border (its values come from incomplete neighborhood),
     and paste into the output array.
  4. Apply the final watershed mask once at the end.

Constraints — what this CANNOT do
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This applier handles **local-pixel** and **local-neighborhood** operations.
It DOES NOT WORK for global flow-routing operations (flow accumulation, TWI,
drainage extraction).  Water propagates across the entire basin, so chip-local
accumulation produces wrong values at chip edges no matter how large the
overlap.  For those, use the existing MERIT-pyflwdir route in
``aihydro-tools/ai_hydro/analysis/delineation/router.py``.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np
import xarray as xr
from shapely.geometry.base import BaseGeometry

from aihydro_data.sampling.catchment import CatchmentGridSampler

log = logging.getLogger(__name__)

# 10M cells ≈ 80 MB at float64; below this, single-pass is always faster.
DEFAULT_AUTO_TRIGGER = 10_000_000


def chunked_raster_apply(
    raster: xr.DataArray,
    watershed: BaseGeometry,
    fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
    *,
    chip_size: int = 2048,
    stride: Optional[int] = None,
    kernel_pad: int = 0,
    auto_trigger_size: int = DEFAULT_AUTO_TRIGGER,
    fill_value: float = np.nan,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> xr.DataArray:
    """Apply ``fn`` to ``raster`` chunk-by-chunk, mask to ``watershed``, stitch.

    Parameters
    ----------
    raster
        2-D xarray DataArray with ``y, x`` dims.  Must have a CRS / transform
        accessible via ``rio`` accessor or via uniform x/y coordinates.
    watershed
        Shapely polygon in the same CRS as the raster.  Pixels outside the
        polygon end up as ``fill_value``.
    fn
        ``(chip_arr: np.ndarray, chip_mask: np.ndarray) -> np.ndarray``.
        The chip mask carries True where the pixel is inside the watershed;
        ``fn`` may use it (e.g. to skip computation outside) or ignore it.
        Must return an array with the same shape as ``chip_arr``.
    chip_size
        Chip side length in pixels.  Larger → fewer Python iterations but
        higher peak memory per chip.  2048 is a good default (~16 MB float32).
    stride
        Distance between chip origins.  Defaults to
        ``chip_size - 2*kernel_pad`` so adjacent chips' interior regions
        align exactly (no gap, no overlap on the kept region).
    kernel_pad
        Number of border pixels per chip whose result is invalid because the
        neighborhood extends outside the chip.  Use 0 for pure pixel ops
        (band math, NDWI), 1 for 3×3 kernels (slope, aspect, curvature).
    auto_trigger_size
        If ``raster.size`` is at or below this, run ``fn`` once on the whole
        array (single-pass path — zero overhead).
    fill_value
        Value pasted into the output for pixels outside the watershed.
        Default NaN so subsequent operations propagate cleanly.
    progress_cb
        Optional ``(chip_index, total_chips) -> None`` callback for progress
        UIs.  ``total_chips`` is the sampler's ``__len__`` upper bound.

    Returns
    -------
    xarray DataArray with the same dims, coords, and attrs as ``raster``,
    containing the stitched result.
    """
    # ------------------------------------------------------------------
    # Single-pass fast path for small rasters.
    # ------------------------------------------------------------------
    if raster.size <= auto_trigger_size:
        log.debug("chunked_raster_apply: raster size %d ≤ %d — single-pass path",
                  raster.size, auto_trigger_size)
        # Full-basin mask using existing rasterio utility.
        from rasterio.features import geometry_mask
        from aihydro_data.sampling.catchment import _raster_transform_and_crs
        transform, _ = _raster_transform_and_crs(raster)
        mask = geometry_mask(
            [watershed],
            out_shape=(raster.sizes.get("y", raster.shape[-2]),
                       raster.sizes.get("x", raster.shape[-1])),
            transform=transform,
            invert=True,
            all_touched=False,
        )
        result = fn(raster.values, mask)
        out = np.where(mask, result, fill_value)
        return xr.DataArray(
            out, dims=raster.dims, coords=raster.coords,
            attrs={**raster.attrs, "chunked_applied": False},
            name=raster.name,
        )

    # ------------------------------------------------------------------
    # Chunked path.
    # ------------------------------------------------------------------
    log.info("chunked_raster_apply: raster size %d > %d — chunking (chip=%d, "
             "stride=%s, kernel_pad=%d)",
             raster.size, auto_trigger_size, chip_size, stride, kernel_pad)

    sampler = CatchmentGridSampler(
        raster=raster, watershed=watershed,
        chip_size=chip_size, stride=stride, kernel_pad=kernel_pad,
    )

    out_arr = np.full(raster.shape, fill_value, dtype=np.float32)
    total = len(sampler)  # upper bound
    n_processed = 0

    for chip in sampler:
        win = chip.window
        # Slice the raster for this chip.
        row_slice = slice(win.row_off, win.row_off + win.height)
        col_slice = slice(win.col_off, win.col_off + win.width)
        chip_arr = raster.values[row_slice, col_slice]

        # Run user function.
        result = fn(chip_arr, chip.mask)
        if result.shape != chip_arr.shape:
            raise ValueError(
                f"chunked_raster_apply: fn returned shape {result.shape} "
                f"for input shape {chip_arr.shape} — must match."
            )

        # When kernel_pad > 0, drop the invalid border and paste only the
        # interior.  This means adjacent chips' interior regions tile the
        # output exactly (no gap, no overlap on the kept region).
        if kernel_pad > 0:
            cy, cx = chip.core_slice
            interior_result = result[cy, cx]
            interior_mask = chip.mask[cy, cx]
            # Compute the destination slice — drop kernel_pad from each side.
            dst_row = slice(win.row_off + kernel_pad,
                            win.row_off + win.height - kernel_pad)
            dst_col = slice(win.col_off + kernel_pad,
                            win.col_off + win.width - kernel_pad)
            # Apply mask + paste.  np.where(mask, value, current_value) lets
            # later chips overwrite earlier ones in any overlap region, which
            # is harmless when kernel_pad is chosen correctly.
            existing = out_arr[dst_row, dst_col]
            out_arr[dst_row, dst_col] = np.where(
                interior_mask, interior_result, existing
            )
        else:
            existing = out_arr[row_slice, col_slice]
            out_arr[row_slice, col_slice] = np.where(
                chip.mask, result, existing
            )

        n_processed += 1
        if progress_cb is not None:
            progress_cb(n_processed, total)

    log.info("chunked_raster_apply: processed %d chips", n_processed)

    return xr.DataArray(
        out_arr, dims=raster.dims, coords=raster.coords,
        attrs={
            **raster.attrs,
            "chunked_applied": True,
            "chunked_n_chips": n_processed,
            "chunked_chip_size": chip_size,
            "chunked_kernel_pad": kernel_pad,
        },
        name=raster.name,
    )
