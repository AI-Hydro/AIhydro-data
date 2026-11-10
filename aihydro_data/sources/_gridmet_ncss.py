"""
Direct GridMET client over the THREDDS NetCDF Subset Service (NCSS).

Why this exists (evidence: docs/vision-2040/evidence/agu26-demo-1/rehearsal, FINDINGS F1,
re-diagnosed 2026-11-10; evidence: docs/vision-2040/evidence/gridmet-ncss-probe-2026-11-10,
7 diagnostic requests + 3 for the WY2000 / calendar-2000 check + 1 raw WY2000 response kept = 11 to the approved host):

* ``pygridmet.get_bygeom`` treats ANY NaN in a downloaded file as a corrupt
  download: it deletes the file, downloads it again 15 times and then raises
  "GridMET did NOT process your request successfully". GridMET has no data outside
  CONUS, so a box that reaches Canada or the ocean always contains NaN cells and
  can never succeed. The server itself answered the whole 0.89 x 0.63 deg Fish
  River box with a valid NetCDF file (HTTP 200, 4 KB, 9 of 368 cells NaN). The
  "0.18 deg limit" seen in the rehearsal is where the test boxes first reached
  such a cell (0.15 deg box: no NaN cell; 0.18 deg box: one always-NaN cell).
  With the call_with_retry layers on top, one failing call cost about 270 requests.
* The server DOES hold 31 December of leap years (2000-12-31 returned real
  values). pygridmet's ``dates_tolist`` removes that day (its docstring is
  copied from pydaymet, whose calendar really has 365 days), so a fetch through it
  is one day short in every leap year.

This client therefore requests the data itself and decides explicitly what a NaN
is. It keeps the maths of the old path (cell-centre-in-polygon mask, unweighted
mean over the cells that have data) and records both in the result.

Failure behaviour (all bounded, none silent):

* a 200 response whose body is not a NetCDF file (an error page) raises
  ``GridmetRequestRejected`` at once, with the body excerpt. The request box is
  then split in two (at most ``MAX_SPLIT_DEPTH`` times, at most
  ``MAX_REQUESTS_PER_CALL`` requests) in case the server enforces a size limit
  that the NaN finding does not explain; if a one-cell box is rejected it raises.
  ``GridmetRequestRejected`` is an ``AihydroDataError``, so the transient-retry
  helper and the chunk retry do not repeat it.
* 5xx / timeouts are retried by ``call_with_retry`` (3 attempts).
* a cell that is NaN on some days but not others (what a flaky download looks
  like; permanent NaN is outside the grid) triggers ONE re-request; if it
  persists it is disclosed in the notes.
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from aihydro_data.exceptions import SourceUnavailable

log = logging.getLogger(__name__)

NCSS_URL = "http://thredds.northwestknowledge.net:8080/thredds/ncss"
DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"
MISSING_VALUE = 32767.0
GRID_BOUNDS = (-124.7666, 25.0666, -67.0583, 49.4)     # west, south, east, north
CELL_DEG = 1.0 / 24.0                                   # GridMET grid spacing

DEFAULT_MIN_INTERVAL_S = 1.0       # courtesy gap between requests to the shared THREDDS host
MAX_SPLIT_DEPTH = 4
MAX_REQUESTS_PER_CALL = 48
BASIN_MEAN_METHOD = "cell_centre_in_polygon_unweighted_mean"

# pygridmet 0.19.4 `GridMET.long_names`; a test compares this table with it.
LONG_NAMES = {
    "pr": "precipitation_amount",
    "rmax": "daily_maximum_relative_humidity",
    "rmin": "daily_minimum_relative_humidity",
    "sph": "daily_mean_specific_humidity",
    "srad": "daily_mean_shortwave_radiation_at_surface",
    "th": "daily_mean_wind_direction",
    "tmmn": "daily_minimum_temperature",
    "tmmx": "daily_maximum_temperature",
    "vs": "daily_mean_wind_speed",
    "bi": "daily_mean_burning_index_g",
    "fm100": "dead_fuel_moisture_100hr",
    "fm1000": "dead_fuel_moisture_1000hr",
    "erc": "daily_mean_energy_release_component-g",
    "etr": "daily_mean_reference_evapotranspiration_alfalfa",
    "pet": "daily_mean_reference_evapotranspiration_grass",
    "vpd": "daily_mean_vapor_pressure_deficit",
}


class GridmetRequestRejected(SourceUnavailable):
    """THREDDS answered, but not with data: an HTTP 4xx, or a 200 whose body is not
    a NetCDF file (an error page). Never retried by the transient-retry helper."""


@dataclass
class HttpResponse:
    status: int
    content_type: str
    body: bytes


def _requests_get(url: str, timeout: Any) -> HttpResponse:
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "aihydro-data"})
    if r.status_code >= 500:
        r.raise_for_status()                    # "503 Server Error ..." -> transient
    return HttpResponse(r.status_code, r.headers.get("content-type", ""), r.content)


def is_netcdf(body: bytes) -> bool:
    """NetCDF-3 classic (``CDF``) or NetCDF-4 / HDF5 magic number."""
    return body[:3] == b"CDF" or body[:8] == b"\x89HDF\r\n\x1a\n"


def _excerpt(body: bytes, n: int = 300) -> str:
    return " ".join(body[:n].decode("utf-8", "replace").split())


def _open_netcdf(body: bytes) -> Any:
    """Read an NCSS NetCDF response with the netCDF4 engine (declared in the ``hyriver``
    extra; it reads classic NetCDF-3 and NetCDF-4/HDF5, so scipy is not needed)."""
    from aihydro_data.sources._common import require_import
    xr = require_import("xarray", extra="hyriver", backend="hyriver")
    require_import("netCDF4", extra="hyriver", backend="hyriver")
    with tempfile.NamedTemporaryFile(suffix=".nc") as tmp:
        tmp.write(body)
        tmp.flush()
        with xr.open_dataset(tmp.name, engine="netcdf4") as ds:
            return ds.load().copy(deep=True)


def build_url(abbr: str, bounds: tuple[float, float, float, float], start: Any, end: Any) -> str:
    import pandas as pd
    west, south, east, north = bounds
    return f"{NCSS_URL}/agg_met_{abbr}_1979_CurrentYear_CONUS.nc?" + urlencode({
        "var": LONG_NAMES[abbr],
        "north": f"{north:0.6f}", "west": f"{west:0.6f}",
        "east": f"{east:0.6f}", "south": f"{south:0.6f}",
        "disableProjSubset": "on", "horizStride": "1",
        "time_start": pd.Timestamp(start).strftime(DATE_FMT),
        "time_end": pd.Timestamp(end).strftime(DATE_FMT),
        "timeStride": "1", "addLatLon": "true", "accept": "netcdf",
    })


@dataclass
class Ledger:
    """What was requested and what came back; becomes ``FetchResult.upstream``."""
    request_urls: list[str] = field(default_factory=list)
    response_sha256: list[str] = field(default_factory=list)
    splits: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    refetches: int = 0
    cells_in_geometry: Optional[int] = None
    cells_with_data: Optional[int] = None
    all_touched_fallback: bool = False
    partial_nan_cells: int = 0
    notes: list[str] = field(default_factory=list)

    def upstream(self) -> dict[str, Any]:
        digest = hashlib.sha256("\n".join(self.response_sha256).encode()).hexdigest()
        return {"gridmet_ncss": {
            "transport": "ncss_direct", "endpoint": NCSS_URL, "pygridmet_used": False,
            "requests": len(self.request_urls), "request_urls": list(self.request_urls),
            "response_sha256": list(self.response_sha256), "responses_digest": f"sha256:{digest}",
            "split_requests": self.splits, "rejected_responses": list(self.rejected),
            "refetches_for_partial_nan": self.refetches,
            "basin_mean": {
                "method": BASIN_MEAN_METHOD, "grid_deg": round(CELL_DEG, 6),
                "cells_in_geometry": self.cells_in_geometry,
                "cells_with_data": self.cells_with_data,
                "cells_without_data": (None if self.cells_in_geometry is None
                                       else self.cells_in_geometry - (self.cells_with_data or 0)),
                "all_touched_fallback": self.all_touched_fallback,
            },
            "calendar": "every calendar day requested, including 31 December of leap years",
        }}


class NcssClient:
    """Stateful per fetch: one client, one ledger. ``http_get`` is the test seam."""

    def __init__(self, http_get: Optional[Callable[[str, Any], HttpResponse]] = None, *,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S, timeout: Any = (10, 180),
                 max_requests_per_call: int = MAX_REQUESTS_PER_CALL,
                 max_split_depth: int = MAX_SPLIT_DEPTH) -> None:
        self._get = http_get or _requests_get
        self.min_interval_s = min_interval_s
        self.timeout = timeout
        self.max_requests_per_call = max_requests_per_call
        self.max_split_depth = max_split_depth
        self.ledger = Ledger()
        self._last = 0.0
        self._call_requests = 0

    # ── one HTTP request ────────────────────────────────────────────────
    def _request(self, abbr: str, bounds: tuple, start: Any, end: Any) -> Any:
        from aihydro_data.sources._retry import call_with_retry

        if self._call_requests >= self.max_requests_per_call:
            raise GridmetRequestRejected(
                code="GRIDMET_REQUEST_BUDGET_EXCEEDED",
                message=(f"GridMET fetch stopped after {self._call_requests} requests for one "
                         f"call (limit {self.max_requests_per_call})."),
                recovery="Request a smaller area or a shorter window, or use another precipitation "
                         "product.", details={"requests": self._call_requests})
        url = build_url(abbr, bounds, start, end)

        def once() -> HttpResponse:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self._call_requests += 1
            self.ledger.request_urls.append(url)
            return self._get(url, self.timeout)

        resp = call_with_retry(once, label="gridmet.ncss")
        self.ledger.response_sha256.append(hashlib.sha256(resp.body).hexdigest())
        if resp.status != 200 or not is_netcdf(resp.body):
            info = {"status": resp.status, "content_type": resp.content_type,
                    "bytes": len(resp.body), "body_excerpt": _excerpt(resp.body),
                    "bounds": [round(b, 6) for b in bounds],
                    "start": str(start)[:10], "end": str(end)[:10]}
            self.ledger.rejected.append(info)
            raise GridmetRequestRejected(
                code="GRIDMET_REQUEST_REJECTED",
                message=(f"THREDDS answered HTTP {resp.status} without a NetCDF file "
                         f"({resp.content_type or 'no content type'}): {info['body_excerpt'] or 'empty body'}"),
                recovery="The request was not repeated. Try a smaller area or window, or another "
                         "precipitation product.", details=info)
        return _open_netcdf(resp.body)

    # ── one box, splitting on rejection ─────────────────────────────────
    def _fetch_box(self, abbr: str, bounds: tuple, start: Any, end: Any, depth: int = 0) -> Any:
        try:
            return self._request(abbr, bounds, start, end)
        except GridmetRequestRejected as exc:
            if exc.code != "GRIDMET_REQUEST_REJECTED":
                raise
            w, s, e, n = bounds
            wide, tall = (e - w) / CELL_DEG, (n - s) / CELL_DEG
            if depth >= self.max_split_depth or max(wide, tall) < 2.0:
                raise
            self.ledger.splits += 1
            if wide >= tall:
                mid = (w + e) / 2
                halves = [(w, s, mid, n), (mid, s, e, n)]
            else:
                mid = (s + n) / 2
                halves = [(w, s, e, mid), (w, mid, e, n)]
            log.warning("GridMET box %s rejected (%s); splitting into %s", bounds,
                        exc.details.get("body_excerpt", ""), halves)
            return _mosaic([self._fetch_box(abbr, h, start, end, depth + 1) for h in halves])

    # ── public, pygridmet-shaped API ────────────────────────────────────
    def get_bygeom(self, geometry: Any, dates: tuple[str, str], variables: list[str]) -> Any:
        """Daily ``xarray.Dataset`` (dims time, lat, lon) of ``variables[0]``, masked to the
        cells whose centre lies in ``geometry`` (EPSG:4326). Same masking rule as
        ``pygridmet.get_bygeom``; cells outside the GridMET grid stay NaN."""
        abbr = _one_variable(variables)
        _check_domain(geometry)
        self._call_requests = 0
        bounds = tuple(float(b) for b in geometry.bounds)
        out = self._bygeom_once(abbr, geometry, bounds, dates)
        if _partial_nan_cells(out[abbr]):
            self.ledger.refetches += 1
            again = self._bygeom_once(abbr, geometry, bounds, dates)
            if _partial_nan_cells(again[abbr]) <= _partial_nan_cells(out[abbr]):
                out = again
            n_bad = _partial_nan_cells(out[abbr])
            if n_bad:
                self.ledger.partial_nan_cells = max(self.ledger.partial_nan_cells, n_bad)
                self.ledger.notes.append(
                    f"{n_bad} GridMET cell(s) inside the geometry are NaN on some days of "
                    f"{dates[0]}..{dates[1]} but not others after one re-request; the basin mean "
                    f"skips them on those days.")
        return out

    def _bygeom_once(self, abbr: str, geometry: Any, bounds: tuple, dates: tuple[str, str]) -> Any:
        raw = self._fetch_box(abbr, bounds, dates[0], dates[1])
        ds = _tidy(raw, abbr)
        clipped, touched, n_inside = _clip_to_geometry(ds, geometry)
        has_data = int(clipped[abbr].notnull().any("time").sum())
        self.ledger.cells_in_geometry = n_inside
        self.ledger.cells_with_data = has_data
        self.ledger.all_touched_fallback = bool(touched)
        if self.ledger.cells_in_geometry and has_data < self.ledger.cells_in_geometry:
            self.ledger.notes.append(
                f"{self.ledger.cells_in_geometry - has_data} of {self.ledger.cells_in_geometry} grid "
                f"cells inside the geometry have no GridMET data (NaN in the file: Canada or open "
                f"water inside the grid); the basin mean is over the {has_data} cells that have "
                f"data. Parts of the geometry beyond the grid's own extent have no cells at all "
                f"and are not counted.")
        return clipped

    def get_bycoords(self, coords: tuple[float, float], dates: tuple[str, str],
                     variables: list[str]) -> Any:
        """Daily DataFrame (index ``time``, column = the GridMET abbreviation) at the grid
        cell nearest ``coords`` (lon, lat); like pygridmet it asks for a +-0.05 deg box."""
        import pandas as pd
        from shapely.geometry import box

        abbr = _one_variable(variables)
        lon, lat = float(coords[0]), float(coords[1])
        ds = self.get_bygeom(box(lon - 0.05, lat - 0.05, lon + 0.05, lat + 0.05), dates, [abbr])
        pt = ds[abbr].sel(lon=lon, lat=lat, method="nearest")
        df = pd.DataFrame({abbr: pt.values}, index=pd.DatetimeIndex(pt["time"].values, name="time"))
        self.ledger.cells_in_geometry = self.ledger.cells_with_data = 1
        if df[abbr].isna().all():
            self.ledger.cells_with_data = 0
            self.ledger.notes.append(
                f"the GridMET cell nearest ({lon:.4f}, {lat:.4f}) has no data (outside the CONUS grid).")
        return df


# ── helpers ───────────────────────────────────────────────────────────────

def _one_variable(variables: Any) -> str:
    vs = [variables] if isinstance(variables, str) else list(variables)
    if len(vs) != 1 or vs[0] not in LONG_NAMES:
        raise ValueError(f"NcssClient fetches one GridMET variable of {sorted(LONG_NAMES)}; got {vs}")
    return vs[0]


def _check_domain(geometry: Any) -> None:
    w, s, e, n = geometry.bounds
    gw, gs, ge, gn = GRID_BOUNDS
    if e < gw or w > ge or n < gs or s > gn:
        raise SourceUnavailable(
            code="GRIDMET_OUTSIDE_DOMAIN",
            message=f"geometry bounds {(w, s, e, n)} do not intersect the GridMET grid {GRID_BOUNDS}.",
            recovery="GridMET covers CONUS only; use a global precipitation product.")


def _tidy(raw: Any, abbr: str) -> Any:
    """Raw NCSS dataset -> one float32 variable named ``abbr`` on (time, lat, lon);
    the file's missing value (32767) becomes NaN."""
    import numpy as np

    long = LONG_NAMES[abbr]
    if long not in raw:
        raise GridmetRequestRejected(
            code="GRIDMET_REQUEST_REJECTED",
            message=f"NetCDF response has no variable {long!r} (has {list(raw.data_vars)}).",
            recovery="The request was not repeated.", details={"variables": list(raw.data_vars)})
    da = raw[long]
    da = da.where(da < MISSING_VALUE).astype("float32")
    units = raw[long].attrs.get("units", "")
    da = da.rename({"day": "time"}) if "day" in da.dims else da
    da = da.drop_vars([c for c in da.coords if c not in da.dims])
    da.attrs = {"units": units, "long_name": long}
    for c in ("lat", "lon"):
        da[c] = np.round(da[c].values.astype("float64"), 6)
    return da.to_dataset(name=abbr)


def _clip_to_geometry(ds: Any, geometry: Any) -> tuple[Any, bool, int]:
    """Keep the cells whose CENTRE is strictly inside ``geometry`` (``shapely.contains_xy``);
    cells elsewhere become NaN and the grid is cropped to the kept cells. This is the same
    cell-centre-in-polygon idea as the old ``rio.clip`` path but not bit-identical to its
    GDAL scanline rasterisation: a centre lying exactly ON the polygon boundary is excluded
    here, so the two can differ by about one cell per boundary-aligned edge. Delineated
    basins essentially never put a centre exactly on the boundary; grid-aligned bounding
    boxes can. A geometry that holds no cell centre (smaller than a cell) falls back to
    every cell it touches (the old code returned an empty grid for it). Returns ``(dataset, used_all_touched, cells_in_geometry)``. The count
    is of cells selected by the geometry, not of cells with data: cells outside the
    GridMET grid are NaN in the data itself and are counted separately by the caller."""
    import numpy as np
    import shapely

    name = next(iter(ds.data_vars))
    lat, lon = ds["lat"].values, ds["lon"].values
    lon2, lat2 = np.meshgrid(lon, lat)
    touched = False
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        mask = shapely.contains_xy(geometry, lon2, lat2)
        if not mask.any():
            touched = True
    else:
        mask = np.zeros(lon2.shape, bool)
        touched = True
    if touched:
        hx, hy = _half_step(lon), _half_step(lat)
        cells = shapely.box(lon2 - hx, lat2 - hy, lon2 + hx, lat2 + hy)
        mask = shapely.intersects(cells, geometry)
    rows, cols = np.where(mask)
    if not len(rows):
        return ds.isel(lat=slice(0, 0), lon=slice(0, 0)), touched, 0
    sub = ds.isel(lat=slice(rows.min(), rows.max() + 1), lon=slice(cols.min(), cols.max() + 1))
    m = mask[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    sub = sub.assign({name: sub[name].where(_mask_array(m, sub))})
    return sub, touched, int(mask.sum())


def _half_step(axis: Any) -> float:
    import numpy as np
    return float(np.abs(np.diff(axis)).mean() / 2) if len(axis) > 1 else CELL_DEG / 2


def _mask_array(m: Any, like: Any) -> Any:
    import xarray as xr
    return xr.DataArray(m, dims=("lat", "lon"), coords={"lat": like["lat"], "lon": like["lon"]})


def _partial_nan_cells(da: Any) -> int:
    if "time" not in da.dims or da.sizes["time"] < 2:
        return 0
    nn = da.notnull()
    return int((nn.any("time") & ~nn.all("time")).sum())


def _mosaic(parts: list[Any]) -> Any:
    """Join NCSS boxes that share a lattice (overlapping cells must agree)."""
    import numpy as np
    import xarray as xr

    fixed = []
    for p in parts:
        p = p.copy()
        for c in ("lat", "lon"):
            p[c] = np.round(p[c].values.astype("float64"), 6)
        fixed.append(p)
    merged = xr.merge(fixed, join="outer", compat="no_conflicts").sortby("lat", ascending=False).sortby("lon")
    for c in ("lat", "lon"):
        d = np.diff(merged[c].values)
        if len(d) and float(np.max(np.abs(d))) > 1.5 * float(np.min(np.abs(d))):
            raise GridmetRequestRejected(
                code="GRIDMET_MOSAIC_GAP",
                message=f"split GridMET boxes left a gap along {c}; refusing to average a holed grid.",
                recovery="Request a smaller area.", details={"axis": c})
    return merged


__all__ = ["NcssClient", "GridmetRequestRejected", "HttpResponse", "Ledger", "LONG_NAMES",
           "BASIN_MEAN_METHOD", "build_url", "is_netcdf"]
