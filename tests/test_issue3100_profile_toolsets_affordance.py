from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def test_toolsets_dropdown_fetches_configured_mcp_server_names():
    load = _function_body(UI_JS, "function _loadToolsetsCatalog")
    normalize = _function_body(UI_JS, "function _normalizeToolsetsCatalog")
    toggle = _function_body(UI_JS, "function toggleToolsetsDropdown")

    assert "api('/api/mcp/servers')" in load
    assert "payload.servers" in normalize
    assert "server.name" in normalize
    assert "_toolsetsCatalog = false;" in load
    assert "return [];" in load
    assert "_loadToolsetsCatalog().then(function()" in toggle


def test_async_catalog_refresh_preserves_manual_toolset_input():
    toggle = _function_body(UI_JS, "function toggleToolsetsDropdown")

    assert "const state = $('toolsetsDropdownState');" in toggle
    assert "const input = $('toolsetsInput');" in toggle
    assert "_renderToolsetsPresetSections({ state, input });" in toggle


def test_null_state_is_labeled_as_active_profile_defaults():
    apply_chip = _function_body(UI_JS, "function _applyToolsetsChip")
    render_sections = _function_body(UI_JS, "function _renderToolsetsPresetSections")

    assert "null = active profile defaults" in UI_JS
    assert "session_toolsets_profile_defaults" in apply_chip
    assert "session_toolsets_global" not in apply_chip
    assert "session_toolsets_profile_defaults" in render_sections
    assert "Global (default)" not in I18N_JS


def test_profile_default_action_saves_null_override():
    click_start = UI_JS.index("document.addEventListener('click', function(e)")
    clear_start = UI_JS.index("if (e.target.closest('#toolsetsClearBtn'))")
    click_block = UI_JS[click_start : UI_JS.index("});", clear_start) + 3]

    assert "toolsetsProfileDefaultsBtn" in UI_JS
    assert "session_toolsets_use_profile_defaults" in UI_JS
    assert "if (e.target.closest('#toolsetsProfileDefaultsBtn'))" in click_block
    assert "_applySessionToolsets(null);" in click_block
    assert "static/index.html" not in UI_JS
    assert "toolsetsProfileDefaultsBtn" not in INDEX_HTML


def test_custom_checkbox_and_manual_selection_reuse_existing_apply_path():
    render_sections = _function_body(UI_JS, "function _renderToolsetsPresetSections")
    change_start = UI_JS.index("document.addEventListener('change', function(e)")
    change_block = UI_JS[change_start : UI_JS.index("});", change_start) + 3]
    click_start = UI_JS.index("document.addEventListener('click', function(e)")
    apply_block = UI_JS[click_start : UI_JS.index("// Clear button", click_start)]

    assert "toolsets-server-checkbox" in render_sections
    assert "checkbox.value = name" in render_sections
    assert "input.value = checked.concat(manual).join(', ')" in change_block
    assert "const toolsets = raw.split(',').map(s => s.trim()).filter(Boolean);" in apply_block
    assert "_applySessionToolsets(toolsets);" in apply_block


def test_toolsets_affordance_i18n_keys_exist_in_locale_blocks():
    keys = [
        "session_toolsets_profile_defaults",
        "session_toolsets_use_profile_defaults",
        "session_toolsets_configured_servers",
        "session_toolsets_loading_servers",
        "session_toolsets_no_configured_servers",
    ]
    for key in keys:
        assert I18N_JS.count(f"{key}:") >= 8, f"missing locale entries for {key}"
    assert "session_toolsets_custom:'Custom override'" in I18N_JS
    assert "session_toolsets_desc:'Use active profile defaults or choose a custom toolset list for this session'" in I18N_JS


def test_toolsets_dropdown_distinguishes_failed_catalog_loads_from_loading():
    render_sections = _function_body(UI_JS, "function _renderToolsetsPresetSections")

    assert "_toolsetsCatalog === null" in render_sections
    assert "_toolsetsCatalog === false" in render_sections
    assert "session_toolsets_loading_servers" in render_sections
    assert "mcp_load_failed" in render_sections


def test_mcp_server_panel_refreshes_cached_toolsets_catalog():
    invalidate = _function_body(UI_JS, "function invalidateToolsetsCatalog")
    load_servers = _function_body(PANELS_JS, "function loadMcpServers")
    toggle_server = _function_body(PANELS_JS, "function toggleMcpServer")

    assert "_toolsetsCatalog = payload && Array.isArray(payload.servers)" in invalidate
    assert "_normalizeToolsetsCatalog(payload)" in invalidate
    assert ": null" in invalidate
    assert "window.invalidateToolsetsCatalog = invalidateToolsetsCatalog" in UI_JS
    assert "_refreshMcpToolsetsCatalog(r);" in load_servers
    assert "_refreshMcpToolsetsCatalog();" in toggle_server
