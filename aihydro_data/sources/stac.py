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
import math
import time
import warnings
from typing import Any, Optional

import numpy as np
import xarray as xr

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


# Default ceiling on one static fetch's output (heuristic). A DEM is never coarsened
# silently; an over-size request fails with a recovery instead.
_STATIC_MAX_OUTPUT_BYTES = 1_000_000_000


def _item_grid(item: Any, asset: str) -> Optional[dict[str, float]]:
    """Native geographic grid of one item's asset from ``proj:transform`` (None if absent)."""
    props = dict(getattr(item, "properties", None) or {})
    try:
        a = (getattr(item, "assets", None) or {}).get(asset)
        extra = dict(getattr(a, "extra_fields", None) or {}) if a is not None else {}
    except Exception:
        extra = {}
    tr = extra.get("proj:transform") or props.get("proj:transform")
    epsg = extra.get("proj:epsg") or props.get("proj:epsg")
    if not tr or len(tr) < 6 or (epsg is not None and int(epsg) != 4326):
        return None
    try:
        xres, x0, yres, y0 = float(tr[0]), float(tr[2]), -float(tr[4]), float(tr[5])
    except (TypeError, ValueError):
        return None
    if xres <= 0 or yres <= 0:
        return None
    return {"xres": xres, "yres": yres, "x0": x0, "y0": y0}


def _snap(lo: float, hi: float, origin: float, res: float) -> tuple[float, float]:
    """Snap [lo, hi] outward onto the lattice ``origin + k*res`` (edges)."""
    i0 = math.floor((lo - origin) / res + 1e-6)
    i1 = math.ceil((hi - origin) / res - 1e-6)
    return origin + i0 * res, origin + i1 * res


def _static_stack_kwargs(
    items: list, asset: str, bbox: list, resolution: float, epsg: int, categorical: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """``stackstac.stack`` keyword arguments that reproduce the source pixel grid.

    Geographic products are stacked on the finest native grid of the contributing items
    (separate x and y spacing, from ``proj:transform``), with output edges snapped onto the
    source lattice and ``snap_bounds=False`` so output centres coincide with source posts
    (stackstac's default snap would put them half a pixel off). Coordinates are pixel
    centres. Elevation is float32/NaN with bilinear resampling only where spacing bands
    differ; categorical rasters are uint8/0 with nearest.
    """
    from rasterio.enums import Resampling

    notes: list[str] = []
    grids = [g for g in (_item_grid(it, asset) for it in items) if g]
    kwargs: dict[str, Any] = {"epsg": epsg, "xy_coords": "center", "rescale": False}
    if categorical:
        kwargs.update(dtype="uint8", fill_value=np.uint8(0))   # np scalar: stackstac cast-checks the type
        itemsize = 1
    else:
        kwargs.update(dtype="float32", fill_value=np.float32("nan"))
        itemsize = 4
    info: dict[str, Any] = {"itemsize": itemsize, "dtype": kwargs["dtype"], "grid_source": "proj:transform"}
    west, south, east, north = bbox
    if epsg == 4326 and grids:
        xres = min(g["xres"] for g in grids)
        yres = min(g["yres"] for g in grids)
        ref = next(g for g in grids if g["xres"] == xres)
        refy = next(g for g in grids if g["yres"] == yres)
        west, east = _snap(west, east, ref["x0"], xres)
        south, north = _snap(south, north, refy["y0"], yres)
        bands = sorted({round(g["xres"] * 3600.0, 6) for g in grids})
        info["x_spacing_arcsec_bands"] = bands
        if len(bands) > 1:
            notes.append(
                f"Request crosses longitude-spacing bands ({', '.join(str(b) for b in bands)} arcsec): "
                f"served on the finest spacing; coarser-band tiles are interpolated"
                + ("" if categorical else " (bilinear)")
                + ". Posts across a band boundary are not identical in the source."
            )
            if not categorical:
                kwargs["resampling"] = Resampling.bilinear
    elif epsg == 4326:
        lat = (south + north) / 2
        xres = resolution / max(111_320 * math.cos(math.radians(lat)), 1.0)
        yres = resolution / 111_320
        info["grid_source"] = "configured_resolution"
        notes.append(
            "Native grid not read from the items (no proj:transform); served on the configured "
            f"{resolution} m converted to degrees separately per axis, bounds not lattice-snapped."
        )
        kwargs["snap_bounds"] = True
    else:
        xres = yres = float(resolution)
        info["grid_source"] = "configured_resolution"
    if info["grid_source"] == "proj:transform":
        kwargs["snap_bounds"] = False
    kwargs["bounds"] = (west, south, east, north)
    kwargs["resolution"] = (xres, yres)
    info.update(
        xres=xres, yres=yres, bounds=[west, south, east, north],
        nx=int(round((east - west) / xres)), ny=int(round((north - south) / yres)),
    )
    return kwargs, info, notes


def _check_output_size(info: dict[str, Any], cfg: dict, collection: str, bbox: list) -> None:
    cap = int(cfg.get("stac_max_output_bytes", _STATIC_MAX_OUTPUT_BYTES))
    nbytes = info["nx"] * info["ny"] * info["itemsize"]
    info["output_bytes"] = nbytes
    if nbytes > cap:
        from aihydro_data.exceptions import FetchTooLarge
        raise FetchTooLarge(
            code="STAC_OUTPUT_TOO_LARGE",
            message=(
                f"{collection} bbox={bbox} would be {info['nx']} x {info['ny']} pixels "
                f"({nbytes / 1e9:.2f} GB) on the native grid; the cap is {cap / 1e9:.2f} GB. "
                f"A DEM is not coarsened silently (it changes slope and flow paths)."
            ),
            recovery="Fetch a smaller area (split the basin into tiles) or raise backend_config['stac_max_output_bytes'].",
            next_tools=["data_validate_request"],
            docs_anchor="stac#size",
        )


def _write_georef(cube: Any) -> Any:
    """Write CRS and the pixel-centre-derived transform so rioxarray round-trips exactly."""
    try:
        import rioxarray  # noqa: F401
        out = cube.rio.write_crs(4326)
        return out.rio.write_transform(out.rio.transform(recalc=True))
    except Exception as exc:  # pragma: no cover - rioxarray is a declared extra
        log.warning("could not write CRS/transform on static STAC raster: %s", exc)
        return cube


def _mosaic_static(cube: Any, nodata: Any = np.nan) -> Any:
    """Collapse the ``time`` axis of a *static* product by mosaicking, not by picking one item.

    For a tiled static product (Copernicus GLO-30, ESA WorldCover) stackstac's ``time``
    axis holds one entry per STAC item, i.e. per tile; each slice is NaN outside its own
    footprint. Taking ``isel(time=0)`` kept one tile and NaN-ed the rest.

    Each output pixel takes the first valid (non-NaN) value, scanning from the last slice
    to the first (``stackstac.mosaic`` default: the last item is on top). The cube is
    sorted by (datetime, id), so where tiles overlap — GLO-30 tiles share an edge
    row/column — the greater id wins, and where epochs of one tile overlap (WorldCover
    2020 and 2021) the newest wins and the older fills only pixels the newer lacks.
    Overlap pixels are the same ground samples resampled onto one grid, so which tile
    wins moves the value by at most sub-pixel resampling (not measured), not by a tile's worth.
    """
    import stackstac

    return stackstac.mosaic(cube, dim="time", nodata=nodata)


# Heuristic, not validated: a static mosaic with more than this NaN share inside the
# requested bbox gets a warning (e.g. `stac_limit` dropped tiles on a very large bbox).
# Legitimate NaN (ocean with no DEM tile, WorldCover no-data) trips it too; it never
# fails the fetch.
_STATIC_NAN_WARN_FRACTION = 0.01


def _is_valid(arr: Any, nodata: Any) -> Any:
    """Boolean mask of valid pixels (NaN-aware when ``nodata`` is NaN)."""
    return ~np.isnan(arr) if (isinstance(nodata, float) and np.isnan(nodata)) else (arr != nodata)


def _static_mosaic_with_provenance(
    cube: Any, items: list, collection: str, nodata: Any = np.nan,
) -> tuple[Any, dict[str, Any], list[str]]:
    """Mosaic a static cube and return (mosaic, provenance record, notes).

    ``items`` is the sorted list that was stacked (same order as the ``time`` axis).
    Whether more than one epoch contributed pixels is computed from the data: a pixel
    belongs to the newest epoch that has a valid value there, and the flag fires only if
    an older epoch owns at least one pixel. Gap-fill pixels are not marked per pixel.
    """
    times = np.asarray(cube["time"].values)
    n_epochs = len(set(times.tolist()))
    epochs_used: list[str] = []
    if n_epochs > 1:
        cube = cube.compute()                       # need slices to attribute pixels
        valid = _is_valid(np.asarray(cube), nodata)         # (time, y, x) after band drop
        claimed = np.zeros(valid.shape[1:], dtype=bool)
        owners: set = set()
        for i in range(len(times) - 1, -1, -1):     # newest slice first
            take = valid[i] & ~claimed
            if take.any():
                owners.add(times[i])
            claimed |= valid[i]
        epochs_used = [str(np.datetime64(t, "D")) for t in sorted(owners)]
    mosaic = _mosaic_static(cube, nodata).compute()
    nan_frac = float((~_is_valid(np.asarray(mosaic), nodata)).mean())

    notes: list[str] = []
    flags: list[str] = []
    if len(epochs_used) > 1:
        flag = "WORLDCOVER_MIXED_EPOCHS" if collection == "esa-worldcover" else "STATIC_MIXED_EPOCHS"
        flags.append(flag)
        notes.append(
            f"{flag}: more than one epoch contributed pixels ({', '.join(epochs_used)}); the "
            f"newest epoch wins and older epochs fill only its gaps. Epochs can use different "
            f"class algorithms (WorldCover v100 for 2020, v200 for 2021). Gap-fill pixels are "
            f"not marked per pixel."
        )
    if nan_frac > _STATIC_NAN_WARN_FRACTION:
        flags.append("STATIC_MOSAIC_NAN")
        notes.append(
            f"STATIC_MOSAIC_NAN: {nan_frac:.1%} of the mosaic inside the requested bbox is "
            f"NaN (warning threshold {_STATIC_NAN_WARN_FRACTION:.0%} is a heuristic). Causes "
            f"include tiles dropped by the STAC search limit on a very large bbox, areas "
            f"without a tile (ocean) and source no-data; check the item list."
        )
    record = {
        "method": "stac_static_mosaic",
        "collection": collection,
        "items": [
            {"id": str(getattr(it, "id", "")), "datetime": str(getattr(it, "datetime", None) or "")}
            for it in items
        ],
        "overlap_rule": "last item on top after sort by (datetime, id); newest epoch wins",
        "nan_fraction_bbox": nan_frac,
        "epochs_contributing": epochs_used,
        "flags": flags,
    }
    return mosaic, record, notes


# Heuristic, not validated: share of basin pixels with a valid value below which a static
# basin mean is flagged / refused. Any shortfall is flagged; below the refusal floor the
# candidate fails (so the fallback chain continues) rather than serving a mean of a sliver.
_BASIN_COVERAGE_FLAG = 1.0
_BASIN_COVERAGE_REFUSE = 0.5


def _basin_mean(raster: Any, geometry: Any, cfg: dict) -> tuple[float, dict[str, Any], list[str]]:
    """NaN-aware mean of a static raster over the basin mask, plus coverage of the basin.

    Pixels are those inside the polygon (rasterio ``geometry_mask``; ``all_touched`` only if the polygon
    contains no pixel centre). A non-areal geometry (point) takes the nearest pixel. The
    categorical nodata value (0) is excluded like NaN. ``basin_coverage_fraction`` is the
    share of the basin's pixels holding a valid value.
    """
    categorical = bool(cfg.get("categorical", False))
    arr = raster
    geom_type = getattr(geometry, "geom_type", "")
    bounds = getattr(geometry, "bounds", None)
    if geom_type in ("Polygon", "MultiPolygon"):
        mask = None
        try:
            import rioxarray  # noqa: F401
            from rasterio.features import geometry_mask
            tr = arr.rio.transform(recalc=True)
            for touched in (False, True):
                m = ~geometry_mask([geometry], out_shape=arr.shape, transform=tr, all_touched=touched)
                if m.any():
                    mask = m
                    break
        except Exception as exc:
            log.warning("basin mask failed (%s); using the whole bbox", exc)
        basis = "polygon" if mask is not None else "bbox"
        if mask is None:
            mask = np.ones(arr.shape, dtype=bool)
    elif geom_type == "Point" and bounds is not None:
        pt = arr.sel(x=bounds[0], y=bounds[1], method="nearest")
        v = np.asarray(pt).reshape(1)
        valid = (v != 0) if categorical else ~np.isnan(v)
        mask = np.ones(1, dtype=bool)
        data = v
        basis = "point"
        return _finish_mean(data, valid, basis, categorical)
    else:
        mask = np.ones(arr.shape, dtype=bool)
        basis = "bbox"
    data = np.asarray(arr)[mask]
    valid = (data != 0) if categorical else ~np.isnan(data)
    return _finish_mean(data, valid, basis, categorical)


def _finish_mean(data: Any, valid: Any, basis: str, categorical: bool) -> tuple[float, dict[str, Any], list[str]]:
    n = int(data.size)
    nv = int(valid.sum())
    cov = nv / n if n else 0.0
    info = {"basin_coverage_fraction": cov, "n_basin_pixels": n, "n_valid_pixels": nv, "mask_basis": basis}
    notes: list[str] = []
    if nv == 0 or cov < _BASIN_COVERAGE_REFUSE:
        from aihydro_data.exceptions import SourceUnavailable
        raise SourceUnavailable(
            code="STAC_LOW_COVERAGE",
            message=(
                f"Only {cov:.1%} of the basin's {n} pixels hold a valid value (floor "
                f"{_BASIN_COVERAGE_REFUSE:.0%}, a heuristic); a mean of that sliver would not "
                f"describe the basin."
            ),
            recovery="Use another product via the fallback chain, or check tile coverage for the area.",
            next_tools=["data_list_products"],
            docs_anchor="stac#coverage",
            details=info,
        )
    if cov < _BASIN_COVERAGE_FLAG:
        notes.append(
            f"STAC_BASIN_COVERAGE: the basin mean uses {nv} of {n} basin pixels "
            f"({cov:.1%} covered); the rest have no data and were excluded, not zero-filled."
        )
    mean = float(data[valid].astype("float64").mean())
    return mean, info, notes


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
            value, info, notes = _basin_mean(raster, geometry, spec.backend_config)
            df = pd.DataFrame({"date": [pd.Timestamp(start)], spec.variable: [value]})
            raster_notes = list(raster.attrs.get("aihydro_notes", []))
            df.attrs["aihydro_notes"] = raster_notes + notes
            df.attrs["aihydro_quality"] = info
            if raster.attrs.get("aihydro_site_resolution"):
                df.attrs["aihydro_site_resolution"] = raster.attrs["aihydro_site_resolution"]
            return df

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
        native_resolution: bool = False,
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
        is_static = spec.timestep == "static"
        categorical = bool(cfg.get("categorical", False))
        grid_notes: list[str] = []

        # Sign items if they came from Planetary Computer.
        sign_items = items
        if catalog_used == _PC_STAC_URL:
            try:
                import planetary_computer as pc
                sign_items = [pc.sign(item) for item in items]
            except ImportError:
                pass  # unsigned — public assets still work for most collections

        stack_kwargs: dict[str, Any]
        grid_info: dict[str, Any] = {}
        if is_static:
            # Deterministic overlap rule: stackstac sorts by datetime only, and every
            # tile of a static product usually shares one datetime, so break ties by id.
            sign_items = sorted(
                sign_items, key=lambda it: (str(getattr(it, "datetime", None) or ""), str(getattr(it, "id", "")))
            )
            stack_kwargs, grid_info, grid_notes = _static_stack_kwargs(
                sign_items, asset, bbox, resolution, stack_epsg, categorical,
            )
            _check_output_size(grid_info, cfg, collection, bbox)
            if native_resolution:
                grid_notes.append(
                    "native_resolution=True: honoured, static STAC products are always served "
                    "on the native source grid."
                )
        else:
            if native_resolution:
                grid_notes.append(
                    "native_resolution=True was ignored: time-varying STAC products are "
                    "stacked at the configured stac_resolution."
                )
            if stack_epsg == 4326:
                bbox_centre_lat = (bbox[1] + bbox[3]) / 2
                metres_per_deg = max(111_320 * math.cos(math.radians(bbox_centre_lat)), 1.0)
                stack_resolution = resolution / metres_per_deg
            else:
                stack_resolution = float(resolution)
            stack_kwargs = dict(
                bounds_latlon=bbox, resolution=stack_resolution, epsg=stack_epsg,
                fill_value=np.nan,
            )

        def _load():
            return stackstac.stack(sign_items, assets=[asset], **stack_kwargs)

        cube = _retry(_load, label=f"stackstac load {collection}")

        if "band" in cube.dims:
            cube = cube.isel(band=0, drop=True)
        static_record: dict[str, Any] = {}
        static_notes: list[str] = []
        if is_static and "time" in cube.dims:
            cube, static_record, static_notes = _static_mosaic_with_provenance(
                cube, sign_items, collection,
                nodata=0 if categorical else np.nan,
            )
            static_record["grid"] = grid_info
        if unit_conversion != 1.0:
            cube = cube * unit_conversion
        if is_static:
            cube = _write_georef(cube)
            static_notes = grid_notes + static_notes
        elif grid_notes:
            cube.attrs["aihydro_notes"] = list(cube.attrs.get("aihydro_notes", [])) + grid_notes
        if static_record:
            # Same route as NWIS/modelled-flow provenance: the pipeline pops these attrs
            # onto FetchResult.site_resolution / notes and from there the manifest.
            cube.attrs["aihydro_site_resolution"] = static_record
            if static_notes:
                cube.attrs["aihydro_notes"] = list(cube.attrs.get("aihydro_notes", [])) + static_notes
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
