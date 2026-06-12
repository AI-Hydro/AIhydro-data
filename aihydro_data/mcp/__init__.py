"""
Phase 7 — MCP tool registration for aihydro-data.

Exposes these tools into the `aihydro.tools` entry-point group so that
any aihydro-tools MCP server with aihydro-data installed will auto-discover:

    data_fetch              — single-geometry fetch with full fallback
    data_batch_fetch        — parallel fetch over N geometries
    data_list_products      — discover available products (filterable)
    data_describe_product   — full ProductSpec for one product
    data_validate_request   — pre-flight dry-run (no network cost)
    data_get_cache_status   — disk cache summary
    data_invalidate_cache   — remove a cached entry
    data_help               — guided onboarding / topic browser

Design: each tool follows the ai-hydro structured-response convention
(see helpers.py in aihydro-tools):
  - success → plain dict / list (JSON-serialisable)
  - failure → {"error": True, "code": ..., "message": ..., "recovery": ...,
               "next_tools": [...], "docs_anchor": "..."}

This module is import-safe even without fastmcp installed:
`register_tools()` is a no-op when fastmcp is absent and raises a
clear AuthRequired-style message at call-time instead.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _err(code: str, message: str, recovery: str = "", next_tools: list[str] | None = None,
         docs_anchor: str = "", **details: Any) -> dict[str, Any]:
    """Build a structured error envelope matching the aihydro convention."""
    return {
        "error": True,
        "code": code,
        "message": message,
        "recovery": recovery,
        "next_tools": next_tools or [],
        "docs_anchor": docs_anchor,
        **({"details": details} if details else {}),
    }


def _result_to_dict(result: Any) -> dict[str, Any]:
    """Serialise a FetchResult to a JSON-safe dict for MCP transport."""
    import pandas as pd

    data_summary: dict[str, Any] = {}
    if hasattr(result, "data") and result.data is not None:
        d = result.data
        if isinstance(d, pd.DataFrame):
            data_summary = {
                "type": "DataFrame",
                "rows": len(d),
                "columns": list(d.columns),
                "head": d.head(5).to_dict(orient="records"),
            }
        else:
            # xarray or other
            data_summary = {
                "type": type(d).__name__,
                "repr": str(d)[:400],
            }

    return {
        "variable": result.variable,
        "product": result.product,
        "source": result.source,
        "cache_hit": result.cache_hit,
        "cache_key": result.cache_key,
        "fetched_at": result.fetched_at,
        "license": result.license,
        "citation": result.citation,
        # Spatial-support honesty: tells the agent whether `data` is an areal
        # aggregate or a single-location (point/reach/gauge) series.
        "spatial_support": getattr(result, "spatial_support", "areal"),
        "aggregation_actual": getattr(result, "aggregation_actual", ""),
        "next_steps": result.next_steps,
        "notes": result.notes,
        "data": data_summary,
    }


# ── tool implementations ──────────────────────────────────────────────────────

# ── Backends that block for minutes (queued HPC) ─────────────────────────────
# If the resolved product routes to one of these sources, data_fetch returns
# an immediate redirect to data_fetch_background (the async job-dispatch tool)
# instead of blocking the agent loop for minutes while the queue drains.
_QUEUED_SOURCES: frozenset[str] = frozenset({
    "cds_glofas",    # GloFAS EWDS — queued Copernicus HPC, typically 1–30 min
})


def _would_route_to_queued_source(variable: str, geometry: Any, product: str | None) -> tuple[bool, str]:
    """Return (True, source_id) if the request would route to a queued backend.

    Mirrors the fetch pipeline's fallback walk: the request only blocks on a
    queued backend when the FIRST candidate that is both registered and
    available is queued. A queued product sitting later in the chain (e.g.
    GLOFAS as the last-resort streamflow fallback) must NOT trigger a
    redirect — the instant candidates ahead of it will serve.

    Uses the routing layer (detect_region + resolve_product_ids + registry)
    plus cheap backend availability probes (import/credential-file checks,
    no network). Returns (False, '') when the route is fast or cannot be
    determined.
    """
    try:
        from aihydro_data.routing import detect_region, resolve_product_ids
        from aihydro_data.products import get_product
        from aihydro_data.geometry import coerce_geometry
        from aihydro_data.sources.base import get_backend

        geom = coerce_geometry(geometry)
        region = detect_region(geom)
        if product:
            candidates = [product]
        else:
            candidates = resolve_product_ids(variable, region)

        for pid in candidates:
            try:
                spec = get_product(pid)
            except KeyError:
                continue  # unregistered → the fetch loop would skip it too
            try:
                ok, _reason = get_backend(spec.source).is_available()
            except Exception:
                ok = False
            if not ok:
                continue  # unavailable → fetch loop falls through to the next
            # First viable candidate decides the route.
            return (spec.source in _QUEUED_SOURCES, spec.source if spec.source in _QUEUED_SOURCES else "")
    except Exception:
        pass
    return False, ""


def _data_fetch(
    variable: str,
    geometry: Any,
    start: str,
    end: str,
    mode: str = "auto",
    product: str | None = None,
    aggregation: str = "basin_mean",
    cache: bool = True,
) -> dict[str, Any]:
    """
    Fetch a single hydrology variable for one geometry / time window.

    Args:
        variable:    Canonical variable name ('precipitation', 'tmax', 'et', …).
                     Call data_list_products() with no args to see all variables.
        geometry:    One of: (lat, lon) tuple, [minx, miny, maxx, maxy] bbox,
                     GeoJSON dict, WKT string, or a dict with 'type'+'coordinates'.
        start:       ISO-8601 start date, e.g. '2015-01-01'.
        end:         ISO-8601 end date (inclusive), e.g. '2015-12-31'.
        mode:        'auto' (router picks best product for the region) or
                     'manual' (must also supply product=).
        product:     Product ID, e.g. 'CHIRPS', 'GRIDMET_PRECIP'. Required if
                     mode='manual'; ignored otherwise.
        aggregation: How to aggregate the raster over the geometry.
                     'basin_mean' (default) → 1-D time series.
                     'raw_raster' → full clipped xarray.DataArray.
        cache:       True (default) — read from / write to disk cache.

    Returns:
        On success: result dict with keys variable, product, source, cache_hit,
                    data (head + shape), license, citation, next_steps.
        On failure: structured error envelope with recovery hints.

    Note — slow backends (GloFAS / queued HPC):
        For variables that route to a queued backend (e.g. global streamflow →
        GloFAS EWDS), this tool returns an immediate redirect to
        ``data_fetch_background`` rather than blocking the agent loop for minutes.
        Use data_fetch_background() + get_data_fetch_result() for those variables.
    """
    from aihydro_data._pipeline import fetch
    from aihydro_data.exceptions import AihydroDataError

    # ── Pre-flight: redirect queued backends before they block ────────────────
    # Check if the route resolves to a backend that queues on remote HPC
    # (e.g. GloFAS on EWDS). If so, return immediately with a redirect message
    # instead of blocking the entire agent loop for minutes.
    is_queued, queued_source = _would_route_to_queued_source(variable, geometry, product)
    if is_queued:
        return {
            "error": False,
            "redirect": True,
            "code": "USE_ASYNC_TOOL",
            "message": (
                f"data_fetch cannot be used for variable='{variable}' in this region — "
                f"it routes to '{queued_source}', a queued backend that takes 1–30 minutes "
                "and would block the agent loop. Use data_fetch_background() instead."
            ),
            "action": "Use data_fetch_background() with the same arguments, then poll "
                      "with get_data_fetch_result(job_id) every 60 s until status='complete'.",
            "example_call": {
                "tool": "data_fetch_background",
                "arguments": {
                    "variable": variable,
                    "geometry": geometry,
                    "start": start,
                    "end": end,
                    **({"product": product} if product else {}),
                    "aggregation": aggregation,
                },
            },
            "next_tools": ["data_fetch_background", "get_data_fetch_result"],
        }

    try:
        result = fetch(
            variable=variable,
            geometry=geometry,
            start=start,
            end=end,
            mode=mode,  # type: ignore[arg-type]
            product=product,
            aggregation=aggregation,  # type: ignore[arg-type]
            cache=cache,
        )
        return _result_to_dict(result)
    except AihydroDataError as exc:
        return exc.to_dict()
    except Exception as exc:
        return _err(
            "UNEXPECTED_ERROR",
            f"Unexpected error during fetch: {exc}",
            recovery="Check variable name and geometry format. Call data_list_products() for valid variables.",
            next_tools=["data_list_products", "data_validate_request"],
        )


def _data_batch_fetch(
    variable: str,
    geometries: list[Any],
    start: str,
    end: str,
    labels: list[str] | None = None,
    mode: str = "auto",
    product: str | None = None,
    aggregation: str = "basin_mean",
    max_workers: int = 4,
    on_error: str = "warn",
) -> dict[str, Any]:
    """
    Parallel fetch over N geometries (e.g. a set of watersheds or gauges).

    Args:
        variable:     Same as data_fetch.
        geometries:   List of geometries. Each can be (lat, lon), bbox, GeoJSON dict,
                      or WKT string.
        start/end:    Same as data_fetch. Applied to all geometries.
        labels:       Optional list of string labels, same length as geometries.
                      If omitted, labels become "0", "1", …
        mode/product/aggregation: Same as data_fetch.
        max_workers:  Thread count for parallel fetching (default 4).
        on_error:     'warn' (default) — log failures and continue.
                      'raise' — abort on first error.
                      'skip'  — silently omit failures.

    Returns:
        {
          "variable": str,
          "labels": [str, ...],
          "results": {label: result_dict, ...},
          "errors":  {label: error_dict, ...},
          "summary": {"succeeded": int, "failed": int}
        }
    """
    from aihydro_data._pipeline import fetch_batch
    from aihydro_data.exceptions import AihydroDataError

    # Build input: list of (label, geom) pairs or plain list
    if labels:
        geom_input = list(zip(labels, geometries))
    else:
        geom_input = geometries

    try:
        raw = fetch_batch(
            variable=variable,
            geometries=geom_input,
            start=start,
            end=end,
            mode=mode,  # type: ignore[arg-type]
            product=product,
            aggregation=aggregation,  # type: ignore[arg-type]
            max_workers=max_workers,
            on_error=on_error,  # type: ignore[arg-type]
        )
    except AihydroDataError as exc:
        return exc.to_dict()
    except Exception as exc:
        return _err(
            "UNEXPECTED_ERROR",
            f"Batch fetch failed: {exc}",
            next_tools=["data_fetch", "data_list_products"],
        )

    results_out = {lbl: _result_to_dict(r) for lbl, r in raw["results"].items()}
    errors_out = {
        lbl: (e.to_dict() if hasattr(e, "to_dict") else {"error": True, "message": str(e)})
        for lbl, e in raw["errors"].items()
    }
    return {
        "variable": raw.get("variable", variable),
        "start": raw.get("start", start),
        "end": raw.get("end", end),
        "labels": raw.get("labels", []),
        "results": results_out,
        "errors": errors_out,
        "summary": {
            "succeeded": len(results_out),
            "failed": len(errors_out),
        },
    }


def _data_list_products(
    variable: str | None = None,
    region: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """
    Discover available data products, optionally filtered.

    Args:
        variable: Filter by variable name ('precipitation', 'tmax', …).
                  Omit to list everything.
        region:   Filter by coverage region ('CONUS', 'global', 'S_ASIA', …).
        source:   Filter by backend ('gee', 'hyriver', 'direct_api', 'stac').

    Returns:
        List of product dicts, each with: id, variable, source, coverage,
        temporal_start, temporal_end, resolution_m, units, license,
        requires_extras, requires_auth, common_pitfalls, examples.
    """
    from aihydro_data.products import list_products as _list

    specs = _list(variable=variable, region=region, source=source)
    out = []
    for s in specs:
        out.append({
            "id": s.id,
            "variable": s.variable,
            "source": s.source,
            "coverage": s.coverage,
            "temporal_start": s.temporal_start,
            "temporal_end": s.temporal_end,
            "resolution_m": s.resolution_m,
            "timestep": s.timestep,
            "units": s.units,
            "license": s.license,
            "requires_extras": s.requires_extras,
            "requires_auth": s.requires_auth,
            "common_pitfalls": s.common_pitfalls,
            "examples": s.examples,
        })
    return out


def _data_describe_product(product_id: str) -> dict[str, Any]:
    """
    Return the full ProductSpec for a single product, including citation,
    BibTeX, and agent-facing next_steps.

    Args:
        product_id: Case-sensitive product ID, e.g. 'CHIRPS', 'GRIDMET_PRECIP'.
                    Call data_list_products() to browse available IDs.

    Returns:
        Full product spec dict, or error envelope if not found.
    """
    from aihydro_data.products import get_product

    try:
        spec = get_product(product_id)
    except KeyError:
        spec = None
    if spec is None:
        return _err(
            "PRODUCT_NOT_FOUND",
            f"No product registered with id={product_id!r}.",
            recovery="Call data_list_products() to browse available product IDs.",
            next_tools=["data_list_products"],
        )
    return spec.model_dump()


def _data_validate_request(
    variable: str,
    geometry: Any,
    start: str,
    end: str,
    product: str | None = None,
    aggregation: str = "basin_mean",
) -> dict[str, Any]:
    """
    Pre-flight dry-run — validate a request without hitting any backend.

    Checks:
      - variable is known
      - geometry can be coerced
      - region is detected and a product chain exists
      - product temporal range covers (start, end)
      - extras needed are installed

    Returns a structured report so the agent can surface issues before
    consuming compute budget.

    Args:
        variable:    Canonical variable name.
        geometry:    Same formats as data_fetch geometry arg.
        start/end:   ISO-8601 date strings.
        product:     Optional specific product ID to validate against.
        aggregation: Aggregation mode to validate.

    Returns:
        {
          "ok": bool,
          "issues": [{"code": str, "message": str, "fix": str}, ...],
          "candidates_in_priority": [product_id, ...],
          "detected_region": str,
          "warnings": [str, ...]
        }
    """
    from aihydro_data.geometry import coerce_geometry
    from aihydro_data.routing import detect_region, resolve_product_ids
    from aihydro_data.products import get_product
    from aihydro_data.exceptions import AihydroDataError

    issues: list[dict[str, str]] = []
    warnings: list[str] = []
    candidates: list[str] = []
    detected_region = "unknown"

    # 1. geometry
    try:
        geom = coerce_geometry(geometry)
        detected_region = detect_region(geom)
    except Exception as exc:
        issues.append({
            "code": "GEOMETRY_INVALID",
            "message": f"Could not parse geometry: {exc}",
            "fix": "Pass (lat, lon) tuple, a bbox (minx, miny, maxx, maxy), or a GeoJSON dict.",
        })
        geom = None

    # 2. variable + routing
    if geom is not None:
        try:
            candidates = resolve_product_ids(variable, detected_region)
        except AihydroDataError:
            issues.append({
                "code": "NO_PRODUCTS_FOR_REGION",
                "message": (
                    f"No products registered for variable={variable!r} "
                    f"in region={detected_region!r}."
                ),
                "fix": (
                    "Call data_list_products(variable=...) to see supported regions, "
                    "or specify product= in manual mode."
                ),
            })

    # 3. specific product validation
    target_ids = [product] if product else candidates[:3]
    for pid in target_ids:
        spec = get_product(pid)
        if spec is None:
            issues.append({
                "code": "PRODUCT_NOT_FOUND",
                "message": f"Product {pid!r} is not registered.",
                "fix": "Call data_list_products() for valid product IDs.",
            })
            continue

        # date range check
        if spec.temporal_start and spec.temporal_start != "present":
            if start < spec.temporal_start:
                issues.append({
                    "code": "DATE_OUT_OF_RANGE",
                    "message": (
                        f"{pid}: start={start!r} is before product temporal_start "
                        f"({spec.temporal_start!r})."
                    ),
                    "fix": f"Set start='{spec.temporal_start}' or pick a different product.",
                })
        if spec.temporal_end and spec.temporal_end not in ("present", ""):
            if end > spec.temporal_end:
                warnings.append(
                    f"{pid}: end={end!r} may exceed product temporal_end "
                    f"({spec.temporal_end!r}) — backend will likely clip."
                )

        # extras check
        for extra in spec.requires_extras:
            try:
                __import__(extra.replace("-", "_"))
            except ImportError:
                issues.append({
                    "code": "MISSING_EXTRAS",
                    "message": f"{pid} requires the [{extra}] extra which is not installed.",
                    "fix": f"pip install aihydro-data[{extra}]",
                })

        # pitfalls surfacing
        for pitfall in spec.common_pitfalls:
            warnings.append(f"{pid}: {pitfall}")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "candidates_in_priority": candidates,
        "detected_region": detected_region,
        "warnings": warnings,
    }


def _data_get_cache_status() -> dict[str, Any]:
    """
    Return a summary of the disk cache at ~/.aihydro/cache/data/.

    Returns:
        {
          "cache_dir": str,
          "entry_count": int,
          "total_size_mb": float,
          "entries": [
            {"cache_key": str, "variable": str, "product": str,
             "fetched_at": str, "size_kb": float},
            ...
          ]
        }
    """
    from aihydro_data.cache import cache_status
    return cache_status()


def _data_invalidate_cache(cache_key: str) -> dict[str, Any]:
    """
    Remove a specific entry from the disk cache.

    Deletes the .parquet/.nc data file and .manifest.json sidecar for the
    given cache key. Use data_get_cache_status() to browse available keys.

    Args:
        cache_key: 24-character hex cache key from a previous FetchResult
                   or from data_get_cache_status().

    Returns:
        {"deleted": bool, "cache_key": str}
    """
    from aihydro_data.cache import cache_invalidate
    deleted = cache_invalidate(cache_key)
    return {"deleted": deleted, "cache_key": cache_key}


def _data_doctor() -> dict[str, Any]:
    """
    Environment health check — probes each backend, auth state, cache size,
    and version compatibility. Agents call this when something goes wrong
    to figure out *what* is wrong before guessing.

    Returns:
        {
          "ok": bool,                       # all checks green
          "version": str,                   # aihydro-data version
          "python": str,                    # major.minor
          "backends": {
            "gee":        {"installed": bool, "available": bool, "reason": str},
            "hyriver":    {"installed": bool, "available": bool, "reason": str},
            "direct_api": {"installed": bool, "available": bool, "reason": str},
            "stac":       {"installed": bool, "available": bool, "reason": str},
          },
          "auth": {
            "gee": {"present": bool, "path": str},
          },
          "cache": {"dir": str, "entry_count": int, "total_size_mb": float},
          "warnings": [str, ...],
          "recommendations": [str, ...],     # one-liners the agent should act on
        }
    """
    import sys
    from pathlib import Path
    from aihydro_data import __version__
    from aihydro_data.cache import cache_status
    from aihydro_data.sources.base import get_backend

    backends_report: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    recommendations: list[str] = []

    for src in ("gee", "hyriver", "direct_api", "stac"):
        entry: dict[str, Any] = {"installed": False, "available": False, "reason": ""}
        try:
            be = get_backend(src)
            entry["installed"] = True
            try:
                ok, reason = be.is_available()
                entry["available"] = bool(ok)
                entry["reason"] = reason or ""
                if not ok:
                    warnings.append(f"{src}: {reason}")
                    if "auth" in (reason or "").lower():
                        recommendations.append(
                            f"Run `aihydro-data auth {src}` to set up credentials."
                        )
                    elif "install" in (reason or "").lower() or "pip" in (reason or "").lower():
                        recommendations.append(
                            f"Run `pip install aihydro-data[{src}]` to enable {src}."
                        )
            except Exception as exc:
                entry["reason"] = f"is_available() raised: {exc}"
                warnings.append(entry["reason"])
        except Exception as exc:
            entry["reason"] = f"backend load failed: {exc}"
        backends_report[src] = entry

    # GEE auth (check standard credential path)
    gee_cred = Path.home() / ".config" / "earthengine" / "credentials"
    auth_report = {
        "gee": {"present": gee_cred.exists(), "path": str(gee_cred)},
    }
    if not gee_cred.exists():
        recommendations.append(
            "GEE not authed: run `python -c \"import ee; ee.Authenticate()\"`."
        )

    # Cache snapshot (best-effort)
    try:
        cs = cache_status()
        cache_report = {
            "dir": cs.get("cache_dir", ""),
            "entry_count": cs.get("entry_count", 0),
            "total_size_mb": cs.get("total_size_mb", 0.0),
        }
        if cs.get("total_size_mb", 0) > 1024:
            recommendations.append(
                f"Cache is {cs['total_size_mb']:.0f} MB — consider "
                "data_invalidate_cache() for stale entries."
            )
    except Exception as exc:
        cache_report = {"error": str(exc)}

    return {
        "ok": all(b["available"] for b in backends_report.values() if b["installed"]),
        "version": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "backends": backends_report,
        "auth": auth_report,
        "cache": cache_report,
        "warnings": warnings,
        "recommendations": recommendations,
    }


def _data_help(topic: str | None = None) -> str | dict[str, Any]:
    """
    Guided onboarding and topic reference for aihydro-data.

    Args:
        topic: One of: 'first_fetch', 'auth', 'fallback', 'batch',
               'products', 'caching', 'deprecations', 'errors'.
               Pass None (or omit) to see the topic menu.

    Returns:
        A markdown string with the topic content, or a dict listing
        available topics when called with no argument.
    """
    topics = {
        "first_fetch":    "first_fetch.md",
        "auth":           "auth.md",
        "fallback":       "fallback.md",
        "batch":          "batch.md",
        "products":       "products.md",
        "caching":        "caching.md",
        "deprecations":   "deprecations.md",
        "errors":         "errors.md",
    }

    if topic is None:
        return {
            "message": (
                "aihydro-data help browser. Call data_help(topic=<name>) for details."
            ),
            "topics": list(topics.keys()),
            "quick_start": (
                "1. data_list_products()            — discover what's available\n"
                "2. data_validate_request(...)       — dry-run before fetching\n"
                "3. data_fetch(variable, geometry, start, end)  — fetch data\n"
                "4. data_get_cache_status()          — check disk cache"
            ),
        }

    from pathlib import Path
    help_dir = Path(__file__).parent.parent / "help_topics"
    fname = topics.get(topic)
    if fname is None:
        return (
            f"Unknown topic {topic!r}. Available: {', '.join(topics)}. "
            "Call data_help() with no args for the menu."
        )

    p = help_dir / fname
    if p.exists():
        return p.read_text(encoding="utf-8")
    return (
        f"Help file for topic={topic!r} not found at {p}. "
        "This is a bug — please report it."
    )


# ── entry-point callable ──────────────────────────────────────────────────────

def register_tools(mcp: Any | None = None) -> None:
    """
    Register all data_* MCP tools on `mcp` (a FastMCP server instance).

    Called by the aihydro-tools registry when it discovers this package via
    the `aihydro.tools` entry-point group: the registry passes in the shared
    FastMCP singleton (see invoke_plugin_registrars). `mcp` is therefore
    supplied by the caller in normal operation.

    Dependency direction: aihydro-data must NEVER import the ai_hydro tools
    package (that would be a sideways edge). The host MCP server is injected,
    not imported. If called with no argument, fall back only to data's OWN
    server module; otherwise no-op (raw-Python callers import the functions
    directly).
    """
    if mcp is None:
        try:
            from aihydro_data.mcp.app import get_server
            mcp = get_server()
        except Exception:
            log.debug(
                "aihydro-data: no FastMCP server passed and no local server module — "
                "skipping tool registration. Import data_fetch / data_list_products "
                "etc. directly, or pass an mcp server into register_tools(mcp)."
            )
            return

    # Register each tool using @mcp.tool() decorator-style via tool()
    tool = getattr(mcp, "tool", None)
    if tool is None:
        log.warning("aihydro-data: mcp object has no .tool() method — cannot register tools.")
        return

    mcp.tool(name="data_fetch")(_data_fetch)
    mcp.tool(name="data_batch_fetch")(_data_batch_fetch)
    mcp.tool(name="data_list_products")(_data_list_products)
    mcp.tool(name="data_describe_product")(_data_describe_product)
    mcp.tool(name="data_validate_request")(_data_validate_request)
    mcp.tool(name="data_get_cache_status")(_data_get_cache_status)
    mcp.tool(name="data_invalidate_cache")(_data_invalidate_cache)
    mcp.tool(name="data_doctor")(_data_doctor)
    mcp.tool(name="data_help")(_data_help)

    log.info("aihydro-data: registered 9 MCP tools (data_fetch, data_batch_fetch, …).")


# Re-export the tool functions under clean public names so callers can also
# import them directly without going through an MCP server:
#   from aihydro_data.mcp import data_fetch
data_fetch = _data_fetch
data_batch_fetch = _data_batch_fetch
data_list_products = _data_list_products
data_describe_product = _data_describe_product
data_validate_request = _data_validate_request
data_get_cache_status = _data_get_cache_status
data_invalidate_cache = _data_invalidate_cache
data_doctor = _data_doctor
data_help = _data_help

__all__ = [
    "register_tools",
    "data_fetch",
    "data_batch_fetch",
    "data_list_products",
    "data_describe_product",
    "data_validate_request",
    "data_get_cache_status",
    "data_invalidate_cache",
    "data_doctor",
    "data_help",
]
