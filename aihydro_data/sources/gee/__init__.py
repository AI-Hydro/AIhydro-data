"""
Google Earth Engine backend (package).

Wraps the vendored GEE auth + timeseries code from sources/_gee_vendored/.
All earthengine-api imports are lazy — this module can be imported safely
without the `gee` extra installed; it will only fail when `is_available()`
or a fetch method is actually called.

Install: pip install aihydro-data[gee]
Auth:    aihydro-data auth gee

Submodules
~~~~~~~~~~
_download.py  — raster download helpers (_open_geotiff, _clip_to_polygon, …)
_composite.py — optical composite helpers (fetch_multiband_composite, …)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources.base import SourceBackend
from aihydro_data.sources.gee._composite import _CompositeMixin
from aihydro_data.sources.gee._download import _DownloadMixin

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


class Backend(_CompositeMixin, _DownloadMixin, SourceBackend):
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
        """Return a pd.DataFrame with columns ['date', spec.variable].

        Automatic geometry simplification
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        GEE's ``reduceRegion`` API rejects requests whose payload (the ROI
        GeoJSON) exceeds 10 MB — which happens with very complex polygon
        boundaries (e.g. MERIT-Basins vector topology on a continental-scale
        watershed).  When that error is detected the call is retried with a
        progressively simplified geometry (Shapely ``simplify`` at tolerances
        0.001°, 0.01°, 0.05°).  Area error from the most aggressive tolerance
        (0.05° ≈ 5 km at mid-latitudes) is tiny relative to the GEE native
        resolution (ERA5-Land: 9 km, CHIRPS: 5.5 km, MODIS: 250-500 m).  If
        all simplification levels still exceed the limit a ``FetchTooLarge``
        structured error is raised.
        """
        self._assert_available()

        spatial_reducer, temporal_agg = _aggregation_to_gee(aggregation)

        cfg = spec.backend_config
        dataset_id = cfg.get("gee_dataset_id", spec.source_dataset_id)
        band = cfg.get("band", "")
        scale_m = float(cfg.get("scale_m", 5000))
        unit_conv = float(cfg.get("unit_conversion", 1.0))
        compute_ndvi = bool(cfg.get("compute_ndvi", False))
        ndvi_bands = cfg.get("ndvi_bands", ["B8", "B4"])
        qa_band = cfg.get("qa_band", None)
        max_cloud_pct = float(cfg.get("max_cloud_pct", 60))

        from aihydro_data.sources._retry import call_with_retry

        def _call_gee(roi_geojson: dict) -> dict:
            if compute_ndvi:
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
            if not result.get("ok", True):
                raise RuntimeError(
                    f"GEE fetch failed for {dataset_id}/{band}: "
                    f"{result.get('message', 'unknown error')}"
                )
            return result

        _PAYLOAD_LIMIT_MARKER = "payload size exceeds the limit"
        _SIMPLIFY_TOLERANCES = (0.001, 0.01, 0.05)

        geom_candidates = [geometry]
        for tol in _SIMPLIFY_TOLERANCES:
            simplified = geometry.simplify(tol, preserve_topology=True)
            geom_candidates.append(simplified)

        result: dict | None = None
        last_exc: Exception | None = None
        for attempt, geom_candidate in enumerate(geom_candidates):
            roi_geojson = self._geom_to_geojson(geom_candidate)
            try:
                result = _call_gee(roi_geojson)
                if attempt > 0:
                    log.info(
                        "GEE payload limit: succeeded after geometry simplification "
                        "(attempt %d, tol=%.3f°, vertices reduced).", attempt,
                        _SIMPLIFY_TOLERANCES[attempt - 1],
                    )
                break
            except RuntimeError as exc:
                last_exc = exc
                if _PAYLOAD_LIMIT_MARKER in str(exc) and attempt < len(geom_candidates) - 1:
                    log.warning(
                        "GEE payload limit for %s (attempt %d); retrying with "
                        "simplified geometry (tol=%.3f°).",
                        dataset_id, attempt + 1,
                        _SIMPLIFY_TOLERANCES[attempt] if attempt < len(_SIMPLIFY_TOLERANCES) else "?",
                    )
                    continue
                raise

        if result is None:
            from aihydro_data.exceptions import FetchTooLarge
            raise FetchTooLarge(
                code="GEE_PAYLOAD_LIMIT",
                message=(
                    f"GEE payload limit exceeded for {dataset_id} even after "
                    f"geometry simplification (tolerance up to "
                    f"{_SIMPLIFY_TOLERANCES[-1]}°). "
                    f"Basin is likely larger than ~1 M km²."
                ),
                recovery=(
                    "Use a smaller sub-basin, or switch to a non-GEE product "
                    "(e.g. CHIRPS_IRI for precipitation) that has no geometry-size limit."
                ),
                next_tools=["data_list_products"],
            )

        import pandas as pd
        rows = result.get("rows", result.get("timeseries", []))
        df = pd.DataFrame(rows)
        if df.empty:
            import datetime as _dt
            try:
                window_days = (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days
            except Exception:
                window_days = None
            window_hint = (
                f" The requested window is only {window_days} days — "
                "try widening to ≥30 days so at least one composite is captured."
                if window_days is not None and window_days < 30
                else ""
            )
            raise RuntimeError(
                f"GEE returned 0 rows for {dataset_id}/{band} "
                f"({start}..{end}).{window_hint} "
                "Check: (a) date window vs. dataset compositing period, "
                "(b) spatial coverage, (c) cloud/QA masking."
            )

        df = df.rename(columns={"value": spec.variable})
        df = df.dropna(subset=[spec.variable])
        if df.empty:
            raise RuntimeError(
                f"GEE returned all-null values for {dataset_id}/{band} "
                f"({start}..{end}). Likely all pixels masked out by QA/cloud filter."
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

        if cfg.get("soil_properties"):
            return self._fetch_soilgrids_raster(
                spec, geometry, native_resolution=native_resolution,
            )

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

    def _fetch_soilgrids_raster(
        self,
        spec: ProductSpec,
        geometry: Any,
        *,
        native_resolution: bool = False,
    ) -> Any:
        """Fetch ISRIC SoilGrids texture fractions as an :class:`xarray.Dataset`.

        Each requested property (``sand``, ``silt``, ``clay``, …) is a separate
        SoilGrids GEE Image (``projects/soilgrids-isric/<prop>_mean``) carrying
        per-depth bands (e.g. ``sand_0-5cm_mean``). We select the configured
        depth band for each property, download it via the shared
        :meth:`_download_image_array` path, and assemble a Dataset whose
        ``data_vars`` mirror POLARIS naming (``sand_5``, ``silt_5`` …) so the
        downstream Curve-Number classifier works unchanged.

        SoilGrids texture units are g/kg; we convert to percent (÷10) so the
        hydrologic-group thresholds (sand>70 %, clay<10 %, …) apply directly.
        """
        self._assert_available()
        import ee
        import xarray as xr

        cfg = spec.backend_config
        collection = cfg.get("gee_collection", "projects/soilgrids-isric")
        properties: list[str] = list(cfg.get("soil_properties", ["sand", "silt", "clay"]))
        depth = cfg.get("soil_depth", "0-5cm")
        depth_suffix = cfg.get("soil_depth_suffix", "5")
        scale_m = float(cfg.get("scale_m", 250))
        unit_conv = float(cfg.get("unit_conversion", 0.1))

        roi_geojson = self._geom_to_geojson(geometry)
        roi = ee.Geometry(roi_geojson)

        ds = xr.Dataset()
        first_da = None
        scale_m_eff = scale_m
        for prop in properties:
            band = f"{prop}_{depth}_mean"
            img = ee.Image(f"{collection}/{prop}_mean").select(band)
            img = img.reproject(crs="EPSG:4326", scale=scale_m)
            da, eff_scale = self._download_image_array(
                img, roi, roi_geojson, [band], scale_m,
                native_resolution=native_resolution,
                label=f"GEE SoilGrids {prop}",
            )
            da = da.squeeze(drop=True) * unit_conv
            var_name = f"{prop}_{depth_suffix}"
            if first_da is None:
                first_da = da
                ds[var_name] = da
            else:
                if da.shape != first_da.shape:
                    try:
                        da = da.rio.reproject_match(first_da)
                    except Exception as exc:
                        log.warning(
                            "SoilGrids %r grid co-registration failed (%s) — "
                            "property grids may be misaligned.", var_name, exc,
                        )
                ds[var_name] = da
            scale_m_eff = eff_scale

        ds.attrs["dataset_id"] = collection
        ds.attrs["resolution_m"] = scale_m_eff
        try:
            if first_da is not None and first_da.rio.crs is not None:
                ds = ds.rio.write_crs(first_da.rio.crs)
        except Exception as exc:
            log.warning("SoilGrids: could not write CRS onto Dataset (%s).", exc)
        return ds

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

    @staticmethod
    def _geom_to_geojson(geometry: Any) -> dict[str, Any]:
        """Convert a shapely geometry to a GeoJSON geometry dict."""
        try:
            import json as _json
            from shapely.geometry import mapping
            return _json.loads(_json.dumps(mapping(geometry)))
        except Exception as exc:
            raise ValueError(f"Could not convert geometry to GeoJSON: {exc}") from exc
