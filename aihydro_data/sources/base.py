"""
Abstract base class every backend in sources/ inherits from.

Phase 1 deliverable: just the ABC + dispatch helper. Concrete
implementations (gee.py, stac.py, hyriver.py, direct_api.py) land in
Phase 2 onwards.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from aihydro_data.contracts import AggregationMode, ProductSpec


class SourceBackend(ABC):
    """One concrete subclass per backend module (GEEBackend, STACBackend,
    HyRiverBackend, DirectAPIBackend, LocalCacheBackend)."""

    source_id: str = ""  # must be set on each subclass; matches ProductSpec.source

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Return: {'variables': [...], 'coverage': [...], 'requires_auth': [...]}."""

    @abstractmethod
    def is_available(self, spec: Optional[ProductSpec] = None) -> tuple[bool, Optional[str]]:
        """(True, None) if backend is usable right now, else (False, reason).

        `spec` (optional) narrows the check to what THAT product needs —
        e.g. the HyRiver backend hosts four independent libraries, and a
        GridMET product only requires pygridmet. ``spec=None`` answers the
        broader "is any part of this backend usable?" (doctor probes).
        Subclasses that don't differentiate may ignore the argument; callers
        must tolerate implementations that omit it (use signature filtering).

        `reason` becomes the `message` field of an AuthRequired/SourceUnavailable
        error so the agent can act on it."""

    @abstractmethod
    def fetch_timeseries(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
        aggregation: AggregationMode,
    ) -> Any:  # returns pd.DataFrame; typed Any to avoid forcing pandas import here
        """Time-series fetch — agg over geometry, return tabular data."""

    @abstractmethod
    def fetch_raster(
        self,
        spec: ProductSpec,
        geometry: Any,
        start: str,
        end: str,
    ) -> Any:  # returns xarray.DataArray
        """Raster fetch — return the clipped grid (no temporal aggregation)."""


    def _assert_available(self, spec: Optional[ProductSpec] = None) -> None:
        """Raise SourceUnavailable if this backend (or the product's dep) is not usable.

        Backends with special credential handling (e.g. cds_glofas) override this.
        """
        from aihydro_data.sources._common import assert_backend_available
        assert_backend_available(self, spec)


_BACKEND_INSTANCES: dict[str, SourceBackend] = {}


def get_backend(source_id: str) -> SourceBackend:
    """Lazy-load + cache the backend module/class matching `source_id`.
    Lazy because each backend may need optional deps that the user hasn't
    installed; we only error when someone actually tries to use it."""
    if source_id in _BACKEND_INSTANCES:
        return _BACKEND_INSTANCES[source_id]

    import importlib
    try:
        mod = importlib.import_module(f"aihydro_data.sources.{source_id}")
    except ImportError as exc:
        from aihydro_data.exceptions import SourceUnavailable
        raise SourceUnavailable(
            code="BACKEND_NOT_INSTALLED",
            message=f"Backend {source_id!r} is not installed: {exc}",
            recovery=f"pip install aihydro-data[{source_id}]",
            next_tools=["data_list_products", "data_doctor"],
            docs_anchor="install",
        )

    # Convention: each backend module exposes a `Backend` class.
    cls = getattr(mod, "Backend", None)
    if cls is None:
        raise RuntimeError(
            f"Backend module {source_id!r} does not export a `Backend` class."
        )
    instance = cls()
    _BACKEND_INSTANCES[source_id] = instance
    return instance
