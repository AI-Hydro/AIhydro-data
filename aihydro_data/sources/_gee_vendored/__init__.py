"""
GEE modules vendored from aihydro-tools/ai_hydro/gee/ (v1.7.0).

Phase 1 of the aihydro-data restructure: copy-paste preservation so the
existing OAuth flow, DatasetPreset schema, presets, and timeseries
reducers stay available to Phase 2's `sources/gee.py` wrapper without
forcing a same-day refactor.

Phase 2 will lift these into the new SourceBackend ABC and convert each
DatasetPreset into a ProductSpec registered under products/. Phase 2 may
delete this vendored copy once the integration is verified.

DO NOT edit files under this directory during Phase 2 — they're a reference
snapshot of v1.7.0. Make changes in sources/gee.py or products/ instead.
"""

__all__ = [
    "auth",
    "contracts",
    "map_layers",
    "presets",
    "timeseries",
]
