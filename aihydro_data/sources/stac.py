"""
STAC backend — queries STAC catalogs (Planetary Computer primary, Earth Search
fallback) via pystac-client and stacks results into xarray with stackstac.

Why STAC alongside GEE?
  - **No GEE auth required** — useful for users who can't or won't sign up for
    Earth Engine.
  - **Reproducible**: STAC items are versioned and pinned by date.
  - **Cloud-native**: COG assets stream from Azure/AWS directly.

Most COG-based collections work with this backend; NetCDF-only datasets
(Daymet, some ERA5 mirrors) need a different code path and are not exposed
here yet.

Install: pip install aihydro-data[stac]

Robustness model
----------------
Every external call goes through ``_retry_search`` / ``_retry_load``, which
applies exponential-backoff retries on transient network errors.  When ALL
retries against a given catalog endpoint fail, ``_search_with_catalog_fallback``
automatically switches to the next endpoint in the provider chain.  The chain
for any given product is:

  1. Primary endpoint from ``backend_config["stac_endpoint"]`` (default: PC)
  2. Element84 Earth Search (``_ES_STAC_URL``) — independent AWS-hosted mirror
     of the same Copernicus / Sentinel collections; no PC token required.

This means a Planetary Computer timeout never kills the whole call — it
degrades to Element84 instead.
"""
from __future__ import annotations

import logging
import time
import warnings
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)

# Primary catalog: Microsoft Planetary Computer (auto-signs assets).
_PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
# Fallback: Element84 Earth Search (Sentinel-2, Landsat, Copernicus DEM).
_ES_STAC_URL = "https://earth-search.aws.element84.com/v1"

# Errors that indicate a transient network/infrastructure issue (retry-able).
_RETRYABLE_PATTERNS = ("timeout", "time", "connection", "network", "exceeded",
                       "502", "503", "504", "gateway", "service unavailable")

# Maximum wall-clock seconds we wait across all retries for one STAC call.
_MAX_RETRY_WAIT_S = 90


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _RETRYABLE_PATTERNS)


def _retry(func, *, max_retries: int = 3, base_delay: float = 2.0, label: str = ""):
    """Call *func* up to *max_retries* times, backing off on transient errors."""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as exc:
            if attempt < max_retries - 1 and _is_retryable(exc):
                wait = min(base_delay * (2 ** attempt), _MAX_RETRY_WAIT_S)
                warnings.warn(
                    f"STAC {label} transient error (attempt {attempt + 1}/{max_retries}, "
                    f"retry in {wait:.0f}s): {exc}"
                )
                time.sleep(wait)
            else:
                raise


class Backend(SourceBackend):
    """STAC backend — Planetary Computer + Earth Search via pystac-client + stackstac."""

    source_id = "stac"

    # ── SourceBackend interface ───────────────────────────────────────────

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": ["dem", "landcover", "ndvi", "lst"],
            "coverage": ["global"],
            "requires_auth": [],   # PC anonymous works for read
            "requires_extras": ["stac"],
        }

    def is_available(self) -> tuple[bool, Optional[str]]:
        try:
            import pystac_client  # noqa: F401
        except ImportError:
            return False, (
                "pystac-client not installed. "
                "Run `pip install aihydro-data[stac]`."
            )
        try:
            import stackstac  # noqa: F401
        except ImportError:
            return False, (
                "stackstac not installed. "
                "Run `pip install aihydro-data[stac]`."
            )
        return True, None

    # ── fetch APIs ────────────────────────────────────────────────────────

    def fetch_timeseries(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
    ) -> Any:
        """
        Fetch a STAC collection and reduce to a time series at the geometry's
        location / area.

        Static products (timestep='static') return a 1-row DataFrame.
        """
        self._assert_available()
        raster = self.fetch_raster(spec, geometry, start, end)

        import pandas as pd
        import numpy as np

        if "time" not in raster.dims:
            value = float(np.asarray(raster).mean())
            return pd.DataFrame({"date": [pd.Timestamp(start)],
                                 spec.variable: [value]})

        if aggregation in ("basin_mean", "centroid"):
            series = raster.mean(dim=[d for d in raster.dims if d != "time"])
        elif aggregation == "basin_sum":
            series = raster.sum(dim=[d for d in raster.dims if d != "time"])
        else:
            series = raster.mean(dim=[d for d in raster.dims if d != "time"])

        dates = pd.to_datetime(series.time.values)
        values = np.asarray(series.values, dtype=float)
        df = pd.DataFrame({"date": dates, spec.variable: values})
        return df.dropna(subset=[spec.variable]).reset_index(drop=True)

    def fetch_raster(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        """Stack the matching STAC items into an xarray DataArray.

        Tries the primary catalog endpoint first (usually Planetary Computer),
        then falls back to Element84 Earth Search if the primary endpoint
        returns transient errors on all retry attempts.
        """
        self._assert_available()

        cfg = spec.backend_config
        collection = cfg.get("stac_collection") or spec.source_dataset_id
        asset = cfg.get("stac_asset", "data")
        primary_endpoint = cfg.get("stac_endpoint", _PC_STAC_URL)
        query = cfg.get("stac_query", {})
        resolution = cfg.get("stac_resolution", spec.resolution_m or 30)
        unit_conversion = cfg.get("unit_conversion", 1.0)

        bbox = self._geometry_to_bbox(geometry)
        datetime_filter = None if spec.timestep == "static" else f"{start}/{end}"

        # Build the ordered catalog fallback chain.  Element84 is added as a
        # fallback only if the primary is Planetary Computer (same collections,
        # no signing required).
        endpoints = [primary_endpoint]
        if primary_endpoint == _PC_STAC_URL and _ES_STAC_URL not in endpoints:
            endpoints.append(_ES_STAC_URL)
        # Also accept an explicit fallback list from backend_config
        for extra in cfg.get("stac_fallback_endpoints", []):
            if extra not in endpoints:
                endpoints.append(extra)

        items = None
        catalog_used = None
        last_exc: Exception | None = None

        for endpoint in endpoints:
            catalog = self._open_catalog(endpoint)
            try:
                items = _retry(
                    lambda cat=catalog: list(
                        cat.search(
                            collections=[collection],
                            bbox=bbox,
                            datetime=datetime_filter,
                            query=query or None,
                            limit=cfg.get("stac_limit", 100),
                        ).items()
                    ),
                    label=f"search {collection}@{endpoint}",
                )
                catalog_used = endpoint
                break
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "STAC search failed on %s (%s): %s — trying next endpoint",
                    endpoint, collection, exc,
                )

        if not items:
            if last_exc is not None and not _is_retryable(last_exc):
                raise last_exc
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="STAC_NO_ITEMS",
                message=(
                    f"No STAC items in {collection} for bbox={bbox}, "
                    f"datetime={datetime_filter}. Tried: {endpoints}."
                ),
                recovery=(
                    "Widen the date range, check the geometry's coverage, or "
                    "pick a different product via mode='manual'."
                ),
                next_tools=["data_validate_request", "data_list_products"],
                docs_anchor="stac#no-items",
            )

        if log.isEnabledFor(logging.DEBUG):
            log.debug("STAC: %d items from %s", len(items), catalog_used)

        import stackstac
        import math

        stack_epsg = cfg.get("stac_epsg", 4326)

        if stack_epsg == 4326:
            bbox_centre_lat = (bbox[1] + bbox[3]) / 2
            metres_per_deg = max(111_320 * math.cos(math.radians(bbox_centre_lat)), 1.0)
            stack_resolution = resolution / metres_per_deg
        else:
            stack_resolution = float(resolution)

        # Sign items if they came from Planetary Computer.
        sign_items = items
        if catalog_used == _PC_STAC_URL:
            try:
                import planetary_computer as pc
                sign_items = [pc.sign(item) for item in items]
            except ImportError:
                pass  # unsigned — public assets still work for most collections

        def _load():
            return stackstac.stack(
                sign_items,
                assets=[asset],
                bounds_latlon=bbox,
                resolution=stack_resolution,
                epsg=stack_epsg,
            )

        cube = _retry(_load, label=f"stackstac load {collection}")

        if "band" in cube.dims:
            cube = cube.isel(band=0, drop=True)
        if spec.timestep == "static" and "time" in cube.dims:
            cube = cube.isel(time=0, drop=True)
        if unit_conversion != 1.0:
            cube = cube * unit_conversion
        return cube

    def fetch_multiband_composite(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        """Cloud-masked median reflectance composite via stackstac — no size cap.

        This is the large-area fallback for ``Backend(gee).fetch_multiband_composite``:
        when a watershed exceeds GEE's ~32 MB ``getDownloadURL`` ceiling, the
        routing chain falls through to this STAC path, which streams
        Cloud-Optimised GeoTIFFs lazily (dask-chunked) with no download limit.

        Returns an :class:`xarray.Dataset` whose ``data_vars`` are the friendly
        band names (``green``, ``nir`` …) — identical shape to the GEE path so
        ``compute_index`` is source-agnostic.

        ``backend_config`` keys:
            ``stac_collection`` — STAC collection id (e.g. 'sentinel-2-l2a')
            ``band_map``        — ``{friendly_name: stac_asset_id}``
            ``qa_asset``        — QA asset id (e.g. 'SCL' / 'qa_pixel')
            ``cloud_mask``      — 'sentinel2_scl' | 'landsat_qapixel' | None
            ``stac_query``      — STAC query, e.g. {'eo:cloud_cover': {'lt': 60}}
            ``scale_factor`` / ``offset`` — DN → reflectance conversion
        """
        self._assert_available()
        import math

        import numpy as np
        import xarray as xr

        cfg = spec.backend_config
        collection = cfg.get("stac_collection") or spec.source_dataset_id
        endpoint = cfg.get("stac_endpoint", _PC_STAC_URL)
        band_map: dict[str, str] = cfg.get("band_map", {})
        if not band_map:
            raise RuntimeError(
                f"Product {spec.id!r} has no 'band_map' in backend_config."
            )
        qa_asset = cfg.get("qa_asset")
        cloud_mask = cfg.get("cloud_mask")
        resolution = cfg.get("stac_resolution", spec.resolution_m or 20)
        query = cfg.get("stac_query", {})
        scale_factor = float(cfg.get("scale_factor", 1.0))
        offset = float(cfg.get("offset", 0.0))

        catalog = self._open_catalog(endpoint)
        bbox = self._geometry_to_bbox(geometry)

        # Build fallback endpoint chain for multiband composites too.
        endpoints = [endpoint]
        if endpoint == _PC_STAC_URL and _ES_STAC_URL not in endpoints:
            endpoints.append(_ES_STAC_URL)

        items = None
        catalog_used = None
        last_exc: Exception | None = None

        for ep in endpoints:
            cat = self._open_catalog(ep)
            try:
                items = _retry(
                    lambda c=cat: list(
                        c.search(
                            collections=[collection],
                            bbox=bbox,
                            datetime=f"{start}/{end}",
                            query=query or None,
                            limit=cfg.get("stac_limit", 200),
                        ).items()
                    ),
                    label=f"search {collection}@{ep}",
                )
                catalog_used = ep
                break
            except Exception as exc:
                last_exc = exc
                log.warning("STAC multiband search failed on %s: %s", ep, exc)

        if not items:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="STAC_NO_ITEMS",
                message=(
                    f"No STAC items in {collection} for bbox={bbox}, "
                    f"datetime={start}/{end}. Tried: {endpoints}."
                ),
                recovery="Widen the date range or relax the cloud-cover query.",
                next_tools=["data_validate_request", "data_list_products"],
                docs_anchor="stac#no-items",
            )

        # ── Auto-coarsen resolution to fit in RAM ─────────────────────────
        n_bands_total = len(band_map) + (1 if qa_asset and cloud_mask else 0)
        n_scenes = len(items)
        _bbox_lon = bbox[2] - bbox[0]
        _bbox_lat = bbox[3] - bbox[1]
        _bbox_centre_lat = (bbox[1] + bbox[3]) / 2
        _metres_per_deg = max(111_320 * math.cos(math.radians(_bbox_centre_lat)), 1.0)
        _area_m2 = (_bbox_lon * _metres_per_deg) * (_bbox_lat * 111_320)
        _ram_budget = 3 * 1024 ** 3
        _max_pixels = _ram_budget / (n_scenes * n_bands_total * 4)
        _min_scale_m = math.ceil(math.sqrt(_area_m2 / _max_pixels))
        if _min_scale_m > resolution:
            log.warning(
                "STAC multiband: %d scenes × %d bands over ~%.0f km² at %d m "
                "would use >3 GB RAM. Auto-coarsening to %d m.",
                n_scenes, n_bands_total, _area_m2 / 1e6, resolution, _min_scale_m,
            )
            resolution = _min_scale_m

        import stackstac

        assets = list(band_map.values())
        if qa_asset and cloud_mask and qa_asset not in assets:
            assets = assets + [qa_asset]

        bbox_centre_lat = (bbox[1] + bbox[3]) / 2
        metres_per_deg = max(111_320 * math.cos(math.radians(bbox_centre_lat)), 1.0)
        stack_resolution = resolution / metres_per_deg

        # Sign if from PC.
        sign_items = items
        if catalog_used == _PC_STAC_URL:
            try:
                import planetary_computer as pc
                sign_items = [pc.sign(item) for item in items]
            except ImportError:
                pass

        cube = stackstac.stack(
            sign_items,
            assets=assets,
            bounds_latlon=bbox,
            resolution=stack_resolution,
            epsg=4326,
            chunksize=512,
        )

        if cloud_mask == "sentinel2_scl" and qa_asset:
            scl = cube.sel(band=qa_asset)
            good = ~scl.isin([3, 8, 9, 10, 11])
            cube = cube.where(good)
        elif cloud_mask == "landsat_qapixel" and qa_asset:
            qa = cube.sel(band=qa_asset).astype("uint16")
            bad = (
                (qa & (1 << 1)) > 0
            ) | (
                (qa & (1 << 2)) > 0
            ) | (
                (qa & (1 << 3)) > 0
            ) | (
                (qa & (1 << 4)) > 0
            )
            cube = cube.where(~bad)

        composite = cube.median(dim="time", skipna=True)
        ds = xr.Dataset()
        for friendly, asset_id in band_map.items():
            da = composite.sel(band=asset_id).drop_vars("band", errors="ignore")
            if scale_factor != 1.0 or offset != 0.0:
                da = da * scale_factor + offset
            ds[friendly] = da
        ds.attrs["sensor"] = cfg.get("sensor", spec.id.lower())
        ds.attrs["dataset_id"] = collection
        return ds

    # ── helpers ───────────────────────────────────────────────────────────

    def _assert_available(self) -> None:
        ok, reason = self.is_available()
        if not ok:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="STAC_NOT_INSTALLED",
                message=reason or "STAC backend is not available.",
                recovery="pip install aihydro-data[stac]",
                next_tools=["data_doctor"],
                docs_anchor="install#stac",
            )

    def _open_catalog(self, url: str) -> Any:
        """Open a STAC catalog, applying PC token-signing when available."""
        import pystac_client
        if url == _PC_STAC_URL:
            try:
                import planetary_computer as pc
                return pystac_client.Client.open(url, modifier=pc.sign_inplace)
            except ImportError:
                pass  # fall through to unsigned open
        return pystac_client.Client.open(url)

    def _geometry_to_bbox(self, geometry: Any) -> list[float]:
        """Extract a [minx, miny, maxx, maxy] bbox from any geometry input."""
        if getattr(geometry, "geom_type", None) == "GaugeID":
            from aihydro_data.exceptions import GeometryInvalid
            raise GeometryInvalid(
                code="STAC_REQUIRES_GEOM",
                message="STAC backend cannot fetch from a gauge ID.",
                recovery="Pass a Point, Polygon, or GeoDataFrame instead.",
                next_tools=["data_list_products"],
                docs_anchor="stac#geometry",
            )
        bounds = getattr(geometry, "bounds", None)
        if bounds is None:
            raise ValueError(f"Cannot derive bbox from {type(geometry).__name__}.")
        minx, miny, maxx, maxy = bounds
        if minx == maxx:
            minx -= 0.001
            maxx += 0.001
        if miny == maxy:
            miny -= 0.001
            maxy += 0.001
        return [minx, miny, maxx, maxy]
