"""Regression tests for issue #697 — searchable global MCP tool inventory."""
import json
import sys
import types
from unittest.mock import MagicMock, patch

from api.routes import (
    _handle_mcp_servers_list,
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
    @patch("api.routes._active_profile_mcp_config_data")
    @patch("api.routes.get_active_hermes_home")
    def test_endpoint_returns_sanitized_registered_mcp_tools(
        self, mock_home, mock_cfg, mock_runtime, tmp_path
    ):
        mock_home.return_value = sentinel_home = tmp_path / "profiles" / "work"
        mock_cfg.return_value = {
            "mcp_servers": {
                "web-reader": {"url": "http://localhost:3001/mcp", "headers": {"Authorization": "Bearer secret-token"}},
                "disabled": {"command": "disabled-cmd", "enabled": False},
            }
        }
        mock_runtime.return_value = {
            "web-reader": {
                "profile_home": str(sentinel_home),
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
            "disabled": {
                "profile_home": str(sentinel_home),
                "connected": False,
                "tools": 0,
            },
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
        mock_cfg.assert_called_once_with()
        raw = json.dumps(payload)
        assert "secret-token" not in raw
        assert "default" not in raw
        assert "Authorization" not in raw

    @patch("api.routes._active_profile_allows_ownerless_mcp_inventory", return_value=True)
    @patch("api.routes._mcp_runtime_status_by_name")
    @patch("api.routes._active_profile_mcp_config_data")
    @patch("api.routes.get_active_hermes_home")
    def test_legacy_runtime_status_without_owner_metadata_still_lists_tools(
        self, mock_home, mock_cfg, mock_runtime, mock_allow_ownerless, tmp_path
    ):
        mock_home.return_value = tmp_path / "profiles" / "work"
        mock_cfg.return_value = {
            "mcp_servers": {"legacy": {"command": "legacy-cmd"}}
        }
        mock_runtime.return_value = {
            "legacy": {
                "name": "legacy",
                "connected": True,
                "tools": [{"name": "legacy_tool"}],
            }
        }

        h = _make_handler()
        _handle_mcp_tools_list(h)
        payload = _json_payload(h)

        assert payload["source"] == "mcp_runtime_status"
        assert [tool["name"] for tool in payload["tools"]] == ["legacy_tool"]
        assert payload["tools"][0]["active"] is True
        mock_allow_ownerless.assert_called_once_with()

    @patch("api.routes._active_profile_allows_ownerless_mcp_inventory", return_value=True)
    @patch("api.routes._mcp_runtime_status_by_name")
    @patch("api.routes._active_profile_mcp_config_data")
    @patch("api.routes.get_active_hermes_home")
    def test_legacy_registry_without_owner_metadata_still_lists_tools(
        self, mock_home, mock_cfg, mock_runtime, mock_allow_ownerless, monkeypatch, tmp_path
    ):
        mock_home.return_value = tmp_path / "profiles" / "work"
        mock_cfg.return_value = {
            "mcp_servers": {"legacy": {"command": "legacy-cmd"}}
        }
        mock_runtime.return_value = {}

        fake_registry_mod = types.ModuleType("tools.registry")

        class _Registry:
            def get_all_tool_names(self):
                return ["legacy_tool"]

            def get_toolset_for_tool(self, name):
                return "mcp-legacy"

            def get_schema(self, name):
                return {"name": name, "description": "Legacy tool"}

        fake_registry_mod.registry = _Registry()
        monkeypatch.setitem(sys.modules, "tools.registry", fake_registry_mod)

        h = _make_handler()
        _handle_mcp_tools_list(h)
        payload = _json_payload(h)

        assert payload["source"] == "tool_registry"
        assert [tool["name"] for tool in payload["tools"]] == ["legacy_tool"]
        mock_allow_ownerless.assert_called_once_with()

    def test_ownerless_runtime_status_adapter_keeps_empty_owner_and_named_profiles_drop_it(
        self, monkeypatch
    ):
        from api import routes

        fake_mcp_mod = types.ModuleType("tools.mcp_tool")
        fake_mcp_mod.get_mcp_status = lambda: [
            {
                "name": "legacy",
                "profile_home": "",
                "connected": True,
                "tools": [{"name": "legacy_tool"}],
            }
        ]
        monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake_mcp_mod)

        statuses = routes._mcp_runtime_status_by_name()

        assert list(statuses) == [("", "legacy")]
        assert routes._mcp_runtime_status_for_server(
            statuses,
            "/tmp/hermes/profiles/work",
            "legacy",
            allow_ownerless=True,
        )["connected"] is True
        assert routes._mcp_runtime_status_for_server(
            statuses,
            "/tmp/hermes/profiles/work",
            "legacy",
            allow_ownerless=False,
        ) is None

    @patch("api.routes._mcp_runtime_status_by_name", return_value={})
    @patch("api.routes.get_active_hermes_home")
    def test_named_profile_does_not_use_ownerless_registry_for_same_server_name(
        self, mock_home, mock_runtime, monkeypatch, tmp_path
    ):
        from api import routes

        work_home = tmp_path / "profiles" / "work"
        mock_home.return_value = work_home
        monkeypatch.setattr(
            routes,
            "_active_profile_mcp_config_data",
            lambda: {"mcp_servers": {"shared": {"command": "work-shared"}}},
        )
        monkeypatch.setattr(routes, "get_active_profile_name", lambda: "work")
        monkeypatch.setattr(routes, "_is_root_profile", lambda name: False)
        monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)

        fake_registry_mod = types.ModuleType("tools.registry")

        class _Registry:
            def get_all_tool_names(self):
                return ["shared_tool"]

            def get_toolset_for_tool(self, name):
                return "mcp-shared"

            def get_schema(self, name):
                return {"name": name, "description": "Ownerless shared tool"}

        fake_registry_mod.registry = _Registry()
        monkeypatch.setitem(sys.modules, "tools.registry", fake_registry_mod)

        h = _make_handler()
        _handle_mcp_tools_list(h)
        payload = _json_payload(h)

        assert payload["source"] == "none"
        assert payload["tools"] == []
        mock_runtime.assert_called_once_with()

    def test_root_request_does_not_use_ownerless_named_profile_agent_inventory(
        self, monkeypatch, tmp_path
    ):
        from api import routes

        root_home = tmp_path / "default"
        work_home = tmp_path / "profiles" / "work"
        root_home.mkdir(parents=True)
        work_home.mkdir(parents=True)
        monkeypatch.setattr(routes, "get_active_hermes_home", lambda: root_home)
        monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
        monkeypatch.setattr(routes, "_is_root_profile", lambda name: name == "default")
        monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
        monkeypatch.setattr(
            routes,
            "_active_profile_mcp_config_data",
            lambda: {"mcp_servers": {"shared": {"command": "root-shared"}}},
        )

        fake_mcp_mod = types.ModuleType("tools.mcp_tool")
        fake_mcp_mod.get_mcp_status = lambda: [
            {
                "name": "shared",
                "connected": True,
                "tools": 3,
            }
        ]
        monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake_mcp_mod)

        fake_registry_mod = types.ModuleType("tools.registry")

        class _Registry:
            def get_all_tool_names(self):
                return ["shared_tool"]

            def get_toolset_for_tool(self, name):
                return "mcp-shared"

            def get_schema(self, name):
                return {"name": name, "description": "Ownerless named-profile tool"}

        fake_registry_mod.registry = _Registry()
        monkeypatch.setitem(sys.modules, "tools.registry", fake_registry_mod)

        servers_handler = MagicMock()
        servers_handler.path = "/api/mcp/servers"
        servers_handler.command = "GET"
        _handle_mcp_servers_list(servers_handler)
        servers = _json_payload(servers_handler)["servers"]

        assert servers[0]["name"] == "shared"
        assert servers[0]["active"] is False
        assert servers[0]["tool_count"] is None

        tools_handler = _make_handler()
        _handle_mcp_tools_list(tools_handler)
        payload = _json_payload(tools_handler)

        assert payload["source"] == "none"
        assert payload["tools"] == []
        assert payload["unavailable_servers"] == ["shared"]

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
