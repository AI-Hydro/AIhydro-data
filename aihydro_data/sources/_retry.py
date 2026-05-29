"""
Transient-retry helper for backend fetches.

Many of our upstreams (HyRiver THREDDS, Daymet ORNL, GEE) have flaky 5xx
spikes lasting <1 minute. Without retry, a single 504/500/connection-reset
breaks the user's call even though the next attempt would succeed.

Usage:
    from aihydro_data.sources._retry import retryable

    @retryable(attempts=3, base_delay=1.5)
    def _fetch(...):
        ...

Or imperatively:

    result = call_with_retry(lambda: pygridmet.get_bycoords(...))

Retries are only attempted for *transient* errors — connection timeouts,
5xx responses, and a small allowlist of HyRiver/asyncio error classes.
Permission errors, 4xx, and our own AihydroDataError subclasses are
re-raised immediately so we don't waste a backoff on something that won't
get better.
"""
from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Env-var kill switch — tests and CI set this to 1 so the offline suite
# doesn't burn minutes retrying real network failures.
_NO_RETRY_ENV = "AIHYDRO_DATA_NO_RETRY"


def _retries_disabled() -> bool:
    return os.environ.get(_NO_RETRY_ENV, "").lower() in ("1", "true", "yes")

# Substrings that mark a transient upstream failure worth retrying.
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "broken pipe",
    "500",
    "502",
    "503",
    "504",
    "temporarily unavailable",
    "remote disconnected",
    "ssl error",
)


def _is_transient(exc: BaseException) -> bool:
    """Heuristic: does this exception look like a recoverable upstream blip?"""
    # Never retry our own structured errors (they're already deterministic
    # diagnostics — auth missing, region unsupported, …).
    try:
        from aihydro_data.exceptions import AihydroDataError
        if isinstance(exc, AihydroDataError):
            return False
    except Exception:
        pass

    name = type(exc).__name__.lower()
    if name in (
        "connectiontimeouterror",
        "connectionerror",
        "serviceerror",
        "timeouterror",
        "readtimeout",
        "connecterror",
        "remotedisconnected",
        "incompleteread",
    ):
        return True

    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def call_with_retry(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.5,
    max_delay: float = 30.0,
    label: str = "",
) -> T:
    """
    Call `func()` up to `attempts` times, sleeping `base_delay * 2**i`
    between failures (clamped to `max_delay`). Re-raises the last exception
    if all attempts fail.

    Only retries when `_is_transient(exc)` says the failure is upstream-blippy.
    Permanent failures (4xx, AuthRequired, …) re-raise on first miss.
    """
    if _retries_disabled():
        attempts = 1
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if not _is_transient(exc):
                raise
            if i == attempts - 1:
                break
            delay = min(base_delay * (2 ** i), max_delay)
            log.warning(
                "Transient error in %s (attempt %d/%d): %s — retrying in %.1fs",
                label or "backend", i + 1, attempts, exc, delay,
            )
            time.sleep(delay)
    # Exhausted retries — re-raise the last transient error so the caller
    # / fallback chain can react.
    assert last_exc is not None
    raise last_exc


def retryable(
    *, attempts: int = 3, base_delay: float = 1.5, max_delay: float = 30.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator form of call_with_retry — wraps a method/function."""
    def _decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> T:
            return call_with_retry(
                lambda: fn(*args, **kwargs),
                attempts=attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                label=fn.__qualname__,
            )
        return _wrapped
    return _decorator
