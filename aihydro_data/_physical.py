"""Physical-validity masking shared by every backend and the fetch pipeline.

A provider fill value (netCDF ``_FillValue`` / ``missing_value``, e.g. 1e33 or
9.97e36), a non-finite number or a physically impossible value is *missing
data*, never a measurement. Averaging it into a basin mean silently turns one
bad cell into an absurd series (e2e proof 2, defect D4: CHIRPS_IRI served a
~1e33 fill as precipitation and runoff_ratio came out as 2.3e-33).

Two layers use this module:

* a backend that spatially aggregates (e.g. CHIRPS_IRI) must call
  :func:`mask_invalid` on the per-cell values BEFORE averaging, because after
  averaging fill and data cannot be separated;
* the pipeline calls :func:`enforce_physical_range` on whatever any backend
  returned, as the general backstop (backends are not trusted to have masked).

The bounds are generous engineering bounds (source to verify), chosen to
reject fill values and unit errors, not to second-guess real climate.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import numpy as np

log = logging.getLogger(__name__)

#: (variable, unit as in ProductSpec.units) -> (lo, hi), inclusive. Engineering
#: bounds; source to verify. Products whose (variable, units) are not listed
#: are masked only for non-finite values / declared fill values, never by range.
PHYSICAL_BOUNDS: dict[tuple[str, str], tuple[float, float]] = {
    ("precipitation", "mm/day"): (0.0, 2000.0),
    ("pet", "mm/day"): (-30.0, 30.0),          # ERA5-Land PET is sign-negative
    ("tmax", "K"): (150.0, 350.0),
    ("tmin", "K"): (150.0, 350.0),
    ("tmax", "degC"): (-100.0, 70.0),
    ("tmin", "degC"): (-100.0, 70.0),
    ("soil_moisture", "cm3/cm3"): (0.0, 1.0),
}


def bounds_for(variable: str, units: str) -> Optional[tuple[float, float]]:
    return PHYSICAL_BOUNDS.get((variable or "", (units or "").strip()))


def declared_fill_values(*attr_maps: Any) -> list[float]:
    """Fill values a dataset declares (``_FillValue``, ``missing_value``,
    ``fill_value``) in any of the given attribute/encoding mappings."""
    out: list[float] = []
    for m in attr_maps:
        if not m:
            continue
        for key in ("_FillValue", "missing_value", "fill_value"):
            try:
                v = m.get(key)
            except Exception:  # noqa: BLE001
                continue
            if v is None:
                continue
            for x in np.atleast_1d(v):
                try:
                    f = float(x)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(f):
                    out.append(f)
    return out


def mask_invalid(
    values: Any,
    *,
    fill_values: Iterable[float] = (),
    bounds: Optional[tuple[float, float]] = None,
) -> tuple[np.ndarray, int]:
    """Return ``(float array with invalid entries set to NaN, n newly masked)``.

    Masked: non-finite values, values equal to a declared fill value (relative
    tolerance 1e-6, so float32 round-off of 9.96921e36 still matches), and
    values outside ``bounds``. Already-NaN entries are not counted.
    """
    arr = np.array(values, dtype=float, copy=True)
    before = np.isnan(arr)
    bad = ~np.isfinite(arr)
    for fv in fill_values:
        bad |= np.isclose(arr, fv, rtol=1e-6, atol=0.0)
    if bounds is not None:
        with np.errstate(invalid="ignore"):
            bad |= (arr < bounds[0]) | (arr > bounds[1])
    arr[bad] = np.nan
    return arr, int(np.count_nonzero(bad & ~before))


def enforce_physical_range(spec: Any, data: Any) -> tuple[Any, int]:
    """Mask fill / non-finite / impossible values in a backend result.

    Works on a time-series ``DataFrame`` (numeric columns) and on xarray
    ``DataArray`` / ``Dataset``. Returns ``(data, n_masked)``; ``data`` is a
    masked copy only when something was masked. Never raises: an object that
    cannot be introspected is returned unchanged.
    """
    variable = getattr(spec, "variable", "")
    bounds = bounds_for(variable, getattr(spec, "units", ""))
    try:
        import pandas as pd
        if isinstance(data, pd.DataFrame):
            num_cols = [c for c in data.select_dtypes("number").columns]
            if not num_cols:
                return data, 0
            out, total = data.copy(), 0
            out.attrs = dict(data.attrs)
            for c in num_cols:
                masked, n = mask_invalid(out[c].to_numpy(dtype="float64", na_value=np.nan),
                                         bounds=bounds)
                if n:
                    out[c] = masked
                    total += n
            return (out, total) if total else (data, 0)
        if hasattr(data, "to_array") and hasattr(data, "data_vars"):      # Dataset
            total = 0
            out = data.copy()
            for name in list(out.data_vars):
                out[name], n = _mask_dataarray(out[name], bounds)
                total += n
            return (out, total) if total else (data, 0)
        if hasattr(data, "values") and hasattr(data, "dims"):             # DataArray
            out, n = _mask_dataarray(data, bounds)
            return (out, n) if n else (data, 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("physical-range enforcement skipped: %s", exc)
    return data, 0


def _mask_dataarray(da: Any, bounds):
    if not np.issubdtype(np.asarray(da.values).dtype, np.number):
        return da, 0
    fills = declared_fill_values(getattr(da, "attrs", None), getattr(da, "encoding", None))
    masked, n = mask_invalid(da.values, fill_values=fills, bounds=bounds)
    if not n:
        return da, 0
    return da.copy(data=masked), n
