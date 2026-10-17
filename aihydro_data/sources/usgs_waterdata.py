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
# (transient codes: USGS_TIMEOUT, USGS_UNREACHABLE, USGS_SERVER_ERROR, USGS_RATE_LIMITED).
TERMINAL_CODES = frozenset({
    "GAUGE_REQUIRED", "GAUGE_NOT_FOUND", "GAUGE_AMBIGUOUS",
    "USGS_SITE_INVALID", "USGS_SITE_NOT_FOUND", "USGS_NO_DATA", "USGS_MULTIPLE_SERIES",
    "USGS_APPROVAL_UNKNOWN", "USGS_UNIT_UNKNOWN", "USGS_VALUE_UNPARSEABLE", "USGS_VALUE_SENTINEL",
    "USGS_DUPLICATE_TIME", "USGS_CHUNK_CONFLICT", "USGS_QUALIFIER_INVALID", "USGS_TIME_NAIVE", "USGS_TIME_MISSING",
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


@dataclass
class GaugeResolution:
    site: str
    method: str                              # explicit_id | basin_ref_alias | outlet_snap
    distance_m: Optional[float]
    comid: Optional[int]
    candidates: list
    alias_verified: Optional[bool]
    location: Optional[MonitoringLocation] = None


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


# ---------------------------------------------------------------- gauge resolution
def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl, dp = math.radians(lon2 - lon1), p2 - p1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _alias_site(basin_ref: Mapping[str, Any]) -> tuple[Optional[str], Optional[bool]]:
    for a in basin_ref.get("aliases", []) or []:
        if isinstance(a, Mapping) and a.get("scheme") == "usgs" and a.get("relation", "same_as") == "same_as":
            return str(a.get("id")), bool(a.get("verified", False))
    return None, None


def resolve_gauge(geometry=None, *, gauge_id: Optional[str] = None, basin_ref: Optional[Mapping[str, Any]] = None,
                  outlet: Optional[Sequence[float]] = None, max_distance_m: Optional[float] = None) -> GaugeResolution:
    """Resolve a gauge. Terminal outcomes only: never a nearest-by-centroid guess.

    * ``gauge_id`` -> method ``explicit_id``;
    * ``basin_ref`` with a ``usgs`` alias -> ``basin_ref_alias`` (alias_verified recorded);
    * ``outlet`` (lon, lat) -> NLDI ``comid/position`` then UM/DM ``nwissite`` within
      ``max_distance_m`` (required: the threshold is a pending ruling, no default is assumed);
      exactly one candidate passes, zero -> GAUGE_NOT_FOUND, several -> GAUGE_AMBIGUOUS;
    * a geometry alone -> GAUGE_REQUIRED.
    """
    rec = _new_record()

    def _confirm(site: str) -> MonitoringLocation:
        sid = _site_id(site)
        locs = _paged_features(f"{BASE}/collections/monitoring-locations/items", {"id": sid, "f": "json"}, rec)
        if not locs:
            raise _err("GAUGE_NOT_FOUND", f"{sid} does not exist in the Water Data API", site_id=sid)
        return _loc(locs[0])

    if gauge_id:
        loc = _confirm(gauge_id)
        return GaugeResolution(loc.site_id, "explicit_id", None, None, [loc.site_id], None, loc)
    if basin_ref is not None:
        site, ver = _alias_site(basin_ref)
        if site:
            loc = _confirm(site)
            return GaugeResolution(loc.site_id, "basin_ref_alias", None, None, [loc.site_id], ver, loc)
        if outlet is None and isinstance(basin_ref.get("outlet"), Mapping):
            o = basin_ref["outlet"]; outlet = (o.get("lon"), o.get("lat"))
    if outlet is None:
        raise _err("GAUGE_REQUIRED", "no gauge id, usgs alias or outlet was supplied",
                   "pass gauge_id='USGS-xxxxxxxx' or an outlet (lon, lat)")
    if max_distance_m is None:
        raise _err("GAUGE_THRESHOLD_REQUIRED", "max_distance_m must be given explicitly",
                   "the default distance threshold is an open ruling (Wave U U1b)")
    lon, lat = float(outlet[0]), float(outlet[1])
    pos = _request(f"{NLDI_BASE}/comid/position", {"coords": f"POINT({lon} {lat})", "f": "json"}, rec)
    try:
        comid = int(pos["features"][0]["properties"]["comid"])
    except Exception:
        raise _err("GAUGE_NOT_FOUND", "NLDI returned no COMID for the outlet", outlet=[lon, lat]) from None
    cand: dict[str, dict] = {}
    for d in ("UM", "DM"):
        nav = _request(f"{NLDI_BASE}/comid/{comid}/navigation/{d}/nwissite", {"f": "json", "distance": 25}, rec)
        for f in nav.get("features", []):
            ident = f["properties"].get("identifier")
            x, y = f["geometry"]["coordinates"][:2]
            dist = _haversine_m(lon, lat, x, y)
            if dist <= max_distance_m:
                cand[ident] = {"site": ident, "distance_m": dist, "comid": f["properties"].get("comid"),
                               "name": f["properties"].get("name")}
    cands = sorted(cand.values(), key=lambda c: c["distance_m"])
    if not cands:
        raise _err("GAUGE_NOT_FOUND", f"no NWIS gauge within {max_distance_m} m of the outlet on COMID {comid}",
                   outlet=[lon, lat], comid=comid, max_distance_m=max_distance_m)
    if len(cands) > 1:
        raise _err("GAUGE_AMBIGUOUS", f"{len(cands)} gauges within {max_distance_m} m of the outlet",
                   "pass gauge_id explicitly", candidates=cands, comid=comid)
    c = cands[0]
    return GaugeResolution(c["site"], "outlet_snap", c["distance_m"], comid, cands, None, _confirm(c["site"]))
