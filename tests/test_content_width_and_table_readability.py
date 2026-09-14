"""Contracts for the composer content-width picker and markdown tables."""

import re
from pathlib import Path

import api.config as config


ROOT = Path(__file__).resolve().parents[1]


def read_static(name):
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def test_content_width_is_a_server_validated_appearance_setting():
    assert config._SETTINGS_DEFAULTS["content_width"] == "default"
    assert config._SETTINGS_ENUM_VALUES["content_width"] == {"default", "wide", "full"}


def test_composer_width_picker_has_one_active_control_and_three_options():
    html = read_static("index.html")
    assert html.count('id="composerContentWidthBtn"') == 1
    assert html.count('data-content-width-value="default"') == 1
    assert html.count('data-content-width-value="wide"') == 1
    assert html.count('data-content-width-value="full"') == 1
    assert 'aria-haspopup="listbox"' in html
    assert 'id="composerContentWidthPopup"' in html
    assert 'localStorage.getItem(\'hermes-content-width\')' in html


def test_width_picker_is_anchored_to_the_left_composer_controls():
    html = read_static("index.html")
    left_start = html.index('<div class="composer-left">')
    right_start = html.index('<div class="composer-right">')
    picker_start = html.index('<div class="composer-content-width-wrap"')
    assert left_start < picker_start < right_start
    assert 'class="icon-btn composer-content-width-btn has-tooltip"' in html
    assert 'flex:0 0 34px' in read_static("style.css")


def test_width_picker_applies_immediately_and_persists_through_appearance_autosave():
    boot = read_static("boot.js")
    panels = read_static("panels.js")
    assert "const _CONTENT_WIDTH_MODES=new Set(['default','wide','full'])" in boot
    assert "function _applyContentWidth(width)" in boot
    assert "document.documentElement.dataset.contentWidth=next" in boot
    assert "localStorage.setItem('hermes-content-width',next)" in boot
    assert "_scheduleAppearanceAutosave()" in boot
    assert "content_width: localStorage.getItem('hermes-content-width') || 'default'" in panels
    assert "saved.content_width" in panels


def test_width_picker_is_keyboard_and_outside_click_usable():
    boot = read_static("boot.js")
    assert "event.key==='Escape'" in boot
    assert "event.key==='ArrowRight'||event.key==='ArrowDown'" in boot
    assert "event.key==='ArrowLeft'||event.key==='ArrowUp'" in boot
    assert "if(!wrap.contains(event.target)) _setContentWidthPickerOpen(false)" in boot
    assert "function _positionContentWidthPicker()" in boot
    assert "popup.style.left=`${Math.round(left)}px`" in boot
    assert "position:fixed" in read_static("style.css")


def test_markdown_tables_use_a_scroll_surface_and_readable_wrapping():
    messages = read_static("messages.js")
    style = read_static("style.css")
    helper = messages[messages.index("function enhanceMarkdownTables(root)"):messages.index("function _markdownTableText")]

    assert "wrap.className='md-table-scroll'" in helper
    assert "wrap.appendChild(table)" in helper
    assert ".md-table-scroll{max-width:100%;overflow-x:auto" in style
    assert ".md-table-scroll table{width:max-content;min-width:100%" in style
    assert "overflow-wrap:normal;word-break:normal;hyphens:none" in style
    # Default-skin tables keep the mono contract (upstream typography test); the prose skins
    # intentionally override to conversation font for readability.
    assert ".msg-body table { font-family:var(--font-mono); }" in style
    assert ':root[data-skin="github"] .msg-body table { font-family:var(--font-conversation)!important; }' in style
    assert "table-layout:fixed" not in style


def test_width_presets_are_css_driven_without_global_cell_nowrap():
    style = read_static("style.css")
    assert ':root[data-content-width="wide"] { --msg-max: 1100px; }' in style
    assert ':root[data-content-width="full"] { --msg-max: 100%; }' in style
    assert ".messages-inner { max-width: var(--msg-max); }" in style
    assert not re.search(r"\.msg-body\s+(?:th|td)\s*\{[^}]*white-space\s*:\s*nowrap", style)
