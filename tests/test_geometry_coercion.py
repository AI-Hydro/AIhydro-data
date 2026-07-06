from shapely.geometry import Point

from aihydro_data.geometry import coerce_geometry


def test_coerce_geometry_accepts_lat_lon_dict():
    geom = coerce_geometry({"lat": 45.175, "lon": -69.3147})

    assert isinstance(geom, Point)
    assert geom.y == 45.175
    assert geom.x == -69.3147
