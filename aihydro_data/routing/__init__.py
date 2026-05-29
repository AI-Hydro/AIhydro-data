"""
Region detection + product policy.

Public surface:
    detect_region(geometry)         → str (CoverageTag)
    resolve_product_ids(var, region) → list[str]
    resolve_product(req)            → ProductSpec  (fills req.resolved_product)
"""
from __future__ import annotations

from aihydro_data.routing.detect import detect_region
from aihydro_data.routing.policy import resolve_product_ids

__all__ = ["detect_region", "resolve_product_ids", "resolve_product"]


def resolve_product(req: "aihydro_data.contracts.FetchRequest") -> "aihydro_data.contracts.ProductSpec":
    """
    Given a normalised FetchRequest, return the best ProductSpec.

    - auto mode: detect region → walk policy → return first registered product
    - manual mode: look up req.product directly in the registry

    Raises:
        aihydro_data.exceptions.RegionUnsupported – no policy entry for (variable, region)
        KeyError – product_id not in registry (manual mode)
    """
    from aihydro_data.contracts import FetchRequest
    from aihydro_data.products import get_product, list_products
    from aihydro_data.exceptions import RegionUnsupported

    if req.mode == "manual" and req.product:
        return get_product(req.product)

    # Auto: detect region from the coerced geometry
    from aihydro_data.geometry import coerce_geometry
    geom = coerce_geometry(req.geometry)
    region = detect_region(geom)

    candidates = resolve_product_ids(req.variable, region)
    if not candidates:
        # Try global as last resort
        candidates = resolve_product_ids(req.variable, "global")
    if not candidates:
        raise RegionUnsupported(
            code="REGION_NO_POLICY",
            message=(
                f"No routing policy for variable={req.variable!r}, region={region!r}. "
                f"Call list_products(variable={req.variable!r}) to see what's available."
            ),
            recovery="Try mode='manual' with a specific product id, or choose a supported variable.",
            next_tools=["data_list_products"],
            docs_anchor="routing",
        )

    # Walk the candidate list and return the first one registered in the registry.
    # (Some products may be absent if the user hasn't installed the required extra.)
    registered = {p.id: p for p in list_products(variable=req.variable)}
    for pid in candidates:
        if pid in registered:
            return registered[pid]

    raise RegionUnsupported(
        code="REGION_NO_INSTALLED_PRODUCT",
        message=(
            f"Policy candidates {candidates} for ({req.variable!r}, {region!r}) "
            f"are not installed. Install the required extras."
        ),
        recovery=(
            "Run `pip install aihydro-data[gee]` and/or `pip install aihydro-data[hyriver]`. "
            "Then call list_products() again."
        ),
        next_tools=["data_list_products", "data_doctor"],
        docs_anchor="install",
    )
