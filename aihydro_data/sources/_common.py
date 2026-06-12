"""Shared backend helpers — import-guard + availability boilerplate.

Backends repeat the same two patterns: "lazily import an optional library and
raise a structured SourceUnavailable if it's missing", and "assert this backend
is available before fetching". Centralising them keeps the error envelopes
consistent (same code/recovery/next_tools shape) across every backend.
"""
from __future__ import annotations

import importlib
from typing import Any


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
