"""
Batch geometry helpers.

Converts a variety of multi-geometry inputs into an ordered list of
(label, shapely_geometry) pairs so fetch_batch() can iterate over them.

Supported inputs:
    - geopandas.GeoDataFrame        → one entry per row (label = index / id column)
    - list of geometries            → coerce each with coerce_geometry
    - list of (label, geometry)     → explicit labelled pairs
    - dict[str, geometry]           → label = key
    - single geometry               → wrapped as [(0, geom)]

All geometries are coerced to WGS84 shapely via coerce_geometry so that
fetch_batch() receives a uniform input regardless of how the user passed things.
"""
from __future__ import annotations

from typing import Any, Iterator


def iter_geometries(input_geom: Any) -> Iterator[tuple[str, Any]]:
    """
    Yield (label, shapely_geom) pairs from a variety of multi-geometry inputs.

    Labels are strings (converted from whatever the source key/index is).
    Geometries are coerced via coerce_geometry.
    """
    from aihydro_data.geometry import coerce_geometry

    # ── dict[label, geom] ────────────────────────────────────────────────
    if isinstance(input_geom, dict):
        for k, v in input_geom.items():
            yield str(k), coerce_geometry(v)
        return

    # ── GeoDataFrame ─────────────────────────────────────────────────────
    if hasattr(input_geom, "iterrows") and hasattr(input_geom, "geometry"):
        # geopandas.GeoDataFrame
        for idx, row in input_geom.iterrows():
            yield str(idx), coerce_geometry(row.geometry)
        return

    # ── tuple / list ─────────────────────────────────────────────────────
    if isinstance(input_geom, (list, tuple)):
        if len(input_geom) == 0:
            return

        # Detect a single (lat, lon) or (minx, miny, maxx, maxy) coordinate tuple:
        # all elements are scalars and length is 2 or 4.
        if all(isinstance(v, (int, float)) for v in input_geom) and len(input_geom) in (2, 4):
            yield "0", coerce_geometry(input_geom)
            return

        first = input_geom[0]

        # list of (label, geom) pairs — first element is a 2-tuple whose first
        # item is a string/int label (not a float coordinate).
        if (
            isinstance(first, (list, tuple))
            and len(first) == 2
            and isinstance(first[0], (str, int))
            and not isinstance(first[0], float)
        ):
            for label, geom in input_geom:
                yield str(label), coerce_geometry(geom)
            return

        # list of bare geometries (each element is a geometry or coord-tuple)
        for i, geom in enumerate(input_geom):
            yield str(i), coerce_geometry(geom)
        return

    # ── single geometry (shapely / string gauge-id) → 1-element batch ────
    yield "0", coerce_geometry(input_geom)


def split_geodataframe(
    gdf: Any,
    *,
    id_column: str | None = None,
) -> list[tuple[str, Any]]:
    """
    Return (id, geom) pairs from a GeoDataFrame.

    If `id_column` is given, use it as the label; otherwise use the index.
    """
    pairs: list[tuple[str, Any]] = []
    for idx, row in gdf.iterrows():
        label = str(row[id_column]) if id_column and id_column in row else str(idx)
        pairs.append((label, row.geometry))
    return pairs
