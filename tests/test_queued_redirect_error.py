"""U4: the queued-backend redirect is an MCP error, not a success-shaped dict.

``data_fetch`` used to return ``{"error": False, "redirect": True, ...}``, which a client reads as
a successful call. It now raises ``fastmcp.exceptions.ToolError`` whose text is the JSON envelope
(``error: true``, ``code: USE_ASYNC_TOOL``), with no ``redirect`` key. aihydro-data does not import
the host's ``StructuredToolError`` (that would be an upward dependency); the host recognises a plain
``ToolError`` carrying ``{"error": true, "code": ...}``.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastmcp")
from fastmcp.exceptions import ToolError  # noqa: E402


def test_queued_redirect_raises_tool_error_with_envelope(monkeypatch):
    import aihydro_data.mcp as m

    monkeypatch.setattr(m, "_would_route_to_queued_source", lambda *a, **k: (True, "cds_glofas"))
    with pytest.raises(ToolError) as ei:
        m._data_fetch(variable="streamflow", geometry=(52.5, 13.4), start="2020-01-01", end="2020-01-31")
    body = json.loads(str(ei.value))
    assert body["error"] is True
    assert body["code"] == "USE_ASYNC_TOOL"
    assert "redirect" not in body
    assert body["next_tools"] == ["data_fetch_background", "get_data_fetch_result"]
    assert body["example_call"]["tool"] == "data_fetch_background"
    assert body["example_call"]["arguments"]["variable"] == "streamflow"


def test_queued_redirect_without_fastmcp_returns_the_error_dict(monkeypatch):
    import builtins

    import aihydro_data.mcp as m

    monkeypatch.setattr(m, "_would_route_to_queued_source", lambda *a, **k: (True, "cds_glofas"))
    real_import = builtins.__import__

    def no_fastmcp(name, *a, **k):
        if name.startswith("fastmcp"):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fastmcp)
    out = m._data_fetch(variable="streamflow", geometry=(52.5, 13.4), start="2020-01-01", end="2020-01-31")
    assert out["error"] is True and out["code"] == "USE_ASYNC_TOOL" and "redirect" not in out
