"""
GEOGLOWS v2 retrospective backend — global modelled river discharge, NO auth, NO queue.

GEOGLOWS (https://geoglows.ecmwf.int) is ECMWF's global hydrologic model: the IFS
meteorological reanalysis routed through RAPID on a ~1-million-reach river network
(TDX-Hydro). The retrospective ("hindcast") simulation covers **1940→present** at
**daily** resolution and is published as anonymous-access **Zarr on AWS S3** plus a
REST data service hosted by ECMWF.

Why this backend exists — robustness:
    The legacy global-streamflow path (GloFAS via the Copernicus EWDS) is queued,
    auth-gated (~/.cdsapirc + licence) and async-only — minutes-to-hours per pull.
    GEOGLOWS removes BOTH fragilities: the retrospective Zarr is open S3 (no token,
    no queue) and a basin returns ~86 yr of daily Q in a few seconds. It is also
    reach-level (vs GloFAS 0.05° grid) so it resolves far smaller basins.

Snapping (the only hard part, mirrors cds_glofas.py methodology):
    A point dropped near a big river snaps to the nearest *reach outlet*, which for
    an off-channel coordinate is often a small tributary (empirically ~1 m³/s next
    to a ~14,000 m³/s main stem at Mississippi/Vicksburg). We fix this topologically:

      1. NEAREST reach   — REST `getriverid?lat&lon` returns the closest reach id.
      2. DOWNSTREAM WALK — follow `DSLINKNO` in the model metadata table, collecting
         each reach's `USContArea` (upstream contributing area, a *static* lookup —
         no discharge download needed).
      3. AREA-MATCH      — if a delineated basin area is supplied (polygon input),
         pick the reach in the chain whose upstream area best matches the basin.
         This lands on the basin-outlet reach (the main channel) instead of the
         tributary the naive snap found. For a bare point with no area target we
         keep the nearest reach (same contract as GloFAS point mode).

All imports are lazy — safe to import without the [geoglows] extra installed.

Install:  pip install aihydro-data[geoglows]      (geoglows + s3fs + zarr)
Auth:     none — anonymous AWS Open Data S3.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)

_REST_ENDPOINT = "https://geoglows.ecmwf.int/api/v2"

# Snap-quality thresholds (snap_uparea / target_basin_area).
# Validated against 5 NWIS gauges (2019): correct snaps had ratio 0.99–1.04;
# wrong snaps had ratio 0.00033–0.27 and NSE < −0.2.
_SNAP_RATIO_FAIL = 0.05   # below this → definitely wrong reach → raise, force fallback
_SNAP_RATIO_WARN = 0.30   # below this → suspicious → warn but return data


class Backend(SourceBackend):
    """GEOGLOWS v2 retrospective river-discharge backend (anonymous AWS S3 Zarr)."""

    source_id = "geoglows_retro"

    def __init__(self) -> None:
        self._meta_idx = None  # memoised metadata table (LINKNO-indexed)

    # ── Capability / availability ────────────────────────────────────────────

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": ["streamflow"],
            "coverage": ["global"],
            "requires_auth": [],            # anonymous S3 — no token
            "requires_extras": ["geoglows"],
        }

    def is_available(self) -> tuple[bool, Optional[str]]:
        try:
            import geoglows   # noqa: F401
            import s3fs       # noqa: F401
            import zarr       # noqa: F401
        except ImportError as exc:
            return False, (
                f"GEOGLOWS backend needs the [geoglows] extra ({exc.name} missing). "
                "Run `pip install aihydro-data[geoglows]`."
            )
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
        """Snap `geometry` to a GEOGLOWS reach and return daily Q.

        `outlet` (lat, lon), when supplied, is used as the snap target instead
        of the geometry centroid — pass a delineated basin pour point for the
        most reliable main-channel snap (the centroid often sits off-channel).

        Returns pd.DataFrame[date, streamflow]  (streamflow in m³/s).
        """
        self._assert_available()
        import pandas as pd
        import geoglows

        outlet_lat, outlet_lon, target_area_km2 = self._outlet_and_area(geometry)
        if outlet is not None:
            outlet_lat, outlet_lon = float(outlet[0]), float(outlet[1])
            log.info("GEOGLOWS: using caller-supplied outlet (%.4f, %.4f) for snap.",
                     outlet_lat, outlet_lon)

        # Warn for bare-point input — snapping is unreliable without basin area.
        if target_area_km2 is None:
            log.warning(
                "GEOGLOWS: bare-point input (%.3f, %.3f) — snap will use nearest reach "
                "only, which often lands on a tributary. Supply a delineated basin polygon "
                "for reliable area-match snapping.",
                outlet_lat, outlet_lon,
            )

        # 1–3. Snap to the main-channel reach.
        snap = self._snap(outlet_lat, outlet_lon, target_area_km2, spec)
        river_id = snap["river_id"]

        # Snap-quality gate: compare snap upstream area to target basin area.
        # Validated thresholds (2026-06-04, 5 NWIS gauges): correct snaps had ratio
        # 0.99–1.04; bad snaps that caused >10× discharge errors had ratio 0.00–0.27.
        if target_area_km2 and snap.get("uparea_km2"):
            ratio = snap["uparea_km2"] / target_area_km2
            if ratio < _SNAP_RATIO_FAIL:
                from aihydro_data.exceptions import SourceUnavailable
                raise SourceUnavailable(
                    code="GEOGLOWS_SNAP_MISMATCH",
                    message=(
                        f"GEOGLOWS snap quality check failed: snapped reach has "
                        f"upstream area {snap['uparea_km2']:.0f} km² but target basin "
                        f"is {target_area_km2:.0f} km² (ratio={ratio:.3f} < "
                        f"{_SNAP_RATIO_FAIL}). The nearest reach is almost certainly "
                        f"a tributary, not the basin outlet."
                    ),
                    recovery=(
                        "Supply a properly delineated basin polygon "
                        "(use delineate_watershed_from_point first) so area-match "
                        "snapping can find the correct main-channel reach. "
                        "Open-Meteo or GloFAS are the next fallbacks."
                    ),
                    next_tools=["delineate_watershed_from_point", "data_doctor"],
                    docs_anchor="products#geoglows",
                )
            elif ratio < _SNAP_RATIO_WARN:
                log.warning(
                    "GEOGLOWS snap quality warning: snapped reach upstream area "
                    "%.0f km² vs target %.0f km² (ratio=%.2f). The snap may be a "
                    "tributary — results should be treated with caution. Use a "
                    "delineated basin polygon for reliable snapping.",
                    snap["uparea_km2"], target_area_km2, ratio,
                )

        # 4. Pull the daily retrospective series for that reach (anonymous S3 Zarr).
        df_raw = geoglows.data.retro_daily(river_id)
        # retro_daily returns a DataFrame indexed by datetime with one column
        # named by the river_id (Q in m³/s).
        col = df_raw.columns[0]
        idx = pd.to_datetime(df_raw.index)
        # retro_daily indexes in UTC (tz-aware); drop tz so it compares cleanly
        # with the naive start/end Timestamps below.
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        df = pd.DataFrame({
            "date": idx,
            "streamflow": df_raw[col].astype(float).values,
        })
        df = df.dropna(subset=["streamflow"])
        df = df[(df["date"] >= pd.Timestamp(start)) &
                (df["date"] <= pd.Timestamp(end))].reset_index(drop=True)

        df.attrs["geoglows_snap"] = snap
        log.info(
            "GEOGLOWS snapped (%.3f, %.3f) → reach %s via %s; uparea=%s km² "
            "vs basin=%s km²; mean Q=%.1f m³/s over %d days",
            outlet_lat, outlet_lon, river_id, snap["strategy"],
            f"{snap.get('uparea_km2'):.0f}" if snap.get("uparea_km2") else "n/a",
            f"{target_area_km2:.0f}" if target_area_km2 else "n/a",
            df["streamflow"].mean() if len(df) else float("nan"), len(df),
        )
        return df

    def fetch_raster(self, spec: ProductSpec, geometry: Any, start: str, end: str) -> Any:
        raise NotImplementedError(
            "GEOGLOWS serves snapped reach discharge time series only, not rasters. "
            "Use aggregation='basin_mean' (default)."
        )

    # ── Geometry → outlet point + basin area ─────────────────────────────────

    def _outlet_and_area(self, geometry: Any):
        """Return (lat, lon, area_km2|None). Polygon → centroid + geodesic area."""
        if getattr(geometry, "geom_type", None) in ("Polygon", "MultiPolygon"):
            c = geometry.centroid
            return float(c.y), float(c.x), self._geodesic_area_km2(geometry)
        try:
            return float(geometry.y), float(geometry.x), None
        except Exception:
            c = geometry.centroid
            return float(c.y), float(c.x), None

    @staticmethod
    def _geodesic_area_km2(polygon: Any) -> Optional[float]:
        try:
            from pyproj import Geod
            area_m2, _ = Geod(ellps="WGS84").geometry_area_perimeter(polygon)
            return abs(area_m2) / 1e6
        except Exception as exc:
            log.debug("geodesic area failed: %s", exc)
            return None

    # ── Snapping ─────────────────────────────────────────────────────────────

    def _snap(self, lat: float, lon: float, target_area_km2: Optional[float],
              spec: ProductSpec) -> dict:
        """Nearest reach → downstream-walk → area-match (see module docstring)."""
        rid = self._get_river_id(lat, lon)
        snap = {"river_id": rid, "strategy": "nearest_reach", "uparea_km2": None}

        meta = self._metadata()
        if meta is not None and rid in meta.index:
            snap["uparea_km2"] = float(meta.loc[rid, "USContArea"]) / 1e6
            snap["strm_order"] = int(meta.loc[rid, "strmOrder"])

        # Area-match refinement only possible with a basin area target + metadata.
        if target_area_km2 and meta is not None and rid in meta.index:
            max_hops = int(spec.backend_config.get("max_downstream_hops", 30))
            chain = self._walk_downstream(rid, meta, max_hops)
            # Pick the reach whose upstream area best matches the delineated basin.
            best = min(chain, key=lambda c: abs(c[2] - target_area_km2))
            if best[0] != rid:
                snap = {
                    "river_id": best[0],
                    "uparea_km2": best[2],
                    "strm_order": best[1],
                    "strategy": "area_match_downstream",
                    "nearest_reach": rid,
                }
        return snap

    def _get_river_id(self, lat: float, lon: float) -> int:
        """REST: nearest GEOGLOWS reach id for a coordinate."""
        import requests
        from aihydro_data.exceptions import SourceUnavailable
        try:
            r = requests.get(
                f"{_REST_ENDPOINT}/getriverid",
                params={"lat": lat, "lon": lon}, timeout=30,
            )
            r.raise_for_status()
            return int(r.json()["river_id"])
        except Exception as exc:
            raise SourceUnavailable(
                code="GEOGLOWS_RIVERID_FAILED",
                message=f"GEOGLOWS reach lookup failed for ({lat}, {lon}): {str(exc)[:160]}",
                recovery="Retry; if it persists the ECMWF data service may be down. "
                         "GloFAS is the async fallback for global streamflow.",
                next_tools=["data_doctor"],
                docs_anchor="products#geoglows",
            ) from exc

    @staticmethod
    def _walk_downstream(rid: int, meta, max_hops: int) -> list[tuple[int, int, float]]:
        """Follow DSLINKNO from `rid`, returning [(reach, strm_order, uparea_km2)]."""
        chain: list[tuple[int, int, float]] = []
        cur = rid
        for _ in range(max_hops):
            if cur not in meta.index:
                break
            row = meta.loc[cur]
            chain.append((int(cur), int(row["strmOrder"]), float(row["USContArea"]) / 1e6))
            nxt = int(row["DSLINKNO"])
            if nxt == -1 or nxt == cur:
                break
            cur = nxt
        return chain

    # ── Model metadata table (LINKNO-indexed; cached on disk + memoised) ──────

    def _metadata(self):
        """Load the GEOGLOWS model metadata table (topology + upstream areas).

        Memoised on the instance; persisted to the aihydro cache dir so repeated
        processes don't re-download. Returns a DataFrame indexed by LINKNO, or
        None if it cannot be loaded (snap degrades to nearest-reach only).
        """
        if self._meta_idx is not None:
            return self._meta_idx
        try:
            import geoglows
            import pandas as pd
            cache_dir = os.path.join(_cache_root(), "geoglows")
            os.makedirs(cache_dir, exist_ok=True)
            local = os.path.join(cache_dir, "v2-model-table.parquet")
            cols = ["LINKNO", "DSLINKNO", "strmOrder", "USContArea"]
            if os.path.exists(local):
                df = pd.read_parquet(local, columns=cols)
            else:
                log.info("GEOGLOWS: downloading model metadata table (one-time) …")
                df = geoglows.data.metadata_table(columns=cols)
                try:
                    df.to_parquet(local)
                except Exception as exc:
                    log.debug("GEOGLOWS metadata cache write failed: %s", exc)
            self._meta_idx = df.set_index("LINKNO")
            return self._meta_idx
        except Exception as exc:
            log.warning("GEOGLOWS metadata load failed (%s) — snap will use "
                        "nearest reach only, no area validation.", exc)
            return None

    def _assert_available(self) -> None:
        ok, reason = self.is_available()
        if not ok:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="GEOGLOWS_UNAVAILABLE",
                message=reason or "GEOGLOWS backend is not available.",
                recovery="pip install aihydro-data[geoglows]",
                next_tools=["data_doctor"],
                docs_anchor="products#geoglows",
            )


def _cache_root() -> str:
    """Reuse aihydro-data's cache root so GEOGLOWS artifacts live with the rest."""
    try:
        from aihydro_data.cache import cache_dir
        return os.path.dirname(str(cache_dir()))
    except Exception:
        return os.path.expanduser("~/.aihydro/cache")
