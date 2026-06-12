"""GEE optical-composite and index helpers.

Extracted from sources/gee.py to keep each file under ~600 lines while
preserving exactly the same behaviour. Consumed by Backend via _CompositeMixin.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


class _CompositeMixin:
    """Mixin carrying multi-band composite and spectral-index fetch methods.

    Inherited by Backend (sources/gee/__init__.py) so the public API is
    unchanged; this file just houses the implementation.
    """

    def fetch_multiband_composite(
        self,
        spec: Any,
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

        image = self._masked_median_composite(
            dataset_id, roi, start, end, gee_bands,
            cloud_property, max_cloud_pct, cloud_mask,
        ).clip(roi)

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
        ds.attrs["resolution_m"] = scale_m
        return ds

    def fetch_index_composite(
        self,
        spec: Any,
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

        Raises ``ValueError`` if *index_name* has no server-side formula; the
        caller (pipeline) then falls back to the raw-band path.
        """
        self._assert_available()
        import ee

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

        composite = self._masked_median_composite(
            dataset_id, roi, start, end, gee_bands,
            cloud_property, max_cloud_pct, cloud_mask,
        )
        if scale_factor != 1.0 or offset != 0.0:
            composite = composite.multiply(scale_factor).add(offset)
        reflectance = composite.rename(required_friendly)
        expr_vars = {b: reflectance.select(b) for b in required_friendly}
        index_img = reflectance.expression(formula, expr_vars).rename(
            index_name.lower()
        ).clip(roi)

        band_name = index_name.lower()
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

    def _masked_median_composite(
        self,
        dataset_id: str,
        roi: Any,
        start: str,
        end: str,
        gee_bands: list[str],
        cloud_property: Optional[str],
        max_cloud_pct: float,
        cloud_mask: Optional[str],
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
            except Exception as exc:
                log.warning(
                    "Scene-level cloud pre-filter on %r skipped (%s) — composite "
                    "may include cloudier scenes than max_cloud_pct=%s.",
                    cloud_property, exc, max_cloud_pct,
                )

        if cloud_mask == "sentinel2_scl":
            def _mask_s2(img):
                scl = img.select("SCL")
                bad = scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10)).Or(scl.eq(11))
                return img.updateMask(bad.Not())
            coll = coll.map(_mask_s2)
        elif cloud_mask == "landsat_qapixel":
            def _mask_ls(img):
                qa = img.select("QA_PIXEL")
                mask = (
                    qa.bitwiseAnd(1 << 1).eq(0)
                    .And(qa.bitwiseAnd(1 << 2).eq(0))
                    .And(qa.bitwiseAnd(1 << 3).eq(0))
                    .And(qa.bitwiseAnd(1 << 4).eq(0))
                )
                return img.updateMask(mask)
            coll = coll.map(_mask_ls)

        return coll.select(gee_bands).median()

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
        """Compute NDVI on-the-fly from raw bands (Sentinel-2 / Landsat).

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
            try:
                coll = coll.filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
            except Exception as exc:
                log.warning(
                    "NDVI cloud pre-filter skipped (%s) — series may include "
                    "cloudier scenes than max_cloud_pct=%s.", exc, max_cloud_pct,
                )

            reducer = (
                ee.Reducer.mean() if spatial_reducer == "mean"
                else ee.Reducer.median()
            )

            def _ndvi_feature(image):
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
