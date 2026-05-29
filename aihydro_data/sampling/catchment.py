"""
Catchment-bounded raster chip samplers.

Two samplers, both accept the same constructor signature:

  - :class:`CatchmentGridSampler` — yields a deterministic grid of chips
    that intersect the watershed polygon.  Used for chunked-apply over a
    whole basin (slope/aspect/NDWI/CN/etc.) and for multi-level tile
    pyramid generation.
  - :class:`CatchmentRandomSampler` — yields ``n_samples`` chips drawn
    uniformly from positions inside the watershed.  Used for spatial-ML
    training set construction (deferred consumer; class ships now so the
    contract is locked in).

Both yield :class:`ChipInfo` named tuples:

    ChipInfo(window, bounds, mask, ix, iy, core_slice)

where:
  - ``window`` is a :class:`rasterio.windows.Window` for slicing the source
    raster.
  - ``bounds`` is the chip's geographic extent ``(minx, miny, maxx, maxy)``
    in the source CRS.
  - ``mask`` is a boolean ``np.ndarray`` of shape ``(window.height,
    window.width)`` where True = pixel falls inside the watershed polygon.
  - ``ix``, ``iy`` are the chip's column/row index in the grid (random
    sampler sets these to -1).
  - ``core_slice`` is a tuple of slices ``(slice_y, slice_x)`` that picks
    the chip's interior region (excluding the ``kernel_pad`` border used
    by stitching).  Identity slice when ``kernel_pad == 0``.

Reuses :func:`rasterio.features.geometry_mask` — same call as
``aihydro-tools/ai_hydro/analysis/twi.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np
import xarray as xr
from rasterio.features import geometry_mask
from rasterio.transform import Affine, from_bounds
from rasterio.windows import Window
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChipInfo:
    """A single sampled chip — see module docstring."""
    window: Window
    bounds: tuple[float, float, float, float]
    mask: np.ndarray
    ix: int
    iy: int
    core_slice: tuple[slice, slice]


def _raster_transform_and_crs(raster: xr.DataArray) -> tuple[Affine, object | None]:
    """Pull the affine transform + CRS off a DataArray.

    Prefers ``rio`` accessor (rioxarray); falls back to building one from
    the ``x``/``y`` coordinate arrays when rioxarray isn't attached.
    """
    transform = None
    crs = None
    try:
        rio = raster.rio  # type: ignore[attr-defined]
        transform = rio.transform()
        crs = rio.crs
    except Exception:
        pass
    if transform is None:
        # Build from coordinates assuming uniform spacing.
        x = raster.x.values
        y = raster.y.values
        dx = float(x[1] - x[0]) if x.size >= 2 else 1.0
        dy = float(y[1] - y[0]) if y.size >= 2 else -1.0
        # rasterio's Affine: (a=dx, b=0, c=x0, d=0, e=dy, f=y0) where x0/y0
        # are pixel-corner coordinates (rasterio convention).
        x0 = float(x[0]) - dx / 2
        y0 = float(y[0]) - dy / 2
        transform = Affine(dx, 0, x0, 0, dy, y0)
    return transform, crs


class _BaseSampler:
    """Shared constructor + helpers for grid/random samplers."""

    def __init__(
        self,
        raster: xr.DataArray,
        watershed: BaseGeometry,
        *,
        chip_size: int = 2048,
        stride: Optional[int] = None,
        kernel_pad: int = 0,
    ) -> None:
        if chip_size <= 0:
            raise ValueError(f"chip_size must be positive, got {chip_size}")
        if kernel_pad < 0:
            raise ValueError(f"kernel_pad must be >= 0, got {kernel_pad}")
        if kernel_pad * 2 >= chip_size:
            raise ValueError(
                f"kernel_pad ({kernel_pad}) too large for chip_size ({chip_size}); "
                "result would have no interior."
            )

        self.raster = raster
        self.watershed = watershed
        self.chip_size = int(chip_size)
        self.kernel_pad = int(kernel_pad)
        # Default stride: chip - 2*pad so chips overlap by exactly the border
        # that gets dropped during stitching.  When pad=0, stride==chip (no
        # overlap, no waste).
        self.stride = int(stride) if stride is not None else self.chip_size - 2 * self.kernel_pad

        self.height = int(raster.sizes.get("y", raster.shape[-2]))
        self.width = int(raster.sizes.get("x", raster.shape[-1]))
        self.transform, self.crs = _raster_transform_and_crs(raster)
        # Affine columns: a=pixel-width, e=pixel-height (negative when y
        # decreases northward, which is the rioxarray default).
        self._dx = abs(self.transform.a)
        self._dy = abs(self.transform.e)
        # x0/y0: coordinates of the top-left pixel CORNER.
        self._x0 = self.transform.c
        self._y0 = self.transform.f

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def _window_bounds(self, win: Window) -> tuple[float, float, float, float]:
        """Convert a Window to geographic bounds (minx, miny, maxx, maxy)."""
        col0 = win.col_off
        row0 = win.row_off
        col1 = col0 + win.width
        row1 = row0 + win.height
        x_left = self._x0 + col0 * self._dx
        x_right = self._x0 + col1 * self._dx
        # y axis convention: row 0 is north → y_top corresponds to row0.
        # Transform.e is typically negative; we used abs(_dy) so we subtract.
        y_top = self._y0 - row0 * self._dy if self.transform.e < 0 else self._y0 + row0 * self._dy
        y_bot = self._y0 - row1 * self._dy if self.transform.e < 0 else self._y0 + row1 * self._dy
        miny, maxy = sorted((y_top, y_bot))
        minx, maxx = sorted((x_left, x_right))
        return (minx, miny, maxx, maxy)

    def _window_transform(self, win: Window) -> Affine:
        """Affine for a sub-window — used by geometry_mask."""
        bounds = self._window_bounds(win)
        return from_bounds(*bounds, width=win.width, height=win.height)

    def _make_chip_mask(self, win: Window) -> np.ndarray:
        """Boolean mask: True where the pixel falls inside the watershed."""
        transform = self._window_transform(win)
        # geometry_mask returns True for pixels NOT in the geometry by default
        # (invert=False, all_touched=False).  We want True INSIDE → invert=True.
        return geometry_mask(
            [self.watershed],
            out_shape=(int(win.height), int(win.width)),
            transform=transform,
            invert=True,
            all_touched=False,
        )

    def _intersects_watershed(self, win: Window) -> bool:
        """Quick rejection: does this chip's bbox intersect the watershed at all?"""
        from shapely.geometry import box
        bounds = self._window_bounds(win)
        return self.watershed.intersects(box(*bounds))

    def _core_slice(self) -> tuple[slice, slice]:
        """The interior of a chip (excluding the kernel-pad border)."""
        if self.kernel_pad == 0:
            return (slice(None), slice(None))
        p = self.kernel_pad
        return (slice(p, -p), slice(p, -p))


class CatchmentGridSampler(_BaseSampler):
    """Deterministic grid of chips covering a watershed.

    Examples
    --------
    >>> sampler = CatchmentGridSampler(dem, basin_poly,
    ...                                chip_size=2048, kernel_pad=1)
    >>> for chip in sampler:
    ...     dem_chip = dem.isel(y=slice(chip.window.row_off,
    ...                                 chip.window.row_off + chip.window.height),
    ...                          x=slice(chip.window.col_off,
    ...                                  chip.window.col_off + chip.window.width))
    ...     slope_chip = compute_slope_3x3(dem_chip.values)
    ...     # core_slice drops the 1-px border that has an incomplete kernel
    ...     output[chip.window.toslices()] = slope_chip[chip.core_slice]
    """

    def __iter__(self) -> Iterator[ChipInfo]:
        # Skip-rate counter for the warning at the end.
        n_total = 0
        n_emitted = 0

        # Stride determines distance between chip ORIGINS, not chip extents.
        # For pad=0, stride==chip → no overlap, no waste.  For pad=1 and
        # chip=256, stride=254 → 2-px overlap which exactly covers the
        # dropped 1-px border on each side.
        step = self.stride
        # Iterate over chip origins.  Last chip in each dimension is
        # snapped back to the raster edge so we don't lose the trailing
        # strip when (height - chip_size) isn't a multiple of stride.
        y_starts = list(range(0, max(self.height - self.chip_size, 0) + 1, step))
        if not y_starts or y_starts[-1] + self.chip_size < self.height:
            y_starts.append(max(self.height - self.chip_size, 0))
        x_starts = list(range(0, max(self.width - self.chip_size, 0) + 1, step))
        if not x_starts or x_starts[-1] + self.chip_size < self.width:
            x_starts.append(max(self.width - self.chip_size, 0))

        core = self._core_slice()

        for iy, row0 in enumerate(y_starts):
            for ix, col0 in enumerate(x_starts):
                n_total += 1
                # Last chip in a row/col may be smaller if raster < chip_size.
                h = min(self.chip_size, self.height - row0)
                w = min(self.chip_size, self.width - col0)
                if h <= 0 or w <= 0:
                    continue
                win = Window(col0, row0, w, h)
                if not self._intersects_watershed(win):
                    continue
                mask = self._make_chip_mask(win)
                if not mask.any():
                    continue
                n_emitted += 1
                yield ChipInfo(
                    window=win,
                    bounds=self._window_bounds(win),
                    mask=mask,
                    ix=ix,
                    iy=iy,
                    core_slice=core,
                )

        log.debug(
            "CatchmentGridSampler: emitted %d / %d candidate chips (%.0f%% pruned)",
            n_emitted, n_total,
            (1 - n_emitted / max(n_total, 1)) * 100,
        )

    def __len__(self) -> int:
        # True upper bound — must match the iteration which appends a
        # snap-to-edge chip when the trailing strip isn't covered.
        def _starts(extent: int) -> int:
            base = list(range(0, max(extent - self.chip_size, 0) + 1, self.stride))
            if not base or base[-1] + self.chip_size < extent:
                base.append(max(extent - self.chip_size, 0))
            # Dedupe in case the snap chip lands on the last regular start.
            return len(set(base))
        return _starts(self.height) * _starts(self.width)


class CatchmentRandomSampler(_BaseSampler):
    """Yield ``n_samples`` chips drawn uniformly from positions inside the
    watershed.  Useful for spatial-ML training-set construction.

    Reproducible via the ``seed`` parameter.
    """

    def __init__(
        self,
        raster: xr.DataArray,
        watershed: BaseGeometry,
        *,
        n_samples: int,
        chip_size: int = 2048,
        kernel_pad: int = 0,
        max_attempts_per_chip: int = 20,
        seed: int | None = None,
    ) -> None:
        super().__init__(raster, watershed,
                         chip_size=chip_size, stride=chip_size,
                         kernel_pad=kernel_pad)
        if n_samples <= 0:
            raise ValueError(f"n_samples must be positive, got {n_samples}")
        self.n_samples = int(n_samples)
        self.max_attempts_per_chip = int(max_attempts_per_chip)
        self._rng = np.random.default_rng(seed)

    def __iter__(self) -> Iterator[ChipInfo]:
        core = self._core_slice()
        max_row = max(self.height - self.chip_size, 0)
        max_col = max(self.width - self.chip_size, 0)

        for _ in range(self.n_samples):
            for _attempt in range(self.max_attempts_per_chip):
                row0 = int(self._rng.integers(0, max_row + 1))
                col0 = int(self._rng.integers(0, max_col + 1))
                h = min(self.chip_size, self.height - row0)
                w = min(self.chip_size, self.width - col0)
                win = Window(col0, row0, w, h)
                if not self._intersects_watershed(win):
                    continue
                mask = self._make_chip_mask(win)
                if not mask.any():
                    continue
                yield ChipInfo(
                    window=win,
                    bounds=self._window_bounds(win),
                    mask=mask,
                    ix=-1, iy=-1,
                    core_slice=core,
                )
                break
            else:
                log.debug("CatchmentRandomSampler: gave up on a chip after %d attempts",
                          self.max_attempts_per_chip)

    def __len__(self) -> int:
        return self.n_samples
