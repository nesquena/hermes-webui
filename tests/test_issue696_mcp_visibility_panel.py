"""Regression tests for issue #696 — MCP server visibility panel MVP."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_settings_system_panel_contains_readonly_mcp_visibility_section():
    html = read("static/index.html")
    assert 'data-i18n="mcp_servers_title"' in html
    assert 'id="mcpServerList"' in html
    assert 'class="mcp-restart-hint"' in html
    assert 'id="mcpAddFormWrap"' not in html
    assert 'onclick="showMcpAddForm()"' not in html


def test_mcp_panel_renders_status_badges_tool_counts_and_empty_error_states():
    js = read("static/panels.js")
    assert "function _mcpStatusLabel" in js
    assert "mcp-status-badge" in js
    assert "mcp-tool-count" in js
    assert "mcp-empty-state" in js
    assert "mcp-error-state" in js
    assert "mcp_toggle_followup" in js
    assert "api('/api/mcp/servers')" in js
    assert "mcp-delete-btn" not in js
    assert "showMcpAddForm" not in js
    assert "saveMcpServer" not in js


def test_mcp_i18n_includes_visibility_status_labels():
    i18n = read("static/i18n.js")
    for key in [
        "mcp_status_active",
        "mcp_status_configured",
        "mcp_status_disabled",
        "mcp_status_invalid_config",
        "mcp_tool_count",
        "mcp_enabled_yes",
        "mcp_enabled_no",
        "mcp_toggle_followup",
    ]:
        assert key in i18n
