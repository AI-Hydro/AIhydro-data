"""
CDS/EWDS GloFAS backend — global modelled river discharge for a point/basin.

GloFAS (Global Flood Awareness System) is a gridded global river-discharge model
(LISFLOOD forced by ERA5, 0.05° ≈ 5 km, daily, 1979→near-real-time). It is the
ONLY open, programmatic source of streamflow anywhere on Earth — which is exactly
the capability NWIS (CONUS-only) lacks. Because GloFAS moved off the legacy CDS
onto the **CEMS Early Warning Data Store (EWDS)** in 2024, this backend talks to
`https://ewds.climate.copernicus.eu/api` via `cdsapi`.

The hard part is not the download — it is **snapping a point to the correct river
pixel**. A 0.05° cell directly under a lat/lon is usually a hillslope reading
~1 m³/s next to a channel reading ~1000 m³/s; a naive nearest-cell grab is wrong
by orders of magnitude (empirically ~30,000× at Mississippi/Vicksburg). The snap
algorithm here is the methodology documented in
`local-docs/GLOFAS_METHODS.md` (paper/thesis methods section):

  1. SELECT on the discharge field — pick the cell maximising mean discharge
     within the basin (or a window around a point outlet). Q ∝ upstream area, so
     the local discharge maximum *is* the main channel. Selecting on the
     discharge grid itself (not the static upstream-area grid) makes the snap
     immune to the known cross-cycle mis-registration between the two grids.
  2. VALIDATE with upstream area — compare the snapped cell's GloFAS upstream
     area against the delineated basin area. A close match confirms we found the
     right river (this is why watershed delineation matters: it supplies the
     ground-truth area).
  3. CONFLUENCE TIE-BREAK with area-match — if the max-Q cell's upstream area
     badly overshoots the delineated area (a larger river sits inside the search
     window), fall back to the cell whose upstream area best matches the
     delineated basin, keeping us on the correct tributary.

All imports are lazy — this module is safe to import without `[glofas]` installed.

Install:  pip install aihydro-data[glofas]    (cdsapi + xarray + netCDF4)
Auth:     free EWDS token in ~/.cdsapirc  →  https://cds.climate.copernicus.eu/profile
          (one-time: accept the `cems-floods` licence on the dataset page)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec
from aihydro_data.sources.base import SourceBackend

log = logging.getLogger(__name__)

# Static upstream-area grid (global 0.05°, m²) from the JRC open-data FTP.
# Auth-free. Cached under the aihydro cache dir on first use (~99 MB).
_UPAREA_URL = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-GLOFAS/"
    "LISFLOOD_static_and_parameter_maps_for_GloFAS/"
    "Catchments_morphology_and_river_network/upArea_repaired.nc"
)


class Backend(SourceBackend):
    """GloFAS global river-discharge backend (EWDS via cdsapi)."""

    source_id = "cds_glofas"

    # ── Capability / availability ────────────────────────────────────────────

    def capabilities(self) -> dict[str, Any]:
        return {
            "variables": ["streamflow"],
            "coverage": ["global"],
            "requires_auth": ["cds"],
            "requires_extras": ["glofas"],
        }

    def is_available(self) -> tuple[bool, Optional[str]]:
        try:
            import cdsapi  # noqa: F401
            import xarray  # noqa: F401
            import netCDF4  # noqa: F401
        except ImportError as exc:
            return False, (
                f"GloFAS backend needs the [glofas] extra ({exc.name} missing). "
                "Run `pip install aihydro-data[glofas]`."
            )
        # Token present?  cdsapi reads ~/.cdsapirc or CDSAPI_URL/CDSAPI_KEY env.
        if not (os.path.exists(os.path.expanduser("~/.cdsapirc"))
                or os.environ.get("CDSAPI_KEY")):
            return False, (
                "No CDS/EWDS credentials found. Create ~/.cdsapirc with the EWDS "
                "endpoint (https://ewds.climate.copernicus.eu/api) and your free "
                "personal access token from https://cds.climate.copernicus.eu/profile, "
                "then accept the CEMS-FLOODS licence on the cems-glofas-historical "
                "dataset page."
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
        """Snap `geometry` to the GloFAS main-channel cell and return daily Q.

        `outlet` (lat, lon), when supplied, overrides the geometry centroid as
        the snap origin — pass a delineated pour point for the most reliable
        main-channel cell pick.

        Returns a pd.DataFrame[date, streamflow]  (streamflow in m³/s).
        """
        self._assert_available()
        import pandas as pd

        cfg = spec.backend_config
        outlet_lat, outlet_lon, target_area_km2, polygon = self._outlet_and_area(geometry)
        if outlet is not None:
            outlet_lat, outlet_lon = float(outlet[0]), float(outlet[1])
            log.info("GloFAS: using caller-supplied outlet (%.4f, %.4f) for snap.",
                     outlet_lat, outlet_lon)

        # 1. Download a discharge window around the outlet for the date range.
        ds = self._retrieve_window(cfg, outlet_lat, outlet_lon, start, end)
        try:
            dvar = self._discharge_var(ds)
            qcube = self._normalise_lon(ds[dvar])               # (time, lat, lon), lon in -180..180
            qbar = qcube.mean(dim=[d for d in qcube.dims if d not in ("lat", "lon")])

            # 2. Snap to the channel cell (max-Q → validate → confluence tie-break).
            snap = self._snap(qbar, outlet_lat, outlet_lon, target_area_km2, polygon)

            # 3. Extract the daily series at the chosen cell.
            series = qcube.sel(lat=snap["lat"], lon=snap["lon"], method="nearest")
            times = pd.to_datetime(series["time"].values) if "time" in series.coords \
                else pd.to_datetime(series[[d for d in series.dims][0]].values)
            df = pd.DataFrame({
                "date": times,
                "streamflow": series.values.astype(float).ravel(),
            })
        finally:
            try:
                ds.close()
            except Exception:
                pass

        df = df.dropna(subset=["streamflow"]).reset_index(drop=True)
        df = df[(df["date"] >= pd.Timestamp(start)) &
                (df["date"] <= pd.Timestamp(end))].reset_index(drop=True)

        # Stash snap provenance so the pipeline/notes can surface it.
        df.attrs["glofas_snap"] = snap
        log.info(
            "GloFAS snapped (%.3f, %.3f) → cell (%.3f, %.3f) via %s; "
            "uparea=%s km² vs basin=%s km²; mean Q=%.1f m³/s",
            outlet_lat, outlet_lon, snap["lat"], snap["lon"], snap["strategy"],
            f"{snap['uparea_km2']:.0f}" if snap.get("uparea_km2") else "n/a",
            f"{target_area_km2:.0f}" if target_area_km2 else "n/a",
            df["streamflow"].mean() if len(df) else float("nan"),
        )
        return df

    def fetch_raster(self, spec: ProductSpec, geometry: Any, start: str, end: str) -> Any:
        raise NotImplementedError(
            "GloFAS backend serves snapped point/basin discharge time series only, "
            "not rasters. Use aggregation='basin_mean' (default)."
        )

    # ── Geometry → outlet point + basin area ─────────────────────────────────

    def _outlet_and_area(self, geometry: Any):
        """Return (outlet_lat, outlet_lon, area_km2|None, polygon|None).

        For a basin polygon: the search is constrained to the polygon and the
        delineated area is the validation target. For a point/gauge: a fixed
        window is searched and there is no area target (pure max-Q).
        """
        # Polygon (delineated basin) — preferred input.
        if getattr(geometry, "geom_type", None) in ("Polygon", "MultiPolygon"):
            c = geometry.centroid
            area_km2 = self._geodesic_area_km2(geometry)
            return float(c.y), float(c.x), area_km2, geometry
        # Point.
        try:
            return float(geometry.y), float(geometry.x), None, None
        except Exception:
            c = geometry.centroid
            return float(c.y), float(c.x), None, None

    @staticmethod
    def _geodesic_area_km2(polygon: Any) -> Optional[float]:
        try:
            from pyproj import Geod
            geod = Geod(ellps="WGS84")
            area_m2, _ = geod.geometry_area_perimeter(polygon)
            return abs(area_m2) / 1e6
        except Exception as exc:
            log.debug("geodesic area failed: %s", exc)
            return None

    # ── EWDS retrieval ───────────────────────────────────────────────────────

    def _retrieve_window(self, cfg: dict, lat0: float, lon0: float, start: str, end: str):
        """Download the GloFAS discharge bbox for [start, end] (cached on disk)."""
        import calendar
        import cdsapi
        import xarray as xr
        from datetime import datetime

        half = float(cfg.get("search_half_deg", 0.25))
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")

        # Build the tightest valid Cartesian (hyear × hmonth × hday) that covers
        # [start, end] without invalid date combinations.
        #
        # EWDS evaluates the three lists as a Cartesian product and silently
        # discards unknown date combos server-side — but it also raises a Warning
        # badge for every impossible date (Feb 30, Apr 31, …) in the product.
        # The strategy below eliminates those spurious combos wherever possible:
        #
        #   • single month    → exact day window; zero invalid combos
        #   • single year     → narrow months + cap days to the longest month
        #                       in the request (e.g. only-Feb → 28/29 days)
        #   • multi-year      → all 12 months needed; cap to 31 days (unavoidable
        #                       because Jul, Jan, … require it; invalid-combos for
        #                       Apr/Jun/Sep/Nov and Feb are accepted as harmless)
        #
        # In all cases we trim to the exact [start, end] window after download.
        years  = [str(y) for y in range(s.year, e.year + 1)]

        if s.year == e.year and s.month == e.month:
            # Single calendar month — exact window, no invalid combos possible.
            months  = [f"{s.month:02d}"]
            days    = [f"{d:02d}" for d in range(s.day, e.day + 1)]
        elif s.year == e.year:
            # Single year, multiple months.
            months = [f"{m:02d}" for m in range(s.month, e.month + 1)]
            # Use the longest month in the requested span to cap the day list.
            max_day = max(calendar.monthrange(s.year, m)[1]
                          for m in range(s.month, e.month + 1))
            days = [f"{d:02d}" for d in range(1, max_day + 1)]
        else:
            # Multi-year: all 12 months are needed; 31 days covers the longest months.
            # Apr/Jun/Sep/Nov day-31 and Feb day-29..31 remain "impossible" in the
            # Cartesian product — these are skipped by EWDS but trigger a warning
            # badge.  This is unavoidable with the Cartesian API format for
            # arbitrary multi-year ranges.
            months = [f"{m:02d}" for m in range(1, 13)]
            days   = [f"{d:02d}" for d in range(1, 32)]

        cache_dir = os.path.join(_cache_root(), "glofas")
        os.makedirs(cache_dir, exist_ok=True)
        key = f"{lat0:.3f}_{lon0:.3f}_{start}_{end}_{half}".replace("-", "m")
        out = os.path.join(cache_dir, f"dis_{key}.nc")

        if not os.path.exists(out):
            request = {
                "system_version": [cfg.get("system_version", "version_4_0")],
                "hydrological_model": [cfg.get("hydrological_model", "lisflood")],
                "product_type": [cfg.get("product_type", "consolidated")],
                "variable": ["river_discharge_in_the_last_24_hours"],
                "hyear": years, "hmonth": months, "hday": days,
                "area": [lat0 + half, lon0 - half, lat0 - half, lon0 + half],  # N,W,S,E
                "data_format": "netcdf", "download_format": "unarchived",
            }
            log.info("GloFAS: retrieving discharge window %s …", key)
            self._cds_retrieve(cfg.get("dataset", "cems-glofas-historical"), request, out)
        return xr.open_dataset(out)

    def _cds_retrieve(self, dataset: str, request: dict, out_path: str) -> None:
        import cdsapi
        from aihydro_data.exceptions import SourceUnavailable, AuthRequired
        try:
            cdsapi.Client().retrieve(dataset, request).download(out_path)
        except Exception as exc:
            msg = str(exc)
            if "licence" in msg.lower() or "403" in msg:
                raise AuthRequired(
                    code="GLOFAS_LICENCE_NOT_ACCEPTED",
                    message=(
                        "EWDS rejected the request — the CEMS-FLOODS licence is not "
                        f"accepted on your account, or the token is invalid. ({msg[:160]})"
                    ),
                    recovery=(
                        "Accept the licence once at "
                        "https://ewds.climate.copernicus.eu/datasets/cems-glofas-historical?tab=download "
                        "and verify ~/.cdsapirc uses the EWDS endpoint."
                    ),
                    next_tools=["data_doctor"],
                    docs_anchor="products#glofas",
                ) from exc
            raise SourceUnavailable(
                code="GLOFAS_RETRIEVE_FAILED",
                message=f"GloFAS EWDS retrieval failed: {msg[:200]}",
                recovery="Retry; EWDS queues large requests. Narrow the date range if it persists.",
                next_tools=["data_doctor"],
                docs_anchor="products#glofas",
            ) from exc

    # ── Snapping ─────────────────────────────────────────────────────────────

    def _snap(self, qbar, lat0, lon0, target_area_km2, polygon) -> dict:
        """Pick the main-channel cell. See module docstring for the algorithm."""
        import numpy as np

        lats = qbar["lat"].values
        lons = qbar["lon"].values
        grid = qbar.values  # (nlat, nlon), mean discharge

        LAT, LON = np.meshgrid(lats, lons, indexing="ij")
        # Candidate mask: inside the basin polygon if we have one, else the whole window.
        mask = np.isfinite(grid)
        if polygon is not None:
            inside = self._points_in_polygon(LON.ravel(), LAT.ravel(), polygon)
            inside = inside.reshape(grid.shape)
            if inside.sum() >= 1:                       # basin large enough to hold ≥1 cell
                mask &= inside

        if not mask.any():                              # tiny basin → fall back to whole window
            mask = np.isfinite(grid)

        qmask = np.where(mask, grid, -np.inf)
        i, j = np.unravel_index(np.nanargmax(qmask), qmask.shape)
        chosen = {"lat": float(lats[i]), "lon": float(lons[j]),
                  "q_mean": float(grid[i, j]), "strategy": "max_discharge"}

        # Validate + confluence tie-break with upstream area (only if we know the
        # basin area — i.e. a polygon was supplied).
        ua = self._upstream_area_grid()
        if ua is not None:
            chosen["uparea_km2"] = self._uparea_at(ua, chosen["lat"], chosen["lon"])
            if target_area_km2:
                overshoot = (chosen["uparea_km2"] or 0) / target_area_km2
                if overshoot > 1.5 or overshoot < 0.5:
                    alt = self._area_match(ua, lats, lons, mask, target_area_km2)
                    if alt is not None:
                        alt["q_mean"] = float(grid[alt["_i"], alt["_j"]])
                        alt["strategy"] = "area_match_confluence_tiebreak"
                        alt.pop("_i"); alt.pop("_j")
                        chosen = alt
        else:
            chosen.setdefault("uparea_km2", None)
        return chosen

    @staticmethod
    def _points_in_polygon(xs, ys, polygon):
        import numpy as np
        try:
            from shapely.vectorized import contains
            return contains(polygon, xs, ys)
        except Exception:
            from shapely.geometry import Point
            return np.array([polygon.contains(Point(x, y)) for x, y in zip(xs, ys)])

    def _area_match(self, ua, lats, lons, mask, target_area_km2):
        """Among masked cells, the one whose GloFAS upstream area best matches."""
        import numpy as np
        best = None
        for i, la in enumerate(lats):
            for j, lo in enumerate(lons):
                if not mask[i, j]:
                    continue
                a = self._uparea_at(ua, float(la), float(lo))
                if a is None:
                    continue
                err = abs(a - target_area_km2)
                if best is None or err < best["_err"]:
                    best = {"lat": float(la), "lon": float(lo),
                            "uparea_km2": a, "_err": err, "_i": i, "_j": j}
        if best is not None:
            best.pop("_err")
        return best

    # ── Upstream-area static grid (lazy download + cache) ─────────────────────

    def _upstream_area_grid(self):
        """Open the global 0.05° upstream-area grid (m²), downloading once."""
        import xarray as xr
        cache_dir = os.path.join(_cache_root(), "glofas")
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, "upArea_glofas.nc")
        if not os.path.exists(path):
            try:
                import urllib.request
                log.info("GloFAS: downloading static upstream-area grid (~99 MB, one-time) …")
                urllib.request.urlretrieve(_UPAREA_URL, path)
            except Exception as exc:
                log.warning("GloFAS uparea download failed (%s) — snap will use "
                            "discharge only, no area validation.", exc)
                return None
        try:
            ds = xr.open_dataset(path)
            return ds["Band1"]   # m²; coords lat/lon
        except Exception as exc:
            log.warning("GloFAS uparea open failed: %s", exc)
            return None

    @staticmethod
    def _uparea_at(ua, lat: float, lon: float) -> Optional[float]:
        import numpy as np
        try:
            v = ua.sel(lat=lat, lon=lon, method="nearest").values
            v = float(np.asarray(v).ravel()[0])
            return v / 1e6 if np.isfinite(v) else None   # m² → km²
        except Exception:
            return None

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _discharge_var(ds) -> str:
        for v in ds.data_vars:
            if ds[v].ndim >= 2 and str(v).lower() not in ("crs", "spatial_ref"):
                return v
        raise RuntimeError(f"no discharge variable in {list(ds.data_vars)}")

    @staticmethod
    def _normalise_lon(da):
        """Rename latitude/longitude→lat/lon and convert 0..360 lon to -180..180."""
        ren = {}
        if "latitude" in da.coords:
            ren["latitude"] = "lat"
        if "longitude" in da.coords:
            ren["longitude"] = "lon"
        if ren:
            da = da.rename(ren)
        if float(da["lon"].max()) > 180.0:
            da = da.assign_coords(lon=(((da["lon"] + 180) % 360) - 180)).sortby("lon")
        return da

    def _assert_available(self) -> None:
        ok, reason = self.is_available()
        if not ok:
            from aihydro_data.exceptions import SourceUnavailable
            raise SourceUnavailable(
                code="GLOFAS_UNAVAILABLE",
                message=reason or "GloFAS backend is not available.",
                recovery="pip install aihydro-data[glofas]  (+ configure ~/.cdsapirc)",
                next_tools=["data_doctor"],
                docs_anchor="products#glofas",
            )


def _cache_root() -> str:
    """Reuse aihydro-data's cache root so GloFAS artifacts live with the rest."""
    try:
        from aihydro_data.cache import cache_dir
        return os.path.dirname(str(cache_dir()))   # parent of …/cache/data
    except Exception:
        return os.path.expanduser("~/.aihydro/cache")
