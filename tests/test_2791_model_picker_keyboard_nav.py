"""Regression check for #2791 — keyboard navigation in the model picker.

The picker is a click-driven dropdown; users asked for arrow-key navigation
and Enter-to-select on the existing search input. Verified at the source
level so this stays fast.
"""
from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_arrow_keys_wired_on_search_input():
    # ArrowDown / ArrowUp / Enter all handled in one keydown listener on _si.
    assert "ArrowDown" in UI_JS
    assert "ArrowUp" in UI_JS
    assert "is-highlighted" in UI_JS


def test_highlight_class_has_style():
    assert ".model-opt.is-highlighted" in STYLE_CSS


def test_escape_still_closes_dropdown():
    # Existing Escape-to-close behavior must not regress.
    assert "if(e.key==='Escape'){closeModelDropdown();return;}" in UI_JS


def test_enter_picks_highlighted_row():
    # Enter should call .click() on the highlighted (or first) row.
    snippet = UI_JS[UI_JS.index("ArrowDown"):]
    assert "pick.click()" in snippet
