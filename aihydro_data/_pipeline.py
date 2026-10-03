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
    # Flood
    "flood":                "flood_inundation",
    "inundation":           "flood_inundation",
    "observed_flood":       "flood_inundation",
    "gfm":                  "flood_inundation",
    # Geology
    "lithology":            "geology",
    "hydrogeology":         "geology",
    "permeability":         "geology",
    "porosity":             "geology",
    "glim":                 "geology",
    "glhymps":              "geology",
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


class BatchResult(list):
    """List of FetchResult (label order) returned by fetch() batch dispatch.

    Subclasses ``list`` so existing callers that iterate/index keep working,
    while failures stay visible instead of being silently dropped:

        results = fetch("streamflow", ["03245500", "bogus"], start, end)
        results.errors   # {"bogus": SourceUnavailable(...)}
        results.labels   # ["03245500", "bogus"]  (requested order, incl. failed)
    """

    def __init__(self, results: list, errors: dict, labels: list) -> None:
        super().__init__(results)
        self.errors: dict[str, Exception] = errors
        self.labels: list[str] = labels


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
    import numbers

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
        # Bare (lat, lon) or (minx, miny, maxx, maxy) → scalar tuple, NOT a
        # batch. numbers.Real covers numpy float32/float64, Decimal, etc.,
        # which `isinstance(v, (int, float))` misses (np.float32 is not a
        # Python float) — those coords would otherwise dispatch as a batch.
        if all(isinstance(v, numbers.Real) for v in geom) and len(geom) in (2, 4):
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
    region: Optional[str] = None,
    outlet: Optional[tuple[float, float]] = None,
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

    Routing / snapping overrides:
        region: Pin the routing region (a CoverageTag like 'EUROPE', 'CONUS')
            instead of auto-detecting it from the geometry centroid/bbox. Use
            when a basin straddles a region boundary or auto-detection picks
            the wrong continental chain. Invalid tags raise REGION_INVALID.
            (Note: auto-detection is centroid/bbox-based; a GaugeID always
            routes to CONUS.)
        outlet: (lat, lon) snap target for reach/gauge backends (GEOGLOWS,
            GloFAS, Open-Meteo Flood). Defaults to the geometry centroid, which
            often sits off-channel; pass a delineated basin pour point for the
            most reliable main-stem snap. Ignored by areal/gridded backends.
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
            index=index, native_resolution=native_resolution, validate=validate,
        )
        errors: dict[str, Exception] = batch["errors"]
        results = [batch["results"][lbl] for lbl in batch["labels"]
                   if lbl in batch["results"]]
        if errors and not results:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="ALL_BATCH_ITEMS_FAILED",
                message=(
                    f"All {len(errors)} batch items failed for variable={variable!r}. "
                    f"First error ({next(iter(errors))}): {next(iter(errors.values()))}"
                ),
                details={"failed_labels": sorted(errors)},
                recovery="Check geometries and backend availability (`aihydro-data doctor`).",
                next_tools=["data_doctor", "data_validate_request"],
                docs_anchor="troubleshooting",
            )
        if errors:
            log.warning(
                "fetch() batch: %d of %d items failed (labels: %s) — "
                "inspect result.errors for details.",
                len(errors), len(batch["labels"]), sorted(errors),
            )
        # BatchResult is a list (label order) carrying .errors / .labels so
        # partial failures are visible instead of silently shortening the list.
        return BatchResult(results, errors, batch["labels"])

    # ── 1. Validate ───────────────────────────────────────────────────────
    # Region override (S4): if the caller pins a region, validate it against the
    # known CoverageTags so a typo fails loudly instead of silently routing to
    # the global chain.
    if region is not None:
        from typing import get_args
        from aihydro_data.contracts import CoverageTag
        valid_regions = set(get_args(CoverageTag))
        if region not in valid_regions:
            from aihydro_data.exceptions import RegionUnsupported
            raise RegionUnsupported(
                code="REGION_INVALID",
                message=f"region={region!r} is not a known CoverageTag.",
                recovery=f"Use one of: {sorted(valid_regions)}, or omit region to auto-detect.",
                next_tools=["data_list_products"],
                docs_anchor="routing",
            )

    # Product-pin intent (S5): naming a product is an instruction, not a hint.
    # Previously `product=` was accepted and silently discarded unless the
    # caller also passed mode="manual", so an explicit pin could return a
    # different product with nothing on the result to say so. Treat the pin as
    # sufficient intent and promote the request to manual mode, recording the
    # promotion so the behaviour is visible rather than magical.
    _pin_promoted = False
    if product is not None and mode == "auto":
        mode = "manual"
        _pin_promoted = True

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
        region=region,
        outlet=outlet,
    )

    # ── 2. Coerce geometry ────────────────────────────────────────────────
    from aihydro_data.geometry import coerce_geometry
    geom = coerce_geometry(req.geometry)

    # ── 3. Detect region + build candidate list ───────────────────────────
    from aihydro_data.routing import detect_region, resolve_product_ids
    from aihydro_data.products import get_product

    # Region override (S4) short-circuits centroid/bbox auto-detection.
    region = req.region if req.region is not None else detect_region(geom)

    if mode == "manual" and product:
        primary_spec = get_product(product)
        # Fallback chain: explicit list or policy minus primary
        if fallback is not None:
            fallback_ids = [f for f in fallback if f != product]
        else:
            policy_ids = resolve_product_ids(variable, region, geom)
            fallback_ids = [pid for pid in policy_ids if pid != product]
        candidate_specs = [primary_spec] + [
            get_product(fid) for fid in fallback_ids
            if _is_registered(fid)
        ]
    else:
        candidate_ids = resolve_product_ids(variable, region, geom)
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
    # Local only: stored in the manifest so cache_read can rebuild a request.
    # It is NOT part of the key; the key uses the canonical geometry id so ring
    # start/direction and hole order do not split the cache.
    geom_wkt = geom.wkt
    from aihydro_data.geometry import geometry_id as _geometry_id
    geom_id = _geometry_id(geom)   # raises GeometryInvalid; no WKT fallback
    key_payload: dict[str, Any] = {
        # Contract schema version. Bump whenever FetchResult gains a field that
        # carries meaning rather than convenience, so entries written under the
        # old contract are not restored with defaults that are indistinguishable
        # from "the product declared nothing". A cached result missing `units`
        # is far more dangerous than a cache miss.
        "result_schema": _result_schema_for(variable),
        "variable": variable,
        "start": start,
        "end": end,
        "aggregation": aggregation,
        "geom_id": geom_id,
        "geom_key": "aihydro.geom/1",
    }
    if mode == "manual" and product:
        key_payload["product"] = product
    if index:
        key_payload["index"] = index.upper()
    if native_resolution:
        key_payload["native_resolution"] = True
    if req.outlet is not None:
        # Reach/cell backends select against this point, which can differ for
        # two requests with the same basin polygon.
        key_payload["outlet"] = tuple(req.outlet)
    ck = _make_key(key_payload)

    history: list[dict[str, str]] = []
    if cache:
        # Verify-on-read (S2): the auto-mode key is product-agnostic, so a
        # cached entry whose serving product is no longer in the current
        # candidate chain (policy changed, or a different region was detected)
        # must be treated as a MISS — never serve data a current request would
        # never have selected. Manual pins already key on the product.
        # A manual pin can still have fallbacks. A later strict request must
        # not reuse a fallback product written under the same pinned key.
        allowed = [s.id for s in candidate_specs]
        cached = cache_read(ck, req, allowed_products=allowed)
        if cached is not None:
            accepted = True
            if validate is not None:
                try:
                    accepted = bool(validate(cached))
                    reason = "rejected by validate()" if not accepted else ""
                except Exception as ve:
                    accepted = False
                    reason = f"validate() raised: {ve}"
                    log.warning("validate() raised for cached %r (%s); fetching again.", cached.product, ve)
                if not accepted:
                    history.append({
                        "product": cached.product, "source": cached.source,
                        "outcome": "rejected", "reason": f"cached result {reason}",
                    })
            if accepted:
                log.debug("Cache hit for %s (%s, product=%s).", ck, variable, cached.product)
                return cached.model_copy(update={"geometry_id": geom_id})

    # ── 5. Fetch with fallback chain ──────────────────────────────────────
    last_exc: Exception | None = None
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
            _update = {"cache_key": ck, "fallback_history": history,
                       "geometry_id": geom_id}
            if _pin_promoted:
                _update["notes"] = list(result.notes) + [
                    f"product={product!r} was pinned without mode='manual'; the "
                    f"request was promoted to manual mode so the pin is honoured. "
                    f"Pass fallback=[] to make the pin strict."
                ]
            result = result.model_copy(update=_update)
            # Write to disk cache (best-effort, never raises). Manifest
            # records WHICH product actually served the data, so the
            # provenance trail stays intact even though the key is
            # product-agnostic in auto mode.
            if cache:
                try:
                    cache_write(result, geom_wkt=geom_wkt, geom_id=geom_id)
                except Exception as ce:
                    log.debug("Cache write failed (non-fatal): %s", ce)
            return result
        except _EmptyResult as empty:
            # Empty-but-successful → reject (not "failed") and keep walking the
            # chain. Recorded distinctly so the decision trail shows it was a
            # data gap, not a backend error.
            log.info("Product %r returned empty data; trying next in chain.", spec.id)
            history.append({
                "product": spec.id, "source": spec.source,
                "outcome": "rejected", "reason": "empty result",
            })
            last_exc = empty
            continue
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

class _EmptyResult(Exception):
    """Internal sentinel: a candidate returned no usable data. Caught by the
    fetch() fallback loop and recorded as outcome='rejected' so the next
    product is tried. Never surfaces to callers."""


# Version of the FetchResult contract as far as the cache is concerned. Bump on
# any change to the fields a consumer relies on for interpretation:
#   1 -> 2  added units, timestep, resolution_m, common_pitfalls,
#           coverage_start/end, coverage_complete, days_missing_head/tail
# v3 invalidates cached native IMERG rates previously labelled as daily totals.
# v4 records product identity and avoids old CHIRPS citation metadata.
RESULT_SCHEMA_VERSION = 5

# Per-variable cache revisions. A fix that changes what one variable serves
# bumps only that variable's entry, so its old cache entries stop matching
# while every other variable's cache stays valid.
#   impervious 1: NLCD_IMPERVIOUS used to return land-cover class codes
#                 instead of percent impervious (fixed 2026-09-29).
#   streamflow 1: results used to be labelled with the product-spec unit even
#                 when the payload declared another (GEOGLOWS: ft3/s); they now
#                 record the declared unit and `units_spec` (2026-10-03).
#   dem 1:        small CONUS requests now route to 3DEP 10 m before GLO-30
#                 (2026-09-29); old GLO-30 entries for them must not be reused.
_VARIABLE_CACHE_REVISION: dict[str, int] = {"impervious": 1, "dem": 1, "streamflow": 1}


def _result_schema_for(variable: str) -> int | str:
    """Schema tag for the cache key: the global version, plus a per-variable
    revision when one is declared. Unlisted variables keep their old keys."""
    rev = _VARIABLE_CACHE_REVISION.get(variable)
    return RESULT_SCHEMA_VERSION if rev is None else f"{RESULT_SCHEMA_VERSION}.{variable}.{rev}"


# Timesteps for which a single record legitimately represents the whole window.
# A 5-yearly population epoch or a static soil grid is not "missing" the other
# 364 days of a year-long request, and flagging it as such would fire on nearly
# every fetch of those products — a warning nobody would keep reading.
_EPOCHAL_TIMESTEPS = frozenset({
    "static", "annual", "5-yearly", "yearly", "decadal", "monthly",
    "8-day", "16-day", "5-day", "climatology",
})


def _temporal_coverage(data: Any, start: str, end: str,
                       tolerance_days: int = 1,
                       timestep: str = "") -> dict[str, Any]:
    """Compare the window a caller asked for against the window actually served.

    A product whose archive ends before the requested window does not fail — it
    returns a shorter series. Downstream aggregations over that series (a rolling
    sum, a "last 7 days" total) then silently describe a period for which no data
    exists. This records the discrepancy so it can be seen and, where it matters,
    rejected.

    Returns keys matching FetchResult's coverage fields. Unknown/undatable data
    is reported as complete, so we never invent a warning we cannot substantiate.
    """
    out: dict[str, Any] = {
        "coverage_start": "", "coverage_end": "", "coverage_complete": True,
        "days_missing_head": 0, "days_missing_tail": 0,
        "coverage_tolerance_days": tolerance_days,
    }
    try:
        import pandas as pd

        idx = None
        if isinstance(data, pd.DataFrame):
            if "date" in data.columns:
                idx = pd.to_datetime(data["date"], errors="coerce")
            elif isinstance(data.index, pd.DatetimeIndex):
                idx = pd.Series(data.index)
        elif hasattr(data, "coords"):                     # xarray
            for name in ("time", "date"):
                if name in getattr(data, "coords", {}):
                    idx = pd.Series(pd.to_datetime(data.coords[name].values))
                    break
        if idx is None:
            return out

        idx = idx.dropna()
        if idx.empty:
            return out

        actual_start, actual_end = idx.min(), idx.max()
        if (timestep or "").strip().lower() in _EPOCHAL_TIMESTEPS:
            # Report the served range, but do not treat coarse epochs as gaps.
            out.update(coverage_start=str(actual_start.date()),
                       coverage_end=str(actual_end.date()),
                       coverage_complete=True)
            return out
        req_start, req_end = pd.to_datetime(start), pd.to_datetime(end)
        head = max(0, (actual_start - req_start).days)
        tail = max(0, (req_end - actual_end).days)

        out.update(
            coverage_start=str(actual_start.date()),
            coverage_end=str(actual_end.date()),
            days_missing_head=int(head),
            days_missing_tail=int(tail),
            coverage_complete=bool(head <= tolerance_days and tail <= tolerance_days),
        )
    except Exception:
        pass                                              # never fail a fetch over this
    return out


def _has_signal(data: Any) -> bool:
    """True if `data` carries at least one finite value worth keeping.

    Covers the time-series (DataFrame) and raster (xarray) cases; anything
    else (None, unknown types) is conservatively treated as having signal so
    we never reject a result we can't introspect. 'Finite' (not 'non-zero')
    so a legitimately zero-valued series — categorical landcover, a dry-season
    zero-flow record — still passes.
    """
    if data is None:
        return False
    try:
        import numpy as np
        import pandas as pd

        if isinstance(data, pd.DataFrame):
            if data.empty:
                return False
            num = data.select_dtypes("number")
            if num.shape[1] == 0:
                return len(data) > 0  # non-numeric (e.g. categorical) → trust it
            return bool(np.isfinite(num.to_numpy(dtype="float64", na_value=np.nan)).any())

        # xarray DataArray / Dataset
        if hasattr(data, "to_array"):        # Dataset
            if not data.data_vars:
                return False
            arr = data.to_array().values
        elif hasattr(data, "values") and hasattr(data, "dims"):  # DataArray
            arr = data.values
        else:
            return True
        arr = np.asarray(arr)
        if arr.size == 0:
            return False
        if np.issubdtype(arr.dtype, np.number):
            return bool(np.isfinite(arr).any())
        return True
    except Exception:
        return True


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

    # Pass the spec so backends with per-product deps (HyRiver) check the
    # right library; _call drops the kwarg for backends that don't accept it.
    ok, reason = _call(backend.is_available, spec=spec)
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

    # ── Spatial-support honesty (S1) ──────────────────────────────────────────
    # A point/reach/gauge product returns a single-location series. basin_sum
    # over such a value is spatially meaningless → reject so an areal product
    # can serve via the fallback chain. basin_mean is allowed but recorded as a
    # point value (NOT an areal average) in aggregation_actual + a note.
    support = getattr(spec, "spatial_support", "areal")
    agg_actual = _agg
    support_note: Optional[str] = None
    if support != "areal" and _agg not in ("raw_raster",):
        if _agg == "basin_sum":
            from aihydro_data.exceptions import AggregationUnsupported
            raise AggregationUnsupported(
                code="AGGREGATION_UNSUPPORTED",
                message=(
                    f"Product {spec.id!r} has {support!r} spatial support — its "
                    f"values are at a single {support.replace('_', ' ')}, so "
                    f"aggregation='basin_sum' (a catchment total) is not meaningful."
                ),
                recovery=(
                    "Use aggregation='basin_mean' to accept the single-location "
                    "series, or rely on the fallback chain to reach an areal "
                    "(gridded) product that can be summed over the basin."
                ),
                details={"product": spec.id, "spatial_support": support},
                next_tools=["data_list_products"],
                docs_anchor="aggregation",
            )
        # basin_mean / centroid on a non-areal product → single-location value
        agg_actual = f"{support}_value"
        support_note = (
            f"Values are a single {support.replace('_', ' ')} series from "
            f"{spec.id!r}, NOT an areal average over the geometry "
            f"(spatial_support={support!r})."
        )

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
        # Route through _call so optional kwargs (outlet, for reach/gauge
        # snapping backends) degrade gracefully on backends that don't accept
        # them. outlet lets callers pass a delineated pour point instead of the
        # geometry centroid for GEOGLOWS/GloFAS/Open-Meteo snapping (S5).
        data = _call(backend.fetch_timeseries, spec, geom, start, end, _agg,
                     outlet=getattr(req, "outlet", None))

    # ── Empty-result gate (S3) ────────────────────────────────────────────────
    # A backend that returns no usable data (e.g. NWIS with no record for the
    # gauge/window) is treated as a failed candidate so the fallback chain keeps
    # going, UNLESS the product opts in via allow_empty. Raised before the
    # result is built so empties are never cached.
    if not getattr(spec, "allow_empty", False) and not _has_signal(data):
        raise _EmptyResult(
            f"{spec.id!r} returned no usable data for {start}..{end}."
        )

    # Harvest backend-attached caveats (e.g. "polygon mask failed") so
    # degradations surface on the result instead of dying in debug logs.
    notes: list[str] = []
    try:
        notes = [str(n) for n in getattr(data, "attrs", {}).get("aihydro_notes", [])]
    except Exception:
        pass
    if support_note:
        notes.insert(0, support_note)

    cov = _temporal_coverage(data, start, end,
                             timestep=getattr(spec, "timestep", "") or "")
    if not cov["coverage_complete"]:
        notes.append(
            f"COVERAGE INCOMPLETE: requested {start}..{end}, "
            f"{spec.id} served {cov['coverage_start']}..{cov['coverage_end']} "
            f"({cov['days_missing_head']}d missing at start, "
            f"{cov['days_missing_tail']}d at end). Aggregations over this series "
            f"describe only the days present; absent days are not zeros."
        )

    # Unit the payload itself declared (backend-reported), else the spec's.
    spec_units = getattr(spec, "units", "") or ""
    declared_units = ""
    try:
        declared_units = str(getattr(data, "attrs", {}).get("aihydro_units", "") or "").strip()
    except Exception:
        pass
    units = declared_units or spec_units

    return FetchResult(
        **cov,
        product_identity={
            "product_id": spec.id, "source": spec.source,
            "source_dataset_id": spec.source_dataset_id,
            "dataset_version": spec.dataset_version, "variant": spec.variant,
            "temporal_derivation": spec.temporal_derivation,
            "input_dependencies": list(spec.input_dependencies),
            "units": units, "units_spec": spec_units, "units_declared": declared_units,
            "timestep": spec.timestep,
            "resolution_m": spec.resolution_m,
            "common_pitfalls": list(spec.common_pitfalls),
            "identity_basis": "configured_product", "asset_revision_verified": False,
        },
        units=units,
        units_spec=spec_units,
        units_declared=declared_units,
        timestep=getattr(spec, "timestep", "") or "",
        resolution_m=getattr(spec, "resolution_m", None),
        common_pitfalls=list(getattr(spec, "common_pitfalls", []) or []),
        variable=spec.variable,
        product=spec.id,
        source=spec.source,
        request=req,
        data=data,
        license=spec.license,
        citation=spec.citation,
        bibtex=spec.bibtex,
        spatial_support=support,
        aggregation_actual=agg_actual,
        next_steps=list(spec.next_steps),
        notes=notes,
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
    index: Optional[str] = None,
    native_resolution: bool = False,
    validate: Optional[Callable[[FetchResult], bool]] = None,
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
          "raise" → re-raise immediately; pending (not-yet-started) fetches are
                    cancelled and queued workers exit early via a stop flag.
                    NOTE: fetches already executing a network call run to
                    completion — Python futures cannot be interrupted mid-call.
          "skip"  → silently omit the failed entry
    index, native_resolution, validate
        Forwarded verbatim to each per-geometry fetch() call (spectral index
        name, native-resolution raster export, quality-gate callback).

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
    import threading

    from aihydro_data.geometry.batch import iter_geometries

    pairs = list(iter_geometries(geometries))
    if not pairs:
        return {"results": {}, "errors": {}, "labels": [], "variable": variable,
                "start": start, "end": end}

    results: dict[str, Any] = {}
    errors: dict[str, Exception] = {}
    stop = threading.Event()   # set on on_error="raise" so queued workers bail

    def _one(label_geom: tuple[str, Any]) -> tuple[str, Any]:
        label, geom = label_geom
        if stop.is_set():
            raise concurrent.futures.CancelledError(
                f"fetch_batch aborted before label={label!r} started."
            )
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
            index=index,
            native_resolution=native_resolution,
            validate=validate,
        )
        return label, result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_one, pair): pair[0] for pair in pairs}
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            try:
                lbl, res = future.result()
                results[lbl] = res
            except concurrent.futures.CancelledError:
                pass  # worker bailed after stop was set — original error propagates
            except Exception as exc:
                if on_error == "raise":
                    # future.cancel() only stops futures that haven't started;
                    # the stop flag makes already-queued workers exit at entry.
                    # In-flight network calls still run to completion.
                    stop.set()
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
