"""
Core dataclasses that flow through the entire library.

These three types are the contract between users/agents and the library;
everything else (products, sources, routing, MCP tools, help system) builds
on top of them.

Design note: ProductSpec is intentionally a Pydantic model rather than a
plain dataclass. Pydantic gives us free JSON-schema generation (which
becomes the MCP tool param schema), validation at construction time, and
clean serialisation for the structured help/discovery responses agents
read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Coverage tags. CONUS is special-cased because so many existing data
# products are CONUS-locked. Continental tags follow MERIT-Hydro Pfafstetter
# level-2 conventions where possible.
CoverageTag = Literal[
    "global",
    "CONUS",
    "NORTH_AMERICA",
    "SOUTH_AMERICA",
    "EUROPE",
    "AFRICA",
    "ASIA",
    "S_ASIA",
    "OCEANIA",
    "ANTARCTICA",
]

# Backend identifiers. Each one corresponds to a module in sources/.
SourceId = Literal[
    "gee", "stac", "hyriver", "direct_api", "local_cache",
    "cds_glofas", "geoglows_retro", "openmeteo_flood", "pygeoglim",
]

# Aggregation modes a user can request.
AggregationMode = Literal[
    "basin_mean",   # spatial mean over the geometry → 1-D time series
    "basin_sum",    # spatial sum (e.g. precip → catchment volume)
    "centroid",     # extract at the geometry's representative point
    "raw_raster",   # return the full clipped raster as xarray.DataArray
]

# What a product's values actually represent spatially. "areal" products can
# honour basin_mean/basin_sum reductions; the rest return a single-location
# series no matter what geometry is supplied:
#   point        — value at one grid cell / coordinate (e.g. Open-Meteo centroid)
#   reach        — modelled discharge for one river reach (GEOGLOWS, GloFAS)
#   gauge_point  — observed value at a physical gauge (NWIS)
SpatialSupport = Literal["areal", "point", "reach", "gauge_point"]


class ProductSpec(BaseModel):
    """
    A single data product (e.g. CHIRPS, GridMET, MOD16). One ProductSpec
    is the full programmatic record of what a product offers — agents and
    docs both read it.

    NOTE: every field is required-with-defaults rather than optional, so
    `data_describe_product()` always returns a uniform shape regardless of
    which backend the product came from. Backends fill what they know.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    id: str = Field(..., description="Stable unique key, e.g. 'CHIRPS'.")
    variable: str = Field(..., description="Canonical variable name, e.g. 'precipitation'.")
    source: SourceId = Field(..., description="Which backend serves this product.")
    source_dataset_id: str = Field("", description="Backend-specific dataset key (e.g. GEE asset ID).")

    # Capabilities
    coverage: list[CoverageTag] = Field(default_factory=list, description="Regions this product covers.")
    temporal_start: str = Field("", description="ISO-8601 earliest available date (empty = unknown/static).")
    temporal_end: str = Field("", description="ISO-8601 latest date or 'present'.")
    resolution_m: int = Field(0, description="Native spatial resolution in metres (0 = vector/non-grid).")
    timestep: str = Field("", description="Native cadence: 'daily', 'hourly', 'monthly', 'static', ...")
    units: str = Field("", description="Native units, e.g. 'mm/day', 'K', 'm3/s'.")
    spatial_support: SpatialSupport = Field(
        "areal",
        description=(
            "What the values represent spatially. 'areal' products honour "
            "basin_mean/basin_sum over the geometry; 'point'/'reach'/'gauge_point' "
            "return a single-location series regardless of the geometry, so "
            "basin_sum is rejected and basin_mean is reported as a point value."
        ),
    )
    allow_empty: bool = Field(
        False,
        description=(
            "If True, an empty/all-NaN result is accepted instead of triggering "
            "fallback to the next product. Leave False for observational series; "
            "set True only where 'no data' is a valid scientific answer."
        ),
    )

    # Provenance
    license: str = Field("", description="Plain-English licence summary.")
    citation: str = Field("", description="Human-readable citation string.")
    bibtex: str = Field("", description="BibTeX entry — wires into aihydro citations ledger.")
    homepage: str = Field("", description="Canonical product homepage URL.")

    # Install / runtime requirements
    requires_extras: list[str] = Field(default_factory=list, description="pyproject extras needed, e.g. ['gee'].")
    requires_auth: list[str] = Field(default_factory=list, description="Auth flows needed, e.g. ['gee'].")

    # Agent-facing affordances
    common_pitfalls: list[str] = Field(default_factory=list, description="Known gotchas worth surfacing.")
    examples: list[str] = Field(default_factory=list, description="Copy-pasteable call snippets.")
    next_steps: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Natural follow-on tools after this product is fetched. "
            "Each entry: {'tool': 'compute_signatures', 'rationale': '...'}."
        ),
    )

    # Free-form per-backend config (validated by the backend, not here)
    backend_config: dict[str, Any] = Field(default_factory=dict)


class FetchRequest(BaseModel):
    """A normalised fetch request. fetch() builds one from user kwargs and
    hands it to the router → source → backend pipeline."""

    model_config = ConfigDict(extra="forbid")

    variable: str
    geometry: Any  # accepts: GDF, GeoJSON dict, shapely, (lat,lon) tuple, bbox tuple
    start: str
    end: str
    mode: Literal["auto", "manual"] = "auto"
    product: Optional[str] = None  # required if mode="manual"; ignored if mode="auto"
    fallback: Optional[list[str]] = None  # explicit fallback chain; None = use policy default
    aggregation: AggregationMode = "basin_mean"
    cache: bool = True

    # Optional routing/snapping overrides
    region: Optional[str] = None       # skip auto region detection (a CoverageTag)
    outlet: Optional[tuple[float, float]] = None  # (lat, lon) snap target for
    #                                              reach/gauge backends

    # Internal — filled in by the router, not the user
    detected_region: Optional[str] = None
    resolved_product: Optional[str] = None


class FetchResult(BaseModel):
    """Successful fetch result. Always carries provenance + agent-facing
    next-step hints."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Identity / provenance
    variable: str
    product: str
    source: SourceId
    request: FetchRequest
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    cache_key: str = ""
    cache_hit: bool = False

    # The actual data — pd.DataFrame for time series, xr.DataArray for rasters
    data: Any

    # Provenance for the citations ledger
    license: str = ""
    citation: str = ""
    bibtex: str = ""

    # Spatial-support honesty. `spatial_support` mirrors the served product's
    # declared support; `aggregation_actual` records what the value really is
    # — e.g. a basin_mean request served by a point backend reports
    # "point_value" here (NOT an areal average), so downstream code and write-ups
    # never mistake a single-cell series for a catchment aggregate.
    spatial_support: str = "areal"
    aggregation_actual: str = ""

    # Agent-facing affordances
    next_steps: list[dict[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # Decision trail — mirrors delineation/router.py's fallback_history. Each
    # entry records one candidate the router considered, in order:
    #   {"product": str, "source": str, "outcome": "served"|"failed"|"rejected",
    #    "reason": str}
    # The final entry's outcome is always "served" (the product that won). This
    # lets agents see *why* a particular backend was chosen, not just which one.
    fallback_history: list[dict[str, str]] = Field(default_factory=list)

    def help(self) -> str:
        """REPL convenience: return a short usage summary for this result."""
        return (
            f"FetchResult(variable={self.variable!r}, product={self.product!r}, "
            f"source={self.source!r}, cache_hit={self.cache_hit})\n"
            f"  data: {type(self.data).__name__}\n"
            f"  citation: {self.citation[:80]}{'...' if len(self.citation) > 80 else ''}\n"
            f"  next_steps: {[s['tool'] for s in self.next_steps]}"
        )

    def plot(self, *, ax: Any = None, **kwargs: Any) -> Any:
        """
        Auto-dispatched plot of the result.

        Time series → line/bar plot; raster → imshow with colorbar.
        Returns a matplotlib Axes (or Figure for multi-panel). Lazy-imports
        the viz layer — requires ``pip install aihydro-data[viz]``.

        Example:
            r = fetch("precipitation", gdf, "2020-01-01", "2020-12-31")
            r.plot()                      # auto: time series bar chart
            r.plot(logy=True)             # streamflow with log y-axis
        """
        from aihydro_data.viz import auto_plot
        return auto_plot(self, ax=ax, **kwargs)

    def map(self, **kwargs: Any) -> Any:
        """
        Interactive folium map preview of the result's geometry + data overlay.

        Returns a folium.Map object. Renders inline in Jupyter automatically.
        Requires ``pip install aihydro-data[viz]``.
        """
        from aihydro_data.viz import map_preview
        return map_preview(self, **kwargs)


class FetchError(BaseModel):
    """
    Mirror of exceptions.AihydroDataError as a Pydantic model — returned
    when fetch() is called via the MCP tool surface (which serialises
    everything to JSON). Pure-Python callers get raised exceptions
    instead.
    """
    error: bool = True
    code: str
    message: str
    recovery: str = ""
    next_tools: list[str] = Field(default_factory=list)
    docs_anchor: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
