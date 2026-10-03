"""Shared backend helpers — import-guard + availability boilerplate.

Backends repeat the same two patterns: "lazily import an optional library and
raise a structured SourceUnavailable if it's missing", and "assert this backend
is available before fetching". Centralising them keeps the error envelopes
consistent (same code/recovery/next_tools shape) across every backend.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

log = logging.getLogger(__name__)


#: Attribute under which a backend reports the unit its payload declared for the
#: values it returned (``DataFrame.attrs`` / ``DataArray.attrs``). The pipeline
#: records it as the result's ``units``.
DECLARED_UNITS_ATTR = "aihydro_units"
#: Longest provider-declared unit string accepted (real units are well under 30).
MAX_DECLARED_UNITS_LEN = 64


def declare_units(obj: Any, declared: Any) -> Any:
    """Record the provider-declared unit of ``obj``'s values; no-op for an empty one.

    Call only with a unit the payload itself carries, and only when the values
    returned are in that unit (a backend that converts must not declare the
    pre-conversion unit).

    The string comes from a remote provider, so it is checked on the way in. It
    is dropped, as if the payload declared nothing (debug-logged), when it is
    empty, one of ``none`` / ``nan`` / ``unknown`` (any case), longer than
    ``MAX_DECLARED_UNITS_LEN`` characters, contains a non-printable character
    (control characters, ESC, newlines), or looks like a path or URL (leading
    ``/`` or ``~``, ``..``, a backslash, ``://``).
    """
    text = str(declared).strip() if declared is not None else ""
    if not text or text.lower() in {"none", "nan", "unknown"}:
        return obj
    reason = None
    if len(text) > MAX_DECLARED_UNITS_LEN:
        reason = f"longer than {MAX_DECLARED_UNITS_LEN} characters"
    elif not text.isprintable():
        reason = "contains non-printable characters"
    elif text[0] in "/~" or ".." in text or "\\" in text or "://" in text:
        reason = "looks like a path or URL"
    if reason:
        log.debug("declared unit rejected (%s); treated as not declared: %r", reason, text[:80])
        return obj
    obj.attrs[DECLARED_UNITS_ATTR] = text
    return obj


def payload_units(attrs: Any) -> str:
    """The unit named by a payload attribute mapping (``units`` / ``unit``), or ""."""
    try:
        for key in ("units", "unit", "Units"):
            v = attrs.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    except Exception:
        pass
    return ""


def require_import(module: str, *, extra: str, backend: str = "") -> Any:
    """Import `module`, or raise SourceUnavailable pointing at the pip extra.

    Replaces the per-backend try/except ImportError → SourceUnavailable blocks.

        pygridmet = require_import("pygridmet", extra="hyriver")
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        from aihydro_data.exceptions import SourceUnavailable
        who = f"{backend} backend" if backend else f"{module!r}"
        raise SourceUnavailable(
            code=f"{(backend or module).upper().replace('-', '_')}_NOT_INSTALLED",
            message=f"{who} needs {module!r}, which is not installed ({exc}).",
            recovery=f"pip install aihydro-data[{extra}]",
            next_tools=["data_doctor"],
            docs_anchor="install",
        ) from exc


def assert_backend_available(backend: Any, spec: Any = None) -> None:
    """Call ``backend.is_available(spec)`` and raise SourceUnavailable if not.

    A default ``_assert_available`` for backends that don't need custom auth
    handling (GEE overrides this with an AuthRequired + EE-connect path).
    Tolerates ``is_available`` implementations that don't accept ``spec``.
    """
    try:
        ok, reason = backend.is_available(spec)
    except TypeError:
        ok, reason = backend.is_available()
    if not ok:
        from aihydro_data.exceptions import SourceUnavailable
        src = getattr(backend, "source_id", "backend")
        raise SourceUnavailable(
            code=f"{src.upper()}_UNAVAILABLE",
            message=reason or f"{src} backend is not available.",
            recovery=f"pip install aihydro-data[{src}]",
            next_tools=["data_doctor"],
            docs_anchor="install",
        )
