import pathlib
import re


STYLE_CSS = (pathlib.Path(__file__).parent.parent / "static" / "style.css").read_text(encoding="utf-8")
UI_JS = (pathlib.Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
COMPACT_CSS = re.sub(r"\s+", "", STYLE_CSS)


def test_tool_card_toggle_uses_transformable_layout_and_transition():
    assert ".tool-card-toggle{" in COMPACT_CSS
    assert "display:inline-flex" in COMPACT_CSS
    assert "transition:transform.18sease" in COMPACT_CSS


def test_tool_card_detail_uses_transitionable_collapsed_state():
    assert ".tool-card-detail{display:block;max-height:0;opacity:0;overflow:hidden;" in COMPACT_CSS
    assert re.search(
        r"\.tool-card\.open\s+\.tool-card-detail\s*\{[^}]*max-height:\s*600px;[^}]*opacity:\s*1;",
        STYLE_CSS,
    )
    # Open state must set overflow to auto so the inner <pre> scroll is not clipped (#1170).
    assert re.search(
        r"\.tool-card\.open\s+\.tool-card-detail\s*\{[^}]*overflow:\s*auto;",
        STYLE_CSS,
    )


def test_thinking_card_toggle_and_body_use_animation_friendly_state():
    assert ".thinking-card-toggle{margin-left:auto;font-size:10px;display:inline-flex;" in COMPACT_CSS
    assert ".thinking-card-header{display:flex;align-items:center;gap:8px;" in COMPACT_CSS
    # Body uses div default (display:block); canonical rule lives in the
    # consolidated block. Open state caps at 260px (intentional "quieter" sizing).
    assert ".thinking-card-body{max-height:0;opacity:0;overflow:hidden;" in COMPACT_CSS
    assert re.search(
        r"\.thinking-card\.open\s+\.thinking-card-body\s*\{[^}]*max-height:\s*260px;[^}]*opacity:\s*1;",
        STYLE_CSS,
    )


def test_tool_card_toggle_uses_same_chevron_icon_markup_as_thinking_card():
    assert "<span class=\"thinking-card-toggle\">${li('chevron-right',12)}</span>" in UI_JS
    assert "<span class=\"tool-card-toggle\">${li('chevron-right',12)}</span>" in UI_JS
    assert "<div class=\"thinking-card\"><div class=\"thinking-card-header\" onclick=\"this.parentElement.classList.toggle('open')\"><span class=\"thinking-card-icon\">" in UI_JS


def test_live_thinking_updates_existing_card_body_in_place():
    assert "function _renderThinkingInto(row,text='')" in UI_JS
    assert "row.querySelector('.thinking-card-body pre')" in UI_JS
    assert "pre.textContent=clean" in UI_JS
    assert "_renderThinkingInto(row,text);" in UI_JS


def test_thinking_card_uses_panel_chrome_with_gold_palette():
    # Canonical thinking-card rule lives in the consolidated block (border-radius
    # tightened from 10px → 8px as part of the "quieter card" design pass).
    assert re.search(
        r"\.thinking-card\s*\{[^}]*background:\s*var\(--accent-bg\);[^}]*border:\s*1px\s+solid\s+var\(--accent-bg-strong\);[^}]*border-radius:\s*8px;",
        STYLE_CSS,
    )
    assert "border-left: 2px solid rgba(201,168,76,.4);" not in STYLE_CSS
