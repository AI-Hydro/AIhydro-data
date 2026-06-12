"""
Disk cache + provenance manifest.

Cache layout:
    ~/.aihydro/cache/data/
        <cache_key>.parquet          ← time-series (pd.DataFrame)
        <cache_key>.nc               ← raster (xr.DataArray / Dataset)
        <cache_key>.manifest.json    ← provenance sidecar (list of ManifestEntry)

Public surface:
    cache_key(payload)          → str (deterministic 24-char hash)
    cache_dir()                 → Path
    cache_read(ck)              → FetchResult | None
    cache_write(result, req)    → None
    cache_invalidate(ck)        → bool
    cache_status()              → dict  (disk usage, entry count, …)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from aihydro_data.contracts import FetchRequest, FetchResult

log = logging.getLogger(__name__)

# ── Key ───────────────────────────────────────────────────────────────────

def cache_key(payload: Any) -> str:
    """
    Deterministic hash for a request payload — used as the cache filename
    and the FetchResult.cache_key field even when caching is disabled.

    Delegates to aihydro-core's shared ``content_hash`` so the entire platform
    (jobs, features, cache, aihydro-data) hashes through ONE implementation —
    no silent drift where the same params produce different keys in different
    layers. ``length=24`` preserves the historical 24-char key width.

    aihydro-core is a hard dependency (declared in pyproject) — it is a pure
    stdlib substrate with zero heavy deps — so there is no import fallback here.
    """
    from aihydro_core.primitives.hashing import content_hash as _core_hash
    return _core_hash(payload, length=24)


# ── Directory ─────────────────────────────────────────────────────────────

def cache_dir() -> Path:
    """Return (and create if absent) the default cache directory."""
    d = Path.home() / ".aihydro" / "cache" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Read ──────────────────────────────────────────────────────────────────

def cache_read(
    ck: str,
    req: Optional["FetchRequest"] = None,
    allowed_products: Optional[list[str]] = None,
) -> Optional["FetchResult"]:
    """
    Return a cached FetchResult if one exists for `ck`, else None.

    Reads .parquet (time series) or .nc (raster) depending on whichever
    file exists. Reconstructs a FetchResult from the manifest sidecar.

    `allowed_products` (verify-on-read): when given, a cached entry whose
    serving product is NOT in this list is treated as a miss. The auto-mode
    cache key is product-agnostic, so this guards against serving data that
    the current routing policy would never select (policy change, different
    detected region). Pass the current candidate chain's product ids; pass
    None to disable the check (manual pins, which already key on product).
    """
    from aihydro_data.cache.manifest import latest_manifest

    d = cache_dir()
    parquet_path = d / f"{ck}.parquet"
    nc_path = d / f"{ck}.nc"

    if not parquet_path.exists() and not nc_path.exists():
        return None

    manifest = latest_manifest(d, ck)
    if manifest is None:
        log.warning("Cache hit for %s but no manifest — skipping.", ck)
        return None

    if allowed_products is not None and manifest.product not in allowed_products:
        log.debug(
            "Cache entry %s served by %r is not in the current candidate chain "
            "%s — treating as miss (verify-on-read).",
            ck, manifest.product, allowed_products,
        )
        return None

    try:
        if parquet_path.exists():
            import pandas as pd
            data = pd.read_parquet(parquet_path)
        else:
            import xarray as xr
            ds = xr.open_dataset(nc_path)
            # Recover a DataArray when the cached raster was single-variable.
            # xr.open_dataset adds rioxarray's 0-dim `spatial_ref` scalar as a
            # data_var, so filter to proper N-dim arrays to find the payload.
            nd_vars = [v for v in ds.data_vars if ds[v].ndim > 0]
            if len(nd_vars) == 1:
                data = ds[nd_vars[0]]   # restore as DataArray
            else:
                data = ds
    except Exception as exc:
        log.warning("Cache read failed for %s: %s — treating as miss.", ck, exc)
        return None

    from aihydro_data.contracts import FetchResult
    from aihydro_data.contracts import FetchRequest as FR

    # Reconstruct a minimal FetchRequest for the result (may not match exactly)
    _req = req or FR(
        variable=manifest.variable,
        geometry=manifest.geom_wkt,
        start=manifest.start,
        end=manifest.end,
        aggregation=manifest.aggregation,  # type: ignore[arg-type]
    )

    return FetchResult(
        variable=manifest.variable,
        product=manifest.product,
        source=manifest.source,  # type: ignore[arg-type]
        request=_req,
        fetched_at=manifest.fetched_at,
        cache_key=ck,
        cache_hit=True,
        data=data,
        license=manifest.license,
        citation=manifest.citation,
        bibtex=manifest.bibtex,
        spatial_support=getattr(manifest, "spatial_support", "areal"),
        aggregation_actual=getattr(manifest, "aggregation_actual", ""),
        next_steps=[],
        notes=[f"Served from disk cache (fetched {manifest.fetched_at[:10]})."],
    )


# ── Write ─────────────────────────────────────────────────────────────────

def cache_write(result: "FetchResult", geom_wkt: str = "") -> None:
    """
    Persist a FetchResult to disk and write its manifest sidecar.

    Skips silently on any IO error (cache is best-effort).
    """
    from aihydro_data.cache.manifest import ManifestEntry, write_manifest

    d = cache_dir()
    ck = result.cache_key or cache_key({
        "variable": result.variable,
        "product": result.product,
        "fetched_at": result.fetched_at,
    })

    data = result.data
    data_file = ""

    try:
        try:
            import pandas as pd
            if isinstance(data, pd.DataFrame):
                out = d / f"{ck}.parquet"
                data.to_parquet(out, index=False, compression="snappy")
                data_file = str(out)
        except ImportError:
            pass

        if not data_file:
            try:
                import xarray as xr
                if isinstance(data, (xr.DataArray, xr.Dataset)):
                    out = d / f"{ck}.nc"
                    # Drop rioxarray's 0-dim `spatial_ref` scalar — it ends up
                    # as a data_var on netCDF round-trip, confusing auto_plot.
                    # Also strip any non-serialisable attributes (e.g. stackstac's
                    # RasterSpec object) that would crash the netCDF encoder.
                    _to_save = data
                    if isinstance(data, xr.DataArray) and "spatial_ref" in data.coords:
                        _to_save = data.drop_vars("spatial_ref")
                    # Scrub non-primitive attrs and object-dtype coordinates
                    # that netCDF4 cannot encode (e.g. stackstac's RasterSpec,
                    # rasterio Affine objects, STAC proj:* coordinate arrays).
                    import numpy as _np
                    _to_save = _to_save.copy(deep=False)
                    # 1. Strip non-serialisable DataArray-level attrs
                    _to_save.attrs = {
                        k: v for k, v in _to_save.attrs.items()
                        if isinstance(v, (str, int, float, bytes, bool, list, tuple))
                    }
                    # 2. Drop coordinates whose dtype is 'object' or whose values
                    #    cannot be serialised (covers proj:shape, proj:transform,
                    #    proj:bbox from stackstac and similar).
                    if isinstance(_to_save, xr.DataArray):
                        _drop_coords = [
                            c for c, v in _to_save.coords.items()
                            if v.dtype == object
                            or not isinstance(v.values.flat[0] if v.size > 0
                                              else 0.0,
                                              (int, float, _np.integer,
                                               _np.floating, str, bytes))
                        ]
                        if _drop_coords:
                            _to_save = _to_save.drop_vars(_drop_coords,
                                                          errors="ignore")
                    _to_save.to_netcdf(out)
                    data_file = str(out)
            except ImportError:
                pass

        if not data_file:
            log.debug("Cache write skipped for %s — data type %s not serialisable.", ck, type(data).__name__)
            return

    except Exception as exc:
        log.warning("Cache write failed for %s: %s", ck, exc)
        return

    entry = ManifestEntry(
        cache_key=ck,
        variable=result.variable,
        product=result.product,
        source=result.source,
        start=result.request.start,
        end=result.request.end,
        geom_wkt=geom_wkt or getattr(result.request, "_geom_wkt", ""),
        aggregation=result.request.aggregation,
        fetched_at=result.fetched_at,
        license=result.license,
        citation=result.citation,
        bibtex=result.bibtex,
        data_file=data_file,
        spatial_support=getattr(result, "spatial_support", "areal"),
        aggregation_actual=getattr(result, "aggregation_actual", ""),
    )
    try:
        write_manifest(d, entry)
    except Exception as exc:
        log.warning("Manifest write failed for %s: %s", ck, exc)


# ── Invalidate ────────────────────────────────────────────────────────────

def cache_invalidate(ck: str) -> bool:
    """
    Remove all cache files for `ck` (data + manifest).
    Returns True if any file was deleted, False if nothing was found.
    """
    d = cache_dir()
    deleted = False
    for suffix in (".parquet", ".nc", ".manifest.json"):
        p = d / f"{ck}{suffix}"
        if p.exists():
            p.unlink()
            deleted = True
    return deleted


# ── Status ────────────────────────────────────────────────────────────────

def cache_status() -> dict[str, Any]:
    """
    Return a summary of disk cache usage.

    Returns:
        {
          "cache_dir": str,
          "entry_count": int,        # number of unique cache keys
          "total_size_mb": float,
          "entries": [               # one dict per key
            {"cache_key": str, "variable": str, "product": str,
             "fetched_at": str, "size_kb": float},
            ...
          ]
        }
    """
    from aihydro_data.cache.manifest import read_manifest

    d = cache_dir()
    entries_out: list[dict[str, Any]] = []
    total_bytes = 0

    # Collect all unique cache keys via manifest files
    keys: set[str] = set()
    for mf in d.glob("*.manifest.json"):
        keys.add(mf.stem.replace(".manifest", ""))

    for ck in sorted(keys):
        manifests = read_manifest(d, ck)
        latest = manifests[-1] if manifests else None
        key_bytes = 0
        for suffix in (".parquet", ".nc"):
            p = d / f"{ck}{suffix}"
            if p.exists():
                key_bytes += p.stat().st_size
        total_bytes += key_bytes
        entries_out.append({
            "cache_key": ck,
            "variable": latest.variable if latest else "",
            "product": latest.product if latest else "",
            "fetched_at": latest.fetched_at[:10] if latest else "",
            "size_kb": round(key_bytes / 1024, 1),
        })

    return {
        "cache_dir": str(d),
        "entry_count": len(keys),
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        "entries": entries_out,
    }
