"""
Typed exceptions for aihydro-data.

Every exception carries a `to_dict()` method that produces the structured
error envelope agents consume:

    {"error": True,
     "code": "GEE_AUTH_MISSING",
     "message": "...",
     "recovery": "...",          # what the agent/user should do next
     "next_tools": [...],        # MCP tool names to try
     "docs_anchor": "auth#gee"}  # link into bundled help_topics/

See the Agent-friendly contracts section of the Wave 2 plan
(/Users/mgalib/.claude/plans/i-want-you-to-polymorphic-axolotl.md) for the
design rationale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AihydroDataError(Exception):
    """Base class — never raise directly; raise one of the subclasses."""
    code: str = "UNKNOWN"
    message: str = ""
    recovery: str = ""
    next_tools: list[str] = field(default_factory=list)
    docs_anchor: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        d = {
            "error": True,
            "code": self.code,
            "message": self.message,
            "recovery": self.recovery,
            "next_tools": list(self.next_tools),
            "docs_anchor": self.docs_anchor,
        }
        if self.details:
            d["details"] = self.details
        return d


class SourceUnavailable(AihydroDataError):
    """A backend (GEE/STAC/HyRiver) refused to serve a request that the
    router thought it could handle. Always surfaces a recovery hint that
    points at a fallback product."""


class RegionUnsupported(AihydroDataError):
    """No product in the registry covers the requested (variable, region)
    combination. Recovery typically suggests `data_list_products()` to
    discover what IS supported, or a manual product override."""


class AuthRequired(AihydroDataError):
    """A backend needs credentials the user hasn't provided. Recovery
    points at the matching `aihydro-data auth <backend>` CLI flow or the
    MCP `data_auth_status` tool."""


class DateOutOfRange(AihydroDataError):
    """Requested time window falls outside the product's temporal
    coverage. `details` carries the product's actual (start, end)."""


class GeometryInvalid(AihydroDataError):
    """The supplied geometry could not be coerced to a usable shapely
    object, or it has zero area / null CRS / etc."""


class AggregationUnsupported(AihydroDataError):
    """The requested aggregation is spatially meaningless for the product's
    support — e.g. ``basin_sum`` against a point/reach product (Open-Meteo,
    GEOGLOWS, NWIS): a single-location value cannot be summed over a basin.
    Raised inside the fallback walk so an areal product can serve instead."""


class FetchTooLarge(AihydroDataError):
    """Estimated request volume exceeds the per-call ceiling. Recovery
    suggests splitting into batches via `data_fetch` with `aggregation=
    "basin_mean"` or chunked time ranges."""
