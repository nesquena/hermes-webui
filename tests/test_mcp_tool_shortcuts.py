"""Static regression coverage for MCP tool shortcuts in Settings → System."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_mcp_tools_has_shortcuts_mount_before_search():
    assert 'id="mcpToolShortcuts" class="mcp-tool-shortcuts"' in INDEX_HTML
    assert INDEX_HTML.index('id="mcpToolShortcuts"') < INDEX_HTML.index('id="mcpToolSearch"')


def test_mcp_tool_shortcuts_are_local_storage_backed_and_re_rendered():
    assert "const MCP_TOOL_SHORTCUTS_STORAGE_KEY='hermes-webui-mcp-tool-shortcuts'" in PANELS_JS
    assert "function _loadMcpToolShortcutKeys()" in PANELS_JS
    assert "function _saveMcpToolShortcutKeys(keys)" in PANELS_JS
    assert "localStorage.getItem(MCP_TOOL_SHORTCUTS_STORAGE_KEY)" in PANELS_JS
    assert "localStorage.setItem(MCP_TOOL_SHORTCUTS_STORAGE_KEY,JSON.stringify([...keys]))" in PANELS_JS
    assert "function toggleMcpToolShortcut(toolKey)" in PANELS_JS
    assert "_renderMcpTools(_mcpToolsCache,input?input.value:'')" in PANELS_JS


def test_mcp_tool_shortcut_insert_targets_composer_not_direct_tool_execution():
    assert "function insertMcpToolShortcut(toolKey)" in PANELS_JS
    assert "const input=$('msg');" in PANELS_JS
    assert "input.value=current?`${current}\\n\\n${prompt}`:prompt;" in PANELS_JS
    assert "input.dispatchEvent(new Event('input',{bubbles:true}))" in PANELS_JS
    assert "api('/api/mcp/tools')" in PANELS_JS
    assert "/api/mcp/call" not in PANELS_JS


def test_mcp_tool_prompt_mentions_required_parameters_and_missing_values():
    assert "function _mcpToolPrompt(tool)" in PANELS_JS
    assert ".filter(p=>p&&p.required&&p.name)" in PANELS_JS
    assert "Required parameters: ${required.join(', ')}" in PANELS_JS
    assert "Ask me for any missing values before calling it." in PANELS_JS


def test_mcp_tool_rows_have_use_and_pin_buttons_with_safe_js_args():
    assert "function _mcpToolShortcutJsArg(value)" in PANELS_JS
    assert "JSON.stringify(String(value||'')).replace(/</g,'\\\\u003c')" in PANELS_JS
    assert "onclick=\"insertMcpToolShortcut(${_mcpToolShortcutJsArg(toolKey)})\"" in PANELS_JS
    assert "onclick=\"toggleMcpToolShortcut(${_mcpToolShortcutJsArg(toolKey)})\"" in PANELS_JS
    assert "aria-pressed=\"${pinned?'true':'false'}\"" in PANELS_JS


def test_mcp_tool_shortcut_chips_and_actions_are_styled():
    for selector in [
        ".mcp-tool-shortcuts",
        ".mcp-tool-shortcut-chip",
        ".mcp-tool-action-btn",
        ".mcp-tool-action-btn[aria-pressed=\"true\"]",
    ]:
        assert selector in STYLE_CSS


def test_mcp_tool_shortcut_i18n_keys_exist_in_all_locale_blocks():
    keys = [
        "mcp_tool_shortcuts_label",
        "mcp_tool_shortcuts_empty",
        "mcp_tool_shortcut_use",
        "mcp_tool_shortcut_pin",
        "mcp_tool_shortcut_unpin",
        "mcp_tool_shortcut_inserted",
        "mcp_tool_shortcut_insert_title",
    ]
    locale_count = I18N_JS.count("mcp_tools_next_page_aria:")
    assert locale_count >= 10
    for key in keys:
        assert I18N_JS.count(f"{key}:") == locale_count


def test_changelog_mentions_mcp_tool_shortcuts():
    assert "MCP Tools now supports pinned tool shortcuts" in CHANGELOG
    assert "#3042" in CHANGELOG
