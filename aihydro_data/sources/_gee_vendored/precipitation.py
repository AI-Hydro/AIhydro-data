"""Strict integration of IMERG half-hourly rates into complete UTC days."""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

CONTRACT = "imerg_half_hourly_rate_to_daily_v1"


def daily_window(start: str, end: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Inclusive ISO calendar dates; return a UTC half-open interval."""
    for value in (start, end):
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("IMERG daily requests require YYYY-MM-DD dates")
    first = pd.Timestamp(start, tz="UTC")
    stop = pd.Timestamp(date.fromisoformat(end) + timedelta(days=1), tz="UTC")
    if stop <= first:
        raise ValueError("IMERG daily end must be on or after start")
    return first, stop


def integrate_daily(rows: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    """Reject incomplete temporal support, never infer missing rain as zero."""
    first, stop = daily_window(start, end)
    expected = pd.date_range(first, stop, freq="30min", inclusive="left")
    observed: dict[pd.Timestamp, tuple[float, str]] = {}
    for row in rows:
        stamp = row.get("time_start_ms")
        value = row.get("value")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise ValueError("IMERG requires finite system:time_start epoch milliseconds")
        instant = pd.to_datetime(stamp, unit="ms", utc=True)
        if instant < first or instant >= stop or instant != instant.floor("30min"):
            raise ValueError(f"IMERG timestamp outside requested half-hour grid: {instant}")
        if instant in observed:
            raise ValueError(f"IMERG duplicate interval: {instant}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"IMERG invalid or masked precipitation rate at {instant}")
        status = row.get("status")
        observed[instant] = (float(value), status if isinstance(status, str) and status else "unknown")
    missing = expected.difference(pd.DatetimeIndex(list(observed), tz="UTC"))
    if len(missing):
        raise ValueError(f"IMERG incomplete temporal coverage: {len(missing)} missing half-hours; first {missing[0]}")
    output = []
    for day in pd.date_range(first, stop, freq="D", inclusive="left"):
        values = [observed[day + pd.Timedelta(minutes=30 * n)] for n in range(48)]
        depth = math.fsum(rate * 0.5 for rate, _ in values)
        if not math.isfinite(depth):
            raise ValueError("IMERG integrated daily precipitation is not finite")
        output.append({
            "date": day.strftime("%Y-%m-%d"), "value": depth,
            "interval_count": 48, "expected_interval_count": 48,
            "source_status": ",".join(sorted({status for _, status in values})),
        })
    return output
