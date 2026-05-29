"""
Product registry — variable-centric.

Each module in this package (precipitation.py, temperature.py, ...) declares
a module-level PRODUCTS list of ProductSpec entries. This package's __init__
walks those modules at import time and builds the unified PRODUCT_REGISTRY.

Adding a new product = adding a new ProductSpec to the appropriate variable
module (or creating a new variable module). No edits to routing, fetch, or
this file required — discovery is fully declarative.
"""
from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Optional

from aihydro_data.contracts import CoverageTag, ProductSpec

# Populated lazily on first call so importing aihydro_data doesn't force
# imports of every variable module (some may have heavy backend deps).
_REGISTRY: dict[str, ProductSpec] = {}
_LOADED = False
# Lock the lazy load — fetch_batch() uses a ThreadPoolExecutor, and without
# this, two threads racing on `if _LOADED: return` will both populate the
# registry, tripping the duplicate-ID guard.
_REGISTRY_LOCK = threading.Lock()


def _load_registry() -> None:
    """Walk this package, import each variable module, harvest its PRODUCTS."""
    global _LOADED
    # Fast-path: most calls hit a hot _LOADED=True and return without taking
    # the lock. Only the first load (or a contended first load) goes inside.
    if _LOADED:
        return
    with _REGISTRY_LOCK:
        if _LOADED:   # double-checked locking — another thread won the race
            return
        pkg = importlib.import_module(__name__)
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            if mod_info.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"{__name__}.{mod_info.name}")
            except ImportError:
                # Missing optional backend deps — skip silently. The product
                # will simply not appear in list_products() until the user
                # installs the relevant extra.
                continue
            for spec in getattr(mod, "PRODUCTS", []):
                if not isinstance(spec, ProductSpec):
                    continue
                if spec.id in _REGISTRY:
                    # Duplicate IDs are a developer bug — fail loudly in tests.
                    raise ValueError(f"Duplicate ProductSpec id: {spec.id!r}")
                _REGISTRY[spec.id] = spec
        _LOADED = True


def list_products(
    variable: Optional[str] = None,
    region: Optional[CoverageTag] = None,
    source: Optional[str] = None,
) -> list[ProductSpec]:
    """
    Discovery: return ProductSpecs matching the given filters.

    All-None call returns the complete registry. Useful for the
    `data_list_products` MCP tool.
    """
    _load_registry()
    out = list(_REGISTRY.values())
    if variable:
        out = [p for p in out if p.variable == variable]
    if region:
        out = [p for p in out if region in p.coverage or "global" in p.coverage]
    if source:
        out = [p for p in out if p.source == source]
    return out


def get_product(product_id: str) -> ProductSpec:
    """Look up a single ProductSpec by id. Raises KeyError if missing."""
    _load_registry()
    if product_id not in _REGISTRY:
        raise KeyError(
            f"No product registered with id={product_id!r}. "
            f"Call list_products() to discover available products."
        )
    return _REGISTRY[product_id]
