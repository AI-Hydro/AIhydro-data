"""
Spatial / map previews — folium + matplotlib for rasters and geometries.

`map_preview(result)` returns a folium.Map with the geometry centred and
(if the result is a raster) a colourised overlay. Useful for sanity-checking
a fetch in notebooks: did we pull data from the right place?
"""
from __future__ import annotations

from typing import Any

from aihydro_data.viz._common import _result_data, _result_meta


def map_preview(
    result: Any,
    *,
    zoom_start: int | None = None,
    tiles: str = "OpenStreetMap",
    add_raster: bool = True,
) -> Any:
    """
    Render an interactive folium map for a fetched geometry / raster.

    Parameters
    ----------
    result : FetchResult | geometry | GeoDataFrame
        What to preview. If a raster (xr.DataArray), it is overlaid with
        a colormap.
    zoom_start : int, optional
        Initial zoom. Auto-fitted to geometry if omitted.
    tiles : str
        Folium tile layer name.
    add_raster : bool
        If True and result.data is a raster, overlay it.

    Returns
    -------
    folium.Map
    """
    try:
        import folium
    except ImportError as exc:
        raise ImportError(
            "folium is required for map_preview. "
            "Install with: pip install aihydro-data[viz]"
        ) from exc

    meta = _result_meta(result)

    # Extract geometry — try .request.geometry first, then top level
    geom = None
    if hasattr(result, "request") and result.request and hasattr(result.request, "geometry"):
        geom = result.request.geometry
    elif hasattr(result, "bounds"):
        geom = result
    elif hasattr(result, "geometry"):
        geom = result.geometry

    # Find a centre point
    centre = (0.0, 0.0)
    if geom is not None and hasattr(geom, "bounds") and geom.bounds is not None:
        minx, miny, maxx, maxy = geom.bounds
        centre = ((miny + maxy) / 2, (minx + maxx) / 2)
    elif hasattr(result, "data"):
        data = _result_data(result)
        try:
            import xarray as xr
            if isinstance(data, xr.DataArray):
                lat_dim = next((d for d in ("y", "lat", "latitude", "Y") if d in data.coords), None)
                lon_dim = next((d for d in ("x", "lon", "longitude", "X") if d in data.coords), None)
                if lat_dim and lon_dim:
                    centre = (float(data[lat_dim].mean()), float(data[lon_dim].mean()))
        except ImportError:
            pass

    m = folium.Map(location=centre, zoom_start=zoom_start or 6, tiles=tiles)

    # Plot geometry outline
    if geom is not None:
        try:
            from shapely.geometry import mapping
            if hasattr(geom, "geom_type") and geom.geom_type not in ("GaugeID",):
                folium.GeoJson(
                    mapping(geom),
                    name="geometry",
                    style_function=lambda x: {
                        "color": "#e31a1c", "weight": 2, "fillOpacity": 0.1,
                    },
                ).add_to(m)
                if zoom_start is None and hasattr(geom, "bounds"):
                    minx, miny, maxx, maxy = geom.bounds
                    m.fit_bounds([[miny, minx], [maxy, maxx]])
        except Exception:
            pass

    # Overlay raster if applicable
    if add_raster:
        _maybe_add_raster_overlay(m, result, meta)

    # Provenance footer
    if meta.get("citation"):
        folium.map.Marker(
            centre,
            icon=folium.DivIcon(html=(
                f'<div style="font-size:10px; background:rgba(255,255,255,0.85); '
                f'padding:2px 4px; border-radius:3px; max-width:300px;">'
                f'<b>{meta.get("product", "")}</b><br>'
                f'{meta.get("citation", "")[:120]}...</div>'
            )),
        ).add_to(m)

    folium.LayerControl().add_to(m)
    return m


def _maybe_add_raster_overlay(m, result, meta) -> None:
    """If result.data is a 2-D raster, overlay it as an ImageOverlay."""
    try:
        import xarray as xr
        data = _result_data(result)
        if not isinstance(data, xr.DataArray):
            return
        # Reduce time dim if present
        if "time" in data.dims:
            data = data.isel(time=0)
        lat_dim = next((d for d in ("y", "lat", "latitude", "Y") if d in data.coords), None)
        lon_dim = next((d for d in ("x", "lon", "longitude", "X") if d in data.coords), None)
        if not (lat_dim and lon_dim):
            return

        import matplotlib.pyplot as plt
        import matplotlib.cm as cm
        import numpy as np
        import folium

        from aihydro_data.viz.auto import _suggest_cmap

        arr = np.asarray(data.values, dtype=float)
        # Normalise to 0..1
        vmin = np.nanpercentile(arr, 2)
        vmax = np.nanpercentile(arr, 98)
        if vmax == vmin:
            return
        norm = np.clip((arr - vmin) / (vmax - vmin), 0, 1)

        cmap = cm.get_cmap(_suggest_cmap(meta.get("variable", "")))
        rgba = (cmap(norm) * 255).astype(np.uint8)

        lat = data[lat_dim].values
        lon = data[lon_dim].values
        bounds = [[float(lat.min()), float(lon.min())],
                  [float(lat.max()), float(lon.max())]]
        folium.raster_layers.ImageOverlay(
            image=rgba, bounds=bounds, opacity=0.65, name=meta.get("product", "raster"),
        ).add_to(m)
    except Exception:
        # Map preview is best-effort — silently skip if anything goes wrong
        pass
