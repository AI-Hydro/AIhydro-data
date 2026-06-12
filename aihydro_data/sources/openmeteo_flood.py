"""
Open-Meteo Flood backend — instant GloFAS-equivalent river discharge via REST.

Open-Meteo's Global Flood API (https://open-meteo.com/en/docs/flood-api) serves
**GloFAS v4** river discharge (0.05° ≈ 5 km) through a plain, key-free REST endpoint.
It is, in effect, "GloFAS data without the EWDS queue": the same modelled numbers our
cds_glofas backend would return, but synchronously in one HTTP call.

Role in the fallback chain — an availability cushion:
    Primary global streamflow is GEOGLOWS (independent model, 1940→present, reach
    level). Open-Meteo sits *after* GEOGLOWS and *before* the async GloFAS/EWDS
    backend: if GEOGLOWS is briefly unreachable, this still returns instant data.

Caveats (documented in the ProductSpec):
    • Historical reanalysis ends ~July 2022 (GloFAS v4 Seamless); recent dates are
      served from forecast/forecast-record blends, not consolidated reanalysis.
    • It exposes no river topology, so its server-side "largest river within the
      5 km cell" pick CANNOT be refined the way GEOGLOWS' reach walk can. For a
      coordinate that sits off the main channel the returned reach may be a
      tributary. Supply a basin centroid that falls on the main stem, or prefer
      GEOGLOWS when precise snapping matters.
    • MODELLED, not observed — never present as gauge truth.

Only dependency is `requests` (a base dependency) — no extra needed.
Auth: none (non-commercial use is unrestricted).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)

_FLOOD_ENDPOINT = "https://flood-api.open-meteo.com/v1/flood"

# Minimum specific discharge (m³/s per km²) that a real GloFAS channel cell should
# produce. If mean Q falls below area_km2 × _SPECIFIC_Q_FLOOR, the query landed on
# a hillslope or missing-channel cell → raise SourceUnavailable to force fallback.
# Validated against NWIS (2026-06-04): Little Miami (3,116 km²) returned 0.66 m³/s
# (specific Q = 0.000212, below floor) vs observed 62 m³/s. Missouri (1.36M km²)
# returned 0.80 m³/s (specific Q = 5.9e-7, far below floor) vs observed 5,923 m³/s.
# Threshold = 1e-3 m³/s/km² (1 L/s/km²). Only applied when polygon area is known.
_SPECIFIC_Q_FLOOR = 1e-3


class Backend(SourceBackend):
    """Open-Meteo Global Flood (GloFAS v4) river-discharge backend (REST, no auth)."""

    source_id = "openmeteo_flood"

    # ── Capability / availability ────────────────────────────────────────────

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": ["streamflow"],
            "coverage": ["global"],
            "requires_auth": [],
            "requires_extras": [],   # only `requests`, a base dep
        }

    def is_available(self) -> tuple[bool, Optional[str]]:
        try:
            import requests  # noqa: F401
        except ImportError as exc:
            return False, f"Open-Meteo backend needs `requests` ({exc.name} missing)."
        return True, None

    # ── Time-series fetch (the public contract) ──────────────────────────────

    def fetch_timeseries(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
        outlet: Optional[tuple[float, float]] = None,
    ) -> Any:
        """Return daily river discharge at `geometry`'s outlet/centroid.

        `outlet` (lat, lon), when supplied, overrides the centroid as the query
        point — supply a main-stem pour point so the 0.05° cell pick lands on
        the channel rather than a hillslope.

        Returns pd.DataFrame[date, streamflow]  (streamflow in m³/s).
        """
        import pandas as pd
        import requests
        from aihydro_data.exceptions import SourceUnavailable, DateOutOfRange
        from aihydro_data.sources._retry import call_with_retry

        from aihydro_data.geometry.measures import outlet_and_area
        lat, lon, area_km2 = outlet_and_area(geometry, outlet)  # area None for points
        cfg = spec.backend_config

        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "river_discharge",
            "start_date": start,
            "end_date": end,
            "cell_selection": cfg.get("cell_selection", "nearest"),
            "ensemble": False,
        }
        try:
            r = call_with_retry(
                lambda: requests.get(_FLOOD_ENDPOINT, params=params, timeout=60),
                label="openmeteo_flood.get",
            )
        except Exception as exc:
            raise SourceUnavailable(
                code="OPENMETEO_REQUEST_FAILED",
                message=f"Open-Meteo Flood API request failed: {str(exc)[:160]}",
                recovery="Retry; if it persists, GEOGLOWS/GloFAS are the fallbacks.",
                next_tools=["data_doctor"],
                docs_anchor="products#openmeteo",
            ) from exc

        if r.status_code != 200:
            # Open-Meteo returns {"error": true, "reason": "..."} for bad ranges.
            reason = ""
            try:
                reason = r.json().get("reason", "")
            except Exception:
                reason = r.text[:160]
            if "date" in reason.lower() or r.status_code == 400:
                raise DateOutOfRange(
                    code="OPENMETEO_DATE_OUT_OF_RANGE",
                    message=f"Open-Meteo rejected the date range: {reason}",
                    recovery="Open-Meteo flood reanalysis covers ~1984→present; "
                             "narrow the window or use GEOGLOWS (1940→present).",
                    next_tools=["data_doctor"],
                    docs_anchor="products#openmeteo",
                )
            raise SourceUnavailable(
                code="OPENMETEO_HTTP_ERROR",
                message=f"Open-Meteo Flood API HTTP {r.status_code}: {reason}",
                recovery="Retry; GEOGLOWS/GloFAS are the fallbacks.",
                next_tools=["data_doctor"],
                docs_anchor="products#openmeteo",
            )

        payload = r.json()
        daily = payload.get("daily") or {}
        times = daily.get("time") or []
        qvals = daily.get("river_discharge") or []
        df = pd.DataFrame({
            "date": pd.to_datetime(times),
            "streamflow": pd.to_numeric(qvals, errors="coerce"),
        }).dropna(subset=["streamflow"]).reset_index(drop=True)

        df.attrs["openmeteo_cell"] = {"lat": lat, "lon": lon,
                                      "cell_selection": cfg.get("cell_selection", "nearest")}

        # Cell-miss detection: when a polygon basin is supplied, check whether the
        # returned mean discharge is plausible for that basin size. A GloFAS channel
        # cell must produce at least _SPECIFIC_Q_FLOOR m³/s per km² of basin area;
        # values below this indicate the query landed on a hillslope or missing cell.
        # Validated against NWIS (2026-06-04): catches Missouri (5.9e-7) and
        # Little Miami (2.1e-4) misses while passing Potomac (14.5) and Neuse (11.7).
        if area_km2 and len(df) > 0:
            mean_q = float(df["streamflow"].mean())
            specific_q = mean_q / area_km2
            if specific_q < _SPECIFIC_Q_FLOOR:
                raise SourceUnavailable(
                    code="OPENMETEO_CELL_MISS",
                    message=(
                        f"Open-Meteo returned mean Q={mean_q:.3f} m³/s for a basin "
                        f"of {area_km2:.0f} km² (specific discharge={specific_q:.2e} "
                        f"m³/s/km² < floor {_SPECIFIC_Q_FLOOR:.0e}). The query "
                        f"coordinate landed on a hillslope or missing-channel GloFAS "
                        f"cell, not the main river. Open-Meteo exposes no topology "
                        f"to correct this; falling back to GloFAS/EWDS."
                    ),
                    recovery=(
                        "Open-Meteo can't resolve this basin. GloFAS/EWDS is the "
                        "next fallback. Alternatively, supply a basin centroid that "
                        "falls on the main-stem channel (avoid confluences and braided "
                        "reaches where the 0.05° cell is ambiguous)."
                    ),
                    next_tools=["data_fetch_background", "data_doctor"],
                    docs_anchor="products#openmeteo",
                )

        mean_q_log = df["streamflow"].mean() if len(df) else float("nan")
        log.info(
            "Open-Meteo Flood (%.3f, %.3f) → %d days, mean Q=%.1f m³/s",
            lat, lon, len(df), mean_q_log,
        )
        return df

    def fetch_raster(self, spec: ProductSpec, geometry: Any, start: str, end: str) -> Any:
        raise NotImplementedError(
            "Open-Meteo Flood serves point/cell discharge time series only, not rasters."
        )

    # Geometry → outlet point + basin area: see geometry/measures.py
    # (outlet_and_area), shared with the GEOGLOWS and GloFAS snap backends.
