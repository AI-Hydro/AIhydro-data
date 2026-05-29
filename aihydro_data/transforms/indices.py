"""
Spectral indices — numpy-native port of torchgeo.transforms.indices.

All ten formulas operate on ``xr.DataArray`` inputs (each input is a single
band) and return an ``xr.DataArray`` carrying full provenance via ``.attrs``:

    {"index": "NDWI",
     "formula": "(green - nir) / (green + nir)",
     "citation": "McFeeters 1996",
     "range": (-1, 1),
     "colormap": "Blues",
     "use_case": "surface water mapping",
     "threshold_hint": 0.3}

The convenience wrapper ``compute_index(name, ds, band_map=None)`` accepts an
``xr.Dataset`` with a ``sensor`` attr or an explicit ``band_map`` and
auto-resolves the required bands.

Design notes
~~~~~~~~~~~~
- No torch / kornia / lightning dependency — numpy + xarray only.
- Eps guard (1e-10 by default) on every denominator to avoid `nan` on
  zero-sum pixels (occurs over deep shadow / no-data).
- Output dtype follows the input dtype family — float32 in, float32 out.
- Cloud masking is auto-applied for known optical sensors via
  ``aihydro_data.transforms.cloud_mask.mask_clouds`` unless the caller
  passes ``mask_clouds=False``.

References
~~~~~~~~~~
- https://www.indexdatabase.de/db/i.php
- https://github.com/awesome-spectral-indices/awesome-spectral-indices
- torchgeo/transforms/indices.py (MIT-licensed; formulas only)
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

import numpy as np
import xarray as xr

log = logging.getLogger(__name__)

_EPSILON = 1e-10

# ---------------------------------------------------------------------------
# Sensor → band-name lookup tables.  Sensor name is matched case-insensitive
# against ``ds.attrs.get('sensor')`` or against the `sensor` kwarg.  Each map
# uses the canonical band names AIH-Hydro speaks (blue, green, red, re1, nir,
# swir1, swir2) keyed to the sensor's native band identifiers.
# ---------------------------------------------------------------------------
SENSOR_BAND_MAPS: dict[str, dict[str, str]] = {
    "sentinel2": {
        "blue":  "B2",
        "green": "B3",
        "red":   "B4",
        "re1":   "B5",
        "re2":   "B6",
        "re3":   "B7",
        "nir":   "B8",
        "nir2":  "B8A",
        "swir1": "B11",
        "swir2": "B12",
        "qa":    "SCL",
    },
    "landsat8": {
        "blue":  "SR_B2",
        "green": "SR_B3",
        "red":   "SR_B4",
        "nir":   "SR_B5",
        "swir1": "SR_B6",
        "swir2": "SR_B7",
        "qa":    "QA_PIXEL",
    },
    "landsat9": {
        "blue":  "SR_B2",
        "green": "SR_B3",
        "red":   "SR_B4",
        "nir":   "SR_B5",
        "swir1": "SR_B6",
        "swir2": "SR_B7",
        "qa":    "QA_PIXEL",
    },
    "landsat7": {
        "blue":  "SR_B1",
        "green": "SR_B2",
        "red":   "SR_B3",
        "nir":   "SR_B4",
        "swir1": "SR_B5",
        "swir2": "SR_B7",
        "qa":    "QA_PIXEL",
    },
    "modis_mod09": {
        "red":   "sur_refl_b01",
        "nir":   "sur_refl_b02",
        "blue":  "sur_refl_b03",
        "green": "sur_refl_b04",
        "swir1": "sur_refl_b06",
        "swir2": "sur_refl_b07",
        "qa":    "state_1km",
    },
}


def _resolve_band(ds_or_da: xr.Dataset | xr.DataArray, canonical: str,
                  sensor: str | None = None,
                  band_map: Mapping[str, str] | None = None) -> xr.DataArray:
    """Pull a single named band out of *ds_or_da*.

    Lookup order:
      1. Explicit ``band_map`` (e.g. ``{"green": "B03"}``) — caller wins.
      2. ``SENSOR_BAND_MAPS[sensor]`` (case-insensitive).
      3. Canonical name itself — if the Dataset already uses ``green``/
         ``nir``/etc., we use those directly.
      4. The canonical name uppercased.
    """
    # If the caller passed a single-band DataArray, just return it (assume
    # they know which band it is).
    if isinstance(ds_or_da, xr.DataArray):
        return ds_or_da

    candidates: list[str] = []
    if band_map and canonical in band_map:
        candidates.append(band_map[canonical])
    if sensor:
        sm = SENSOR_BAND_MAPS.get(sensor.lower())
        if sm and canonical in sm:
            candidates.append(sm[canonical])
    candidates.extend([canonical, canonical.upper()])

    for name in candidates:
        if name in ds_or_da.data_vars:
            return ds_or_da[name]
    raise KeyError(
        f"Could not resolve band '{canonical}' (sensor={sensor!r}). "
        f"Tried: {candidates}. Available data_vars: {list(ds_or_da.data_vars)}"
    )


def _ndi(a: xr.DataArray, b: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Generic normalized difference index: (a - b) / (a + b)."""
    # Promote to float to avoid integer-division surprises on raw uint16 DNs.
    a_f = a.astype("float32")
    b_f = b.astype("float32")
    return (a_f - b_f) / (a_f + b_f + eps)


def _attach_attrs(da: xr.DataArray, **attrs: Any) -> xr.DataArray:
    """Set provenance attrs on the result in-place."""
    da.attrs.update(attrs)
    return da


# ---------------------------------------------------------------------------
# The ten indices
# ---------------------------------------------------------------------------

def ndvi(red: xr.DataArray, nir: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Normalized Difference Vegetation Index — Rouse et al. 1974.

    NDVI = (NIR - Red) / (NIR + Red)
    """
    return _attach_attrs(
        _ndi(nir, red, eps),
        index="NDVI",
        formula="(nir - red) / (nir + red)",
        citation="Rouse et al. 1974",
        range=(-1.0, 1.0),
        colormap="RdYlGn",
        use_case="vegetation health",
        threshold_hint=0.4,
    )


def ndwi(green: xr.DataArray, nir: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Normalized Difference Water Index — McFeeters 1996.

    NDWI = (Green - NIR) / (Green + NIR).  Water typically > 0.3.
    """
    return _attach_attrs(
        _ndi(green, nir, eps),
        index="NDWI",
        formula="(green - nir) / (green + nir)",
        citation="McFeeters 1996",
        range=(-1.0, 1.0),
        colormap="Blues",
        use_case="surface water mapping",
        threshold_hint=0.3,
    )


def mndwi(green: xr.DataArray, swir1: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Modified NDWI — Xu 2006. Uses SWIR1 instead of NIR; better at separating
    water from built-up surfaces.

    MNDWI = (Green - SWIR1) / (Green + SWIR1)
    """
    return _attach_attrs(
        _ndi(green, swir1, eps),
        index="MNDWI",
        formula="(green - swir1) / (green + swir1)",
        citation="Xu 2006",
        range=(-1.0, 1.0),
        colormap="Blues",
        use_case="surface water (urban areas)",
        threshold_hint=0.2,
    )


def ndbi(swir1: xr.DataArray, nir: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Normalized Difference Built-up Index — Zha et al. 2003.

    NDBI = (SWIR1 - NIR) / (SWIR1 + NIR).  Positive over impervious surfaces.
    """
    return _attach_attrs(
        _ndi(swir1, nir, eps),
        index="NDBI",
        formula="(swir1 - nir) / (swir1 + nir)",
        citation="Zha et al. 2003",
        range=(-1.0, 1.0),
        colormap="Reds",
        use_case="built-up / impervious surface",
        threshold_hint=0.0,
    )


def nbr(nir: xr.DataArray, swir2: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Normalized Burn Ratio — Key & Benson 2006.

    NBR = (NIR - SWIR2) / (NIR + SWIR2).  Drops sharply after fire; difference
    (dNBR) over time quantifies burn severity.
    """
    return _attach_attrs(
        _ndi(nir, swir2, eps),
        index="NBR",
        formula="(nir - swir2) / (nir + swir2)",
        citation="Key & Benson 2006",
        range=(-1.0, 1.0),
        colormap="RdYlGn_r",
        use_case="burn severity",
        threshold_hint=0.1,
    )


def ndsi(green: xr.DataArray, swir1: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Normalized Difference Snow Index — Hall et al. 1995.

    NDSI = (Green - SWIR1) / (Green + SWIR1).  Snow > ~0.4.
    """
    return _attach_attrs(
        _ndi(green, swir1, eps),
        index="NDSI",
        formula="(green - swir1) / (green + swir1)",
        citation="Hall et al. 1995",
        range=(-1.0, 1.0),
        colormap="Blues",
        use_case="snow / ice cover",
        threshold_hint=0.4,
    )


def gndvi(green: xr.DataArray, nir: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Green NDVI — Gitelson et al. 1996.  Substitutes Green for Red; less
    saturated over dense canopy.

    GNDVI = (NIR - Green) / (NIR + Green)
    """
    return _attach_attrs(
        _ndi(nir, green, eps),
        index="GNDVI",
        formula="(nir - green) / (nir + green)",
        citation="Gitelson et al. 1996",
        range=(-1.0, 1.0),
        colormap="RdYlGn",
        use_case="chlorophyll content",
        threshold_hint=0.3,
    )


def ndre(nir: xr.DataArray, re1: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Normalized Difference Red-Edge — Barnes et al. 2000.  Needs red-edge
    (Sentinel-2 B5).  Used for canopy nitrogen / stress.

    NDRE = (NIR - RE1) / (NIR + RE1)
    """
    return _attach_attrs(
        _ndi(nir, re1, eps),
        index="NDRE",
        formula="(nir - re1) / (nir + re1)",
        citation="Barnes et al. 2000",
        range=(-1.0, 1.0),
        colormap="RdYlGn",
        use_case="canopy nitrogen / stress",
        threshold_hint=0.2,
    )


def swi(re1: xr.DataArray, swir2: xr.DataArray, eps: float = _EPSILON) -> xr.DataArray:
    """Sentinel-2 Water Index — combines red-edge + SWIR2.  Less false-positive
    over wet vegetation than NDWI/MNDWI.

    SWI = (RE1 - SWIR2) / (RE1 + SWIR2)
    """
    return _attach_attrs(
        _ndi(re1, swir2, eps),
        index="SWI",
        formula="(re1 - swir2) / (re1 + swir2)",
        citation="Fernandes et al. 2017",
        range=(-1.0, 1.0),
        colormap="Blues",
        use_case="surface water (Sentinel-2)",
        threshold_hint=0.1,
    )


def evi(blue: xr.DataArray, red: xr.DataArray, nir: xr.DataArray,
        L: float = 1.0, C1: float = 6.0, C2: float = 7.5, G: float = 2.5,
        eps: float = _EPSILON) -> xr.DataArray:
    """Enhanced Vegetation Index — Huete et al. 2002.  Corrects NDVI saturation
    over dense canopy and reduces atmospheric / soil background effects.

    EVI = G * (NIR - Red) / (NIR + C1*Red - C2*Blue + L)
    """
    n = nir.astype("float32")
    r = red.astype("float32")
    b = blue.astype("float32")
    out = G * (n - r) / (n + C1 * r - C2 * b + L + eps)
    return _attach_attrs(
        out,
        index="EVI",
        formula="G*(nir - red) / (nir + C1*red - C2*blue + L)",
        citation="Huete et al. 2002",
        range=(-1.0, 1.0),
        colormap="RdYlGn",
        use_case="vegetation health (dense canopy)",
        threshold_hint=0.4,
    )


# ---------------------------------------------------------------------------
# Registry — single source of truth.
# Required-bands list drives the band fetch in compute_spectral_index().
# colormap drives plot_raster_tile().  citation feeds the citation system.
# ---------------------------------------------------------------------------
INDEX_REGISTRY: dict[str, dict[str, Any]] = {
    "NDVI":  {"fn": ndvi,  "bands": ["red", "nir"],
              "range": (-1.0, 1.0), "colormap": "RdYlGn",
              "citation": "Rouse et al. 1974",
              "use_case": "vegetation health", "threshold_hint": 0.4,
              "needs_optical_qc": True,
              "formula": "(nir - red) / (nir + red)"},
    "NDWI":  {"fn": ndwi,  "bands": ["green", "nir"],
              "range": (-1.0, 1.0), "colormap": "Blues",
              "citation": "McFeeters 1996",
              "use_case": "surface water mapping", "threshold_hint": 0.3,
              "needs_optical_qc": True,
              "formula": "(green - nir) / (green + nir)"},
    "MNDWI": {"fn": mndwi, "bands": ["green", "swir1"],
              "range": (-1.0, 1.0), "colormap": "Blues",
              "citation": "Xu 2006",
              "use_case": "surface water (urban areas)",
              "threshold_hint": 0.2, "needs_optical_qc": True,
              "formula": "(green - swir1) / (green + swir1)"},
    "NDBI":  {"fn": ndbi,  "bands": ["swir1", "nir"],
              "range": (-1.0, 1.0), "colormap": "Reds",
              "citation": "Zha et al. 2003",
              "use_case": "built-up / impervious surface",
              "threshold_hint": 0.0, "needs_optical_qc": True,
              "formula": "(swir1 - nir) / (swir1 + nir)"},
    "NBR":   {"fn": nbr,   "bands": ["nir", "swir2"],
              "range": (-1.0, 1.0), "colormap": "RdYlGn_r",
              "citation": "Key & Benson 2006",
              "use_case": "burn severity", "threshold_hint": 0.1,
              "needs_optical_qc": True,
              "formula": "(nir - swir2) / (nir + swir2)"},
    "NDSI":  {"fn": ndsi,  "bands": ["green", "swir1"],
              "range": (-1.0, 1.0), "colormap": "Blues",
              "citation": "Hall et al. 1995",
              "use_case": "snow / ice cover", "threshold_hint": 0.4,
              "needs_optical_qc": True,
              "formula": "(green - swir1) / (green + swir1)"},
    "GNDVI": {"fn": gndvi, "bands": ["green", "nir"],
              "range": (-1.0, 1.0), "colormap": "RdYlGn",
              "citation": "Gitelson et al. 1996",
              "use_case": "chlorophyll content",
              "threshold_hint": 0.3, "needs_optical_qc": True,
              "formula": "(nir - green) / (nir + green)"},
    "NDRE":  {"fn": ndre,  "bands": ["nir", "re1"],
              "range": (-1.0, 1.0), "colormap": "RdYlGn",
              "citation": "Barnes et al. 2000",
              "use_case": "canopy nitrogen / stress",
              "threshold_hint": 0.2, "needs_optical_qc": True,
              "formula": "(nir - re1) / (nir + re1)"},
    "SWI":   {"fn": swi,   "bands": ["re1", "swir2"],
              "range": (-1.0, 1.0), "colormap": "Blues",
              "citation": "Fernandes et al. 2017",
              "use_case": "surface water (Sentinel-2 red-edge)",
              "threshold_hint": 0.1, "needs_optical_qc": True,
              "formula": "(re1 - swir2) / (re1 + swir2)"},
    "EVI":   {"fn": evi,   "bands": ["blue", "red", "nir"],
              "range": (-1.0, 1.0), "colormap": "RdYlGn",
              "citation": "Huete et al. 2002",
              "use_case": "vegetation health (dense canopy)",
              "threshold_hint": 0.4, "needs_optical_qc": True,
              "formula": "2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0)"},
}



def list_indices(use_case: str | None = None) -> list[dict[str, Any]]:
    """Return all registered indices with their metadata.

    Filter by partial-match on ``use_case`` (e.g. ``"water"`` → NDWI, MNDWI,
    SWI, NDSI).  Discovery layer for the agent and for
    ``aihydro_data.list_products()``.
    """
    out = []
    for name, meta in INDEX_REGISTRY.items():
        if use_case and use_case.lower() not in meta["use_case"].lower():
            continue
        out.append({
            "name": name,
            "bands": meta["bands"],
            "range": meta["range"],
            "colormap": meta["colormap"],
            "use_case": meta["use_case"],
            "citation": meta["citation"],
            "threshold_hint": meta["threshold_hint"],
        })
    return out


def gee_index_formula(name: str) -> tuple[str, list[str]] | None:
    """Return ``(formula, required_bands)`` for computing *name* SERVER-SIDE.

    The formula is a plain arithmetic string in the canonical friendly band
    names (``green``, ``nir``, ``swir1`` …) suitable for GEE's
    :meth:`ee.Image.expression`.  ``required_bands`` lists exactly the bands
    that must be present (and scaled to true reflectance) in the image before
    evaluating the expression.

    Returns ``None`` if the index is unknown or has no server-side formula
    (in which case the caller should fall back to the local numpy path).

    This is the single source of truth that lets the GEE backend push index
    computation onto Earth Engine's servers — downloading one band instead of
    N raw reflectance bands (~N× area/resolution headroom).
    """
    key = name.upper()
    meta = INDEX_REGISTRY.get(key)
    if not meta:
        return None
    formula = meta.get("formula")
    if not formula:
        return None
    return formula, list(meta["bands"])


def compute_index(name: str,
                  ds: xr.Dataset | xr.DataArray | Mapping[str, xr.DataArray],
                  *,
                  sensor: str | None = None,
                  band_map: Mapping[str, str] | None = None,
                  mask_clouds_first: bool = True,
                  **fn_kwargs: Any) -> xr.DataArray:
    """Compute a registered index.

    Parameters
    ----------
    name
        Index name (case-insensitive) — must be in ``INDEX_REGISTRY``.
    ds
        Either an ``xr.Dataset`` with band data_vars, an ``xr.DataArray``
        (only valid if the index takes a single band — none do today), or a
        plain mapping ``{"green": DataArray, "nir": DataArray}``.
    sensor
        Used to resolve sensor-native band names from ``SENSOR_BAND_MAPS``.
        If ``ds`` is a Dataset with ``ds.attrs['sensor']`` set, that wins.
    band_map
        Per-call override for band lookup — ``{"green": "B03_my_name"}``.
        Takes priority over the sensor preset.
    mask_clouds_first
        If True and the input Dataset's sensor is in ``CLOUD_AWARE_SENSORS``,
        apply ``mask_clouds(ds, sensor)`` before computing the index.
    fn_kwargs
        Extra kwargs forwarded to the index function (e.g. ``eps`` override
        or EVI's L/C1/C2/G constants).
    """
    key = name.upper()
    if key not in INDEX_REGISTRY:
        raise KeyError(
            f"Unknown index '{name}'. Registered: {sorted(INDEX_REGISTRY)}"
        )
    meta = INDEX_REGISTRY[key]
    fn: Callable[..., xr.DataArray] = meta["fn"]
    required_bands: list[str] = meta["bands"]

    # Resolve sensor.
    if sensor is None and isinstance(ds, xr.Dataset):
        sensor = ds.attrs.get("sensor")

    # Optional cloud masking.
    if mask_clouds_first and isinstance(ds, xr.Dataset) and meta.get("needs_optical_qc"):
        try:
            from aihydro_data.transforms.cloud_mask import (
                CLOUD_AWARE_SENSORS, mask_clouds,
            )
            if sensor and sensor.lower() in CLOUD_AWARE_SENSORS:
                ds = mask_clouds(ds, sensor=sensor)
        except Exception as exc:
            log.debug("cloud_mask skipped: %s", exc)

    # Pull the bands.
    if isinstance(ds, Mapping) and not isinstance(ds, xr.Dataset):
        # Plain {"green": DA, "nir": DA} dict mode.
        band_arrays = {b: ds[b] for b in required_bands}
    else:
        band_arrays = {
            b: _resolve_band(ds, b, sensor=sensor, band_map=band_map)
            for b in required_bands
        }

    return fn(**band_arrays, **fn_kwargs)
