from shapely.geometry import Point

from aihydro_data.geometry import coerce_geometry


def test_coerce_geometry_accepts_lat_lon_dict():
    geom = coerce_geometry({"lat": 45.175, "lon": -69.3147})

    assert isinstance(geom, Point)
    assert geom.y == 45.175
    assert geom.x == -69.3147


# ---- string inputs: a gauge id is digits / USGS-prefixed digits, nothing else ----

import pytest

from aihydro_data.exceptions import GeometryInvalid
from aihydro_data.geometry import GaugeID


@pytest.mark.parametrize("s", ["01013500", "USGS-01013500", "usgs-01013500", " 03353000 "])
def test_gauge_id_strings(s):
    g = coerce_geometry(s)
    assert isinstance(g, GaugeID) and g.scheme == "usgs"


@pytest.mark.parametrize("s", ["-68.5828,47.2375", "47.2375, -68.5828", "-68.5828 47.2375", "45,-69"])
def test_numeric_coordinate_string_is_never_a_gauge(s):
    with pytest.raises(GeometryInvalid) as ei:
        coerce_geometry(s)
    assert ei.value.code == "GEOMETRY_COORDINATE_STRING"


@pytest.mark.parametrize("s", ["not-a-gauge", "camels_01013500", "01013500abc"])
def test_other_strings_are_refused(s):
    with pytest.raises(GeometryInvalid) as ei:
        coerce_geometry(s)
    assert ei.value.code == "GEOMETRY_UNPARSEABLE_STRING"


def test_wkt_string_and_tuple_convention_unchanged():
    assert coerce_geometry("POINT (-68.5828 47.2375)").x == -68.5828
    p = coerce_geometry((47.2375, -68.5828))          # documented library (lat, lon)
    assert (p.x, p.y) == (-68.5828, 47.2375)
