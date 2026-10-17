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
# Identity / integrity refusals: terminal (TerminalSourceError), never answered by a
# modelled product. Everything else - HTTP errors, rate limits, timeouts, unreachable
# host, 5xx, bad bodies - means "could not look" and stays a plain SourceUnavailable
# (transient codes: USGS_TIMEOUT, USGS_UNREACHABLE, USGS_SERVER_ERROR (5xx), USGS_RATE_LIMITED (429),
# USGS_TRANSPORT_ERROR).
TERMINAL_CODES = frozenset({
    "GAUGE_REQUIRED", "GAUGE_NOT_FOUND", "GAUGE_AMBIGUOUS",
    "USGS_SITE_INVALID", "USGS_SITE_NOT_FOUND", "USGS_NO_DATA", "USGS_MULTIPLE_SERIES",
    "USGS_APPROVAL_UNKNOWN", "USGS_UNIT_UNKNOWN", "USGS_VALUE_UNPARSEABLE", "USGS_VALUE_SENTINEL",
    "USGS_DUPLICATE_TIME", "USGS_CHUNK_CONFLICT", "USGS_QUALIFIER_INVALID", "USGS_TIME_NAIVE", "USGS_TIME_MISSING",
    # malformed / unexpected data and caller or request errors are not "could not look"
    "USGS_BAD_BODY", "USGS_HTTP_REDIRECT", "USGS_PAGING_RUNAWAY", "USGS_URL_REFUSED", "USGS_HOST_NOT_ALLOWED",
    "USGS_HTTP_ERROR", "USGS_INVALID_QUERY", "USGS_BAD_WINDOW", "USGS_UNSUPPORTED",
    "GEOMETRY_CRS_INVALID", "GEOMETRY_UNSUPPORTED",
})
TRANSIENT_CODES = frozenset({"USGS_TIMEOUT", "USGS_UNREACHABLE", "USGS_SERVER_ERROR", "USGS_RATE_LIMITED",
                             "USGS_TRANSPORT_ERROR"})


def _err(code: str, message: str, recovery: str = "", **details: Any) -> SourceUnavailable:
    cls = TerminalSourceError if code in TERMINAL_CODES else SourceUnavailable
    return cls(code=code, message=message, recovery=recovery, details=dict(details))


# ---------------------------------------------------------------- transport
def _session():
    """Factory for the requests session (tests mount an in-process adapter here)."""
    import requests
    return requests.Session()


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https":
        raise _err("USGS_URL_REFUSED", f"refusing non-https URL (scheme {u.scheme!r})")
    if u.hostname not in _ALLOWED_HOSTS:
        raise _err("USGS_HOST_NOT_ALLOWED", f"refusing request to {u.hostname!r}")


def _http_get(url: str, params: Optional[Mapping[str, Any]] = None,
              headers: Optional[Mapping[str, str]] = None) -> tuple[int, dict, bytes]:
    """The ONLY network call in this module. Returns (status, headers, body).

    https and the host allow-list are enforced here, per request. Redirects are NEVER
    followed (requests would forward ``X-Api-Key`` across hosts); a 3xx is returned to
    the caller, which treats it as an error. Timeouts / connection failures map to
    transient SourceUnavailable codes."""
    import requests  # local: keeps import cost off non-USGS paths

    _check_url(url)
    host = urlparse(url).hostname
    h = {"User-Agent": "aihydro-data/usgs_waterdata", "Accept": "application/json"}
    key = os.environ.get("API_USGS_PAT")
    if key and host == "api.waterdata.usgs.gov":
        h["X-Api-Key"] = key
    h.update(headers or {})
    try:
        r = _session().get(url, params=params, headers=h, timeout=120, allow_redirects=False)
    except requests.exceptions.Timeout as e:
        raise _err("USGS_TIMEOUT", f"timeout talking to {host}", "retry later") from e
    except requests.exceptions.ConnectionError as e:
        raise _err("USGS_UNREACHABLE", f"cannot reach {host}", "check the network / retry later") from e
    except requests.exceptions.RequestException as e:      # ChunkedEncodingError, ContentDecodingError, ...
        raise _err("USGS_TRANSPORT_ERROR", f"{type(e).__name__} talking to {host}", "retry later") from e
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
DISTANCE_BASIS = ("shortest along-network path (Dijkstra over every returned NLDI UM/DM flowline, so confluences "
                  "and braids are all considered) of spherical great-circle segment lengths (R=6371008.8 m) "
                  "between the projections of the outlet and of the gauge point onto the NLDI NHDPlusV2 flowline "
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
    if not re.fullmatch(r"[0-9]{8,15}", s):
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


def _date10(x) -> str:
    """'YYYY-MM-DD' from a date/datetime or an ASCII ISO string; anything else is refused."""
    if isinstance(x, (_dt.date, pd.Timestamp)):
        return x.strftime("%Y-%m-%d")
    s = str(x)
    if not (s.isascii() and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}([T ].*)?", s, re.ASCII)):
        raise _err("USGS_BAD_WINDOW", f"{s!r} is not an ISO date (YYYY-MM-DD)")
    return s[:10]


def _norm_instant(x, end_of_day: bool = False) -> str:
    if isinstance(x, (_dt.datetime, pd.Timestamp)):
        t = pd.Timestamp(x)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(x)
    if not s.isascii():
        raise _err("USGS_BAD_WINDOW", f"non-ASCII characters in time {s!r}")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
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
        if 300 <= status < 400:
            code = "USGS_HTTP_REDIRECT"            # never followed: the key would travel with it
        elif status >= 500:
            code = "USGS_SERVER_ERROR"
        elif status == 400 and isinstance(detail, dict) and detail.get("code") == "InvalidQuery":
            code = "USGS_INVALID_QUERY"
        else:
            code = "USGS_HTTP_ERROR"
        raise _err(code, f"HTTP {status} from {urlparse(url).path}: "
                         f"{detail.get('description') if isinstance(detail, dict) else detail}",
                   "adjust the query (e.g. continuous windows must be <= 1100 days)"
                   if code == "USGS_INVALID_QUERY" else "retry later" if code == "USGS_SERVER_ERROR" else "",
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
    seen: set[str] = set()
    doc = _request(url, params, record)
    for _ in range(_MAX_PAGES):
        record["n_pages"] += 1
        feats.extend(doc.get("features", []))
        nxt = next((l.get("href") for l in doc.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            return feats
        _check_url(nxt)                      # https + host, before any request is made
        if nxt in seen:
            raise _err("USGS_PAGING_RUNAWAY", "the server repeated a next link")
        seen.add(nxt)
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


_VALUE_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?", re.ASCII)
LEGACY_SENTINEL = -999999.0          # legacy noDataValue; must never reach a discharge series


def _parse_value(v: Any, when: Any) -> float:
    """Strict: a value is a JSON string holding a finite decimal, or null (handled by the
    caller). No whitespace, no NaN/Infinity words, no JSON numbers or booleans."""
    if not isinstance(v, str) or not _VALUE_RE.fullmatch(v):
        raise _err("USGS_VALUE_UNPARSEABLE", f"value {v!r} at {when} is not a plain decimal string")
    x = float(v)
    if not math.isfinite(x):
        raise _err("USGS_VALUE_UNPARSEABLE", f"value {v!r} at {when} is not finite")
    if x == LEGACY_SENTINEL:
        raise _err("USGS_VALUE_SENTINEL", f"value {v!r} at {when} is the legacy no-data sentinel",
                   "this must not be converted to discharge; report it as missing upstream")
    return x


def _parse_qualifier(q: Any, when: Any) -> tuple:
    if q is None:
        return ()
    if not isinstance(q, list) or not all(isinstance(t, str) for t in q):
        raise _err("USGS_QUALIFIER_INVALID", f"qualifier {q!r} at {when} is not null or a list of strings")
    return tuple(q)


def _has_zone(t: Any) -> bool:
    return isinstance(t, str) and bool(re.search(r"(Z|[+-][0-9]{2}:?[0-9]{2})$", t, re.ASCII))


def _gap_stats(df: pd.DataFrame, continuous: bool, window: Optional[tuple]) -> dict:
    """Completeness facts. Daily: calendar days expected vs present in the requested window.
    Continuous: spacing statistics (the API reports no expected cadence)."""
    out: dict = {}
    if not continuous:
        if window is not None:
            lo, hi = pd.Timestamp(window[0]), pd.Timestamp(window[1])
            exp = int((hi - lo).days) + 1
            have = int(df["time"].between(lo, hi).sum())
            out.update(n_expected=exp, n_missing=max(0, exp - have))
        return out
    if len(df) >= 2:
        steps = df["time"].diff().dropna().dt.total_seconds()
        med = float(steps.median())
        out.update(median_step_s=med, max_gap_s=float(steps.max()),
                   n_gaps_gt_2x_median=int((steps > 2 * med).sum()))
    return out


def summarize_frame(df: pd.DataFrame, *, null_dropped: int = 0) -> dict:
    """Quality summary of a served frame (needs time, approval_status, approval_code, qualifier).
    Counts always add up to the number of rows in ``df``."""
    n = len(df)
    ac = Counter(df["approval_status"])
    qc = Counter(t for q in df["qualifier"] for t in q)
    prov = df.loc[df["approval_code"] == "P", "time"]
    neg = int((df["value_native"] < 0).sum()) if "value_native" in df.columns else 0
    return {
        "n_rows": n,
        "approval_counts": dict(ac),
        "qualifier_counts": dict(qc),
        "qualifiers_other": {t: c for t, c in qc.items() if t not in QUALIFIER_CODES},
        "n_null_values": int(null_dropped),
        "n_negative": neg,        # flagged, never converted or dropped (can be real at tidal/backwater sites)
        "provisional_fraction": (float(ac.get("Provisional", 0)) / n) if n else 0.0,
        "provisional_first": prov.min().isoformat() if len(prov) else None,
        "provisional_last": prov.max().isoformat() if len(prov) else None,
    }


def _build(features: list[dict], site_id: str, parameter_code: str, statistic_id: Optional[str],
           time_series_id: Optional[str], record: dict, continuous: bool,
           window: Optional[tuple] = None) -> UsgsSeries:
    if not features:
        raise _empty_outcome(site_id, parameter_code, statistic_id, record)
    for f in features:
        if not isinstance(f, dict) or not isinstance(f.get("properties"), dict):
            raise _err("USGS_BAD_BODY", "feature without a properties object")
        t = f["properties"].get("time")
        if not isinstance(t, str) or not t:
            raise _err("USGS_TIME_MISSING", f"feature without a usable time: {t!r}")
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
        vals.append(_parse_value(v, p.get("time")))
    approvals = [p.get("approval_status") for p in props]
    bad = sorted({str(a) for a in approvals if a not in APPROVAL_CODES})
    if bad:
        raise _err("USGS_APPROVAL_UNKNOWN", f"unrecognised approval_status values: {bad}",
                   "extend APPROVAL_CODES deliberately after reading USGS documentation", values=bad)
    quals = [_parse_qualifier(p.get("qualifier"), p.get("time")) for p in props]
    q_codes = [",".join(QUALIFIER_CODES.get(t, t) for t in q) for q in quals]

    raw_times = [p["time"] for p in props]
    if continuous:
        naive = [t for t in raw_times if not _has_zone(t)]
        if naive:
            raise _err("USGS_TIME_NAIVE", f"continuous timestamp without a zone designator: {naive[0]!r}",
                       "the API documents UTC; a zone-less instant is refused, not assumed")
    times = pd.to_datetime(raw_times, utc=continuous)
    if not continuous:
        times = pd.DatetimeIndex(times).tz_localize(None)   # daily: calendar date, no zone
    df = pd.DataFrame({
        "time": times,
        "value_native": vals,
        "negative_flag": [bool(v < 0) for v in vals],
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
    summary.update(_gap_stats(df, continuous, window))
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
         "time": f"{_date10(start)}/{_date10(end)}", "limit": int(limit), "f": "json"}
    if time_series_id:
        p["time_series_id"] = time_series_id
    feats = _paged_features(f"{BASE}/collections/daily/items", p, rec)
    return _build(feats, sid, parameter_code, statistic_id, time_series_id, rec, continuous=False,
                  window=(_date10(start), _date10(end)))


def get_continuous(site, start, end, parameter_code: str = "00060", *,
                   time_series_id: Optional[str] = None, limit: int = 10000) -> UsgsSeries:
    """Instantaneous values (UTC). Windows are chunked to <= CONTINUOUS_MAX_DAYS."""
    sid = _site_id(site)
    rec = _new_record()
    # A date-only END is inclusive (to 23:59:59Z), like get_daily; a full timestamp is taken as given.
    t0 = pd.Timestamp(_norm_instant(start)); t1 = pd.Timestamp(_norm_instant(end, end_of_day=True))
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
    seen: dict[tuple, tuple] = {}
    uniq = []
    for f in feats:
        pr = f["properties"]
        k = (pr.get("time_series_id"), pr.get("time"))
        sig = (pr.get("value"), pr.get("approval_status"), json.dumps(pr.get("qualifier")), pr.get("unit_of_measure"))
        if k in seen:
            if seen[k] != sig:
                raise _err("USGS_CHUNK_CONFLICT", f"chunk-boundary instant {k} differs between chunks "
                           f"(value/approval/qualifier): {seen[k]} vs {sig}")
            continue
        seen[k] = sig
        uniq.append(f)
    return _build(uniq, sid, parameter_code, "00011", time_series_id, rec, continuous=True,
                  window=(t0, t1))


# ---------------------------------------------------------------- locations
class LocationResult(list):
    """list[MonitoringLocation] that also carries the request provenance (``upstream``), so an
    empty bbox/name result is an explicit, attributable "the server returned 0 features"."""

    upstream: dict = {}


def _loc(f: dict) -> MonitoringLocation:
    p = f.get("properties") or {}
    g = f.get("geometry")
    try:
        lon, lat = g["coordinates"][:2]
        lon, lat = float(lon), float(lat)
    except (TypeError, KeyError, ValueError, IndexError):
        raise _err("USGS_BAD_BODY", f"monitoring location {p.get('id')!r} has no usable point geometry") from None
    da = p.get("drainage_area")
    try:
        da = None if da is None else float(da)
    except (TypeError, ValueError):
        raise _err("USGS_BAD_BODY", f"drainage_area {da!r} of {p.get('id')!r} is not numeric") from None
    return MonitoringLocation(
        site_id=p.get("id"), name=p.get("monitoring_location_name"), lon=lon, lat=lat,
        site_type_code=p.get("site_type_code"), state_code=p.get("state_code"),
        hydrologic_unit_code=p.get("hydrologic_unit_code"),
        horizontal_datum=p.get("original_horizontal_datum"),
        geometry_crs_note="geometry [lon, lat]; the API does not state its CRS/datum "
                          "(original_horizontal_datum is the source datum)",
        drainage_area_raw=da, drainage_area_units=_DRAINAGE_UNITS,
        altitude=p.get("altitude"), vertical_datum=p.get("vertical_datum"), raw=p)


def get_monitoring_locations(*, ids: Optional[Sequence[str]] = None, bbox: Optional[Sequence[float]] = None,
                             name_contains: Optional[str] = None, name: Optional[str] = None,
                             site_type_code: Optional[str] = "ST", limit: int = 500) -> LocationResult:
    """Monitoring locations by id, bbox or exact name.

    ``name_contains`` is NOT supported: only an exact-match ``monitoring_location_name``
    query was recorded; partial matching is unverified, so it is refused rather than guessed.

    An id lookup that finds nothing raises ``USGS_SITE_NOT_FOUND``. A bbox/name search that
    matches nothing returns an empty ``LocationResult`` whose ``upstream`` shows the request,
    never a bare ``[]``.
    """
    if name_contains is not None:
        raise _err("USGS_UNSUPPORTED", "partial name search is unverified; use name=<exact name>")
    rec = _new_record(); out = LocationResult(); out.upstream = rec
    if ids:
        for i in ids:
            p = {"id": _site_id(i), "f": "json"}
            got = [_loc(f) for f in _paged_features(f"{BASE}/collections/monitoring-locations/items", p, rec)]
            if not got:
                raise _err("USGS_SITE_NOT_FOUND", f"{p['id']} is not a monitoring location in the Water Data API",
                           "check the site number", site_id=p["id"])
            out.extend(got)
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
    out.extend(_loc(f) for f in _paged_features(f"{BASE}/collections/monitoring-locations/items", p, rec))
    return out



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
    """Spherical-excess area of a shapely Polygon/MultiPolygon or a GeoJSON geometry mapping
    (None for anything else)."""
    gi = geometry if isinstance(geometry, Mapping) else getattr(geometry, "__geo_interface__", None)
    if not gi or gi.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    return _geojson_area_km2(gi)


def _coords(o):
    if isinstance(o, (list, tuple)):
        if o and isinstance(o[0], (int, float)):
            yield o
        else:
            for x in o:
                yield from _coords(x)


def _checked_geometry_area(geometry) -> Optional[float]:
    """Area (km2) of the request geometry, after checking it is EPSG:4326 lon/lat.

    Polygon/MultiPolygon (shapely or GeoJSON mapping) give an area; a Point/MultiPoint gives None
    (it can anchor an outlet but says nothing about area). Anything else is refused, never silently
    ignored (which would swap the independent area basis for the NLDI catchment)."""
    if geometry is None:
        return None
    gi = geometry if isinstance(geometry, Mapping) else getattr(geometry, "__geo_interface__", None)
    if not isinstance(gi, Mapping) or "coordinates" not in gi:
        raise _err("GEOMETRY_UNSUPPORTED", f"cannot use a {type(geometry).__name__} as a basin geometry",
                   "pass a shapely Polygon/MultiPolygon or a GeoJSON geometry in EPSG:4326")
    for c in _coords(gi["coordinates"]):
        if not (-180.0 <= c[0] <= 180.0 and -90.0 <= c[1] <= 90.0):
            raise _err("GEOMETRY_CRS_INVALID", f"coordinate {tuple(c[:2])} is outside lon/lat range; "
                       "EPSG:4326 (lon, lat degrees) is required", "reproject the geometry to EPSG:4326")
    if gi.get("type") in ("Point", "MultiPoint"):
        return None
    a = polygon_area_km2(gi)
    if a is None:
        raise _err("GEOMETRY_UNSUPPORTED", f"geometry type {gi.get('type')!r} cannot give a basin area",
                   "pass a Polygon or MultiPolygon, or a Point/outlet only")
    if not a > 0:
        raise _err("GEOMETRY_UNSUPPORTED", "the basin geometry has zero area")
    return a


def _node(pt) -> tuple:
    return (round(pt[0], 7), round(pt[1], 7))


def _near_distances(reaches: dict, start_node: tuple, start_dist: float, direction: str, skip: int) -> dict:
    """Dijkstra over ALL returned reaches (confluences, divergences and braids included).

    NHDPlus flowlines are digitised downstream: a reach's first vertex is its upstream node and its
    last vertex its downstream node. Moving upstream (``UM``) from node n we may enter every reach
    whose DOWNSTREAM node is n and arrive at its UPSTREAM node; ``DM`` is the mirror. The result maps
    each reach to the along-network distance from P_o to the node where it is first entered
    (its downstream node for UM, its upstream node for DM), taking the shortest path. Reaches are
    visited in (distance, comid) order, so the outcome never depends on response order."""
    import heapq
    enter = {}                      # node -> sorted [(comid, exit_node, length)]
    for cid in sorted(reaches):
        if cid == skip:
            continue
        c = reaches[cid]
        up, dn = _node(c[0]), _node(c[-1])
        at, to = (dn, up) if direction == "UM" else (up, dn)
        enter.setdefault(at, []).append((cid, to, _line_length(c)))
    best = {start_node: start_dist}
    near: dict[int, float] = {}
    heap = [(start_dist, start_node)]
    while heap:
        d, n = heapq.heappop(heap)
        if d > best.get(n, float("inf")):
            continue
        for cid, to, L in enter.get(n, ()):
            if cid not in near or d < near[cid]:
                near[cid] = d
            nd = d + L
            if nd < best.get(to, float("inf")):
                best[to] = nd
                heapq.heappush(heap, (nd, to))
    return near


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
    found = []
    for a in basin_ref.get("aliases", []) or []:
        if isinstance(a, Mapping) and a.get("scheme") == "usgs" and a.get("relation", "same_as") == "same_as":
            found.append((_site_id(str(a.get("id"))), bool(a.get("verified", False))))
    sites = sorted({f[0] for f in found})
    if len(sites) > 1:
        raise _err("GAUGE_AMBIGUOUS", f"the BasinRef carries conflicting usgs aliases {sites}",
                   "fix the BasinRef or pass gauge_id explicitly", reason="identity_conflict", aliases=sites, candidates=[])
    if not found:
        return None, None
    return sites[0], all(v for sid, v in found if sid == sites[0])


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
    area_geom = _checked_geometry_area(geometry)

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
        if method == "basin_ref_alias" and not ver:
            g.warnings.append(f"the BasinRef usgs: alias for {loc.site_id} is not verified (alias_verified=false); "
                              "identity rests on an unconfirmed assertion")
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
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise _err("GEOMETRY_CRS_INVALID", f"outlet ({lon}, {lat}) is outside lon/lat range; pass (lon, lat) in EPSG:4326",
                   "the outlet is (lon, lat) for resolve_gauge and (lat, lon) for fetch()")

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
        for f in sorted(nav.get("features", []), key=lambda f: str(f.get("properties", {}).get("identifier"))):
            ident = f.get("properties", {}).get("identifier")
            if ident and ident not in sites:
                sites[ident] = f
    fl = {c_o: own}
    near = {}
    for d in ("UM", "DM"):
        navf = _request(f"{NLDI_BASE}/comid/{c_o}/navigation/{d}/flowlines", q, rec)
        got = _flowlines(navf)
        for k in sorted(got):
            fl.setdefault(k, got[k])
        if d == "UM":
            near[d] = _near_distances({**got, c_o: own}, _node(own[0]), s_o, "UM", c_o)
        else:
            near[d] = _near_distances({**got, c_o: own}, _node(own[-1]), L_o - s_o, "DM", c_o)

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
        # R3 distance: same reach by arc position; otherwise the shortest returned path
        if cid == c_o:
            sg, _ = _project(own, x, y)
            row["distance_m"] = sg - s_o
        else:
            opts = []
            if cid in near["UM"]:
                sg, _ = _project(fl[cid], x, y)
                opts.append(-(near["UM"][cid] + (_line_length(fl[cid]) - sg)))
            if cid in near["DM"]:
                sg, _ = _project(fl[cid], x, y)
                opts.append(near["DM"][cid] + sg)
            if opts:
                row["distance_m"] = min(opts, key=lambda v: (abs(v), v))
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
