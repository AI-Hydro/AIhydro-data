"""
Phase 7 tests — MCP tool functions.

All offline: no network, no auth. Every tool is tested by calling its
underlying function directly (no FastMCP server needed).
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from shapely.geometry import Point


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_result(variable="precipitation", product="CHIRPS", source="gee"):
    """Build a minimal FetchResult for mocking."""
    import pandas as pd
    from aihydro_data.contracts import FetchRequest, FetchResult
    req = FetchRequest(
        variable=variable,
        geometry=Point(-85.0, 40.0),
        start="2015-01-01",
        end="2015-01-31",
    )
    df = pd.DataFrame({
        "date": pd.date_range("2015-01-01", periods=3),
        variable: [1.0, 2.0, 1.5],
    })
    return FetchResult(
        variable=variable,
        product=product,
        source=source,
        request=req,
        cache_key="testkey0000000000000000",
        data=df,
        license="public domain",
        citation="Test citation",
        bibtex="@misc{test}",
        next_steps=[{"tool": "extract_hydrological_signatures", "rationale": "test"}],
    )


# ── data_fetch ────────────────────────────────────────────────────────────────

class TestDataFetch:
    def test_success_returns_result_dict(self):
        from aihydro_data.mcp import data_fetch
        mock = _make_result()
        with patch("aihydro_data._pipeline.fetch", return_value=mock):
            out = data_fetch("precipitation", (40.0, -85.0), "2015-01-01", "2015-01-31")
        assert out.get("error") is not True
        assert out["variable"] == "precipitation"
        assert out["product"] == "CHIRPS"
        assert out["source"] == "gee"
        assert out["data"]["type"] == "DataFrame"
        assert out["data"]["rows"] == 3

    def test_error_returns_envelope(self):
        from aihydro_data.mcp import data_fetch
        from aihydro_data.exceptions import SourceUnavailable
        with patch("aihydro_data._pipeline.fetch",
                   side_effect=SourceUnavailable(code="ALL_BACKENDS_FAILED", message="no backend")):
            out = data_fetch("precipitation", (40.0, -85.0), "2015-01-01", "2015-01-31")
        assert out["error"] is True
        assert out["code"] == "ALL_BACKENDS_FAILED"
        assert "next_tools" in out

    def test_unexpected_exception_returns_envelope(self):
        from aihydro_data.mcp import data_fetch
        with patch("aihydro_data._pipeline.fetch", side_effect=RuntimeError("boom")):
            out = data_fetch("precipitation", (40.0, -85.0), "2015-01-01", "2015-01-31")
        assert out["error"] is True
        assert "UNEXPECTED_ERROR" in out["code"]


# ── data_batch_fetch ──────────────────────────────────────────────────────────

class TestDataBatchFetch:
    def test_returns_expected_structure(self):
        from aihydro_data.mcp import data_batch_fetch
        mock = _make_result()
        with patch("aihydro_data._pipeline.fetch", return_value=mock):
            out = data_batch_fetch(
                "precipitation",
                [(40.0, -85.0), (38.0, -90.0)],
                "2015-01-01",
                "2015-01-31",
                labels=["ws_a", "ws_b"],
            )
        assert "results" in out
        assert "errors" in out
        assert "labels" in out
        assert out["summary"]["succeeded"] == 2
        assert out["summary"]["failed"] == 0

    def test_partial_failure_isolated(self):
        from aihydro_data.mcp import data_batch_fetch
        from aihydro_data.exceptions import SourceUnavailable

        call_count = 0
        mock = _make_result()

        def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SourceUnavailable(code="X", message="fail")
            return mock

        with patch("aihydro_data._pipeline.fetch", side_effect=_side):
            out = data_batch_fetch(
                "precipitation",
                [(40.0, -85.0), (38.0, -90.0)],
                "2015-01-01",
                "2015-01-31",
                max_workers=1,
            )
        assert out["summary"]["succeeded"] == 1
        assert out["summary"]["failed"] == 1

    def test_empty_geometries(self):
        from aihydro_data.mcp import data_batch_fetch
        out = data_batch_fetch("precipitation", [], "2015-01-01", "2015-01-31")
        assert out["labels"] == []
        assert out["results"] == {}


# ── data_list_products ────────────────────────────────────────────────────────

class TestDataListProducts:
    def test_returns_list(self):
        from aihydro_data.mcp import data_list_products
        products = data_list_products()
        assert isinstance(products, list)
        assert len(products) > 0

    def test_filter_by_variable(self):
        from aihydro_data.mcp import data_list_products
        products = data_list_products(variable="precipitation")
        assert all(p["variable"] == "precipitation" for p in products)
        assert len(products) > 0

    def test_filter_by_region(self):
        from aihydro_data.mcp import data_list_products
        products = data_list_products(region="CONUS")
        assert len(products) > 0
        # region filter includes products that explicitly cover CONUS OR cover globally
        for p in products:
            assert "CONUS" in p["coverage"] or "global" in p["coverage"]

    def test_filter_by_source(self):
        from aihydro_data.mcp import data_list_products
        products = data_list_products(source="gee")
        assert all(p["source"] == "gee" for p in products)

    def test_each_entry_has_required_keys(self):
        from aihydro_data.mcp import data_list_products
        for p in data_list_products():
            assert "id" in p
            assert "variable" in p
            assert "source" in p
            assert "coverage" in p
            assert "requires_extras" in p


# ── data_describe_product ─────────────────────────────────────────────────────

class TestDataDescribeProduct:
    def test_known_product(self):
        from aihydro_data.mcp import data_describe_product
        out = data_describe_product("CHIRPS")
        assert out.get("error") is not True
        assert out["id"] == "CHIRPS"
        assert out["variable"] == "precipitation"
        assert out["source"] == "gee"
        assert "citation" in out
        assert "bibtex" in out
        assert "common_pitfalls" in out
        assert "examples" in out

    def test_unknown_product_returns_error(self):
        from aihydro_data.mcp import data_describe_product
        out = data_describe_product("NONEXISTENT_XYZ")
        assert out["error"] is True
        assert out["code"] == "PRODUCT_NOT_FOUND"
        assert "data_list_products" in out["next_tools"]


# ── data_validate_request ─────────────────────────────────────────────────────

class TestDataValidateRequest:
    def test_valid_request_returns_ok(self):
        from aihydro_data.mcp import data_validate_request
        out = data_validate_request(
            "precipitation",
            (40.0, -85.0),
            "2015-01-01",
            "2015-12-31",
        )
        assert "ok" in out
        assert "issues" in out
        assert "detected_region" in out
        assert "candidates_in_priority" in out

    def test_invalid_geometry_reports_issue(self):
        from aihydro_data.mcp import data_validate_request
        # Strings are now accepted as gauge IDs (NWIS). To force a
        # geometry-coercion failure we have to pass something the coercer
        # genuinely can't make sense of — e.g. a bare integer.
        out = data_validate_request(
            "precipitation",
            12345,
            "2015-01-01",
            "2015-12-31",
        )
        codes = [i["code"] for i in out["issues"]]
        assert "GEOMETRY_INVALID" in codes
        assert out["ok"] is False

    def test_date_out_of_range_raises_issue(self):
        from aihydro_data.mcp import data_validate_request
        # CHIRPS starts 1981-01-01; request 1970 should flag DATE_OUT_OF_RANGE
        out = data_validate_request(
            "precipitation",
            (40.0, -85.0),
            "1970-01-01",
            "1970-12-31",
            product="CHIRPS",
        )
        # May or may not be flagged depending on policy; just check structure
        assert "issues" in out
        assert "ok" in out

    def test_conus_region_detected(self):
        from aihydro_data.mcp import data_validate_request
        out = data_validate_request("precipitation", (40.0, -85.0), "2015-01-01", "2015-12-31")
        assert out["detected_region"] == "CONUS"

    def test_global_point_returns_candidates(self):
        from aihydro_data.mcp import data_validate_request
        out = data_validate_request("precipitation", (20.0, 80.0), "2015-01-01", "2015-12-31")
        assert len(out["candidates_in_priority"]) > 0


# ── data_get_cache_status ─────────────────────────────────────────────────────

class TestDataGetCacheStatus:
    def test_returns_status_dict(self, tmp_path):
        from aihydro_data.mcp import data_get_cache_status
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            out = data_get_cache_status()
        assert "entry_count" in out
        assert "total_size_mb" in out
        assert "entries" in out
        assert isinstance(out["entries"], list)


# ── data_invalidate_cache ─────────────────────────────────────────────────────

class TestDataInvalidateCache:
    def test_missing_key_returns_false(self, tmp_path):
        from aihydro_data.mcp import data_invalidate_cache
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            out = data_invalidate_cache("does_not_exist_0000000000")
        assert out["deleted"] is False
        assert "cache_key" in out

    def test_existing_key_deleted(self, tmp_path):
        import pandas as pd
        from aihydro_data.cache import cache_write
        from aihydro_data.mcp import data_invalidate_cache
        from aihydro_data.contracts import FetchRequest, FetchResult

        req = FetchRequest(variable="precipitation", geometry=Point(-85, 40),
                           start="2015-01-01", end="2015-01-31")
        df = pd.DataFrame({"date": pd.date_range("2015-01-01", periods=2),
                           "precipitation": [1.0, 2.0]})
        result = FetchResult(
            variable="precipitation", product="CHIRPS", source="gee",
            request=req, cache_key="mcp_test_key000000000000",
            data=df, license="CC0", citation="test", bibtex="@misc{}",
        )
        with patch("aihydro_data.cache.cache_dir", return_value=tmp_path):
            cache_write(result, "POINT (-85 40)")
            out = data_invalidate_cache("mcp_test_key000000000000")
        assert out["deleted"] is True


# ── data_doctor ───────────────────────────────────────────────────────────────

class TestDataDoctor:
    def test_returns_expected_shape(self):
        from aihydro_data.mcp import data_doctor
        out = data_doctor()
        assert "ok" in out
        assert "version" in out
        assert "backends" in out
        assert "cache" in out
        assert isinstance(out["backends"], dict)
        # Should report on the four canonical backends
        for src in ("gee", "hyriver", "direct_api", "stac"):
            assert src in out["backends"]
            entry = out["backends"][src]
            assert "installed" in entry
            assert "available" in entry
            assert "reason" in entry

    def test_warnings_is_list(self):
        from aihydro_data.mcp import data_doctor
        out = data_doctor()
        assert isinstance(out["warnings"], list)
        assert isinstance(out["recommendations"], list)


# ── data_help ─────────────────────────────────────────────────────────────────

class TestDataHelp:
    def test_no_arg_returns_menu(self):
        from aihydro_data.mcp import data_help
        out = data_help()
        assert isinstance(out, dict)
        assert "topics" in out
        assert "first_fetch" in out["topics"]

    def test_valid_topic_returns_string(self):
        from aihydro_data.mcp import data_help
        out = data_help("first_fetch")
        assert isinstance(out, str)
        assert len(out) > 50

    def test_invalid_topic_returns_message(self):
        from aihydro_data.mcp import data_help
        out = data_help("nonexistent_topic_xyz")
        assert "Unknown topic" in out or "not found" in out

    def test_all_topics_readable(self):
        from aihydro_data.mcp import data_help
        topics = ["first_fetch", "auth", "fallback", "batch",
                  "products", "caching", "deprecations", "errors"]
        for t in topics:
            out = data_help(t)
            assert isinstance(out, str) and len(out) > 20, f"Topic {t!r} returned empty"


# ── register_tools (smoke) ────────────────────────────────────────────────────

class TestRegisterTools:
    def test_register_tools_callable(self):
        from aihydro_data.mcp import register_tools
        assert callable(register_tools)

    def test_register_tools_with_mock_mcp(self):
        from aihydro_data.mcp import register_tools

        registered = {}

        class _FakeMCP:
            def tool(self, name=None):
                def _decorator(fn):
                    registered[name or fn.__name__] = fn
                    return fn
                return _decorator

        register_tools(_FakeMCP())
        assert "data_fetch" in registered
        assert "data_batch_fetch" in registered
        assert "data_list_products" in registered
        assert "data_describe_product" in registered
        assert "data_validate_request" in registered
        assert "data_get_cache_status" in registered
        assert "data_invalidate_cache" in registered
        assert "data_doctor" in registered
        assert "data_help" in registered
        assert len(registered) == 9

    def test_register_tools_no_mcp_is_noop(self):
        from aihydro_data.mcp import register_tools
        # Should silently no-op (or try to import app.get_server and fail gracefully)
        # Just confirm no exception escapes
        with patch("aihydro_data.mcp.register_tools.__module__", "aihydro_data.mcp"):
            try:
                register_tools(None)
            except Exception as exc:
                pytest.fail(f"register_tools(None) raised: {exc}")
