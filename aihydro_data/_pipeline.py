"""
Unified fetch entry-point.

Pipeline (Phase 2+):
    1. Validate kwargs → FetchRequest (Pydantic)
    2. Coerce geometry → shapely (geometry.coerce_geometry)
    3. Resolve product → ProductSpec (routing.resolve_product)
    4. Check backend availability → raise SourceUnavailable/AuthRequired if not OK
    5. Try fallback chain if primary fails
    6. Return FetchResult with provenance
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from aihydro_data.contracts import (
    AggregationMode,
    FetchRequest,
    FetchResult,
)

log = logging.getLogger(__name__)

# ── Variable alias table ──────────────────────────────────────────────────────
# Maps common natural-language names → canonical variable IDs used in policy.py.
# Applied at the very top of fetch() so aliases work for both single and batch
# calls, and for product-pin (manual mode) too.
#
# Motivation: LLMs (and humans) naturally write "temperature", "discharge",
# "elevation", etc. — all of which resolve to tmax/tmin/tmean, streamflow, dem.
# Without these aliases, the router raises REGION_NO_POLICY on perfectly
# reasonable requests. Adding aliases here is zero-cost (one dict lookup)
# and prevents unhelpful "no policy for variable='temperature'" errors.
_VARIABLE_ALIASES: dict[str, str] = {
    # Temperature
    "temperature":          "tmean",
    "temp":                 "tmean",
    "mean_temperature":     "tmean",
    "avg_temperature":      "tmean",
    "average_temperature":  "tmean",
    "max_temperature":      "tmax",
    "maximum_temperature":  "tmax",
    "min_temperature":      "tmin",
    "minimum_temperature":  "tmin",
    # Precipitation
    "precip":               "precipitation",
    "rainfall":             "precipitation",
    "rain":                 "precipitation",
    "total_precipitation":  "precipitation",
    # Streamflow / discharge
    "discharge":            "streamflow",
    "flow":                 "streamflow",
    "river_discharge":      "streamflow",
    "runoff":               "streamflow",
    # Evapotranspiration
    "evapotranspiration":   "et",
    "evaporation":          "et",
    "actual_et":            "et",
    "aet":                  "et",
    "reference_et":         "pet",
    "potential_et":         "pet",
    "potential_evapotranspiration": "pet",
    # Elevation / DEM
    "elevation":            "dem",
    "altitude":             "dem",
    "topography":           "dem",
    # Land cover
    "land_cover":           "landcover",
    "land_use":             "landcover",
    "lulc":                 "landcover",
    # NDVI / vegetation
    "vegetation":           "ndvi",
    "greenness":            "ndvi",
    # Soil moisture
    "moisture":             "soil_moisture",
    "sm":                   "soil_moisture",
    # Optical imagery
    "imagery":              "optical",
    "satellite":            "optical",
    "remote_sensing":       "optical",
}


def _normalise_variable(variable: str) -> str:
    """Canonicalise a variable name, applying the alias table case-insensitively.

    Returns the canonical name (lower-case), logging a DEBUG note when an alias
    fires so engineers can trace variable substitution without it being noisy.
    """
    canon = variable.strip().lower().replace("-", "_").replace(" ", "_")
    alias = _VARIABLE_ALIASES.get(canon)
    if alias and alias != canon:
        log.debug("Variable alias applied: %r → %r", variable, alias)
        return alias
    return canon


def _looks_like_collection(geom: Any) -> bool:
    """
    Detect "user passed multiple geometries" so fetch() can auto-dispatch
    to fetch_batch() without the caller knowing about the batch API.

    Returns True for:
      - geopandas.GeoDataFrame with >1 row
      - dict (label → geom) with >0 entries
      - list/tuple of geometries (length ≥ 2), but NOT scalar coordinate
        tuples like (lat, lon) or (minx, miny, maxx, maxy)

    Returns False for single shapely geoms, single strings, single coord tuples,
    or anything else that's clearly a single fetch.
    """
    # GeoDataFrame
    if hasattr(geom, "iterrows") and hasattr(geom, "geometry"):
        try:
            return len(geom) > 1
        except TypeError:
            return False

    # dict[label, geom]
    if isinstance(geom, dict) and "type" not in geom:
        # Heuristic: GeoJSON dicts have a "type" key — skip those
        return len(geom) > 0

    # list / tuple
    if isinstance(geom, (list, tuple)):
        if len(geom) < 2:
            return False
        # Bare (lat, lon) or (minx, miny, maxx, maxy) → scalar tuple, NOT a batch
        if all(isinstance(v, (int, float)) for v in geom) and len(geom) in (2, 4):
            return False
        # Otherwise treat as a collection (list of geoms, gauge IDs, or pairs)
        return True

    return False


def fetch(
    variable: str,
    geometry: Any,
    start: str,
    end: str,
    *,
    mode: str = "auto",
    product: Optional[str] = None,
    fallback: Optional[list[str]] = None,
    aggregation: AggregationMode = "basin_mean",
    cache: bool = True,
    index: Optional[str] = None,
    native_resolution: bool = False,
    validate: Optional[Callable[[FetchResult], bool]] = None,
) -> FetchResult:
    """
    Fetch one variable for one geometry/time window.

    Auto mode (default):
        Router picks the best product for the geometry's region using the
        declarative table in routing/policy.py. On failure, walks the
        fallback chain until one source succeeds.

    Manual mode:
        Pin a specific product. Fallback chain still applies if you pass
        `fallback=[...]`; pass `fallback=None` (default) to use the policy
        default, or `fallback=[]` to disable fallbacks entirely.

    Quality-gated fallback (`validate=`):
        Pass a callback `validate(result) -> bool`. After each candidate
        succeeds, the callback inspects the FetchResult; returning False (or
        raising) *rejects* that result and forces the router to try the next
        product in the chain — mirroring delineation/router.py's escalation
        logic. The rejection is recorded in `result.fallback_history`.

    Decision trail:
        Every returned FetchResult carries `.fallback_history`: an ordered list
        of `{product, source, outcome, reason}` describing each candidate the
        router considered (`failed`/`rejected`/`served`), so the chosen backend
        is always explainable.

    Batch dispatch:
        If `geometry` is a list/tuple/dict/GeoDataFrame of MULTIPLE entries,
        the call is auto-dispatched to fetch_batch() and a list[FetchResult]
        is returned instead of a single FetchResult. This lets you write
        `fetch("streamflow", ["03245500", "01646500"], ...)` naturally.

    Returns FetchResult (or list[FetchResult] for batch inputs) with .data
    (pd.DataFrame or xr.DataArray), .citation, .next_steps, etc.

    On failure raises one of aihydro_data.exceptions.{SourceUnavailable,
    RegionUnsupported, AuthRequired, DateOutOfRange, GeometryInvalid,
    FetchTooLarge}. Each carries a structured error envelope agents can
    chain off (.to_dict() → recovery, next_tools, docs_anchor).

    See:
        - aihydro_data.list_products() to discover what's available
        - aihydro_data.get_product(id) for one product's full spec
        - the bundled help_topics/first_fetch.md for an end-to-end walk-through

    Common aliases accepted (silently normalised):
        temperature / temp → tmean  |  precip / rain → precipitation
        discharge / flow   → streamflow  |  elevation / altitude → dem
        land_cover / lulc  → landcover   |  evapotranspiration   → et
    """
    # ── -1. Normalise variable name (alias table + lower/strip) ──────────
    variable = _normalise_variable(variable)

    # ── 0. Auto-dispatch lists / dicts / GeoDataFrames to fetch_batch ─────
    # User-friendliness: `fetch("streamflow", ["gauge1", "gauge2"], ...)` is
    # the natural shape. Detect collection-style inputs and forward to the
    # batch path so callers don't have to know about fetch_batch() to do
    # multi-geometry fetches.
    if _looks_like_collection(geometry):
        batch = fetch_batch(
            variable, geometry, start, end,
            mode=mode, product=product, fallback=fallback,
            aggregation=aggregation, cache=cache,
        )
        # Return list of FetchResults in label order — what users intuitively expect
        return [batch["results"][lbl] for lbl in batch["labels"]
                if lbl in batch["results"]]

    # ── 1. Validate ───────────────────────────────────────────────────────
    req = FetchRequest(
        variable=variable,
        geometry=geometry,
        start=start,
        end=end,
        mode=mode,
        product=product,
        fallback=fallback,
        aggregation=aggregation,
        cache=cache,
    )

    # ── 2. Coerce geometry ────────────────────────────────────────────────
    from aihydro_data.geometry import coerce_geometry
    geom = coerce_geometry(req.geometry)

    # ── 3. Detect region + build candidate list ───────────────────────────
    from aihydro_data.routing import detect_region, resolve_product_ids
    from aihydro_data.products import get_product

    region = detect_region(geom)

    if mode == "manual" and product:
        primary_spec = get_product(product)
        # Fallback chain: explicit list or policy minus primary
        if fallback is not None:
            fallback_ids = [f for f in fallback if f != product]
        else:
            policy_ids = resolve_product_ids(variable, region)
            fallback_ids = [pid for pid in policy_ids if pid != product]
        candidate_specs = [primary_spec] + [
            get_product(fid) for fid in fallback_ids
            if _is_registered(fid)
        ]
    else:
        candidate_ids = resolve_product_ids(variable, region)
        if not candidate_ids:
            from aihydro_data.exceptions import RegionUnsupported
            from aihydro_data.routing.policy import PRODUCT_POLICY
            # Collect all canonical variable names that have at least one policy row.
            known_variables = sorted({v for v, _r in PRODUCT_POLICY})
            # Surface any alias reverse-mapping to help the caller pick the right name.
            _rev = {v: k for k, v in _VARIABLE_ALIASES.items() if k != v}
            alias_hint = (
                f" (Did you mean {_rev.get(variable)!r}?)"
                if variable in _rev else ""
            )
            raise RegionUnsupported(
                code="REGION_NO_POLICY",
                message=(
                    f"No routing policy for variable={variable!r}, region={region!r}.{alias_hint} "
                    f"Valid variable names: {known_variables}. "
                    f"Call data_list_products() with no args to see all available products."
                ),
                recovery=(
                    f"Use one of the supported variables: {known_variables}. "
                    "Common aliases are accepted — e.g. 'temperature'→'tmean', "
                    "'discharge'→'streamflow', 'elevation'→'dem', 'precip'→'precipitation'."
                ),
                next_tools=["data_list_products"],
                docs_anchor="routing",
            )
        candidate_specs = [
            get_product(pid) for pid in candidate_ids
            if _is_registered(pid)
        ]
        if not candidate_specs:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="NO_INSTALLED_PRODUCT",
                message=(
                    f"Policy candidates {candidate_ids} for ({variable!r}, {region!r}) "
                    f"are not installed. Install the required extras first."
                ),
                recovery="Run `pip install aihydro-data[gee]` and/or `pip install aihydro-data[hyriver]`.",
                next_tools=["data_list_products", "data_doctor"],
                docs_anchor="install",
            )

    # ── 4. Cache key + disk read ──────────────────────────────────────────
    # Design: cache key is per-(variable, geom, dates, aggregation) so an
    # auto-mode caller gets the same cached series regardless of which
    # backend served it. In manual mode the product is included so users
    # who pin a product don't accidentally pick up another product's data.
    from aihydro_data.cache import cache_key as _make_key, cache_read, cache_write
    geom_wkt = geom.wkt
    key_payload: dict[str, Any] = {
        "variable": variable,
        "start": start,
        "end": end,
        "aggregation": aggregation,
        "geom_wkt": geom_wkt,
    }
    if mode == "manual" and product:
        key_payload["product"] = product
    if index:
        key_payload["index"] = index.upper()
    if native_resolution:
        key_payload["native_resolution"] = True
    ck = _make_key(key_payload)

    if cache:
        cached = cache_read(ck, req)
        if cached is not None:
            log.debug("Cache hit for %s (%s).", ck, variable)
            return cached

    # ── 5. Fetch with fallback chain ──────────────────────────────────────
    last_exc: Exception | None = None
    history: list[dict[str, str]] = []
    for spec in candidate_specs:
        try:
            result = _fetch_one(
                spec, geom, start, end, aggregation, req,
                index=index, native_resolution=native_resolution,
            )
            # Quality gate: let the caller reject a low-quality result and
            # force the next fallback (delineation-style escalation).
            if validate is not None:
                try:
                    accepted = validate(result)
                except Exception as ve:
                    accepted = False
                    log.warning(
                        "validate() raised for %r (%s); rejecting and trying next.",
                        spec.id, ve,
                    )
                    reason = f"validate() raised: {ve}"
                else:
                    reason = "" if accepted else "rejected by validate()"
                if not accepted:
                    history.append({
                        "product": spec.id, "source": spec.source,
                        "outcome": "rejected", "reason": reason,
                    })
                    continue

            history.append({
                "product": spec.id, "source": spec.source,
                "outcome": "served", "reason": "",
            })
            result = result.model_copy(update={
                "cache_key": ck, "fallback_history": history,
            })
            # Write to disk cache (best-effort, never raises). Manifest
            # records WHICH product actually served the data, so the
            # provenance trail stays intact even though the key is
            # product-agnostic in auto mode.
            if cache:
                try:
                    cache_write(result, geom_wkt=geom_wkt)
                except Exception as ce:
                    log.debug("Cache write failed (non-fatal): %s", ce)
            return result
        except Exception as exc:
            log.warning(
                "Product %r failed (%s: %s); trying next in chain.",
                spec.id, type(exc).__name__, exc,
            )
            history.append({
                "product": spec.id, "source": spec.source,
                "outcome": "failed", "reason": f"{type(exc).__name__}: {exc}",
            })
            last_exc = exc
            continue

    from aihydro_data.exceptions import SourceUnavailable
    raise SourceUnavailable(
        code="ALL_BACKENDS_FAILED",
        message=(
            f"All candidates failed for variable={variable!r}: "
            f"{[s.id for s in candidate_specs]}. "
            f"Last error: {last_exc}"
        ),
        details={"fallback_history": history},
        recovery=(
            "Check backend availability with `aihydro-data doctor`. "
            "For GEE products, ensure GEE is authenticated (`aihydro-data auth gee`)."
        ),
        next_tools=["data_doctor", "data_list_products"],
        docs_anchor="troubleshooting",
    ) from last_exc


# ── Helpers ───────────────────────────────────────────────────────────────

def _is_registered(product_id: str) -> bool:
    """Return True if product_id is in the registry (ignore missing extras)."""
    from aihydro_data.products import list_products
    return any(p.id == product_id for p in list_products())


def _fetch_one(
    spec: "aihydro_data.contracts.ProductSpec",
    geom: Any,
    start: str,
    end: str,
    aggregation: AggregationMode,
    req: FetchRequest,
    index: Optional[str] = None,
    native_resolution: bool = False,
) -> FetchResult:
    """Fetch from a single product spec. Raises on any failure."""
    import inspect

    from aihydro_data.sources.base import get_backend

    backend = get_backend(spec.source)

    def _call(method, *args, **kwargs):
        """Call a backend method, dropping kwargs it doesn't declare.

        Only GEE accepts ``native_resolution``; STAC/hyriver backends would
        raise TypeError. Filter to the callable's real signature so optional
        capabilities degrade gracefully.
        """
        try:
            params = inspect.signature(method).parameters
            if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):
            pass
        return method(*args, **kwargs)

    ok, reason = backend.is_available()
    if not ok:
        from aihydro_data.exceptions import SourceUnavailable
        raise SourceUnavailable(
            code=f"{spec.source.upper()}_UNAVAILABLE",
            message=reason or f"{spec.source} backend is not available.",
            recovery=f"pip install aihydro-data[{spec.source}]",
            next_tools=["data_doctor"],
            docs_anchor="install",
        )

    # Static products (DEM, land cover, soil) have no time dimension.
    # Auto-promote to raw_raster so backends never receive empty date strings.
    _agg = aggregation
    if spec.timestep == "static" and _agg != "raw_raster":
        _agg = "raw_raster"

    # Multi-band optical composites (variable='optical') return an xr.Dataset
    # of named reflectance bands via a dedicated backend method, regardless of
    # the requested aggregation. compute_spectral_index rides this path to get
    # raw bands through the full routing + fallback chain.
    if spec.backend_config.get("multiband") and hasattr(backend, "fetch_multiband_composite"):
        # Server-side index path: when an `index` is requested AND the backend
        # can compute it on its servers (GEE), push the computation upstream
        # and download only the single-band result. This gives ~N× more
        # area/resolution headroom than downloading all N raw bands. Falls
        # through to the raw-band path on any failure (e.g. STAC backend, or an
        # index with no server-side formula).
        if index and hasattr(backend, "fetch_index_composite"):
            try:
                data = backend.fetch_index_composite(
                    spec, geom, start, end, index,
                    mask_clouds=spec.backend_config.get("cloud_mask") is not None,
                    native_resolution=native_resolution,
                )
            except ValueError as ve:
                # No server-side formula for this index → raw-band fallback.
                log.debug("Server-side index unavailable (%s); using raw bands.", ve)
                data = _call(backend.fetch_multiband_composite, spec, geom, start, end,
                             native_resolution=native_resolution)
        else:
            data = _call(backend.fetch_multiband_composite, spec, geom, start, end,
                         native_resolution=native_resolution)
    elif _agg == "raw_raster":
        data = _call(backend.fetch_raster, spec, geom, start, end,
                     native_resolution=native_resolution)
    else:
        data = backend.fetch_timeseries(spec, geom, start, end, _agg)

    return FetchResult(
        variable=spec.variable,
        product=spec.id,
        source=spec.source,
        request=req,
        data=data,
        license=spec.license,
        citation=spec.citation,
        bibtex=spec.bibtex,
        next_steps=list(spec.next_steps),
        notes=[],
    )


# ── Batch fetch ───────────────────────────────────────────────────────────

def fetch_batch(
    variable: str,
    geometries: Any,
    start: str,
    end: str,
    *,
    mode: str = "auto",
    product: Optional[str] = None,
    fallback: Optional[list[str]] = None,
    aggregation: AggregationMode = "basin_mean",
    cache: bool = True,
    max_workers: int = 4,
    on_error: str = "warn",   # "warn" | "raise" | "skip"
) -> dict[str, Any]:
    """
    Fetch one variable for multiple geometries in parallel.

    Parameters
    ----------
    variable : str
        Variable name (e.g. 'precipitation').
    geometries : any
        One of:
          - geopandas.GeoDataFrame → one fetch per row (label = index)
          - dict[str, geom]        → label = key
          - list[geom]             → label = "0", "1", …
          - list[(label, geom)]    → explicit labels
    start, end : str
        ISO-8601 date range.
    max_workers : int
        Thread-pool size. GEE and direct-API calls are I/O-bound so
        threads work well here. Default 4 (stay polite to rate limits).
    on_error : "warn" | "raise" | "skip"
        What to do when one geometry fails:
          "warn"  → log a warning, store the exception in results, continue
          "raise" → re-raise immediately, aborting all remaining fetches
          "skip"  → silently omit the failed entry

    Returns
    -------
    dict with:
        "results"  : dict[str, FetchResult]    — successful fetches
        "errors"   : dict[str, Exception]      — failed geometries
        "labels"   : list[str]                 — ordered labels
        "variable" : str
        "start", "end" : str
    """
    import concurrent.futures

    from aihydro_data.geometry.batch import iter_geometries

    pairs = list(iter_geometries(geometries))
    if not pairs:
        return {"results": {}, "errors": {}, "labels": [], "variable": variable,
                "start": start, "end": end}

    results: dict[str, Any] = {}
    errors: dict[str, Exception] = {}

    def _one(label_geom: tuple[str, Any]) -> tuple[str, Any]:
        label, geom = label_geom
        result = fetch(
            variable,
            geom,            # already a shapely geometry from iter_geometries
            start,
            end,
            mode=mode,
            product=product,
            fallback=fallback,
            aggregation=aggregation,
            cache=cache,
        )
        return label, result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_one, pair): pair[0] for pair in pairs}
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            try:
                lbl, res = future.result()
                results[lbl] = res
            except Exception as exc:
                if on_error == "raise":
                    # Cancel remaining and propagate
                    for f in futures:
                        f.cancel()
                    raise
                elif on_error == "warn":
                    log.warning("fetch_batch: label=%r failed: %s", label, exc)
                    errors[label] = exc
                else:
                    # "skip"
                    pass

    return {
        "results": results,
        "errors": errors,
        "labels": [p[0] for p in pairs],
        "variable": variable,
        "start": start,
        "end": end,
    }
