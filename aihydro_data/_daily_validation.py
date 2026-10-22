"""Source-agnostic output validation for daily gridded fetches.

A daily product fetched as several chunks (one per calendar year) can come
back wrong without any error: a chunk can hold another year's days (observed
with pygridmet 0.19.4, see docs/vision-2040/evidence/gridmet-chunk-defect-1),
and a client can skip days it believes do not exist. Merging such chunks
yields a series that looks fine and is not. These helpers check the result
against the *requested* calendar, whatever produced it, and name each gap
with a reason code instead of guessing a cause.

Reason codes carried by ``missing_ranges``:

``client_calendar_assumption``  the client library never asked for the day
    (pygridmet skips Dec 31 in leap years). Whether the SOURCE lacks the day
    is unverified, so it is not reported as missing in the source.
``chunk_mismatch``              a chunk was rejected (wrong time range or
    duplicate) and not merged; its days are absent from the result.
``absent_in_response``          the day is simply not in the returned series.
``absent_after_retry``          a chunk with internal gaps was re-fetched once with
    a fresh private cache and the days were still absent. The chunk is kept and
    the days disclosed; the result is NOT cached (the gap may be transient).

Reasons in ``NO_CACHE_REASONS`` keep a result out of the content-addressed cache.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

import pandas as pd

CLIENT_CALENDAR = "client_calendar_assumption"
CHUNK_MISMATCH = "chunk_mismatch"
ABSENT = "absent_in_response"
ABSENT_AFTER_RETRY = "absent_after_retry"
NO_CACHE_REASONS = frozenset({CHUNK_MISMATCH, ABSENT_AFTER_RETRY})

DAILY_TIMESTEPS = frozenset({"daily", "1d", "day", "1-day"})


def is_daily(timestep: str) -> bool:
    return (timestep or "").strip().lower() in DAILY_TIMESTEPS


def skips_leap_dec31(spec: Any) -> bool:
    """True when the product's client library never requests Dec 31 of leap
    years (pygridmet; unverified for GridMET itself, so only disclosed)."""
    try:
        return (getattr(spec, "source", "") == "hyriver"
                and "pygridmet_variable" in (spec.backend_config or {}))
    except Exception:
        return False


def as_day_index(times: Any) -> pd.DatetimeIndex:
    """Normalised (midnight) DatetimeIndex of ``times``; NaT dropped, order kept."""
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Series(list(times)), errors="coerce").dropna())
    return idx.normalize()


def is_leap_dec31(day: pd.Timestamp) -> bool:
    return bool(day.is_leap_year and day.month == 12 and day.day == 31)


def _runs(days: Sequence[pd.Timestamp], reasons: Sequence[str]) -> list[dict[str, Any]]:
    """Compress per-day (day, reason) into contiguous same-reason ranges."""
    out: list[dict[str, Any]] = []
    for d, r in zip(days, reasons):
        if out and out[-1]["reason"] == r and (d - pd.Timestamp(out[-1]["end"])).days == 1:
            out[-1]["end"] = str(d.date())
            out[-1]["days"] += 1
        else:
            out.append({"start": str(d.date()), "end": str(d.date()),
                        "days": 1, "reason": r})
    return out


def find_missing_ranges(
    times: Any,
    first: Optional[Any] = None,
    last: Optional[Any] = None,
    *,
    skip_leap_dec31: bool = False,
    chunk_mismatch_ranges: Iterable[tuple[Any, Any]] = (),
    reason_hints: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Run-compressed daily gaps between ``first`` and ``last`` (default: the
    served first/last day), each with a reason code."""
    idx = as_day_index(times)
    if idx.empty:
        return []
    lo = pd.Timestamp(first).normalize() if first is not None else idx.min()
    hi = pd.Timestamp(last).normalize() if last is not None else idx.max()
    if hi < lo:
        return []
    missing = pd.date_range(lo, hi, freq="D").difference(idx.unique())
    if len(missing) == 0:
        return []
    bad = [(pd.Timestamp(a).normalize(), pd.Timestamp(b).normalize())
           for a, b in chunk_mismatch_ranges]
    # Reasons recorded when the data was fetched (e.g. restored from the cache
    # manifest) win over anything re-derived from the series alone.
    hints = [(pd.Timestamp(h["start"]).normalize(), pd.Timestamp(h["end"]).normalize(),
              h["reason"]) for h in reason_hints or ()]
    reasons = []
    for d in missing:
        hinted = next((r for a, b, r in hints if a <= d <= b), None)
        if hinted:
            reasons.append(hinted)
        elif any(a <= d <= b for a, b in bad):
            reasons.append(CHUNK_MISMATCH)
        elif skip_leap_dec31 and is_leap_dec31(d):
            reasons.append(CLIENT_CALENDAR)
        else:
            reasons.append(ABSENT)
    return _runs(list(missing), reasons)


def validate_daily_series(
    times: Any,
    start: str,
    end: str,
    *,
    chunks: Optional[Sequence[tuple[Any, Any, Any]]] = None,
    skip_leap_dec31: bool = False,
) -> dict[str, Any]:
    """Check a daily series against the requested calendar.

    ``chunks`` is an optional list of ``(chunk_start, chunk_end, chunk_times)``;
    where given, each chunk's own time coordinate must lie inside its request.
    Returns ``{"ok", "problems", "duplicates", "years_missing", "bad_chunks",
    "missing_ranges"}``. ``ok`` is False for duplicate timestamps, a requested
    year with no data, an internal gap that is not a client calendar
    assumption, or a chunk outside its requested range.
    """
    problems: list[str] = []
    idx = as_day_index(times)
    dups = int(idx.duplicated().sum())
    if dups:
        problems.append(f"{dups} duplicate timestamp(s)")

    req_years = set(range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1))
    years_missing = sorted(req_years - set(int(y) for y in idx.year.unique()))
    if years_missing:
        problems.append(f"requested year(s) with no data: {years_missing}")

    bad_chunks: list[dict[str, Any]] = []
    for cs, ce, ct in (chunks or []):
        cidx = as_day_index(ct)
        lo, hi = pd.Timestamp(cs).normalize(), pd.Timestamp(ce).normalize()
        if cidx.empty:
            why = "no time coordinates"
        elif cidx.min() < lo or cidx.max() > hi:
            why = f"time {cidx.min().date()}..{cidx.max().date()} outside request"
        elif cidx.duplicated().any():
            why = "duplicate timestamps inside chunk"
        else:
            continue
        bad_chunks.append({"start": str(lo.date()), "end": str(hi.date()), "problem": why})
    if bad_chunks:
        problems.append(f"{len(bad_chunks)} chunk(s) outside their requested range")

    # Internal gaps only (between the first and last served day). A shorter
    # head or tail is the archive-extent question `_temporal_coverage` answers.
    ranges = find_missing_ranges(idx, skip_leap_dec31=skip_leap_dec31)
    unexplained = sum(r["days"] for r in ranges if r["reason"] != CLIENT_CALENDAR)
    if unexplained:
        problems.append(f"{unexplained} internal day(s) absent")
    return {"ok": not problems, "problems": problems, "duplicates": dups,
            "years_missing": years_missing, "bad_chunks": bad_chunks,
            "missing_ranges": ranges}


def year_chunks(start: str, end: str, *, skip_leap_dec31: bool = False
                ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Split [start, end] at calendar-year boundaries.

    With ``skip_leap_dec31`` a chunk that is only a leap-year Dec 31 is dropped:
    the client library would request nothing for it (pygridmet returns ``[]``).
    Those days surface as ``client_calendar_assumption`` in ``missing_ranges``.
    """
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    for y in range(s.year, e.year + 1):
        a, b = max(s, pd.Timestamp(y, 1, 1)), min(e, pd.Timestamp(y, 12, 31))
        if skip_leap_dec31 and a == b and is_leap_dec31(a):
            continue
        out.append((a, b))
    return out


def check_daily_chunk(
    times: Any,
    chunk_start: Any,
    chunk_end: Any,
    *,
    is_last: bool = False,
    skip_leap_dec31: bool = False,
) -> dict[str, Any]:
    """Classify one fetched chunk against its own request.

    ``status``:
      ``ok``     every expected day present (expected = requested days minus
                 leap Dec 31 when ``skip_leap_dec31``).
      ``gappy``  right range, no duplicates, but days missing INSIDE the served
                 span; ``gaps`` lists them. Worth one retry; kept if they persist.
      ``bad``    empty, outside its requested range, duplicate timestamps, or
                 truncated (the first expected day, or the last one of a non-final
                 chunk, is absent). Never merged if it persists.
    A tail shortfall in the FINAL chunk is the archive's extent, not a defect:
    it is left to the head/tail coverage fields.
    """
    lo, hi = pd.Timestamp(chunk_start).normalize(), pd.Timestamp(chunk_end).normalize()
    idx = as_day_index(times)
    if idx.empty:
        return {"status": "bad", "problems": ["no time coordinates"], "gaps": []}
    if idx.min() < lo or idx.max() > hi:
        return {"status": "bad", "gaps": [], "problems": [
            f"time {idx.min().date()}..{idx.max().date()} outside request {lo.date()}..{hi.date()}"]}
    if idx.duplicated().any():
        return {"status": "bad", "gaps": [],
                "problems": [f"{int(idx.duplicated().sum())} duplicate timestamp(s)"]}
    expected = pd.date_range(lo, hi, freq="D")
    if skip_leap_dec31:
        expected = expected[~((expected.month == 12) & (expected.day == 31)
                              & expected.is_leap_year)]
    if len(expected) == 0:
        return {"status": "ok", "problems": [], "gaps": []}
    missing = expected.difference(idx)
    if len(missing) == 0:
        return {"status": "ok", "problems": [], "gaps": []}
    if idx.min() > expected[0]:
        return {"status": "bad", "gaps": [], "problems": [
            f"truncated: {len(idx)} of {len(expected)} expected days, first served {idx.min().date()}"]}
    if idx.max() < expected[-1] and not is_last:
        return {"status": "bad", "gaps": [], "problems": [
            f"truncated: {len(idx)} of {len(expected)} expected days, last served {idx.max().date()}"]}
    inner = missing[missing < idx.max()]
    if len(inner) == 0:
        return {"status": "ok", "problems": [], "gaps": []}
    gaps = _runs(list(inner), [ABSENT] * len(inner))
    return {"status": "gappy", "gaps": gaps,
            "problems": [f"{len(inner)} internal day(s) absent"]}


def chunk_hints(chunk_info: dict[str, Any]) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """From a backend's ``aihydro_chunk_validation`` record: day ranges of chunks
    that were dropped (``chunk_mismatch``) and per-range reason hints for gaps in
    chunks that were kept (``absent_after_retry``)."""
    dropped = [(f["start"], f["end"]) for f in chunk_info.get("failures", [])]
    hints = [dict(g) for g in chunk_info.get("retained_gaps", [])]
    return dropped, hints
