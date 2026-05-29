"""
Google Earth Engine backend.

Wraps the vendored GEE auth + timeseries code from sources/_gee_vendored/.
All earthengine-api imports are lazy — this module can be imported safely
without the `gee` extra installed; it will only fail when `is_available()`
or a fetch method is actually called.

Install: pip install aihydro-data[gee]
Auth:    aihydro-data auth gee
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)


def _aggregation_to_gee(agg: AggregationMode) -> tuple[str, str]:
    """Map AggregationMode → (spatial_reducer, temporal_aggregation) for GEE."""
    if agg in ("basin_mean", "centroid"):
        return "mean", "daily"
    if agg == "basin_sum":
        return "sum", "daily"
    if agg == "raw_raster":
        return "mean", "daily"  # raw raster handled differently; spatial reducer not applied
    return "mean", "daily"


class Backend(SourceBackend):
    """GEE backend — fetches via the Earth Engine Python API."""

    source_id = "gee"

    # ── SourceBackend interface ───────────────────────────────────────────

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": ["precipitation", "temperature", "et", "ndvi", "dem", "landcover"],
            "coverage": ["global"],
            "requires_auth": ["gee"],
            "requires_extras": ["gee"],
        }

    def is_available(self) -> tuple[bool, Optional[str]]:
        from aihydro_data.sources._gee_vendored.auth import _import_ee, _credentials_found
        ok, _, err = _import_ee()
        if not ok:
            return False, (
                f"earthengine-api not importable: {err}. "
                "Run `pip install aihydro-data[gee]` then `aihydro-data auth gee`."
            )
        if not _credentials_found():
            return False, (
                "GEE credentials not found. "
                "Run `aihydro-data auth gee` to authenticate."
            )
        return True, None

    def fetch_timeseries(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
    ) -> Any:
        """Return a pd.DataFrame with columns ['date', spec.variable]."""
        self._assert_available()

        roi_geojson = self._geom_to_geojson(geometry)
        spatial_reducer, temporal_agg = _aggregation_to_gee(aggregation)

        cfg = spec.backend_config
        dataset_id = cfg.get("gee_dataset_id", spec.source_dataset_id)
        band = cfg.get("band", "")
        scale_m = float(cfg.get("scale_m", 5000))
        unit_conv = float(cfg.get("unit_conversion", 1.0))
        compute_ndvi = bool(cfg.get("compute_ndvi", False))
        ndvi_bands = cfg.get("ndvi_bands", ["B8", "B4"])  # (NIR, Red) by default
        qa_band = cfg.get("qa_band", None)
        max_cloud_pct = float(cfg.get("max_cloud_pct", 60))

        from aihydro_data.sources._retry import call_with_retry

        if compute_ndvi:
            # Sentinel-2 / Landsat NDVI: compute (NIR - Red) / (NIR + Red)
            # on-the-fly server-side, with optional QA/cloud filtering.
            result = call_with_retry(
                lambda: self._extract_computed_ndvi(
                    dataset_id=dataset_id,
                    start=start, end=end,
                    roi_geojson=roi_geojson,
                    ndvi_bands=ndvi_bands,
                    qa_band=qa_band,
                    max_cloud_pct=max_cloud_pct,
                    scale_m=scale_m,
                    spatial_reducer=spatial_reducer,
                ),
                label=f"gee.compute_ndvi({dataset_id})",
            )
        else:
            from aihydro_data.sources._gee_vendored.timeseries import extract_timeseries
            result = call_with_retry(
                lambda: extract_timeseries(
                    dataset_id=dataset_id,
                    band=band,
                    start_date=start,
                    end_date=end,
                    roi_geojson=roi_geojson,
                    spatial_reducer=spatial_reducer,
                    temporal_aggregation=temporal_agg,
                    scale_m=scale_m,
                ),
                label=f"gee.extract_timeseries({dataset_id})",
            )

        # The vendored helper swallows network/GEE errors into {"ok": False,
        # "rows": []}. That's unsafe for our pipeline — the fallback chain
        # never triggers and the user gets an empty DataFrame with no warning.
        # Promote that to an exception here so fallback can take over.
        if not result.get("ok", True):
            raise RuntimeError(
                f"GEE fetch failed for {dataset_id}/{band}: "
                f"{result.get('message', 'unknown error')}"
            )

        import pandas as pd
        # Vendored API returns rows under "rows", not "timeseries".
        rows = result.get("rows", result.get("timeseries", []))
        df = pd.DataFrame(rows)
        if df.empty:
            # Genuine empty result from GEE (e.g. ROI off the data grid).
            # Surface as an explicit error so the fallback chain triggers.
            raise RuntimeError(
                f"GEE returned 0 rows for {dataset_id}/{band} "
                f"({start}..{end}). ROI likely outside coverage."
            )

        df = df.rename(columns={"value": spec.variable})
        # Drop nulls *before* unit conversion to avoid NaN * scalar issues
        df = df.dropna(subset=[spec.variable])
        if df.empty:
            raise RuntimeError(
                f"GEE returned all-null values for {dataset_id}/{band} "
                f"({start}..{end}). Likely all pixels masked out."
            )
        if unit_conv != 1.0:
            df[spec.variable] = df[spec.variable] * unit_conv
        df["date"] = pd.to_datetime(df["date"])

        return df.reset_index(drop=True)

    def fetch_raster(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        *,
        native_resolution: bool = False,
    ) -> Any:
        """Return an xarray.DataArray clipped to geometry.

        For static products (timestep='static') this downloads a GeoTIFF via
        GEE's getDownloadURL and opens it with rasterio/xarray. Large AOIs are
        handled by the shared :meth:`_download_image_array` path: by default the
        export scale is auto-coarsened to stay under GEE's ~48 MB cap (never
        truncates); with ``native_resolution=True`` the AOI is tiled and
        mosaicked at the product's native scale.
        Temporal products are not yet supported (use aggregation='basin_mean').
        """
        self._assert_available()
        cfg = spec.backend_config

        if not cfg.get("static"):
            raise NotImplementedError(
                "GEE raster export for temporal products is not yet implemented. "
                "Use aggregation='basin_mean' to get a timeseries instead, "
                "or install aihydro-data[stac] for STAC-backed raster access."
            )

        import ee

        dataset_id = cfg.get("gee_dataset_id", spec.source_dataset_id)
        band = cfg.get("band", "")
        scale_m = float(cfg.get("scale_m", 30))
        unit_conv = float(cfg.get("unit_conversion", 1.0))

        roi_geojson = self._geom_to_geojson(geometry)
        roi = ee.Geometry(roi_geojson)
        # Some static products (e.g. Copernicus GLO-30, MERIT-DEM) are tiled
        # ImageCollections rather than a single Image. Use mosaic() to merge tiles.
        if cfg.get("gee_is_collection"):
            img = ee.ImageCollection(dataset_id).mosaic().select(band)
        else:
            img = ee.Image(dataset_id).select(band)

        da, scale_m = self._download_image_array(
            img, roi, roi_geojson, [band] if band else [], scale_m,
            native_resolution=native_resolution,
            label=f"GEE raster {spec.id}",
        )
        da = da.squeeze(drop=True)
        da.name = spec.variable
        if unit_conv != 1.0:
            da = da * unit_conv
        da.attrs["resolution_m"] = scale_m
        return da

    def fetch_multiband_composite(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        *,
        native_resolution: bool = False,
    ) -> Any:
        """Return a cloud-masked median reflectance composite as an xr.Dataset.

        Unlike ``fetch_raster`` (single static band) and ``fetch_timeseries``
        (basin-mean 1-D series), this builds a *multi-band* median composite of
        the optical sensor named in ``spec.backend_config`` over ``[start,
        end]``, applies sensor-appropriate per-pixel cloud masking server-side,
        clips to the ROI, exports a multi-band GeoTIFF, and opens it as a
        :class:`xarray.Dataset` whose ``data_vars`` are the *friendly* band
        names (``green``, ``nir``, ``swir1`` …).

        This is the raw-band fetch that lets ``compute_spectral_index`` compute
        ANY index locally — NDWI, MNDWI, NBR, NDBI, … — through the full
        routing + fallback + cache machinery, instead of relying on a
        pre-computed single-index product.

        Required ``backend_config`` keys:
            ``gee_dataset_id``  — EE ImageCollection id
            ``band_map``        — ``{friendly_name: GEE_band_id}``
        Optional:
            ``scale_m``         — export resolution (default 20)
            ``cloud_property``  — scene-level cloud % property to pre-filter on
            ``max_cloud_pct``   — threshold for ``cloud_property`` (default 60)
            ``cloud_mask``      — ``"sentinel2_scl"`` | ``"landsat_qapixel"`` | None
            ``scale_factor`` / ``offset`` — DN → reflectance conversion
        """
        self._assert_available()
        import ee
        import xarray as xr

        cfg = spec.backend_config
        dataset_id = cfg.get("gee_dataset_id", spec.source_dataset_id)
        band_map: dict[str, str] = cfg.get("band_map", {})
        if not band_map:
            raise RuntimeError(
                f"Product {spec.id!r} has no 'band_map' in backend_config; "
                "cannot build a multi-band composite."
            )
        scale_m = float(cfg.get("scale_m", 20))
        cloud_property = cfg.get("cloud_property")
        max_cloud_pct = float(cfg.get("max_cloud_pct", 60))
        cloud_mask = cfg.get("cloud_mask")
        scale_factor = float(cfg.get("scale_factor", 1.0))
        offset = float(cfg.get("offset", 0.0))

        friendly = list(band_map.keys())
        gee_bands = list(band_map.values())

        roi_geojson = self._geom_to_geojson(geometry)
        roi = ee.Geometry(roi_geojson)

        # ── Build cloud-masked median composite server-side ────────────────
        image = self._masked_median_composite(
            dataset_id, roi, start, end, gee_bands,
            cloud_property, max_cloud_pct, cloud_mask,
        ).clip(roi)

        # Unified download: auto-coarsen (default) or native-res tiling.
        raw, scale_m = self._download_image_array(
            image, roi, roi_geojson, gee_bands, scale_m,
            native_resolution=native_resolution, label="GEE multiband",
        )

        ds = xr.Dataset()
        n_band = raw.sizes.get("band", 1) if "band" in raw.dims else 1
        for i, fname in enumerate(friendly):
            if i >= n_band:
                break
            band_da = (
                raw.isel(band=i).drop_vars("band", errors="ignore")
                if "band" in raw.dims else raw
            )
            if scale_factor != 1.0 or offset != 0.0:
                band_da = band_da * scale_factor + offset
            ds[fname] = band_da

        ds.attrs["sensor"] = cfg.get("sensor", spec.id.lower())
        ds.attrs["dataset_id"] = dataset_id
        ds.attrs["resolution_m"] = scale_m   # may be auto-coarsened
        return ds

    def fetch_index_composite(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        index_name: str,
        *,
        mask_clouds: bool = True,
        native_resolution: bool = False,
    ) -> Any:
        """Compute a spectral index ENTIRELY ON GEE's servers and download only
        the single-band result as an :class:`xarray.DataArray`.

        Instead of downloading all N raw reflectance bands and computing the
        index locally (``fetch_multiband_composite`` + numpy), this:

          1. builds the cloud-masked median composite server-side,
          2. selects only the bands the index needs,
          3. converts DNs → true reflectance (``scale_factor`` / ``offset``)
             server-side — essential for sensors with a non-zero offset
             (Landsat C2 L2 uses ``-0.2``, which does NOT cancel in a
             normalized difference),
          4. evaluates the index via :meth:`ee.Image.expression` using the
             canonical friendly-band formula from
             :func:`aihydro_data.transforms.indices.gee_index_formula`,
          5. downloads the **one** resulting band.

        Because only one band crosses the wire, the AOI/resolution headroom is
        ~N× larger than the raw-band path (e.g. 10× for Sentinel-2), so far
        fewer watersheds need coarsening to stay under GEE's ~48 MB
        ``getDownloadURL`` cap.

        Raises ``ValueError`` if *index_name* has no server-side formula; the
        caller (pipeline) then falls back to the raw-band path.
        """
        self._assert_available()
        import ee
        import os
        import tempfile
        import urllib.request

        from aihydro_data.transforms.indices import gee_index_formula

        spec_formula = gee_index_formula(index_name)
        if spec_formula is None:
            raise ValueError(
                f"Index {index_name!r} has no server-side GEE formula; "
                "use fetch_multiband_composite + local compute_index instead."
            )
        formula, required_friendly = spec_formula

        cfg = spec.backend_config
        dataset_id = cfg.get("gee_dataset_id", spec.source_dataset_id)
        band_map: dict[str, str] = cfg.get("band_map", {})
        if not band_map:
            raise RuntimeError(
                f"Product {spec.id!r} has no 'band_map' in backend_config."
            )
        missing = [b for b in required_friendly if b not in band_map]
        if missing:
            raise ValueError(
                f"Product {spec.id!r} cannot supply bands {missing} required "
                f"by index {index_name!r} (has {sorted(band_map)})."
            )

        scale_m = float(cfg.get("scale_m", 20))
        cloud_property = cfg.get("cloud_property")
        max_cloud_pct = float(cfg.get("max_cloud_pct", 60))
        cloud_mask = cfg.get("cloud_mask") if mask_clouds else None
        scale_factor = float(cfg.get("scale_factor", 1.0))
        offset = float(cfg.get("offset", 0.0))

        gee_bands = [band_map[b] for b in required_friendly]

        roi_geojson = self._geom_to_geojson(geometry)
        roi = ee.Geometry(roi_geojson)

        # ── Server-side: composite → reflectance → rename → index ──────────
        composite = self._masked_median_composite(
            dataset_id, roi, start, end, gee_bands,
            cloud_property, max_cloud_pct, cloud_mask,
        )
        # DN → true reflectance (offset matters for normalized differences!)
        if scale_factor != 1.0 or offset != 0.0:
            composite = composite.multiply(scale_factor).add(offset)
        # Rename sensor-native bands to the friendly names the formula speaks.
        reflectance = composite.rename(required_friendly)
        expr_vars = {b: reflectance.select(b) for b in required_friendly}
        index_img = reflectance.expression(formula, expr_vars).rename(
            index_name.lower()
        ).clip(roi)

        band_name = index_name.lower()
        # Single-band download ⇒ ~N× more area/resolution headroom than the
        # raw-band path. The unified helper auto-coarsens (default) or tiles at
        # native scale (native_resolution=True) and reports the scale it used.
        da, scale_m = self._download_image_array(
            index_img, roi, roi_geojson, [band_name], scale_m,
            native_resolution=native_resolution,
            label=f"GEE index {index_name.upper()}",
        )
        da = da.squeeze(drop=True)
        da.name = band_name
        da.attrs["index"] = index_name.upper()
        da.attrs["sensor"] = cfg.get("sensor", spec.id.lower())
        da.attrs["dataset_id"] = dataset_id
        da.attrs["resolution_m"] = scale_m
        da.attrs["computed"] = (
            "server-side (GEE, tiled native-res)" if native_resolution
            else "server-side (GEE)"
        )
        da.attrs["formula"] = formula
        return da

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _open_geotiff(tmp: str) -> Any:
        """Open a GeoTIFF as a CRS-aware ``xr.DataArray``, keeping the band dim.

        Single-band products keep a length-1 ``band`` dim (callers squeeze it);
        multi-band composites keep all bands. Loaded eagerly so the temp file
        can be deleted immediately.

        Uses ``rioxarray.open_rasterio(masked=True)`` which correctly converts
        the GeoTIFF nodata value (whatever it is — NaN, -9999, or not declared)
        into IEEE-754 NaN.  This is essential for GEE exports where clipped
        (out-of-polygon) pixels are stored as a nodata sentinel rather than NaN.
        """
        import numpy as np
        import xarray as xr
        try:
            import rioxarray
            da = rioxarray.open_rasterio(tmp, masked=True).load()
            # rioxarray.open_rasterio returns a DataArray with a 'band' dim.
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
            # Mask nodata, including common GEE sentinels
            nodata = src.nodata
            if nodata is not None:
                arr = np.where(arr == nodata, np.nan, arr)
            # Also mask any IEEE-754 NaN already present (should be a no-op)
            # and common sentinel values GEE sometimes uses without header
            for sentinel in (-9999.0, 9999.0):
                if nodata != sentinel:
                    arr = np.where(arr == sentinel, np.nan, arr)
        return xr.DataArray(
            arr, dims=["band", "y", "x"],
            coords={"band": np.arange(1, arr.shape[0] + 1),
                    "y": lats, "x": lons},
        )

    def _download_image_array(self, image: Any, roi: Any, roi_geojson: Any,
                              gee_bands: list[str], scale_m: float, *,
                              native_resolution: bool = False,
                              label: str = "GEE") -> tuple[Any, float]:
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
        GeoTIFF header.  This is the authoritative polygon-mask backstop.

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
        GeoTIFF header.  Without it, GEE's clipped exports look rectangular
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
            # Skip clip if the ROI is already a rectangular bbox (5 vertices
            # after closing = the export grid covers exactly the valid area).
            coords = list(geom.exterior.coords) if isinstance(geom, _Polygon) else []
            if len(coords) <= 5:
                return da
        except Exception:
            return da

        try:
            import rioxarray  # noqa: F401 — needed for .rio accessor

            # Ensure the DataArray has a declared CRS (GEE exports EPSG:4326).
            if not da.rio.crs:
                da = da.rio.write_crs("EPSG:4326")

            clipped = da.rio.clip([roi_geojson], crs="EPSG:4326",
                                  drop=False, all_touched=False)
            return clipped
        except Exception as exc:
            log.debug("_clip_to_polygon: rioxarray clip failed (%s) — returning unclipped", exc)
            return da

    def _download_tiled(self, image: Any, roi_geojson: Any,
                        gee_bands: list[str], scale_m: float, *,
                        label: str = "GEE") -> Any:
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

        # Per-tile budget (same 18 MB target as the coarsen guard).
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
                        # A tile entirely outside the clip polygon legitimately
                        # produces no pixels — skip it.
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

        GEE's hard limit is 50,331,648 bytes (~48 MB) per request.  Empirically
        GEE's internal pixel count is ~2.5× a naive bbox estimate (it uses
        equatorial-degree scale without ``cos(lat)`` for EPSG:4326 output, then
        rounds up to tile boundaries).  We target our estimate to ≤ ~18 MB so
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

    def _masked_median_composite(
        self,
        dataset_id: str,
        roi: Any,
        start: str,
        end: str,
        gee_bands: list[str],
        cloud_property: str | None,
        max_cloud_pct: float,
        cloud_mask: str | None,
    ) -> Any:
        """Build a cloud-masked median composite ``ee.Image`` (raw DN bands).

        Shared by ``fetch_multiband_composite`` (downloads all bands) and
        ``fetch_index_composite`` (computes an index then downloads one band).
        """
        import ee

        coll = ee.ImageCollection(dataset_id).filterDate(start, end).filterBounds(roi)
        if cloud_property:
            try:
                coll = coll.filter(ee.Filter.lt(cloud_property, max_cloud_pct))
            except Exception:
                pass

        if cloud_mask == "sentinel2_scl":
            def _mask_s2(img):
                scl = img.select("SCL")
                # Keep vegetation/soil/water/snow; drop cloud, shadow, cirrus.
                bad = scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10)).Or(scl.eq(11))
                return img.updateMask(bad.Not())
            coll = coll.map(_mask_s2)
        elif cloud_mask == "landsat_qapixel":
            def _mask_ls(img):
                qa = img.select("QA_PIXEL")
                # Bits 1-4: dilated cloud, cirrus, cloud, cloud shadow.
                mask = (
                    qa.bitwiseAnd(1 << 1).eq(0)
                    .And(qa.bitwiseAnd(1 << 2).eq(0))
                    .And(qa.bitwiseAnd(1 << 3).eq(0))
                    .And(qa.bitwiseAnd(1 << 4).eq(0))
                )
                return img.updateMask(mask)
            coll = coll.map(_mask_ls)

        return coll.select(gee_bands).median()

    def _assert_available(self) -> None:
        ok, reason = self.is_available()
        if not ok:
            from aihydro_data.exceptions import AuthRequired
            raise AuthRequired(
                code="GEE_AUTH_MISSING",
                message=reason or "GEE backend is not available.",
                recovery="Run `aihydro-data auth gee` to authenticate.",
                next_tools=["data_doctor"],
                docs_anchor="auth#gee",
            )

        # Initialise the EE session (idempotent if already initialised)
        from aihydro_data.sources._gee_vendored.auth import connect
        result = connect()
        if not result.get("ok"):
            from aihydro_data.exceptions import AuthRequired
            raise AuthRequired(
                code="GEE_INIT_FAILED",
                message=result.get("message", "GEE initialization failed."),
                recovery="Run `aihydro-data auth gee` to re-authenticate.",
                next_tools=["data_doctor"],
                docs_anchor="auth#gee",
            )

    def _extract_computed_ndvi(
        self,
        *,
        dataset_id: str,
        start: str,
        end: str,
        roi_geojson: dict[str, Any],
        ndvi_bands: list[str],
        qa_band: Optional[str],
        max_cloud_pct: float,
        scale_m: float,
        spatial_reducer: str,
    ) -> dict[str, Any]:
        """
        Compute NDVI on-the-fly from raw bands (Sentinel-2 / Landsat).
        Returns the same envelope shape as the vendored extract_timeseries.
        """
        from datetime import datetime, timezone
        from aihydro_data.sources._gee_vendored.auth import _import_ee

        ok, ee, err = _import_ee()
        if not ok:
            return {"ok": False, "rows": [], "message": f"earthengine-api not installed: {err}"}

        try:
            roi = ee.Geometry(roi_geojson)
            nir, red = ndvi_bands[0], ndvi_bands[1]

            coll = ee.ImageCollection(dataset_id).filterDate(start, end).filterBounds(roi)
            # Sentinel-2 cloud filter (best-effort — property name varies by collection)
            try:
                coll = coll.filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
            except Exception:
                pass

            reducer = (
                ee.Reducer.mean() if spatial_reducer == "mean"
                else ee.Reducer.median()
            )

            def _ndvi_feature(image):
                # NDVI = (NIR - Red) / (NIR + Red); rename to 'value' for envelope
                ndvi = image.normalizedDifference([nir, red]).rename("value")
                stats = ndvi.reduceRegion(
                    reducer=reducer, geometry=roi, scale=scale_m,
                    bestEffort=True, maxPixels=1e13,
                )
                date = ee.Date(image.get("system:time_start")).format("YYYY-MM-dd")
                return ee.Feature(None, {"date": date, "value": stats.get("value")})

            fc = ee.FeatureCollection(coll.map(_ndvi_feature))
            info = fc.getInfo()
            rows = [
                {"date": f["properties"].get("date"), "value": f["properties"].get("value")}
                for f in info.get("features", [])
            ]
            return {
                "ok": True,
                "rows": rows,
                "provenance": {
                    "adapter": "aihydro_gee_ndvi",
                    "dataset_id": dataset_id,
                    "ndvi_bands": ndvi_bands,
                    "max_cloud_pct": max_cloud_pct,
                    "scale_m": scale_m,
                    "computed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "rows": [],
                "message": f"NDVI computation failed: {exc}",
            }

    @staticmethod
    def _geom_to_geojson(geometry: Any) -> dict[str, Any]:
        """Convert a shapely geometry to a GeoJSON geometry dict."""
        try:
            import json as _json
            from shapely.geometry import mapping
            return _json.loads(_json.dumps(mapping(geometry)))
        except Exception as exc:
            raise ValueError(f"Could not convert geometry to GeoJSON: {exc}") from exc
