"""Oddities found producing AGU evidence (2026-11): single-place dict geometry, gauge_id with one place, and pinned products.

* ``{"lat","lon"}`` / ``{"west","south","east","north"}`` are ONE geometry, not a ``{label: geometry}`` collection.
* ``mode="manual"`` pins a first choice in Python; a substitute is flagged at the top level; the MCP tool is strict.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest
from shapely.geometry import Point

from aihydro_data import fetch
from aihydro_data._pipeline import _looks_like_collection
from aihydro_data.contracts import FetchResult
from aihydro_data.exceptions import GeometryInvalid, SourceUnavailable
from aihydro_data.geometry import coerce_geometry

POINT = {"lat": 45.175, "lon": -69.3147}
BBOX = {"west": -69.40, "south": 45.10, "east": -69.20, "north": 45.25}


def _result_for(spec, req):
    df = pd.DataFrame({"date": pd.date_range("2020-06-01", periods=3), "precipitation": [1.0, 2.0, 3.0]})
    return FetchResult(variable=spec.variable, product=spec.id, source=spec.source, request=req, data=df,
                       license=spec.license, citation=spec.citation)


# ------------------------------------------------------------------ single-place dicts
@pytest.mark.parametrize("geom", [POINT, BBOX, {"lat": 45, "lon": -69}])
def test_single_place_dicts_are_not_collections(geom):
    assert _looks_like_collection(geom) is False


def test_label_mappings_are_still_collections():
    assert _looks_like_collection({"a": POINT, "b": POINT}) is True
    assert _looks_like_collection({"lat": POINT, "lon": POINT}) is True      # labels that happen to be named lat/lon


def test_coerce_point_and_bbox_dicts():
    p = coerce_geometry(POINT)
    assert (p.x, p.y) == (-69.3147, 45.175)
    assert coerce_geometry(BBOX).bounds == (-69.40, 45.10, -69.20, 45.25)
    with pytest.raises(GeometryInvalid) as e:
        coerce_geometry({**BBOX, "west": "x"})
    assert e.value.code == "GEOMETRY_INVALID_BBOX"


def test_fetch_accepts_the_lat_lon_dict_end_to_end():
    with patch("aihydro_data._pipeline._fetch_one", side_effect=lambda spec, g, s, e, a, req, **kw: _result_for(spec, req)):
        r = fetch("precipitation", POINT, "2020-06-01", "2020-06-03", cache=False)
    assert isinstance(r, FetchResult) and r.fallback_history[-1]["outcome"] == "served"


def test_gauge_id_with_a_single_place_dict_is_not_a_collection():
    seen = {}

    def fake(spec, g, s, e, a, req, **kw):
        seen["gauge_id"] = req.gauge_id
        return _result_for(spec, req)

    with patch("aihydro_data._pipeline._fetch_one", side_effect=fake):
        fetch("precipitation", POINT, "2020-06-01", "2020-06-03", cache=False, gauge_id="01031500")
    assert seen["gauge_id"] == "01031500"


def test_gauge_with_a_real_collection_still_refuses_and_says_how_many():
    with pytest.raises(GeometryInvalid) as e:
        fetch("streamflow", {"a": Point(-69, 45), "b": Point(-70, 45)}, "2020-01-01", "2020-01-14", gauge_id="01031500")
    assert e.value.code == "GAUGE_WITH_COLLECTION"
    assert "2 entries" in e.value.message and "lat" in e.value.recovery


# ------------------------------------------------------------------ pinned products
def _fail_pinned_then_serve(pinned):
    def fake(spec, geom, start, end, agg, req, **kw):
        if spec.id == pinned:
            raise RuntimeError("pinned boom")
        return _result_for(spec, req)
    return fake


def test_python_manual_pin_is_a_first_choice_and_a_substitute_is_flagged():
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail_pinned_then_serve("GRIDMET_PRECIP")):
        r = fetch("precipitation", Point(-69.3, 45.2), "2020-06-01", "2020-06-03",
                  mode="manual", product="GRIDMET_PRECIP", cache=False)
    assert r.product != "GRIDMET_PRECIP"
    assert r.product_substituted is True and r.requested_product == "GRIDMET_PRECIP"
    assert r.fallback_history[0]["outcome"] == "failed" and r.fallback_history[0]["product"] == "GRIDMET_PRECIP"
    assert any("SUBSTITUTED PRODUCT" in n for n in r.notes)


def test_python_empty_fallback_makes_the_pin_strict():
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail_pinned_then_serve("GRIDMET_PRECIP")):
        with pytest.raises(SourceUnavailable):
            fetch("precipitation", Point(-69.3, 45.2), "2020-06-01", "2020-06-03",
                  mode="manual", product="GRIDMET_PRECIP", fallback=[], cache=False)


def test_unsubstituted_result_is_unflagged():
    with patch("aihydro_data._pipeline._fetch_one", side_effect=lambda spec, g, s, e, a, req, **kw: _result_for(spec, req)):
        r = fetch("precipitation", Point(-69.3, 45.2), "2020-06-01", "2020-06-03",
                  mode="manual", product="GRIDMET_PRECIP", cache=False)
    assert r.product == "GRIDMET_PRECIP" and r.product_substituted is False and r.requested_product == ""


def test_mcp_data_fetch_pin_is_strict_by_default_and_opt_in_fallback_is_flagged():
    from aihydro_data.mcp import _data_fetch

    args = dict(variable="precipitation", geometry=POINT, start="2020-06-01", end="2020-06-03",
                mode="manual", product="GRIDMET_PRECIP", cache=False)
    with patch("aihydro_data._pipeline._fetch_one", side_effect=_fail_pinned_then_serve("GRIDMET_PRECIP")):
        strict = _data_fetch(**args)
        opt_in = _data_fetch(**args, fallback=["DAYMET_PRECIP"])
    assert strict.get("error") is True
    assert [h["product"] for h in strict["details"]["fallback_history"]] == ["GRIDMET_PRECIP"] if "details" in strict else True
    assert opt_in["product"] == "DAYMET_PRECIP"
    assert opt_in["product_substituted"] is True and opt_in["requested_product"] == "GRIDMET_PRECIP"
