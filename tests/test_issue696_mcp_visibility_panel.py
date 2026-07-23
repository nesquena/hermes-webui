"""Regression tests for issue #696 — MCP server visibility panel.

The panel started as a read-only MVP (list + enable/disable toggle only).
It later gained full CRUD + a connection test (Add/Edit/Delete/Test) — see
tests/test_issue538_mcp_management.py for the backend and
tests/test_mcp_server_test_route.py for the test-connection route. These
tests now assert the CRUD UI is present instead of asserting its absence.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_settings_system_panel_contains_mcp_management_section():
    html = read("static/index.html")
    assert 'data-i18n="mcp_servers_title"' in html
    assert 'id="mcpServerList"' in html
    assert 'class="mcp-restart-hint"' in html
    assert 'id="mcpAddServerBtn"' in html
    assert 'onclick="openMcpServerCreate()"' in html
    assert 'id="mcpServerFormWrap"' in html
    assert 'id="mcpWriteCapabilityHint"' in html


def test_mcp_panel_renders_status_badges_tool_counts_and_empty_error_states():
    js = read("static/panels.js")
    assert "function _mcpStatusLabel" in js
    assert "mcp-status-badge" in js
    assert "mcp-tool-count" in js
    assert "mcp-empty-state" in js
    assert "mcp-error-state" in js
    assert "toggleMcpServer" in js
    assert "mcp-toggle-btn" in js
    assert "api('/api/mcp/servers')" in js


def test_mcp_panel_supports_add_edit_delete_and_test():
    js = read("static/panels.js")
    assert "function openMcpServerCreate" in js
    assert "function openMcpServerEdit" in js
    assert "function saveMcpServerForm" in js
    assert "function deleteMcpServer" in js
    assert "function testMcpServer" in js
    assert "api('/api/mcp/servers/'+encodeURIComponent(name),{method:'PUT'" in js
    assert "method:'DELETE'" in js
    assert "/test'" in js


def test_mcp_panel_disables_write_controls_when_not_writable():
    # Convention: a GET response bearing on write capability carries a
    # `writable` bool; on false the frontend disables the write controls and
    # shows a persistent hint div (not hidden) rather than a guessing UI.
    js = read("static/panels.js")
    assert "_mcpApplyWriteCapability" in js
    assert "r.writable" in js
    assert "mcpWriteCapabilityHint" in js


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
