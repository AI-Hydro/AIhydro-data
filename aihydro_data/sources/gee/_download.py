"""GEE raster download helpers (shared by fetch_raster, multiband and index paths).

Extracted from sources/gee.py to keep each file under ~600 lines while
preserving exactly the same behaviour. Consumed by Backend via _DownloadMixin.
"""
from __future__ import annotations

import logging
from typing import Any

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
    ) -> tuple[Any, float]:
        """Unified GEE raster download → ``(xr.DataArray, effective_scale_m)``.

        ONE strategy shared by ``fetch_raster``, ``fetch_multiband_composite``
        and ``fetch_index_composite``:

        - default: auto-coarsen ``scale_m`` (by band count) so a single
          ``getDownloadURL`` request stays under GEE's ~48 MB cap — never
          raises, never truncates.
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

        if native_resolution:
            da = self._download_tiled(image, roi_geojson, bands, scale_m, label=label)
            da = self._clip_to_polygon(da, roi_geojson)
            return da, scale_m

        scale_m = self._coarsen_scale_for_budget(
            None, roi_geojson, n_bands=len(bands), scale_m=scale_m, label=label,
        )
        params = {"region": roi, "scale": scale_m, "format": "GEO_TIFF"}
        if bands:
            params["bands"] = bands
        url = image.getDownloadURL(params)
        tmp = tempfile.mktemp(suffix=".tif")
        try:
            urllib.request.urlretrieve(url, tmp)  # noqa: S310
            da = self._open_geotiff(tmp)
            da = self._clip_to_polygon(da, roi_geojson)
            return da, scale_m
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

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
        lat_mid = (miny + maxy) / 2
        lon_km = (maxx - minx) * 111.32 * math.cos(math.radians(lat_mid))
        lat_km = (maxy - miny) * 111.32
        bbox_km2 = max(lon_km * lat_km, 1.0)

        budget_bytes = 50_331_648 / 2.5 / 1.1
        pixel_budget = budget_bytes / (max(len(bands), 1) * 4)
        total_px = (bbox_km2 * 1e6) / (scale_m ** 2)
        n_tiles = max(1, math.ceil(total_px / pixel_budget))
        aspect = max((maxx - minx) / max(maxy - miny, 1e-9), 1e-9)
        n_cols = max(1, math.ceil(math.sqrt(n_tiles * aspect)))
        n_rows = max(1, math.ceil(n_tiles / n_cols))
        log.warning(
            "%s: NATIVE-RES tiled download — AOI ~%.0f km² at %d m (%d band%s) "
            "needs %d tile(s) (%d×%d grid). Downloading at full resolution.",
            label, bbox_km2, int(scale_m), len(bands),
            "" if len(bands) == 1 else "s", n_cols * n_rows, n_cols, n_rows,
        )

        dx = (maxx - minx) / n_cols
        dy = (maxy - miny) / n_rows

        tiles = []
        tmps = []
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
                        log.debug("%s: tile (r%d,c%d) skipped: %s", label, r, c, exc)

            if not tiles:
                raise RuntimeError(
                    f"{label}: tiled download produced no data over the AOI."
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
    def _coarsen_scale_for_budget(
        geometry: Any,
        roi_geojson: Any,
        *,
        n_bands: int,
        scale_m: float,
        label: str = "GEE",
    ) -> float:
        """Return a (possibly coarsened) export scale that keeps GEE's
        ``getDownloadURL`` request under the ~48 MB hard limit.

        GEE's hard limit is 50,331,648 bytes (~48 MB) per request. Empirically
        GEE's internal pixel count is ~2.5× a naive bbox estimate (it uses
        equatorial-degree scale without ``cos(lat)`` for EPSG:4326 output, then
        rounds up to tile boundaries). We target our estimate to ≤ ~18 MB so
        GEE's actual request stays under the cap.
        """
        import math as _math
        try:
            from shapely.geometry import shape as _shape
            geom_shp = _shape(roi_geojson) if isinstance(roi_geojson, dict) else geometry
            minx, miny, maxx, maxy = geom_shp.bounds
            lat_mid = (miny + maxy) / 2
            lon_km = (maxx - minx) * 111.32 * _math.cos(_math.radians(lat_mid))
            lat_km = (maxy - miny) * 111.32
            bbox_km2 = max(lon_km * lat_km, 1.0)
            budget_bytes = 50_331_648 / 2.5 / 1.1   # ≈ 18 MB target
            pixel_budget = budget_bytes / (max(n_bands, 1) * 4)   # float32
            area_m2 = bbox_km2 * 1e6
            min_scale = _math.ceil(_math.sqrt(area_m2 / pixel_budget))
            if min_scale > scale_m:
                log.warning(
                    "%s: AOI ~%.0f km² at %d m (%d band%s) exceeds budget. "
                    "Auto-coarsening to %d m so GEE stays under 48 MB limit.",
                    label, bbox_km2, int(scale_m), n_bands,
                    "" if n_bands == 1 else "s", min_scale,
                )
                return float(min_scale)
        except ImportError:
            pass
        return scale_m
