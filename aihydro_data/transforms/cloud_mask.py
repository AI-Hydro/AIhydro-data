"""
Sensor-aware cloud / shadow / cirrus masking.

Without this, any optical index over a Sentinel-2 or Landsat scene with even
moderate cloud cover returns garbage values that propagate downstream into
analysis and any agent-generated conclusions.

Each sensor exposes a different QA encoding:

  - **Sentinel-2 (Level-2A)** carries the *Scene Classification Layer* (SCL)
    as a single band with integer codes (0=no_data, 1=defective, 2=dark,
    3=cloud_shadow, 4=vegetation, 5=bare_soil, 6=water, 7=cloud_low,
    8=cloud_medium, 9=cloud_high, 10=thin_cirrus, 11=snow).  The defaults
    here mask 0/1/3/8/9/10 (always wrong) plus 11 (snow conflates with water
    NDWI).  ``aggressive=True`` also masks 7 (low-confidence cloud).
  - **Landsat-8/9 Collection 2** packs cloud info into the ``QA_PIXEL`` band
    as a bitfield.  Bits 1 (dilated cloud), 3 (cloud), 4 (shadow), 5 (snow),
    and 2 (cirrus, L8/9 only) are masked by default.
  - **MODIS MOD09 / MYD09** packs cloud info into ``state_1km`` bits.

The output is a copy of the input Dataset where masked pixels are NaN in
every band (so subsequent band-math propagates NaN cleanly).
"""
from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import xarray as xr

log = logging.getLogger(__name__)

CLOUD_AWARE_SENSORS: set[str] = {
    "sentinel2", "landsat7", "landsat8", "landsat9", "modis_mod09",
}

# Sentinel-2 SCL class codes — see ESA SCL documentation.
_SCL_DEFAULT_BAD = (0, 1, 3, 8, 9, 10, 11)   # no_data, defective, shadow, cloud_med, cloud_high, cirrus, snow
_SCL_AGGRESSIVE_EXTRA = (7,)                 # cloud_low

# Landsat-8/9 Collection-2 QA_PIXEL bit positions.
_LS_BIT_DILATED_CLOUD = 1
_LS_BIT_CIRRUS = 2
_LS_BIT_CLOUD = 3
_LS_BIT_CLOUD_SHADOW = 4
_LS_BIT_SNOW = 5

# MODIS state_1km bit positions (MOD09GA / MYD09GA).  Bit 0–1 cloud state,
# bit 2 cloud shadow, bit 6–7 aerosol quantity, bit 8–9 cirrus, bit 10
# internal cloud, bit 13 fire, bit 15 snow.
_MODIS_BIT_CLOUD_STATE = 0     # 2-bit field; 0=clear, 1=cloudy, 2=mixed, 3=missing
_MODIS_BIT_CLOUD_SHADOW = 2
_MODIS_BIT_CIRRUS = 8          # 2-bit field; 0=none ... 3=high
_MODIS_BIT_INTERNAL_CLOUD = 10
_MODIS_BIT_SNOW = 15


def _bit_set(qa: xr.DataArray, bit: int) -> xr.DataArray:
    """Return a boolean DataArray where bit *bit* of qa is 1."""
    return (qa.astype("int64") >> bit) & 1 == 1


def _bits_value(qa: xr.DataArray, start_bit: int, n_bits: int) -> xr.DataArray:
    """Return the integer value of a multi-bit field starting at start_bit."""
    mask = (1 << n_bits) - 1
    return (qa.astype("int64") >> start_bit) & mask


def _sentinel2_bad_mask(scl: xr.DataArray, aggressive: bool = False) -> xr.DataArray:
    """Return a boolean DataArray (True = pixel is bad / cloud / shadow)."""
    bad = (_SCL_DEFAULT_BAD + _SCL_AGGRESSIVE_EXTRA) if aggressive else _SCL_DEFAULT_BAD
    return xr.apply_ufunc(np.isin, scl, list(bad), dask="parallelized",
                          output_dtypes=[bool])


def _landsat_bad_mask(qa: xr.DataArray, aggressive: bool = False) -> xr.DataArray:
    """Landsat Collection-2 QA_PIXEL → boolean bad mask."""
    bad = (
        _bit_set(qa, _LS_BIT_CLOUD)
        | _bit_set(qa, _LS_BIT_CLOUD_SHADOW)
        | _bit_set(qa, _LS_BIT_DILATED_CLOUD)
        | _bit_set(qa, _LS_BIT_CIRRUS)
    )
    if aggressive:
        bad = bad | _bit_set(qa, _LS_BIT_SNOW)
    return bad


def _modis_bad_mask(state: xr.DataArray, aggressive: bool = False) -> xr.DataArray:
    """MODIS state_1km → boolean bad mask."""
    cloud_state = _bits_value(state, _MODIS_BIT_CLOUD_STATE, 2)
    cirrus = _bits_value(state, _MODIS_BIT_CIRRUS, 2)
    bad = (
        (cloud_state == 1) | (cloud_state == 3)
        | _bit_set(state, _MODIS_BIT_CLOUD_SHADOW)
        | _bit_set(state, _MODIS_BIT_INTERNAL_CLOUD)
        | (cirrus >= 2)
    )
    if aggressive:
        bad = bad | _bit_set(state, _MODIS_BIT_SNOW)
    return bad


def _qa_band_name(sensor: str) -> str:
    from aihydro_data.transforms.indices import SENSOR_BAND_MAPS
    sm = SENSOR_BAND_MAPS.get(sensor.lower())
    if not sm or "qa" not in sm:
        raise KeyError(f"No QA band registered for sensor {sensor!r}")
    return sm["qa"]


def mask_clouds(
    ds: xr.Dataset,
    sensor: str | None = None,
    *,
    aggressive: bool = False,
    skip_bands: Iterable[str] = (),
) -> xr.Dataset:
    """Apply sensor-appropriate cloud + shadow + cirrus mask.

    Parameters
    ----------
    ds
        Multi-band Dataset.  Must include the sensor's QA band (e.g. ``SCL``
        for Sentinel-2, ``QA_PIXEL`` for Landsat, ``state_1km`` for MODIS).
    sensor
        Sensor identifier (case-insensitive).  Falls back to
        ``ds.attrs['sensor']`` if not passed.
    aggressive
        If True, additionally mask low-confidence cloud / snow.  Useful for
        water mapping where false positives are costly; harmful for crop
        monitoring where snow-on-vegetation is real.
    skip_bands
        Bands left untouched (always includes the QA band itself).

    Returns
    -------
    Dataset where bad pixels are NaN in every data var (except the QA band
    and any ``skip_bands``).  Carries an ``attrs['cloud_masked']`` flag and
    the fraction of masked pixels for downstream provenance.
    """
    if sensor is None:
        sensor = ds.attrs.get("sensor")
    if not sensor:
        log.debug("mask_clouds: no sensor specified — skipping")
        return ds
    s = sensor.lower()
    if s not in CLOUD_AWARE_SENSORS:
        log.debug("mask_clouds: sensor %r is not cloud-aware — skipping", sensor)
        return ds

    qa_name = _qa_band_name(s)
    if qa_name not in ds.data_vars:
        log.warning("mask_clouds: QA band %r not present for %s — skipping",
                    qa_name, sensor)
        return ds

    qa = ds[qa_name]
    if s == "sentinel2":
        bad = _sentinel2_bad_mask(qa, aggressive=aggressive)
    elif s in ("landsat7", "landsat8", "landsat9"):
        bad = _landsat_bad_mask(qa, aggressive=aggressive)
    elif s == "modis_mod09":
        bad = _modis_bad_mask(qa, aggressive=aggressive)
    else:
        # Defensive — should be impossible given the CLOUD_AWARE_SENSORS guard.
        return ds

    masked_fraction = float(bad.mean())
    if masked_fraction >= 0.95:
        log.warning(
            "mask_clouds: %.1f%% of pixels masked for sensor=%s; result may be"
            " mostly NaN.  Consider relaxing aggressive=False or expanding the"
            " date range.", masked_fraction * 100, sensor,
        )

    skip = set(skip_bands) | {qa_name}
    new_vars = {}
    for name, da in ds.data_vars.items():
        if name in skip:
            new_vars[name] = da
        else:
            new_vars[name] = da.where(~bad)

    out = xr.Dataset(new_vars, coords=ds.coords, attrs=dict(ds.attrs))
    out.attrs["cloud_masked"] = True
    out.attrs["cloud_masked_sensor"] = sensor
    out.attrs["cloud_masked_aggressive"] = aggressive
    out.attrs["cloud_masked_fraction"] = round(masked_fraction, 4)
    return out
