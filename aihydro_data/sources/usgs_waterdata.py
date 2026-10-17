"""
USGS Water Data OGC API adapter (Wave U, slice U1a).

Replaces the legacy ``waterservices.usgs.gov`` / ``dataretrieval.nwis`` path
(decommissioned 2027-02-22). Scope: daily + continuous discharge, series
metadata, monitoring locations, and gauge resolution. Stdlib + requests +
pandas only.

Every fact below that is stated as an observation was read from the recorded
bodies in ``tests/fixtures/usgs_waterdata/`` (captured 2026-10-17):

* the approval field is ``approval_status`` (never ``approvals_status``) with
  values ``Approved`` / ``Provisional``;
* ``qualifier`` is ``null`` or a list of strings (``ESTIMATED``,
  ``FORCEINTERPOLATION``, ...); ``value`` is a string, ``null`` when missing;
* ``unit_of_measure`` is ``ft^3/s`` (legacy ``dv`` says ``ft3/s``);
* ``Last-Modified`` and ``ETag`` are NOT sent; paging is a ``rel=next`` link
  carrying an opaque ``cursor`` (no ``numberMatched``);
* ``/collections/continuous`` rejects windows over 1100 days with HTTP 400
  ``InvalidQuery``; a nonexistent site or an empty window is HTTP 200 with
  ``"features": []``.

Single network seam: :func:`_http_get`. Tests monkeypatch it. A key
(env ``API_USGS_PAT``) is sent ONLY as the ``X-Api-Key`` header and is never
logged, recorded or hashed into provenance.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlencode, urlparse

import pandas as pd

from aihydro_data.exceptions import AihydroDataError, SourceUnavailable, TerminalSourceError

BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"
NLDI_BASE = "https://api.water.usgs.gov/nldi/linked-data"
_ALLOWED_HOSTS = frozenset({"api.waterdata.usgs.gov", "api.water.usgs.gov"})

# Exact by definition: 1 ft = 0.3048 m  =>  1 ft^3 = 0.028316846592 m^3.
CFS_TO_M3S = 0.028316846592
_UNIT_FACTORS = {"ft^3/s": CFS_TO_M3S, "ft3/s": CFS_TO_M3S}

# approval_status -> single-letter code (the legacy / validator vocabulary).
# Anything else is refused (USGS_APPROVAL_UNKNOWN), never guessed.
APPROVAL_CODES = {"Approved": "A", "Provisional": "P"}
# qualifier token -> legacy code. Tokens NOT listed stay raw and are counted in
# quality_summary["qualifiers_other"]; they are never dropped or guessed.
QUALIFIER_CODES = {"ESTIMATED": "e"}

# The server rejects windows of more than 1100 days; chunk below that.
CONTINUOUS_MAX_DAYS = 1095
_MAX_PAGES = 200


# ---------------------------------------------------------------- errors
# Identity / integrity refusals: terminal, never answered by a modelled product.
# Everything else (HTTP errors, rate limits, bad bodies) means "could not look"
# and stays a plain SourceUnavailable.
TERMINAL_CODES = frozenset({
    "GAUGE_REQUIRED", "GAUGE_NOT_FOUND", "GAUGE_AMBIGUOUS",
    "USGS_SITE_INVALID", "USGS_SITE_NOT_FOUND", "USGS_NO_DATA", "USGS_MULTIPLE_SERIES",
    "USGS_APPROVAL_UNKNOWN", "USGS_UNIT_UNKNOWN", "USGS_VALUE_UNPARSEABLE", "USGS_DUPLICATE_TIME",
})


def _err(code: str, message: str, recovery: str = "", **details: Any) -> SourceUnavailable:
    cls = TerminalSourceError if code in TERMINAL_CODES else SourceUnavailable
    return cls(code=code, message=message, recovery=recovery, details=dict(details))


# ---------------------------------------------------------------- transport
def _http_get(url: str, params: Optional[Mapping[str, Any]] = None,
              headers: Optional[Mapping[str, str]] = None) -> tuple[int, dict, bytes]:
    """The ONLY network call in this module. Returns (status, headers, body)."""
    import requests  # local: keeps import cost off non-USGS paths

    host = urlparse(url).hostname
    if host not in _ALLOWED_HOSTS:
        raise _err("USGS_HOST_NOT_ALLOWED", f"refusing request to {host!r}")
    h = {"User-Agent": "aihydro-data/usgs_waterdata", "Accept": "application/json"}
    key = os.environ.get("API_USGS_PAT")
    if key and host == "api.waterdata.usgs.gov":
        h["X-Api-Key"] = key
    h.update(headers or {})
    r = requests.get(url, params=params, headers=h, timeout=120)
    return r.status_code, dict(r.headers), r.content


def _canonical(url: str, params: Optional[Mapping[str, Any]]) -> str:
    return url + ("?" + urlencode(params) if params else "")


# ---------------------------------------------------------------- dataclasses
@dataclass
class UsgsSeries:
    site_id: str
    time_series_id: str
    parameter_code: str
    statistic_id: Optional[str]
    units_native: str
    frame: pd.DataFrame
    quality_summary: dict
    upstream: dict


@dataclass
class MonitoringLocation:
    site_id: str
    name: Optional[str]
    lon: float
    lat: float
    site_type_code: Optional[str]
    state_code: Optional[str]
    hydrologic_unit_code: Optional[str]
    horizontal_datum: Optional[str]          # original_horizontal_datum
    geometry_crs_note: str                   # the API does not state the geometry CRS
    drainage_area_raw: Optional[float]
    drainage_area_units: str
    altitude: Optional[float]
    vertical_datum: Optional[str]
    raw: dict = field(repr=False, default_factory=dict)


RULE_VERSION = "ADR-003-A2-2026-10-17"
SQMI_TO_KM2 = 2.589988110336
EARTH_R_M = 6371008.8
DEFAULT_THRESHOLDS = {
    "max_distance_m": 5000.0,
    "max_abs_log_area_ratio": math.log(1.10),
    "outlet_offset_max_m": 200.0,
    "query_distance_km": 10,
}
DISTANCE_BASIS = ("along-network sum of spherical great-circle segment lengths (R=6371008.8 m) between the "
                  "projections of the outlet and of the gauge point onto the NLDI NHDPlusV2 flowline "
                  "geometries; negative = upstream, positive = downstream")
AREA_UNITS_NOTE = ("A_gauge converted as square miles x 2.589988110336; the Water Data API body does not state "
                   "the unit of drainage_area (USGS documents square miles). Cross-check: legacy site rdb "
                   "drain_area_va = 870 for 01013500 equals the API drainage_area 870.0")


@dataclass
class GaugeResolution:
    site: str
    method: str                              # explicit_id | basin_ref_alias | outlet_snap
    distance_m: Optional[float] = None       # signed, outlet_snap only
    comid: Optional[int] = None              # the gauge's indexed COMID
    candidates: list = field(default_factory=list)
    alias_verified: Optional[bool] = None
    location: Optional[MonitoringLocation] = None
    comid_outlet: Optional[int] = None
    outlet_offset_m: Optional[float] = None
    direction: Optional[str] = None
    distance_basis: Optional[str] = None
    A_gauge_km2: Optional[float] = None
    A_gauge_contributing_km2: Optional[float] = None
    A_basin_km2: Optional[float] = None
    area_basis: Optional[str] = None         # request_geometry | nldi_comid_basin | None
    area_ratio: Optional[float] = None       # A_gauge / A_basin
    log_area_ratio: Optional[float] = None
    area_check: str = "unavailable"          # pass | fail | unavailable
    area_units_note: str = AREA_UNITS_NOTE
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    thresholds_overridden: bool = False
    rule_version: str = RULE_VERSION
    warnings: list = field(default_factory=list)
    upstream: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "location"}
        d["site_name"] = self.location.name if self.location else None
        return json.loads(json.dumps(d, default=str))


# ---------------------------------------------------------------- helpers
_DRAINAGE_UNITS = ("unstated (USGS documents square miles; the OpenAPI schema states "
                   "'square miles' only for contributing_drainage_area)")


def _site_id(site: str) -> str:
    s = str(site).strip()
    if s.upper().startswith("USGS-"):
        s = s[5:]
    if not re.fullmatch(r"\d{8,15}", s):
        raise _err("USGS_SITE_INVALID", f"{site!r} is not an 8-15 digit USGS site number")
    return f"USGS-{s}"


def to_m3s(values, units_native: str):
    """Convert discharge to m3/s. Unknown units raise USGS_UNIT_UNKNOWN."""
    f = _UNIT_FACTORS.get(units_native)
    if f is None:
        raise _err("USGS_UNIT_UNKNOWN", f"unrecognised discharge unit {units_native!r}",
                   "Only 'ft^3/s' (Water Data API) and 'ft3/s' (legacy) are converted.",
                   units=units_native)
    return values * f


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s)


def _norm_instant(x, end_of_day: bool = False) -> str:
    if isinstance(x, (_dt.datetime, pd.Timestamp)):
        t = pd.Timestamp(x)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(x)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return f"{s}T23:59:59Z" if end_of_day else f"{s}T00:00:00Z"
    return _norm_instant(pd.Timestamp(s))


def _request(url: str, params: Optional[Mapping[str, Any]], record: dict) -> dict:
    """GET + status handling + provenance capture. Returns the parsed JSON."""
    status, hdrs, body = _http_get(url, params, None)
    h = {k.lower(): v for k, v in (hdrs or {}).items()}
    if status == 429:
        raise _err("USGS_RATE_LIMITED", "USGS Water Data API returned HTTP 429",
                   "set API_USGS_PAT (api.data.gov key) in the environment",
                   retry_after=h.get("retry-after"))
    if status != 200:
        detail: Any = None
        try:
            detail = json.loads(body)
        except Exception:
            detail = body[:300].decode("utf-8", "replace")
        code = "USGS_INVALID_QUERY" if (status == 400 and isinstance(detail, dict)
                                        and detail.get("code") == "InvalidQuery") else "USGS_HTTP_ERROR"
        raise _err(code, f"HTTP {status} from {urlparse(url).path}: "
                         f"{detail.get('description') if isinstance(detail, dict) else detail}",
                   "adjust the query (e.g. continuous windows must be <= 1100 days)"
                   if code == "USGS_INVALID_QUERY" else "retry later",
                   status=status, body=detail)
    record["request_urls"].append(_canonical(url, params))
    record["response_sha256"].append(hashlib.sha256(body).hexdigest())
    record["last_modified"].append(h.get("last-modified"))      # absent in practice -> None
    record["etag"].append(h.get("etag"))                        # absent in practice -> None
    record["date"].append(h.get("date"))
    record["x_build_version"].append(h.get("x-build-version"))
    try:
        return json.loads(body)
    except Exception as e:
        raise _err("USGS_BAD_BODY", f"response is not JSON: {e}") from e


def _new_record() -> dict:
    return {"backend": "usgs_waterdata", "api_version": "ogcapi/v1",
            "request_urls": [], "retrieved_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "response_sha256": [], "last_modified": [], "etag": [], "date": [],
            "x_build_version": [], "feature_last_modified_max": None, "n_pages": 0}


def _paged_features(url: str, params: Mapping[str, Any], record: dict) -> list[dict]:
    """Follow ``rel=next`` links (opaque cursor) until exhausted."""
    feats: list[dict] = []
    doc = _request(url, params, record)
    for _ in range(_MAX_PAGES):
        record["n_pages"] += 1
        feats.extend(doc.get("features", []))
        nxt = next((l.get("href") for l in doc.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            return feats
        if urlparse(nxt).hostname not in _ALLOWED_HOSTS:
            raise _err("USGS_HOST_NOT_ALLOWED", f"next link points at {urlparse(nxt).hostname!r}")
        doc = _request(nxt, None, record)
    raise _err("USGS_PAGING_RUNAWAY", f"more than {_MAX_PAGES} pages")


# ---------------------------------------------------------------- series
def _series_metadata(site_id: str, parameter_code: str, statistic_id: Optional[str],
                     record: dict) -> list[dict]:
    p = {"monitoring_location_id": site_id, "limit": 1000, "f": "json"}
    out = []
    for f in _paged_features(f"{BASE}/collections/time-series-metadata/items", p, record):
        pr = f.get("properties", {})
        if pr.get("parameter_code") != parameter_code:
            continue
        if statistic_id is not None and pr.get("statistic_id") != statistic_id:
            continue
        out.append(pr)
    return out


def _pick_primary(meta: list[dict], site_id: str) -> str:
    prim = [m for m in meta if m.get("primary") == "Primary"]
    if len(prim) == 1:
        return prim[0]["id"]
    raise _err("USGS_MULTIPLE_SERIES",
               f"{site_id} has {len(meta)} matching series and {len(prim)} flagged 'Primary'",
               "pass time_series_id=<one of details.time_series_ids>",
               time_series_ids=[m["id"] for m in meta])


def _empty_outcome(site_id: str, parameter_code: str, statistic_id: Optional[str], record: dict):
    """Empty 200 body: say whether the site exists. Never an empty success."""
    loc = _paged_features(f"{BASE}/collections/time-series-metadata/items",
                          {"monitoring_location_id": site_id, "limit": 1000, "f": "json"}, record)
    if not loc:
        return _err("USGS_SITE_NOT_FOUND", f"{site_id} has no series in the Water Data API",
                    "check the site number", site_id=site_id)
    return _err("USGS_NO_DATA",
                f"{site_id} has no {parameter_code}/{statistic_id} values in the requested window",
                "widen the window or check the series coverage (begin/end) in time-series-metadata",
                site_id=site_id,
                series=[{"id": f["properties"].get("id"), "parameter_code": f["properties"].get("parameter_code"),
                         "statistic_id": f["properties"].get("statistic_id"),
                         "begin": f["properties"].get("begin"), "end": f["properties"].get("end")}
                        for f in loc if f["properties"].get("parameter_code") == parameter_code])


def summarize_frame(df: pd.DataFrame, *, null_dropped: int = 0) -> dict:
    """Quality summary of a served frame (needs time, approval_status, approval_code, qualifier).
    Counts always add up to the number of rows in ``df``."""
    n = len(df)
    ac = Counter(df["approval_status"])
    qc = Counter(t for q in df["qualifier"] for t in q)
    prov = df.loc[df["approval_code"] == "P", "time"]
    return {
        "n_rows": n,
        "approval_counts": dict(ac),
        "qualifier_counts": dict(qc),
        "qualifiers_other": {t: c for t, c in qc.items() if t not in QUALIFIER_CODES},
        "n_null_values": int(null_dropped),
        "provisional_fraction": (float(ac.get("Provisional", 0)) / n) if n else 0.0,
        "provisional_first": prov.min().isoformat() if len(prov) else None,
        "provisional_last": prov.max().isoformat() if len(prov) else None,
    }


def _build(features: list[dict], site_id: str, parameter_code: str, statistic_id: Optional[str],
           time_series_id: Optional[str], record: dict, continuous: bool) -> UsgsSeries:
    if not features:
        raise _empty_outcome(site_id, parameter_code, statistic_id, record)
    ids = sorted({f["properties"].get("time_series_id") for f in features})
    if time_series_id is not None:
        features = [f for f in features if f["properties"].get("time_series_id") == time_series_id]
        chosen = time_series_id
        if not features:
            raise _err("USGS_NO_DATA", f"time_series_id {time_series_id} returned no rows", site_id=site_id)
    elif len(ids) > 1:
        chosen = _pick_primary(_series_metadata(site_id, parameter_code, statistic_id, record), site_id)
        features = [f for f in features if f["properties"].get("time_series_id") == chosen]
        if not features:
            raise _err("USGS_NO_DATA", f"primary series {chosen} returned no rows", site_id=site_id)
    else:
        chosen = ids[0]

    props = [f["properties"] for f in features]
    units = {p.get("unit_of_measure") for p in props}
    if len(units) != 1:
        raise _err("USGS_UNIT_UNKNOWN", f"mixed or missing units in one series: {sorted(map(str, units))}")
    units_native = units.pop()

    vals, nulls = [], 0
    for p in props:
        v = p.get("value")
        if v is None:
            vals.append(float("nan")); nulls += 1
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            raise _err("USGS_VALUE_UNPARSEABLE", f"value {v!r} at {p.get('time')} is not numeric") from None
    approvals = [p.get("approval_status") for p in props]
    bad = sorted({str(a) for a in approvals if a not in APPROVAL_CODES})
    if bad:
        raise _err("USGS_APPROVAL_UNKNOWN", f"unrecognised approval_status values: {bad}",
                   "extend APPROVAL_CODES deliberately after reading USGS documentation", values=bad)
    quals = [tuple(p.get("qualifier") or ()) for p in props]
    q_codes = [",".join(QUALIFIER_CODES.get(t, t) for t in q) for q in quals]

    times = pd.to_datetime([p["time"] for p in props], utc=continuous)
    if not continuous:
        times = pd.DatetimeIndex(times).tz_localize(None)   # daily: calendar date, no zone
    df = pd.DataFrame({
        "time": times,
        "value_native": vals,
        "approval_status": approvals,
        "approval_code": [APPROVAL_CODES[a] for a in approvals],
        "qualifier": quals,
        "qualifier_code": q_codes,
    }).sort_values("time", kind="mergesort").reset_index(drop=True)
    if df["time"].duplicated().any():
        d = df.loc[df["time"].duplicated(), "time"].iloc[0]
        raise _err("USGS_DUPLICATE_TIME", f"duplicate timestamp {d} within series {chosen}")
    df.insert(2, "value_m3s", to_m3s(df["value_native"], units_native))

    summary = summarize_frame(df, null_dropped=nulls)
    lm = [p.get("last_modified") for p in props if p.get("last_modified")]
    record["feature_last_modified_max"] = max(lm) if lm else None
    return UsgsSeries(site_id=site_id, time_series_id=chosen, parameter_code=parameter_code,
                      statistic_id=statistic_id, units_native=units_native, frame=df,
                      quality_summary=summary, upstream=record)


def get_daily(site, start, end, parameter_code: str = "00060", statistic_id: str = "00003", *,
              time_series_id: Optional[str] = None, limit: int = 10000) -> UsgsSeries:
    """Daily values for [start, end] (inclusive calendar dates, 'YYYY-MM-DD')."""
    sid = _site_id(site)
    rec = _new_record()
    p = {"monitoring_location_id": sid, "parameter_code": parameter_code, "statistic_id": statistic_id,
         "time": f"{str(start)[:10]}/{str(end)[:10]}", "limit": int(limit), "f": "json"}
    if time_series_id:
        p["time_series_id"] = time_series_id
    feats = _paged_features(f"{BASE}/collections/daily/items", p, rec)
    return _build(feats, sid, parameter_code, statistic_id, time_series_id, rec, continuous=False)


def get_continuous(site, start, end, parameter_code: str = "00060", *,
                   time_series_id: Optional[str] = None, limit: int = 10000) -> UsgsSeries:
    """Instantaneous values (UTC). Windows are chunked to <= CONTINUOUS_MAX_DAYS."""
    sid = _site_id(site)
    rec = _new_record()
    t0 = pd.Timestamp(_norm_instant(start)); t1 = pd.Timestamp(_norm_instant(end, end_of_day=False))
    if t1 <= t0:
        raise _err("USGS_BAD_WINDOW", "end must be after start")
    step = pd.Timedelta(days=CONTINUOUS_MAX_DAYS)
    feats: list[dict] = []
    a = t0
    while a < t1:
        b = min(a + step, t1)
        p = {"monitoring_location_id": sid, "parameter_code": parameter_code,
             "time": f"{a.strftime('%Y-%m-%dT%H:%M:%SZ')}/{b.strftime('%Y-%m-%dT%H:%M:%SZ')}",
             "limit": int(limit), "f": "json"}
        if time_series_id:
            p["time_series_id"] = time_series_id
        feats.extend(_paged_features(f"{BASE}/collections/continuous/items", p, rec))
        a = b
    # Chunk boundaries are inclusive on both sides: drop an instant seen twice only
    # if both copies agree; a disagreement is a data-integrity problem.
    seen: dict[tuple, str] = {}
    uniq = []
    for f in feats:
        pr = f["properties"]
        k = (pr.get("time_series_id"), pr.get("time"))
        if k in seen:
            if seen[k] != json.dumps(pr, sort_keys=True):
                raise _err("USGS_DUPLICATE_TIME", f"chunk-boundary instant {k} differs between chunks")
            continue
        seen[k] = json.dumps(pr, sort_keys=True)
        uniq.append(f)
    return _build(uniq, sid, parameter_code, "00011", time_series_id, rec, continuous=True)


# ---------------------------------------------------------------- locations
def _loc(f: dict) -> MonitoringLocation:
    p = f.get("properties", {}); lon, lat = f["geometry"]["coordinates"][:2]
    da = p.get("drainage_area")
    return MonitoringLocation(
        site_id=p.get("id"), name=p.get("monitoring_location_name"), lon=float(lon), lat=float(lat),
        site_type_code=p.get("site_type_code"), state_code=p.get("state_code"),
        hydrologic_unit_code=p.get("hydrologic_unit_code"),
        horizontal_datum=p.get("original_horizontal_datum"),
        geometry_crs_note="geometry [lon, lat]; the API does not state its CRS/datum "
                          "(original_horizontal_datum is the source datum)",
        drainage_area_raw=float(da) if da is not None else None, drainage_area_units=_DRAINAGE_UNITS,
        altitude=p.get("altitude"), vertical_datum=p.get("vertical_datum"), raw=p)


def get_monitoring_locations(*, ids: Optional[Sequence[str]] = None, bbox: Optional[Sequence[float]] = None,
                             name_contains: Optional[str] = None, name: Optional[str] = None,
                             site_type_code: Optional[str] = "ST", limit: int = 500) -> list[MonitoringLocation]:
    """Monitoring locations by id, bbox or exact name.

    ``name_contains`` is NOT supported: only an exact-match ``monitoring_location_name``
    query was recorded; partial matching is unverified, so it is refused rather than guessed.
    """
    if name_contains is not None:
        raise _err("USGS_UNSUPPORTED", "partial name search is unverified; use name=<exact name>")
    rec = _new_record(); out: list[MonitoringLocation] = []
    if ids:
        for i in ids:
            p = {"id": _site_id(i), "f": "json"}
            out += [_loc(f) for f in _paged_features(f"{BASE}/collections/monitoring-locations/items", p, rec)]
        return out
    p: dict[str, Any] = {}
    if bbox is not None:
        p["bbox"] = ",".join(repr(float(x)) for x in bbox)
    if name is not None:
        p["monitoring_location_name"] = name
    if site_type_code and bbox is not None:
        p["site_type_code"] = site_type_code
    if not p:
        raise _err("USGS_UNSUPPORTED", "give ids, bbox or name")
    p.update({"limit": int(limit), "f": "json"})
    return [_loc(f) for f in _paged_features(f"{BASE}/collections/monitoring-locations/items", p, rec)]



# ---------------------------------------------------------------- gauge resolution (ADR-003 A2)
def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl, dp = math.radians(lon2 - lon1), p2 - p1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def _line_length(coords) -> float:
    return sum(_haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))


def _project(coords, lon: float, lat: float) -> tuple[float, float]:
    """Project a point onto a polyline. Returns (arc position from the first vertex in metres,
    geodesic offset from the point to the projection in metres). Local planar projection picks the
    foot point; all lengths are spherical (R=6371008.8 m)."""
    best = None
    cum = 0.0
    for a, b in zip(coords, coords[1:]):
        seg = _haversine_m(a[0], a[1], b[0], b[1])
        k = math.cos(math.radians(a[1]))
        bx, by = (b[0] - a[0]) * k, b[1] - a[1]
        px, py = (lon - a[0]) * k, lat - a[1]
        den = bx * bx + by * by
        t = 0.0 if den == 0 else max(0.0, min(1.0, (px * bx + py * by) / den))
        fx, fy = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
        off = _haversine_m(lon, lat, fx, fy)
        if best is None or off < best[1]:
            best = (cum + t * seg, off)
        cum += seg
    if best is None:
        raise _err("USGS_BAD_BODY", "flowline geometry has fewer than two vertices")
    return best


def _ring_area_km2(ring) -> float:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        s += math.radians(x2 - x1) * (2 + math.sin(math.radians(y1)) + math.sin(math.radians(y2)))
    return abs(s) * EARTH_R_M ** 2 / 2 / 1e6


def _geojson_area_km2(g: Mapping[str, Any]) -> float:
    t, c = g.get("type"), g.get("coordinates")
    if t == "Polygon":
        polys = [c]
    elif t == "MultiPolygon":
        polys = c
    else:
        raise _err("USGS_BAD_BODY", f"cannot take an area from geometry type {t!r}")
    return sum(_ring_area_km2([p[:2] for p in poly[0]]) - sum(_ring_area_km2([p[:2] for p in h]) for h in poly[1:])
               for poly in polys)


def polygon_area_km2(geometry) -> Optional[float]:
    """Spherical-excess area of a shapely Polygon/MultiPolygon (None for anything else)."""
    gi = getattr(geometry, "__geo_interface__", None)
    if not gi or gi.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    return _geojson_area_km2(gi)


def _chain(flowlines: dict, start: int, direction: str) -> list[int]:
    """Ordered COMIDs from ``start`` (exclusive), walking by shared end nodes.
    NHDPlus flowlines are digitised downstream: a reach's last vertex is its downstream node."""
    def key(pt):
        return (round(pt[0], 7), round(pt[1], 7))
    first = {key(c[0]): k for k, c in flowlines.items()}
    last = {key(c[-1]): k for k, c in flowlines.items()}
    out, cur, seen = [], start, {start}
    while True:
        if direction == "UM":
            nxt = last.get(key(flowlines[cur][0]))
        else:
            nxt = first.get(key(flowlines[cur][-1]))
        if nxt is None or nxt in seen:
            return out
        out.append(nxt); seen.add(nxt); cur = nxt


def _flowlines(doc: Mapping[str, Any]) -> dict[int, list]:
    out = {}
    for f in doc.get("features", []):
        cid = f.get("properties", {}).get("nhdplus_comid")
        if cid is not None and f.get("geometry", {}).get("type") == "LineString":
            out[int(cid)] = [c[:2] for c in f["geometry"]["coordinates"]]
    return out


def _area_gauge(loc: MonitoringLocation) -> tuple[Optional[float], Optional[float]]:
    tot = loc.drainage_area_raw
    con = loc.raw.get("contributing_drainage_area")
    return (None if tot is None else tot * SQMI_TO_KM2,
            None if con in (None, "") else float(con) * SQMI_TO_KM2)


def _area_check(a_gauge: Optional[float], a_basin: Optional[float], limit: float):
    if a_gauge is None or a_basin is None or a_gauge <= 0 or a_basin <= 0:
        return None, None, "unavailable"
    lr = math.log(a_gauge / a_basin)
    return a_gauge / a_basin, lr, ("pass" if abs(lr) <= limit + 1e-12 else "fail")


def _site_bare(site: str) -> str:
    return _site_id(site)[5:]


def _alias_site(basin_ref: Mapping[str, Any]) -> tuple[Optional[str], Optional[bool]]:
    for a in basin_ref.get("aliases", []) or []:
        if isinstance(a, Mapping) and a.get("scheme") == "usgs" and a.get("relation", "same_as") == "same_as":
            return str(a.get("id")), bool(a.get("verified", False))
    return None, None


def resolve_gauge(geometry=None, *, gauge_id: Optional[str] = None, basin_ref: Optional[Mapping[str, Any]] = None,
                  outlet: Optional[Sequence[float]] = None, max_distance_m: Optional[float] = None,
                  max_abs_log_area_ratio: Optional[float] = None,
                  outlet_offset_max_m: Optional[float] = None) -> GaugeResolution:
    """Resolve a USGS gauge under ADR-003 amendment A2 (rule_version ``ADR-003-A2-2026-10-17``).

    ``outlet`` is (lon, lat), WGS84. The three ``max_*``/``offset`` keywords are LIBRARY-ONLY
    overrides of the pre-registered thresholds; using one sets ``thresholds_overridden``. They are
    deliberately not exposed on the MCP surface. Refusals (GAUGE_REQUIRED / GAUGE_NOT_FOUND /
    GAUGE_AMBIGUOUS) are terminal; transport/HTTP failures raise plain SourceUnavailable.
    """
    th = dict(DEFAULT_THRESHOLDS)
    if max_distance_m is not None:
        th["max_distance_m"] = float(max_distance_m)
        th["query_distance_km"] = max(th["query_distance_km"], int(math.ceil(2 * max_distance_m / 1000.0)))
    if max_abs_log_area_ratio is not None:
        th["max_abs_log_area_ratio"] = float(max_abs_log_area_ratio)
    if outlet_offset_max_m is not None:
        th["outlet_offset_max_m"] = float(outlet_offset_max_m)
    overridden = any(th[k] != DEFAULT_THRESHOLDS[k] for k in th)
    rec = _new_record()
    area_geom = polygon_area_km2(geometry) if geometry is not None else None

    # ---- R0 precedence
    alias_site, alias_ver = _alias_site(basin_ref) if basin_ref is not None else (None, None)
    if gauge_id and alias_site and _site_id(gauge_id) != _site_id(alias_site):
        raise _err("GAUGE_AMBIGUOUS", f"explicit gauge_id {_site_id(gauge_id)} disagrees with the BasinRef "
                   f"alias usgs:{_site_bare(alias_site)}",
                   "pass only one of gauge_id / BasinRef alias, or make them agree",
                   reason="identity_conflict", gauge_id=_site_id(gauge_id), alias=f"usgs:{_site_bare(alias_site)}",
                   candidates=[])

    def _named(site: str, method: str, ver: Optional[bool]) -> GaugeResolution:
        sid = _site_id(site)
        locs = _paged_features(f"{BASE}/collections/monitoring-locations/items", {"id": sid, "f": "json"}, rec)
        if not locs:
            raise _err("GAUGE_NOT_FOUND", f"{sid} does not exist in the Water Data API",
                       "check the site number", reason="site_not_found", site_id=sid, candidates=[])
        loc = _loc(locs[0])
        ag, ac = _area_gauge(loc)
        ratio, lr, chk = _area_check(ag, area_geom, th["max_abs_log_area_ratio"])
        g = GaugeResolution(site=loc.site_id, method=method, alias_verified=ver, location=loc,
                            candidates=[loc.site_id], A_gauge_km2=ag, A_gauge_contributing_km2=ac,
                            A_basin_km2=area_geom, area_basis="request_geometry" if area_geom is not None else None,
                            area_ratio=ratio, log_area_ratio=lr, area_check=chk, thresholds=th,
                            thresholds_overridden=overridden, upstream=rec)
        if chk == "fail":
            g.warnings.append(f"area_check=fail: gauge total area {ag:.2f} km2 vs request geometry "
                              f"{area_geom:.2f} km2 (ratio {ratio:.3f}); the explicit identity assertion was honoured")
        return g

    if gauge_id:
        return _named(gauge_id, "explicit_id", None)
    if alias_site:
        return _named(alias_site, "basin_ref_alias", alias_ver)

    if outlet is None and basin_ref is not None and isinstance(basin_ref.get("outlet"), Mapping):
        o = basin_ref["outlet"]; outlet = (o.get("lon"), o.get("lat"))
    if outlet is None or outlet[0] is None:
        raise _err("GAUGE_REQUIRED", "no gauge id, usgs alias or outlet was supplied",
                   "pass gauge_id='USGS-xxxxxxxx', a BasinRef with a usgs: alias, an outlet (lon, lat) with a "
                   "basin geometry, or request a modelled product explicitly",
                   reason="no_identity", candidates=[])
    lon, lat = float(outlet[0]), float(outlet[1])

    # ---- R1 anchoring
    pos = _request(f"{NLDI_BASE}/comid/position", {"coords": f"POINT({lon} {lat})", "f": "json"}, rec)
    pf = [f for f in pos.get("features", []) if f.get("properties", {}).get("comid") is not None]
    if not pf:
        raise _err("GAUGE_NOT_FOUND", "NLDI found no catchment/flowline at the outlet",
                   "check the outlet coordinates (lon, lat) or pass gauge_id", reason="no_candidates",
                   outlet=[lon, lat], candidates=[])
    c_o = int(pf[0]["properties"]["comid"])
    own = [c[:2] for c in pf[0]["geometry"]["coordinates"]]
    s_o, offset = _project(own, lon, lat)
    L_o = _line_length(own)

    area_basis = "request_geometry" if area_geom is not None else "nldi_comid_basin"
    if area_geom is None and offset > th["outlet_offset_max_m"]:       # R6
        raise _err("GAUGE_REQUIRED", f"the outlet is {offset:.0f} m from its NHDPlus flowline and no basin "
                   "geometry was given, so the area check would not be independent",
                   "pass the delineated basin geometry together with the outlet, or pass gauge_id",
                   reason="outlet_off_network_no_geometry", outlet_offset_m=offset, comid_outlet=c_o, candidates=[])

    # ---- R2 candidate pool
    q = {"f": "json", "distance": th["query_distance_km"]}
    sites: dict[str, dict] = {}
    for d in ("UM", "DM"):
        nav = _request(f"{NLDI_BASE}/comid/{c_o}/navigation/{d}/nwissite", q, rec)
        for f in nav.get("features", []):
            ident = f.get("properties", {}).get("identifier")
            if ident and ident not in sites:
                sites[ident] = f
    fl = {c_o: own}
    chains = {}
    for d in ("UM", "DM"):
        navf = _request(f"{NLDI_BASE}/comid/{c_o}/navigation/{d}/flowlines", q, rec)
        got = _flowlines(navf)
        for k, v in got.items():
            fl.setdefault(k, v)
        chains[d] = _chain({**got, c_o: own}, c_o, d)
    seq = {}
    for d, sign in (("UM", -1), ("DM", 1)):
        run = 0.0
        # distance from P_o to the far end of the starting reach, then whole reaches
        run = s_o if d == "UM" else L_o - s_o
        for cid in chains[d]:
            seq[cid] = (d, sign, run, _line_length(fl[cid]))
            run += _line_length(fl[cid])

    if area_geom is None:
        basin = _request(f"{NLDI_BASE}/comid/{c_o}/basin", {"f": "json"}, rec)
        try:
            a_basin = _geojson_area_km2(basin["features"][0]["geometry"])
        except Exception:
            raise _err("USGS_BAD_BODY", f"NLDI basin of COMID {c_o} has no usable polygon") from None
    else:
        a_basin = area_geom

    rows = []
    for ident, f in sites.items():
        x, y = f["geometry"]["coordinates"][:2]
        cid = f["properties"].get("comid")
        cid = int(cid) if cid is not None else None
        row = {"site": ident, "comid": cid, "direction": None, "distance_m": None, "A_gauge_km2": None,
               "A_gauge_contributing_km2": None, "log_area_ratio": None, "status": None, "name": f["properties"].get("name")}
        # R3 distance
        if cid == c_o:
            sg, _ = _project(own, x, y)
            row["distance_m"] = sg - s_o
        elif cid in seq:
            dname, sign, run, L = seq[cid]
            sg, _ = _project(fl[cid], x, y)
            row["distance_m"] = sign * (run + (L - sg if sign < 0 else sg))
        if row["distance_m"] is not None:
            dm = row["distance_m"]
            row["direction"] = "at_outlet" if dm == 0 else ("upstream" if dm < 0 else "downstream")
        rows.append(row)

    lim = th["max_distance_m"]
    loc_by_site: dict[str, MonitoringLocation] = {}
    for r in rows:
        if r["distance_m"] is not None and abs(r["distance_m"]) > lim + 1e-6:   # 1e-6 m: float noise on the inclusive bound
            r["status"] = "outside_distance"
            continue
        meta = _series_metadata(r["site"], "00060", "00003", rec)         # R2 filter
        if not meta:
            r["status"] = "no_discharge_series"
            continue
        loc = (_paged_features(f"{BASE}/collections/monitoring-locations/items", {"id": r["site"], "f": "json"}, rec) or [None])[0]
        ag = ac = None
        if loc is not None:
            loc_by_site[r["site"]] = _loc(loc)
            ag, ac = _area_gauge(loc_by_site[r["site"]])
        r["A_gauge_km2"], r["A_gauge_contributing_km2"] = ag, ac
        _, lr, chk = _area_check(ag, a_basin, th["max_abs_log_area_ratio"])
        r["log_area_ratio"] = lr
        if chk == "unavailable":
            r["status"] = "area_missing"
        elif chk == "fail":
            r["status"] = "area_mismatch"          # verifiably not this gauge, whatever the distance
        elif r["distance_m"] is None:
            r["status"] = "distance_unverifiable"
        else:
            r["status"] = "admissible"

    rows.sort(key=lambda r: (abs(r["distance_m"]) if r["distance_m"] is not None else float("inf"), r["site"]))
    adm = [r for r in rows if r["status"] == "admissible"]
    unv = [r for r in rows if r["status"] in ("area_missing", "distance_unverifiable")]
    ctx = dict(candidates=rows, comid_outlet=c_o, outlet_offset_m=offset, area_basis=area_basis,
               A_basin_km2=a_basin, rule_version=RULE_VERSION, thresholds=th)
    # ---- R5 decision (no tie-break)
    if len(adm) + len(unv) >= 2:
        raise _err("GAUGE_AMBIGUOUS", f"{len(adm)} admissible and {len(unv)} unverifiable gauges near the outlet",
                   "pass gauge_id explicitly, or a BasinRef with a usgs: alias", reason="multiple_candidates", **ctx)
    if not adm:
        statuses = {r["status"] for r in rows}
        if "area_missing" in statuses:
            reason = "area_unverifiable"
        elif "distance_unverifiable" in statuses:
            reason = "distance_unverifiable"
        elif "area_mismatch" in statuses:
            reason = "area_mismatch"
        elif "outside_distance" in statuses:
            reason = "outside_distance"
        else:
            reason = "no_candidates"
        raise _err("GAUGE_NOT_FOUND", f"no NWIS discharge gauge passes the A2 gates ({reason})",
                   "pass gauge_id explicitly, a BasinRef with a usgs: alias, or request a modelled product "
                   "explicitly", reason=reason, **ctx)
    w = adm[0]
    loc = loc_by_site[w["site"]]
    ratio = w["A_gauge_km2"] / a_basin
    return GaugeResolution(
        site=w["site"], method="outlet_snap", distance_m=w["distance_m"], comid=w["comid"], candidates=rows,
        location=loc, comid_outlet=c_o, outlet_offset_m=offset, direction=w["direction"], distance_basis=DISTANCE_BASIS,
        A_gauge_km2=w["A_gauge_km2"], A_gauge_contributing_km2=w["A_gauge_contributing_km2"], A_basin_km2=a_basin,
        area_basis=area_basis, area_ratio=ratio, log_area_ratio=w["log_area_ratio"], area_check="pass",
        thresholds=th, thresholds_overridden=overridden, upstream=rec,
        warnings=([] if area_basis == "request_geometry" else
                  ["area_basis=nldi_comid_basin: the basin area comes from the same NLDI catchment lookup that "
                   "anchored the outlet, so the area check is not independent"]))
