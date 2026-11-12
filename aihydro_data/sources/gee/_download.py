"""GEE raster download helpers (shared by fetch_raster, multiband and index paths).

Extracted from sources/gee.py to keep each file under ~600 lines while
preserving exactly the same behaviour. Consumed by Backend via _DownloadMixin.
"""
from __future__ import annotations

import logging
from typing import Any

from aihydro_data.exceptions import SourceUnavailable

log = logging.getLogger(__name__)


def _attach_note(da: Any, note: str) -> Any:
    """Append a caveat to ``da.attrs['aihydro_notes']``.

    The fetch pipeline harvests this list into ``FetchResult.notes`` so
    backend-level degradations (e.g. a failed polygon clip) surface to the
    user/agent instead of disappearing into debug logs.
    """
    try:
        notes = list(da.attrs.get("aihydro_notes", []))
        notes.append(note)
        da.attrs["aihydro_notes"] = notes
    except Exception:  # attrs not writable — never let a note break a fetch
        log.warning("Could not attach note to result attrs: %s", note)
    return da


# ── Request-size model for ``ee.Image.getDownloadURL`` ────────────────────────
# Derived from how EE sizes a request, not fitted to an observation:
#
# * EE rejects a request whose uncompressed raster
#   (cols x rows x sum(bytes per band)) exceeds 50,331,648 bytes (48 MiB).
#   The limit is on raw pixel bytes, so base64 / GeoTIFF container overhead is
#   not part of it (the GeoTIFF header is a few KB and the response is not
#   base64-expanded before the check).
# * With no ``crs`` argument the export grid is EPSG:4326 and EE converts the
#   requested ``scale`` (metres) to degrees with the equatorial radius,
#   1 deg = 2*pi*6378137/360 = 111,319.49 m, in BOTH axes. There is no
#   cos(latitude) term, so a degree-box holds (111,319.49/scale)^2 pixels per
#   square degree at every latitude. A true-ground-area estimate
#   (lon_km * cos(lat) ...) therefore under-counts columns by cos(lat); at
#   47 N that is 0.68, i.e. 1.47x too few pixels. This was the root cause of
#   the DEMO-1 F10a failure.
# * Bytes per pixel: EE exports every band as the widest type in the image and
#   server-side arithmetic (scale/offset, ``expression`` indices, medians of
#   rescaled bands) yields double precision, so 8 B/band is the safe upper
#   bound; 4 (float32), 2 (uint16) or 1 (byte) only when a caller knows the
#   type. The old code assumed 4.
# * The region is snapped outward to the pixel grid: at most +1 column and +1
#   row, counted in the estimate.
# * The remaining safety factor covers what the model cannot see (projection
#   of a non-4326 source, EE rounding). It is a stated margin, and the bounded
#   retry in ``_download_image_array`` is the backstop when it is not enough.
EE_REQUEST_LIMIT_BYTES = 50_331_648
EE_METRES_PER_DEGREE = 2 * 3.141592653589793 * 6_378_137.0 / 360.0   # 111,319.49
EE_DEFAULT_BYTES_PER_PIXEL = 8
EE_SAFETY_FACTOR = 1.25
EE_MAX_RETRIES = 3
EE_RETRY_FACTOR = 1.25


def estimate_request_bytes(
    bounds: tuple[float, float, float, float],
    scale_m: float,
    n_bands: int,
    bytes_per_pixel: int = EE_DEFAULT_BYTES_PER_PIXEL,
) -> int:
    """Upper-model size in bytes of a ``getDownloadURL`` request (see above).

    *bounds* is ``(minx, miny, maxx, maxy)`` in degrees (EPSG:4326).
    """
    import math
    minx, miny, maxx, maxy = bounds
    cols = math.ceil((maxx - minx) * EE_METRES_PER_DEGREE / scale_m) + 1
    rows = math.ceil((maxy - miny) * EE_METRES_PER_DEGREE / scale_m) + 1
    return int(cols * rows * max(int(n_bands), 1) * int(bytes_per_pixel))


def min_scale_for_budget(
    bounds: tuple[float, float, float, float],
    n_bands: int,
    bytes_per_pixel: int = EE_DEFAULT_BYTES_PER_PIXEL,
    safety: float = EE_SAFETY_FACTOR,
) -> int:
    """Smallest whole-metre scale whose estimated request, times *safety*,
    fits EE's limit."""
    import math
    minx, miny, maxx, maxy = bounds
    w_m = (maxx - minx) * EE_METRES_PER_DEGREE
    h_m = (maxy - miny) * EE_METRES_PER_DEGREE
    px_budget = EE_REQUEST_LIMIT_BYTES / (safety * max(int(n_bands), 1) * bytes_per_pixel)
    s = max(1, math.ceil(math.sqrt(max(w_m * h_m, 1.0) / px_budget)))
    while estimate_request_bytes(bounds, s, n_bands, bytes_per_pixel) * safety > EE_REQUEST_LIMIT_BYTES:
        s += 1
    return s


def is_ee_size_error(exc: BaseException) -> tuple[bool, int | None]:
    """``(True, reported_bytes_or_None)`` if *exc* is EE's request-too-large error."""
    import re
    msg = str(exc)
    low = msg.lower()
    if "request size" in low and "must be less than or equal to" in low:
        m = re.search(r"request size \((\d+) bytes\)", msg, re.I)
        return True, int(m.group(1)) if m else None
    return False, None


class _DownloadMixin:
    """Mixin carrying all GEE raster-download helpers.

    Inherited by Backend (sources/gee/__init__.py) so the public API is
    unchanged; this file just houses the implementation.
    """

    @staticmethod
    def _open_geotiff(tmp: str) -> Any:
        """Open a GeoTIFF as a CRS-aware ``xr.DataArray``, keeping the band dim.

        Single-band products keep a length-1 ``band`` dim (callers squeeze it);
        multi-band composites keep all bands. Loaded eagerly so the temp file
        can be deleted immediately.

        Uses ``rioxarray.open_rasterio(masked=True)`` which correctly converts
        the GeoTIFF nodata value (whatever it is — NaN, -9999, or not declared)
        into IEEE-754 NaN. This is essential for GEE exports where clipped
        (out-of-polygon) pixels are stored as a nodata sentinel rather than NaN.
        """
        import numpy as np
        import xarray as xr
        try:
            import rioxarray
            da = rioxarray.open_rasterio(tmp, masked=True).load()
            return da
        except ImportError:
            pass
        # rioxarray not available — fall back to rasterio + manual nodata mask.
        import rasterio
        with rasterio.open(tmp) as src:
            t = src.transform
            lats = np.array([t.f + i * t.e for i in range(src.height)])
            lons = np.array([t.c + j * t.a for j in range(src.width)])
            arr = src.read().astype("float64")  # (band, y, x)
            nodata = src.nodata
            if nodata is not None:
                arr = np.where(arr == nodata, np.nan, arr)
            for sentinel in (-9999.0, 9999.0):
                if nodata != sentinel:
                    arr = np.where(arr == sentinel, np.nan, arr)
        return xr.DataArray(
            arr, dims=["band", "y", "x"],
            coords={"band": np.arange(1, arr.shape[0] + 1),
                    "y": lats, "x": lons},
        )

    def _download_image_array(
        self,
        image: Any,
        roi: Any,
        roi_geojson: Any,
        gee_bands: list[str],
        scale_m: float,
        *,
        native_resolution: bool = False,
        label: str = "GEE",
        bytes_per_pixel: int = EE_DEFAULT_BYTES_PER_PIXEL,
        max_retries: int = EE_MAX_RETRIES,
    ) -> tuple[Any, float]:
        """Unified GEE raster download → ``(xr.DataArray, effective_scale_m)``.

        ONE strategy shared by ``fetch_raster``, ``fetch_multiband_composite``
        and ``fetch_index_composite``:

        - default: coarsen ``scale_m`` up front with the request-size model
          (module header) so a single ``getDownloadURL`` request stays under
          EE's 48 MiB cap with a stated safety margin; if EE still answers
          with its size error, coarsen by ``EE_RETRY_FACTOR`` (or the factor
          EE's reported size implies, if larger) and retry, at most
          *max_retries* times. Every attempt is recorded in
          ``da.attrs["aihydro_upstream"]["gee_export"]``; exhausting the
          retries raises ``GEE_REQUEST_TOO_LARGE``. Never truncates.
        - ``native_resolution=True``: keep the native scale and split the AOI
          into a grid of cap-sized tiles, download each, and mosaic — full
          resolution at the cost of N round-trips.

        After download, pixels outside the ROI polygon are forced to NaN using
        ``rioxarray.clip`` regardless of what nodata value GEE wrote into the
        GeoTIFF header. This is the authoritative polygon-mask backstop.

        Returns a DataArray with a ``band`` dim (length = ``len(gee_bands)``).
        """
        import os
        import tempfile
        import urllib.request

        bands = list(gee_bands)
        requested = float(scale_m)
        record: dict[str, Any] = {
            "requested_scale_m": requested,
            "native_resolution": bool(native_resolution),
            "ee_limit_bytes": EE_REQUEST_LIMIT_BYTES,
            "bytes_per_pixel_assumed": int(bytes_per_pixel),
            "safety_factor": EE_SAFETY_FACTOR,
            "max_retries": int(max_retries),
            "attempts": [],
        }

        def _finish(da: Any, final: float) -> tuple[Any, float]:
            record["final_scale_m"] = float(final)
            try:
                up = dict(da.attrs.get("aihydro_upstream", {}) or {})
                up["gee_export"] = record
                da.attrs["aihydro_upstream"] = up
            except Exception:
                log.warning("Could not attach GEE export record to result attrs.")
            if final != requested:
                _attach_note(
                    da,
                    f"{label}: exported at {final:g} m, not the requested "
                    f"{requested:g} m (request-size limit; "
                    f"{len(record['attempts'])} attempt(s) recorded in "
                    "upstream['gee_export']). Pass native_resolution=True for "
                    "the requested scale via tiling.",
                )
            return da, final

        if native_resolution:
            da = self._download_tiled(image, roi_geojson, bands, scale_m, label=label,
                                      bytes_per_pixel=bytes_per_pixel)
            da = self._clip_to_polygon(da, roi_geojson)
            record["planned_scale_m"] = requested
            return _finish(da, scale_m)

        scale_m = self._coarsen_scale_for_budget(
            None, roi_geojson, n_bands=len(bands), scale_m=scale_m, label=label,
            bytes_per_pixel=bytes_per_pixel,
        )
        record["planned_scale_m"] = float(scale_m)
        bounds = self._roi_bounds(None, roi_geojson)

        for attempt in range(1, int(max_retries) + 2):
            est = (estimate_request_bytes(bounds, scale_m, len(bands), bytes_per_pixel)
                   if bounds else None)
            entry: dict[str, Any] = {
                "attempt": attempt, "scale_m": float(scale_m),
                "estimated_bytes": est,
            }
            record["attempts"].append(entry)
            params = {"region": roi, "scale": scale_m, "format": "GEO_TIFF"}
            if bands:
                params["bands"] = bands
            try:
                url = image.getDownloadURL(params)
            except Exception as exc:
                is_size, reported = is_ee_size_error(exc)
                if not is_size:
                    entry["outcome"] = f"error:{type(exc).__name__}"
                    raise
                entry["outcome"] = "ee_size_error"
                entry["ee_reported_bytes"] = reported
                if attempt > max_retries:
                    from aihydro_data.exceptions import SourceUnavailable
                    raise SourceUnavailable(
                        code="GEE_REQUEST_TOO_LARGE",
                        message=(
                            f"{label}: Earth Engine still rejected the request as "
                            f"too large after {attempt} attempt(s) "
                            f"(last scale {scale_m:g} m): {exc}"
                        ),
                        recovery=(
                            "Pass a coarser explicit scale, shrink the AOI, or "
                            "use native_resolution=True to tile the request."
                        ),
                        details=record,
                    ) from exc
                factor = EE_RETRY_FACTOR
                if reported:
                    # bytes scale with 1/scale^2; ask for 5 % under the cap
                    factor = max(factor, (reported / (EE_REQUEST_LIMIT_BYTES * 0.95)) ** 0.5)
                new_scale = float(int(scale_m * factor) + 1)
                log.warning("%s: EE size error at %g m; retry %d/%d at %g m.",
                            label, scale_m, attempt, max_retries, new_scale)
                scale_m = new_scale
                continue
            entry["outcome"] = "ok"
            tmp = tempfile.mktemp(suffix=".tif")
            try:
                urllib.request.urlretrieve(url, tmp)  # noqa: S310
                da = self._open_geotiff(tmp)
                da = self._clip_to_polygon(da, roi_geojson)
                return _finish(da, scale_m)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        raise AssertionError("unreachable")  # loop returns or raises

    @staticmethod
    def _clip_to_polygon(da: Any, roi_geojson: Any) -> Any:
        """Force pixels outside *roi_geojson* to NaN using rioxarray.clip.

        This is the authoritative polygon-mask step that runs after every
        GEE download, regardless of what nodata value GEE embedded in the
        GeoTIFF header. Without it, GEE's clipped exports look rectangular
        because the nodata sentinel (e.g. -9999 or 0) isn't decoded as NaN
        when the header lacks an explicit nodata declaration.

        Silently returns *da* unchanged if rioxarray is unavailable or if the
        ROI is already a bounding box (polygon with 4/5 vertices only), since
        in that case the full extent IS the valid area.
        """
        if roi_geojson is None:
            return da
        try:
            from shapely.geometry import shape as _shape
            from shapely.geometry.polygon import Polygon as _Polygon
            geom = _shape(roi_geojson)
            coords = list(geom.exterior.coords) if isinstance(geom, _Polygon) else []
            if len(coords) <= 5:
                return da
        except Exception as exc:
            log.warning(
                "_clip_to_polygon: could not parse ROI GeoJSON (%s) — "
                "returning bbox-extent raster without polygon mask.", exc,
            )
            return _attach_note(
                da,
                "Polygon mask SKIPPED (ROI parse failed) — raster covers the "
                "full bounding box, including pixels outside the polygon.",
            )

        try:
            import rioxarray  # noqa: F401 — needed for .rio accessor

            if not da.rio.crs:
                da = da.rio.write_crs("EPSG:4326")

            clipped = da.rio.clip([roi_geojson], crs="EPSG:4326",
                                  drop=False, all_touched=False)
            return clipped
        except Exception as exc:
            log.warning(
                "_clip_to_polygon: rioxarray clip failed (%s) — "
                "returning bbox-extent raster without polygon mask.", exc,
            )
            return _attach_note(
                da,
                f"Polygon mask FAILED ({type(exc).__name__}) — raster covers the "
                "full bounding box, including pixels outside the polygon. "
                "Install/upgrade rioxarray for exact polygon clipping.",
            )

    def _download_tiled(
        self,
        image: Any,
        roi_geojson: Any,
        gee_bands: list[str],
        scale_m: float,
        *,
        label: str = "GEE",
        bytes_per_pixel: int = EE_DEFAULT_BYTES_PER_PIXEL,
    ) -> Any:
        """Download ``gee_bands`` at native *scale_m* over an arbitrarily large
        AOI by splitting it into a grid of getDownloadURL-sized tiles and
        mosaicking. No coarsening, no ~48 MB ceiling — N round-trips instead.
        """
        import math
        import os
        import tempfile
        import urllib.request

        import ee
        from shapely.geometry import shape as _shape

        bands = list(gee_bands)
        minx, miny, maxx, maxy = _shape(roi_geojson).bounds
        # Same size model as the single-request path (degree-box pixel count,
        # no cos(lat), widest band type, safety margin): tiles are sized so the
        # modelled bytes of EACH tile stay under the cap.
        est_bytes = estimate_request_bytes(
            (minx, miny, maxx, maxy), scale_m, len(bands), bytes_per_pixel,
        )
        bbox_km2 = ((maxx - minx) * (maxy - miny)
                    * (EE_METRES_PER_DEGREE / 1000.0) ** 2)
        n_tiles = max(1, math.ceil(est_bytes * EE_SAFETY_FACTOR / EE_REQUEST_LIMIT_BYTES))
        aspect = max((maxx - minx) / max(maxy - miny, 1e-9), 1e-9)
        n_cols = max(1, math.ceil(math.sqrt(n_tiles * aspect)))
        n_rows = max(1, math.ceil(n_tiles / n_cols))
        log.warning(
            "%s: NATIVE-RES tiled download — degree-box ~%.0f km² at %d m (%d band%s) "
            "needs %d tile(s) (%d×%d grid). Downloading at full resolution.",
            label, bbox_km2, int(scale_m), len(bands),
            "" if len(bands) == 1 else "s", n_cols * n_rows, n_cols, n_rows,
        )

        dx = (maxx - minx) / n_cols
        dy = (maxy - miny) / n_rows

        tiles = []
        tmps = []
        failed_tiles: list[dict[str, Any]] = []
        try:
            for r in range(n_rows):
                for c in range(n_cols):
                    tx0 = minx + c * dx
                    tx1 = minx + (c + 1) * dx if c < n_cols - 1 else maxx
                    ty0 = miny + r * dy
                    ty1 = miny + (r + 1) * dy if r < n_rows - 1 else maxy
                    tile_geom = ee.Geometry.Rectangle([tx0, ty0, tx1, ty1])
                    params = {"region": tile_geom, "scale": scale_m,
                              "format": "GEO_TIFF"}
                    if bands:
                        params["bands"] = bands
                    try:
                        url = image.getDownloadURL(params)
                        tmp = tempfile.mktemp(suffix=".tif")
                        tmps.append(tmp)
                        urllib.request.urlretrieve(url, tmp)  # noqa: S310
                        tiles.append(self._open_geotiff(tmp))
                    except Exception as exc:
                        # Keep going so the error names every failed tile, but
                        # never merge a partial set: a skipped tile is a hole.
                        log.warning(
                            "%s: tile (r%d,c%d) failed: %s", label, r, c, exc,
                        )
                        failed_tiles.append({
                            "row": r, "col": c,
                            "bounds_wgs84": [tx0, ty0, tx1, ty1],
                            "error": f"{type(exc).__name__}: {exc}",
                        })

            if failed_tiles:
                shown = failed_tiles[:10]
                names = ", ".join(f"(r{t['row']},c{t['col']})" for t in shown)
                if len(failed_tiles) > len(shown):
                    names += f", ... and {len(failed_tiles) - len(shown)} more (see details)"
                raise SourceUnavailable(
                    code="GEE_TILE_DOWNLOAD_FAILED",
                    message=(
                        f"{label}: {len(failed_tiles)} of {n_rows * n_cols} "
                        f"native-resolution tile(s) failed to download: {names}. "
                        "The mosaic would contain holes, so no raster is returned."
                    ),
                    recovery=(
                        "Retry the request, shrink the AOI, or drop "
                        "native_resolution to accept a coarser single-request "
                        "download."
                    ),
                    details={
                        "failed_tiles": failed_tiles,
                        "n_tiles": n_rows * n_cols,
                        "n_tiles_ok": len(tiles),
                    },
                )

            if len(tiles) == 1:
                return tiles[0]
            from rioxarray.merge import merge_arrays
            return merge_arrays(tiles)
        finally:
            for tmp in tmps:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def _roi_bounds(geometry: Any, roi_geojson: Any) -> Any:
        """``(minx, miny, maxx, maxy)`` in degrees, or ``None`` if unavailable."""
        try:
            from shapely.geometry import shape as _shape
            geom_shp = _shape(roi_geojson) if isinstance(roi_geojson, dict) else geometry
            return tuple(geom_shp.bounds)
        except Exception:
            return None

    @staticmethod
    def _coarsen_scale_for_budget(
        geometry: Any,
        roi_geojson: Any,
        *,
        n_bands: int,
        scale_m: float,
        label: str = "GEE",
        bytes_per_pixel: int = EE_DEFAULT_BYTES_PER_PIXEL,
    ) -> float:
        """Return a (possibly coarsened) export scale whose modelled
        ``getDownloadURL`` request, times ``EE_SAFETY_FACTOR``, fits EE's
        50,331,648-byte limit. See the size-model comment at module top for the
        derivation (degree-box pixel count with no cos(lat), 8 B/band by
        default). Never refines: a scale already fine enough is returned as is.
        """
        bounds = _DownloadMixin._roi_bounds(geometry, roi_geojson)
        if bounds is None:
            return scale_m
        min_scale = min_scale_for_budget(bounds, n_bands, bytes_per_pixel)
        if min_scale > scale_m:
            log.warning(
                "%s: request at %g m (%d band%s, %d B/px) is modelled at %.1f MB "
                "against EE's %.1f MB limit; coarsening to %d m.",
                label, scale_m, n_bands, "" if n_bands == 1 else "s", bytes_per_pixel,
                estimate_request_bytes(bounds, scale_m, n_bands, bytes_per_pixel) / 1e6,
                EE_REQUEST_LIMIT_BYTES / 1e6, min_scale,
            )
            return float(min_scale)
        return scale_m
