"""Unit handling for the USGS Water Data adapter (Wave U / U1a)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aihydro_data.exceptions import SourceUnavailable
from aihydro_data.sources import usgs_waterdata as uw


def test_constant_is_the_exact_definition():
    # 1 ft = 0.3048 m exactly, so 1 ft^3 = 0.028316846592 m^3 exactly.
    assert uw.CFS_TO_M3S == 0.028316846592
    assert abs(uw.CFS_TO_M3S - 0.3048 ** 3) < 1e-15


@pytest.mark.parametrize("unit", ["ft^3/s", "ft3/s"])
def test_known_units_convert(unit):
    out = uw.to_m3s(pd.Series([0.0, 1.0, 586.0]), unit)
    assert out.tolist() == [0.0, 0.028316846592, 586.0 * 0.028316846592]
    arr = uw.to_m3s(np.array([10.0]), unit)
    assert arr[0] == 10.0 * 0.028316846592


@pytest.mark.parametrize("unit", ["m3/s", "cfs", "ft^3/s ", "", None, "FT^3/S", "mm/d"])
def test_unknown_units_refuse(unit):
    with pytest.raises(SourceUnavailable) as ei:
        uw.to_m3s(pd.Series([1.0]), unit)
    assert ei.value.code == "USGS_UNIT_UNKNOWN"
