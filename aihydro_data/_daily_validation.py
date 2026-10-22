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
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

import pandas as pd

CLIENT_CALENDAR = "client_calendar_assumption"
CHUNK_MISMATCH = "chunk_mismatch"
ABSENT = "absent_in_response"

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
    reasons = []
    for d in missing:
        if any(a <= d <= b for a, b in bad):
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


def year_chunks(start: str, end: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Split [start, end] at calendar-year boundaries."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    for y in range(s.year, e.year + 1):
        out.append((max(s, pd.Timestamp(y, 1, 1)), min(e, pd.Timestamp(y, 12, 31))))
    return out
