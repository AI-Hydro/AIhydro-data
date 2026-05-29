"""
Scaffold + Phase 2 smoke tests.

Goal: prove the package is importable, the contracts validate, the registry
loads products (Phase 2+), routing works, and the lazy proxies in
`__init__.py` resolve to the right callables. No live backend deps required.
"""
from __future__ import annotations

import pytest

import aihydro_data


def test_version_present():
    assert isinstance(aihydro_data.__version__, str)
    assert aihydro_data.__version__.count(".") >= 1


def test_public_api_exposed():
    assert callable(aihydro_data.fetch)
    assert callable(aihydro_data.list_products)
    assert callable(aihydro_data.get_product)


def test_contracts_construct():
    from aihydro_data.contracts import FetchRequest, FetchResult, ProductSpec

    spec = ProductSpec(
        id="DUMMY",
        variable="precipitation",
        source="gee",
        coverage=["global"],
    )
    assert spec.id == "DUMMY"
    assert spec.variable == "precipitation"
    # Frozen — should raise on mutation
    with pytest.raises(Exception):
        spec.id = "OTHER"

    req = FetchRequest(
        variable="precipitation",
        geometry=(40.0, -85.0),
        start="2010-01-01",
        end="2010-12-31",
    )
    assert req.mode == "auto"
    assert req.aggregation == "basin_mean"

    res = FetchResult(
        variable="precipitation",
        product="DUMMY",
        source="gee",
        request=req,
        data={"placeholder": True},
    )
    assert "FetchResult" in res.help()


def test_exceptions_envelope():
    from aihydro_data.exceptions import (
        AuthRequired,
        SourceUnavailable,
    )

    e = AuthRequired(
        code="GEE_AUTH_MISSING",
        message="Earth Engine not initialised.",
        recovery="Run `aihydro-data auth gee`.",
        next_tools=["data_doctor"],
        docs_anchor="auth#gee",
    )
    env = e.to_dict()
    assert env["error"] is True
    assert env["code"] == "GEE_AUTH_MISSING"
    assert env["next_tools"] == ["data_doctor"]
    assert env["docs_anchor"] == "auth#gee"

    # SourceUnavailable defaults are sane
    su = SourceUnavailable(code="X", message="y")
    assert su.to_dict()["next_tools"] == []


def test_registry_loads_products():
    """Phase 2: registry loads precipitation products."""
    out = aihydro_data.list_products()
    assert isinstance(out, list)
    # Phase 2 ships precipitation products
    ids = {p.id for p in out}
    assert "CHIRPS" in ids, f"CHIRPS missing from registry. Found: {ids}"
    assert "GRIDMET_PRECIP" in ids
    assert "ERA5L_PRECIP" in ids


def test_get_product_missing_raises():
    with pytest.raises(KeyError):
        aihydro_data.get_product("DEFINITELY_DOES_NOT_EXIST")


def test_fetch_validates_and_routes():
    """
    Phase 2: fetch() validates kwargs and routes through the pipeline.

    In an offline / no-auth CI environment all backends are expected to fail,
    so we accept either a successful FetchResult (live env) or a structured
    AihydroDataError. The key assertion: fetch() must NOT raise
    NotImplementedError anymore — the pipeline is wired.
    """
    from aihydro_data.exceptions import AihydroDataError
    try:
        result = aihydro_data.fetch(
            variable="precipitation",
            geometry=(40.0, -85.0),
            start="2015-01-01",
            end="2015-12-31",
        )
        # Live environment — result shape is valid
        assert result.variable == "precipitation"
        assert result.product in {"GRIDMET_PRECIP", "DAYMET_PRECIP", "CHIRPS", "MSWEP", "ERA5L_PRECIP"}
    except AihydroDataError:
        pass  # Expected in offline / no-auth CI
    except Exception as exc:
        pytest.fail(f"fetch() raised unexpected {type(exc).__name__}: {exc}")


def test_geometry_coercion_point_tuple():
    from aihydro_data.geometry import coerce_geometry
    g = coerce_geometry((40.5, -85.3))   # (lat, lon)
    assert g.geom_type == "Point"
    assert g.x == -85.3 and g.y == 40.5


def test_geometry_coercion_bbox():
    from aihydro_data.geometry import coerce_geometry
    g = coerce_geometry((-87.0, 40.0, -85.0, 41.0))
    assert g.geom_type == "Polygon"


def test_geometry_coercion_invalid():
    """
    None and unsupported types (e.g. int) must raise. Strings are NOT
    invalid: they are accepted as either WKT or as a gauge-ID identifier
    (used by the direct_api / NWIS backend).
    """
    from aihydro_data.exceptions import GeometryInvalid
    from aihydro_data.geometry import coerce_geometry, GaugeID
    with pytest.raises(GeometryInvalid):
        coerce_geometry(None)
    with pytest.raises(GeometryInvalid):
        coerce_geometry(12345)   # bare int — not a coord, not a string
    with pytest.raises(GeometryInvalid):
        coerce_geometry("")       # empty string is invalid
    # Strings that aren't WKT fall through to GaugeID (intentional —
    # NWIS gauge IDs are how direct_api receives "geometry"):
    g = coerce_geometry("03353000")
    assert isinstance(g, GaugeID) and g.id == "03353000"
    # Valid WKT strings parse to real geometries:
    g = coerce_geometry("POINT (-85 40)")
    assert g.geom_type == "Point"


def test_cache_key_deterministic():
    from aihydro_data.cache import cache_key
    a = cache_key({"variable": "precip", "start": "2015-01-01"})
    b = cache_key({"start": "2015-01-01", "variable": "precip"})
    assert a == b
    assert len(a) == 24


def test_help_topics_dir_exists():
    from aihydro_data.help_topics import available_topics, topics_dir
    d = topics_dir()
    assert d.is_dir()
    # Phase 7: 8 help topics shipped
    topics = available_topics()
    assert len(topics) >= 8
    for expected in ("first_fetch", "auth", "fallback", "batch",
                     "products", "caching", "deprecations", "errors"):
        assert expected in topics, f"Missing help topic: {expected!r}"


def test_skills_entry_point_callable():
    from aihydro_data.skills import get_skills_dir
    p = get_skills_dir()
    assert p.is_dir()


def test_mcp_register_stub_callable():
    from aihydro_data.mcp import register_tools
    # Stub returns None without raising.
    assert register_tools() is None
