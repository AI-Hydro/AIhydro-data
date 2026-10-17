"""
Direct-API backend.

Wraps REST APIs that don't go through GEE or HyRiver's OPeNDAP stack:
  - USGS NWIS daily values  (USGS Water Data OGC API via sources/usgs_waterdata.py;
                             the legacy waterservices path is the D3 switch only)
  - CHIRPS_IRI precipitation (via IRI Data Library OPeNDAP — auth-free)
  - Open-Meteo temperature + PET (no auth, global, 1940-present, centroid-based)
  - GRDC monthly streamflow (Phase 4)

All imports are lazy — this module is safe to import without extras installed.

Install: nothing extra for NWIS (requests + pandas are base dependencies)
         pip install aihydro-data[legacy-usgs]  (D3 switch only, removed 2027-01-08)
         pip install aihydro-data[opendap]   (for CHIRPS_IRI/xarray+netCDF4)
         requests                            (for Open-Meteo — already a core dep)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources._common import require_import
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)


def _gauge_id_from_geometry(geometry: Any) -> str | None:
    """
    Extract a USGS gauge ID from various input shapes:
      - GaugeID wrapper (set by coerce_geometry)
      - raw string passed through (defensive)
      - anything else → None (caller should fall back to spatial lookup)

    Normalisation: strips 'USGS-' / 'USGS:' prefixes, then zero-pads numeric
    IDs back to at least 8 digits per the USGS convention. Non-numeric IDs
    are passed through uppercased.
    """
    # GaugeID wrapper from geometry.coerce_geometry
    raw = None
    if getattr(geometry, "geom_type", None) == "GaugeID":
        raw = geometry.id
    elif isinstance(geometry, str):
        raw = geometry
    if raw is None:
        return None

    sid = raw.strip().upper().replace("USGS-", "").replace("USGS:", "")
    return sid.zfill(8) if sid.isdigit() else sid


LEGACY_REMOVAL_DATE = "2027-01-08"      # D3: the legacy waterservices switch is deleted on this date


def _frame_from_series(ser: Any) -> tuple[Any, dict, dict]:
    """UsgsSeries -> the served NWIS frame (+ recomputed quality summary, upstream)."""
    from aihydro_data.sources import usgs_waterdata as uw

    df = ser.frame.copy()
    n_null = int(df["value_native"].isna().sum())
    df = df.dropna(subset=["value_native"]).reset_index(drop=True)
    out = df.rename(columns={"time": "date", "value_m3s": "streamflow"})
    out["qualifier"] = out["qualifier"].map(lambda q: ",".join(q))
    out = out[["date", "streamflow", "value_native", "approval_status", "approval_code",
               "qualifier", "qualifier_code"]]
    summary = uw.summarize_frame(df, null_dropped=n_null)
    summary.update({"units_native": ser.units_native, "time_series_id": ser.time_series_id,
                    "site_id": ser.site_id, "backend": "usgs_waterdata"})
    return out, summary, dict(ser.upstream)


def _legacy_waterservices_daily(site: str, start: str, end: str, param_cd: str, stat_cd: str):
    """D3 switch (AIHYDRO_USGS_BACKEND=legacy_waterservices): legacy dataretrieval.nwis path.
    Exists only until LEGACY_REMOVAL_DATE; needs ``pip install aihydro-data[legacy-usgs]``."""
    import datetime as _dt
    import pandas as pd
    from aihydro_data.exceptions import SourceUnavailable
    from aihydro_data.sources import usgs_waterdata as uw

    require_import("dataretrieval", extra="legacy-usgs", backend="usgs_legacy_waterservices")
    import dataretrieval.nwis as nwis       # noqa: WPS433  (D3 branch; the only permitted reference)

    bare = site[5:] if site.startswith("USGS-") else site
    df, _meta = nwis.get_dv(sites=bare, parameterCd=param_cd, statCd=stat_cd, start=start, end=end)
    if df is None or df.empty:
        raise uw._err("USGS_NO_DATA", f"legacy waterservices returned no rows for {site}", site_id=site)
    df = df.reset_index()
    qcols = [c for c in df.columns if param_cd in str(c) and not str(c).endswith("_cd")]
    if len(qcols) != 1:
        raise uw._err("USGS_MULTIPLE_SERIES", f"legacy columns ambiguous: {qcols}", site_id=site)
    qc = qcols[0]
    cdcol = f"{qc}_cd"
    date_col = "datetime" if "datetime" in df.columns else df.columns[0]
    dates = pd.to_datetime(df[date_col], utc=True).dt.tz_localize(None)
    codes = df[cdcol].fillna("").astype(str) if cdcol in df.columns else pd.Series([""] * len(df))
    tokens = [set(c.split()) for c in codes]
    bad = sorted({t for ts in tokens for t in ts} - {"A", "P", "e"})
    if bad or any(not ({"A", "P"} & ts) for ts in tokens):
        raise uw._err("USGS_APPROVAL_UNKNOWN", f"legacy qualifier codes not understood: {bad or 'missing A/P'}")
    val = pd.to_numeric(df[qc], errors="coerce")
    out = pd.DataFrame({
        "date": dates, "streamflow": val * uw.CFS_TO_M3S, "value_native": val,
        "approval_status": ["Approved" if "A" in ts else "Provisional" for ts in tokens],
        "approval_code": ["A" if "A" in ts else "P" for ts in tokens],
        "qualifier": ["ESTIMATED" if "e" in ts else "" for ts in tokens],
        "qualifier_code": ["e" if "e" in ts else "" for ts in tokens],
    })
    n_null = int(out["value_native"].isna().sum())
    out = out.dropna(subset=["value_native"]).reset_index(drop=True)
    summary = uw.summarize_frame(out.rename(columns={"date": "time", "streamflow": "value_m3s"})
                                 .assign(qualifier=lambda d: d["qualifier"].map(lambda q: tuple(t for t in q.split(",") if t))),
                                 null_dropped=n_null)
    summary.update({"units_native": "ft^3/s", "site_id": site, "backend": "legacy_waterservices"})
    upstream = {"backend": "legacy_waterservices", "api_version": "waterservices/nwis/dv",
                "retrieved_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "request_urls": [], "response_sha256": [], "last_modified": [], "etag": [], "n_pages": 1,
                "removal_date": LEGACY_REMOVAL_DATE,
                "note": "legacy waterservices path: decommissioned 2027-02-22; this switch is deleted "
                        f"{LEGACY_REMOVAL_DATE}; response bytes were not captured"}
    return out, summary, upstream


class Backend(SourceBackend):
    """Direct-API backend (NWIS, GRDC, …)."""

    source_id = "direct_api"

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": ["streamflow", "precipitation", "tmax", "tmin", "pet"],
            "coverage": ["CONUS", "global"],
            "requires_auth": [],
            "requires_extras": ["opendap"],
        }

    def is_available(self) -> tuple[bool, Optional[str]]:
        # The direct_api backend hosts NWIS (needs only requests), CHIRPS_IRI
        # (needs xarray + netCDF4), and Open-Meteo (needs only requests — always
        # available).  We report available=True if ANY dep is present; each
        # fetch method does its own finer-grained check.
        try:
            import requests  # noqa: F401
            return True, None
        except ImportError:
            pass
        for pkg in ("xarray",):
            try:
                __import__(pkg)
                return True, None
            except ImportError:
                continue
        return False, (
            "direct_api backend has no usable dependencies installed. "
            "NWIS streamflow needs only `requests` (a base dependency). "
            "For CHIRPS_IRI precipitation: `pip install aihydro-data[opendap]`."
        )

    def fetch_timeseries(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
        outlet: Any = None,
    ) -> Any:
        """Return a pd.DataFrame with date + variable columns.

        ``outlet`` is (lat, lon) as everywhere in fetch(); only NWIS uses it
        (gauge resolution under ADR-003 A2)."""
        cfg = spec.backend_config
        service = cfg.get("service", "nwis_dv")

        if service == "nwis_dv":
            return self._fetch_nwis_dv(spec, cfg, geometry, start, end, outlet=outlet)

        if service == "chirps_iri":
            return self._fetch_chirps_iri(spec, cfg, geometry, start, end)

        if service == "open_meteo":
            return self._fetch_open_meteo(spec, cfg, geometry, start, end)

        raise NotImplementedError(f"direct_api service {service!r} not yet implemented.")

    def fetch_raster(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        raise NotImplementedError(
            "direct_api backend does not support raster fetches — "
            "it serves point-gauge time series only."
        )

    # ── NWIS ─────────────────────────────────────────────────────────────

    def _fetch_nwis_dv(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
        outlet: Any = None,
    ) -> Any:
        """USGS daily discharge via the Water Data OGC API.

        The gauge is resolved by ADR-003 amendment A2 (``usgs_waterdata.resolve_gauge``): an
        explicit id, else an outlet checked by drainage-area agreement. There is no centroid
        walk and no ``sites[0]``. Identity refusals are ``TerminalSourceError`` and are never
        answered by a modelled product. The served frame keeps USGS's per-value approval and
        qualifier; the resolution, quality summary and upstream provenance ride on
        ``data.attrs`` into FetchResult / ManifestEntry.
        """
        import pandas as pd
        from aihydro_data.sources import usgs_waterdata as uw

        param_cd = cfg.get("parameter_code", "00060")
        stat_cd = cfg.get("stat_code", "00003")

        site_no = _gauge_id_from_geometry(geometry)
        if site_no is not None:
            res = uw.resolve_gauge(gauge_id=site_no)
        else:
            lonlat = None
            if outlet is not None:                       # fetch() convention: (lat, lon)
                lonlat = (float(outlet[1]), float(outlet[0]))
            elif getattr(geometry, "geom_type", None) == "Point":
                lonlat = (float(geometry.x), float(geometry.y))
            res = uw.resolve_gauge(geometry, outlet=lonlat)
        log.info("NWIS gauge %s resolved by %s (rule %s).", res.site, res.method, res.rule_version)

        if os.environ.get("AIHYDRO_USGS_BACKEND", "").strip().lower() == "legacy_waterservices":
            frame, summary, upstream = _legacy_waterservices_daily(res.site, start, end, param_cd, stat_cd)
        else:
            ser = uw.get_daily(res.site, start, end, param_cd, stat_cd)
            frame, summary, upstream = _frame_from_series(ser)
        frame.attrs.update({
            "aihydro_units": "m3/s",
            "aihydro_site_resolution": res.to_dict(),
            "aihydro_quality": summary,
            "aihydro_upstream": upstream,
        })
        if res.warnings:
            frame.attrs["aihydro_notes"] = list(res.warnings)
        return frame

    # ── CHIRPS via IRI OPeNDAP ────────────────────────────────────────────

    def _fetch_chirps_iri(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        """
        Fetch CHIRPS v2 daily precipitation via IRI Data Library OPeNDAP.

        Auth-free fallback — no GEE account required.  Uses server-side
        subsetting so only the spatial ROI and date window are transferred.

        Requires: pip install aihydro-data[opendap]  (xarray + netCDF4)
        """
        from aihydro_data.exceptions import SourceUnavailable, DateOutOfRange

        # ── Runtime dependency checks (lazy) ──────────────────────────────
        xr = require_import("xarray", extra="opendap", backend="chirps_iri")
        require_import("netCDF4", extra="opendap", backend="chirps_iri")

        import numpy as np
        import pandas as pd
        from datetime import datetime

        # ── Date range guard ──────────────────────────────────────────────
        CHIRPS_START = datetime(1981, 1, 1)
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt   = datetime.strptime(end,   "%Y-%m-%d")
        if end_dt < CHIRPS_START:
            raise DateOutOfRange(
                code="CHIRPS_IRI_DATE_TOO_EARLY",
                message=(
                    f"CHIRPS starts 1981-01-01; requested end={end} is before that."
                ),
                recovery="Set start/end to 1981-01-01 or later.",
                next_tools=["data_list_products"],
                docs_anchor="products#chirps",
            )
        # Clamp start to CHIRPS epoch quietly
        if start_dt < CHIRPS_START:
            start_dt = CHIRPS_START
            log.debug("CHIRPS_IRI: clamping start to 1981-01-01.")

        # ── Geometry → bounding box ───────────────────────────────────────
        # geometry.bounds → (minx=west, miny=south, maxx=east, maxy=north)
        try:
            west, south, east, north = geometry.bounds
        except (AttributeError, TypeError, ValueError):
            # Point with no .bounds or centroid-only geometry
            c = geometry.centroid
            west = east  = c.x
            south = north = c.y

        # Expand a degenerate point or very small box to guarantee ≥1 CHIRPS
        # pixel (0.05°) is captured — avoids empty spatial slice.
        PAD = 0.06   # just over half a pixel on each side
        if (east - west) < PAD:
            mid_x = (east + west) / 2
            west, east = mid_x - PAD, mid_x + PAD
        if (north - south) < PAD:
            mid_y = (north + south) / 2
            south, north = mid_y - PAD, mid_y + PAD

        # ── Open OPeNDAP dataset (header only — no data yet) ──────────────
        url = cfg["iri_url"]
        lon_dim  = cfg.get("lon_dim",  "X")
        lat_dim  = cfg.get("lat_dim",  "Y")
        time_dim = cfg.get("time_dim", "T")
        varname  = cfg.get("variable", "prcp")

        log.debug("CHIRPS_IRI: opening OPeNDAP dataset header …")
        try:
            ds = xr.open_dataset(url, engine="netcdf4")
        except Exception as exc:
            raise SourceUnavailable(
                code="CHIRPS_IRI_CONNECT_FAILED",
                message=(
                    f"Could not open CHIRPS IRI OPeNDAP endpoint: {exc}. "
                    "Check network connectivity and try again. "
                    "The IRI Data Library may be temporarily down."
                ),
                recovery=(
                    "Verify connectivity: curl -I "
                    "https://iridl.ldeo.columbia.edu. "
                    "Use GEE CHIRPS (product='CHIRPS') as an alternative."
                ),
                next_tools=["data_doctor"],
                docs_anchor="products#chirps-iri",
            ) from exc

        # ── Convert dates → T-axis values ─────────────────────────────────
        # IRI CHIRPS T coordinate is Julian days from an internal epoch.
        # We don't need to know the epoch: the first T value corresponds
        # to 1981-01-01 so we compute offsets relative to that.
        t0_val = float(ds[time_dim].values[0])
        start_jd = t0_val + (start_dt - CHIRPS_START).days
        end_jd   = t0_val + (end_dt   - CHIRPS_START).days

        # ── Server-side spatial + temporal subset ─────────────────────────
        # Y is stored North→South (descending), so slice(north, south) is
        # correct for descending coordinates.
        log.debug(
            "CHIRPS_IRI: subsetting lon=[%.2f, %.2f], lat=[%.2f, %.2f], "
            "T=[%.0f, %.0f] …",
            west, east, south, north, start_jd, end_jd,
        )
        try:
            sub = ds[varname].sel(
                **{
                    lon_dim:  slice(west, east),
                    lat_dim:  slice(north, south),  # descending Y → N first
                    time_dim: slice(start_jd, end_jd),
                }
            )
            # Mask fill / non-finite / impossible values PER CELL, then take the
            # spatial mean over what is left. After averaging a fill value is
            # indistinguishable from data (D4: a ~1e33 fill became "precipitation").
            from aihydro_data._physical import (
                bounds_for, declared_fill_values, mask_invalid,
            )
            cells = sub.load()
            masked, n_masked = mask_invalid(
                cells.values,
                fill_values=declared_fill_values(ds[varname].attrs, ds[varname].encoding),
                bounds=bounds_for("precipitation", "mm/day"),
            )
            if n_masked:
                log.warning("CHIRPS_IRI: masked %d fill/non-finite/impossible cell values.", n_masked)
            vals: np.ndarray = cells.copy(data=masked).mean(dim=[lon_dim, lat_dim]).values
        except Exception as exc:
            ds.close()
            raise SourceUnavailable(
                code="CHIRPS_IRI_FETCH_FAILED",
                message=f"CHIRPS IRI OPeNDAP subset/load failed: {exc}",
                recovery="Check geometry bounds and date range. The IRI server may be busy.",
                next_tools=["data_doctor"],
                docs_anchor="products#chirps-iri",
            ) from exc
        finally:
            try:
                ds.close()
            except Exception:
                pass

        if len(vals) > 0 and not np.isfinite(vals).any():
            raise SourceUnavailable(
                code="CHIRPS_IRI_ALL_MASKED",
                message=(
                    "CHIRPS IRI returned only fill / non-finite / impossible values for "
                    "this geometry and window (e.g. ocean or out-of-coverage cells); "
                    "refusing to serve them as precipitation."
                ),
                recovery="Use GEE CHIRPS (product='CHIRPS') or another precipitation product.",
                next_tools=["data_doctor"],
                docs_anchor="products#chirps-iri",
            )

        if len(vals) == 0:
            import pandas as pd
            log.warning("CHIRPS_IRI: empty result for window %s–%s.", start, end)
            return pd.DataFrame(columns=["date", "precipitation"])

        # Generate dates from known start — avoids float32 precision loss on
        # large Julian day values (same workaround as pyprep/get_chirps.py).
        dates = pd.date_range(
            start_dt.strftime("%Y-%m-%d"), periods=len(vals), freq="D"
        )
        df = pd.DataFrame({"date": dates, "precipitation": vals.astype(float)})
        df["precipitation"] = df["precipitation"].clip(lower=0)
        df["date"] = pd.to_datetime(df["date"])
        # Trim to exact requested window (in case of rounding)
        df = df[
            (df["date"] >= pd.Timestamp(start)) &
            (df["date"] <= pd.Timestamp(end))
        ].reset_index(drop=True)

        log.debug(
            "CHIRPS_IRI: returned %d days, mean=%.2f mm/day.",
            len(df), df["precipitation"].mean() if len(df) else float("nan"),
        )
        return df

    # ── Open-Meteo (no auth, global, 1940-present) ────────────────────────

    def _fetch_open_meteo(
        self,
        spec: ProductSpec,
        cfg: dict[str, Any],
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:
        """
        Fetch daily temperature or PET from Open-Meteo ERA5 reanalysis archive.

        Auth-free, global, 1940-present.  Uses the geometry centroid for the
        request (basin_mean approximation — accurate for point/small basins;
        good-enough for large basins at ERA5's ~0.25° resolution).

        API: https://archive-api.open-meteo.com/v1/archive
        Variable mapping (backend_config["om_variable"]):
            temperature_2m_max          → tmax (°C)
            temperature_2m_min          → tmin (°C)
            et0_fao_evapotranspiration  → pet (mm/day)

        Requires: requests (standard dep — always available)
        """
        from aihydro_data.exceptions import SourceUnavailable, DateOutOfRange

        import requests

        import pandas as pd
        from datetime import datetime

        # ── Date range guard ──────────────────────────────────────────────
        OM_START = datetime(1940, 1, 1)
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt   = datetime.strptime(end,   "%Y-%m-%d")
        if end_dt < OM_START:
            raise DateOutOfRange(
                code="OPEN_METEO_DATE_TOO_EARLY",
                message=(
                    f"Open-Meteo archive starts 1940-01-01; requested end={end} is before that."
                ),
                recovery="Set start/end to 1940-01-01 or later.",
                next_tools=["data_list_products"],
                docs_anchor="products#open-meteo",
            )
        if start_dt < OM_START:
            log.debug("Open-Meteo: clamping start to 1940-01-01.")
            start = "1940-01-01"

        # ── Geometry → centroid ───────────────────────────────────────────
        try:
            centroid = geometry.centroid
            lat, lon = centroid.y, centroid.x
        except Exception:
            # Geometry may not have a centroid (e.g. already a Point)
            try:
                lat, lon = geometry.y, geometry.x
            except Exception as exc:
                from aihydro_data.exceptions import GeometryInvalid
                raise GeometryInvalid(
                    code="OPEN_METEO_GEOMETRY_INVALID",
                    message=f"Cannot extract centroid from geometry: {exc}",
                    recovery="Pass a valid Shapely geometry (Point, Polygon, etc.).",
                    next_tools=["data_help"],
                    docs_anchor="geometry",
                ) from exc

        om_variable = cfg["om_variable"]
        result_col  = cfg["result_column"]

        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude":  round(lat, 4),
            "longitude": round(lon, 4),
            "start_date": start,
            "end_date":   end,
            "daily":      om_variable,
            "timezone":   "UTC",
        }

        log.debug(
            "Open-Meteo: fetching %s at (%.4f, %.4f) for %s–%s",
            om_variable, lat, lon, start, end,
        )

        try:
            resp = requests.get(url, params=params, timeout=30)
        except Exception as exc:
            raise SourceUnavailable(
                code="OPEN_METEO_CONNECT_FAILED",
                message=f"Open-Meteo request failed: {exc}",
                recovery=(
                    "Check your internet connection. "
                    "Open-Meteo is free and auth-free — no credentials needed."
                ),
                next_tools=["data_doctor"],
                docs_anchor="products#open-meteo",
            ) from exc

        if resp.status_code != 200:
            raise SourceUnavailable(
                code="OPEN_METEO_HTTP_ERROR",
                message=(
                    f"Open-Meteo returned HTTP {resp.status_code}: {resp.text[:200]}"
                ),
                recovery="Verify the date range and geometry coordinates.",
                next_tools=["data_doctor"],
                docs_anchor="products#open-meteo",
            )

        payload = resp.json()
        daily = payload.get("daily", {})
        times  = daily.get("time", [])
        values = daily.get(om_variable, [])

        if not times or not values:
            log.warning(
                "Open-Meteo: empty response for %s at (%.4f, %.4f) %s–%s.",
                om_variable, lat, lon, start, end,
            )
            return pd.DataFrame(columns=["date", result_col])

        df = pd.DataFrame({
            "date":    pd.to_datetime(times),
            result_col: [float(v) if v is not None else float("nan") for v in values],
        })

        # Apply optional unit conversion (cfg key: "unit_conversion", default 1.0)
        scale = cfg.get("unit_conversion", 1.0)
        if scale != 1.0:
            df[result_col] = df[result_col] * scale

        df = df.dropna(subset=[result_col]).reset_index(drop=True)

        log.debug(
            "Open-Meteo: returned %d days of %s, mean=%.2f.",
            len(df), result_col, df[result_col].mean() if len(df) else float("nan"),
        )
        return df

