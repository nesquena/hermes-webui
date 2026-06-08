"""Regression tests for issue #697 — searchable global MCP tool inventory."""
import json
from unittest.mock import MagicMock, patch

from api.routes import (
    _handle_mcp_tools_list,
    _mcp_schema_summary,
    _mcp_tool_summary,
)


def _make_handler():
    h = MagicMock()
    h.path = "/api/mcp/tools"
    h.command = "GET"
    return h


def _json_payload(handler):
    body = handler.wfile.write.call_args[0][0]
    return json.loads(body.decode("utf-8"))


def _read(relative_path: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / relative_path).read_text(encoding="utf-8")


class TestMcpToolInventoryApi:
    @patch("api.routes._mcp_runtime_status_by_name")
    @patch("api.routes.get_config")
    def test_endpoint_returns_sanitized_registered_mcp_tools(self, mock_cfg, mock_runtime):
        mock_cfg.return_value = {
            "mcp_servers": {
                "web-reader": {"url": "http://localhost:3001/mcp", "headers": {"Authorization": "Bearer secret-token"}},
                "disabled": {"command": "disabled-cmd", "enabled": False},
            }
        }
        mock_runtime.return_value = {
            "web-reader": {
                "connected": True,
                "tools": [
                    {
                        "name": "mcp_web_reader_fetch_page",
                        "description": "Fetch a page without leaking Authorization: Bearer secret-token",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "URL to fetch", "default": "https://token.example/?key=secret-token"},
                                "limit": {"type": "integer", "description": "Maximum bytes"},
                            },
                            "required": ["url"],
                        },
                    }
                ],
            },
            "disabled": {"connected": False, "tools": 0},
        }
        h = _make_handler()
        _handle_mcp_tools_list(h)
        payload = _json_payload(h)

        assert payload["source"] == "mcp_runtime_status"
        assert payload["total"] == 1
        assert payload["tools"][0]["name"] == "mcp_web_reader_fetch_page"
        assert payload["tools"][0]["server"] == "web-reader"
        assert payload["tools"][0]["status"] == "active"
        assert payload["tools"][0]["active"] is True
        assert payload["tools"][0]["enabled"] is True
        assert payload["tools"][0]["schema_summary"] == [
            {"name": "url", "type": "string", "required": True, "description": "URL to fetch"},
            {"name": "limit", "type": "integer", "required": False, "description": "Maximum bytes"},
        ]
        raw = json.dumps(payload)
        assert "secret-token" not in raw
        assert "default" not in raw
        assert "Authorization" not in raw

    def test_schema_summary_uses_parameter_names_types_required_and_descriptions_only(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text", "examples": ["secret"]},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tag filters"},
            },
            "required": ["query"],
        }
        assert _mcp_schema_summary(schema) == [
            {"name": "query", "type": "string", "required": True, "description": "Search text"},
            {"name": "tags", "type": "array", "required": False, "description": "Tag filters"},
        ]

    def test_tool_summary_rejects_non_dict_schema_and_redacts_description(self):
        summary = _mcp_tool_summary(
            "search",
            {"description": "use API_KEY=super-secret", "parameters": "not-a-dict"},
            {"name": "search", "status": "configured", "enabled": True, "active": False},
        )
        assert summary["description"] != "use API_KEY=super-secret"
        assert "super-secret" not in summary["description"]
        assert summary["schema_summary"] == []


class TestMcpToolInventoryUi:
    def test_system_settings_contains_searchable_global_mcp_tool_section(self):
        html = _read("static/index.html")
        assert 'data-i18n="mcp_tools_title"' in html
        assert 'id="mcpToolSearch"' in html
        assert 'id="mcpToolList"' in html
        assert 'oninput="filterMcpTools()"' in html

    def test_panels_js_loads_tools_and_filters_name_server_description(self):
        js = _read("static/panels.js")
        assert "function loadMcpTools" in js
        assert "api('/api/mcp/tools')" in js
        assert "function filterMcpTools" in js
        assert "_filterMcpToolsForSearch" in js
        assert "tool.name" in js
        assert "tool.server" in js
        assert "tool.description" in js
        assert "mcp-tool-empty-state" in js
        assert "mcp-tool-error-state" in js

    def test_mcp_tool_i18n_keys_are_present(self):
        i18n = _read("static/i18n.js")
        for key in [
            "mcp_tools_title",
            "mcp_tools_desc",
            "mcp_tools_search_placeholder",
            "mcp_tools_no_tools",
            "mcp_tools_no_matches",
            "mcp_tools_load_failed",
            "mcp_tools_schema_empty",
        ]:
            assert key in i18n
