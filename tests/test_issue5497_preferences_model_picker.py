"""Regression coverage for #5497 Preferences default model picker parity."""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", src)
    assert match, f"{name} not found"
    start = match.end()
    depth = 1
    pos = start
    while pos < len(src) and depth:
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"{name} body did not terminate"
    return src[start : pos - 1]


def test_preferences_default_model_field_keeps_source_select_and_adds_rich_picker_shell():
    field_start = INDEX_HTML.index('id="settingsModelChip"')
    field_end = INDEX_HTML.index('data-i18n="settings_desc_model"', field_start)
    field = INDEX_HTML[field_start:field_end]

    assert 'id="settingsModelChip"' in field
    assert 'id="settingsModel"' in field
    assert 'id="settingsModelDropdown"' in field
    assert 'class="model-dropdown settings-model-dropdown"' in field
    assert 'id="mainAdvancedBtn"' in field
    assert field.index('id="settingsModelChip"') < field.index('id="mainAdvancedBtn"')


def test_settings_model_select_is_hidden_but_remains_advanced_options_source():
    assert ".model-advanced-row #settingsModel" in STYLE_CSS
    assert "display:none;" in STYLE_CSS[STYLE_CSS.index(".model-advanced-row #settingsModel") : STYLE_CSS.index(".model-advanced-btn")]

    bind_body = _function_body(PANELS_JS, "_bindMainAdvancedOptionsButton")
    assert "const modelSel=$('settingsModel');" in bind_body
    assert "modelSel.parentElement" in bind_body
    assert "mainAdvancedBtn" in bind_body


def test_render_model_dropdown_accepts_settings_select_and_callbacks():
    body = _function_body(UI_JS, "renderModelDropdown")

    assert "opts.dropdownId||'composerModelDropdown'" in body
    assert "opts.selectId||'modelSelect'" in body
    assert "opts.selectModel" in body
    assert "opts.closeDropdown" in body
    assert "selectFromDropdown(m.value" in body
    assert "row.onclick=()=>selectModelFromDropdown" not in body
    assert "closeDropdown();return;" in body
    assert "opts.forceOpenKey||'composer'" in body
    assert "renderModelDropdown(opts)" in body
    assert "opts.scopeNoteText||" in body


def test_preferences_picker_routes_through_shared_renderer_without_panel_copy():
    mount_body = _function_body(UI_JS, "openSettingsModelDropdown")
    select_body = _function_body(UI_JS, "selectSettingsModelFromDropdown")
    panels_model_block = PANELS_JS[
        PANELS_JS.index("const modelSel=$('settingsModel');") : PANELS_JS.index("// Auxiliary models", PANELS_JS.index("const modelSel=$('settingsModel');"))
    ]

    assert "renderModelDropdown({" in mount_body
    assert "dropdownId:'settingsModelDropdown'" in mount_body
    assert "selectId:'settingsModel'" in mount_body
    assert "selectModel:selectSettingsModelFromDropdown" in mount_body
    assert "scopeNoteText:t('settings_desc_model')" in mount_body
    assert "_ensureModelOptionInDropdown(value,sel,provider)" in select_body
    assert "sel.dispatchEvent(new Event('change',{bubbles:true}))" in select_body
    assert "syncSettingsModelChip" in select_body
    assert "closeSettingsModelDropdown" in panels_model_block
    assert "mountSettingsModelPicker" in panels_model_block
    assert "_settingsChipSyncBound" in panels_model_block
    assert ".model-opt" not in PANELS_JS
    assert "model-search-input" not in PANELS_JS


def test_preferences_picker_refreshes_existing_save_and_dirty_contracts():
    autosave_body = _function_body(PANELS_JS, "_autosavePreferencesSettings")
    save_body = _function_body(PANELS_JS, "saveSettings")
    refresh_body = _function_body(UI_JS, "_refreshOpenModelDropdown")

    assert "_captureModelDropdownSelection(modelSel)" in autosave_body
    assert "_captureModelDropdownSelection($('settingsModel'))" in save_body
    assert "await api('/api/default-model',{method:'POST',body:JSON.stringify({model,provider:modelState.model_provider||null})});" in save_body
    assert "settingsModelDropdown" in refresh_body
    assert "selectModel:selectSettingsModelFromDropdown" in refresh_body


def test_preferences_picker_ux_fixes_label_touchfocus_shadow():
    """Fable SHIP-WITH-UX-FIXES fold-in (#5502):
    (1) the field label targets the interactive chip, not the hidden native select;
    (2) the search auto-focus is skipped on coarse-pointer (no mobile keyboard pop);
    (3) the downward-opening Preferences dropdown casts its shadow downward.
    """
    # (1) label association -> the visible/interactive chip
    assert 'for="settingsModelChip"' in INDEX_HTML
    assert 'for="settingsModel"' not in INDEX_HTML  # no longer points at the hidden select

    # (2) coarse-pointer guard around the search auto-focus in the settings open path,
    #     passed through to renderModelDropdown so its OWN initial focus is suppressed too.
    open_body = _function_body(UI_JS, "openSettingsModelDropdown")
    assert "matchMedia('(pointer: coarse)')" in open_body
    assert "autoFocusSearch:!_coarsePointer" in open_body
    guard_idx = open_body.index("matchMedia('(pointer: coarse)')")
    render_idx = open_body.index("renderModelDropdown(")
    assert guard_idx < render_idx, "coarse-pointer must be computed before the render call"
    # renderModelDropdown honors the option and defaults it true (composer unchanged).
    render_body = _function_body(UI_JS, "renderModelDropdown")
    assert "opts.autoFocusSearch!==false" in render_body
    assert "if(_autoFocusSearch||_hadFocus) _si.focus();" in render_body
    # Focus is still restored during typing (touch user in the search) — the
    # suppression must NOT drop focus mid-word on the per-keystroke re-render.
    assert "document.activeElement===_si" in render_body
    # The OPEN-picker refresh path (late live-model fetch) must also suppress focus
    # on touch for the settings branch, or the keyboard pops after opening.
    refresh_body = _function_body(UI_JS, "_refreshOpenModelDropdown")
    settings_branch = refresh_body[refresh_body.index("settingsModelDropdown"):]
    assert "autoFocusSearch:!_coarsePointer" in settings_branch
    assert "matchMedia('(pointer: coarse)')" in settings_branch

    # (3) downward shadow override on the settings dropdown block
    block_start = STYLE_CSS.index(".settings-model-dropdown{")
    block = STYLE_CSS[block_start : STYLE_CSS.index("}", block_start) + 1]
    assert "box-shadow:0 4px 24px" in block, "settings dropdown should cast its shadow downward"
