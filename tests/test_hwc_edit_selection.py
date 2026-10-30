"""Inline edit of a user message keeps a visible caret and text selection."""
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(encoding="utf-8")

_AREA = '.msg-row[data-role="user"][data-editing="1"] .msg-edit-area'


def _rule(selector: str) -> str:
    idx = CSS.find(selector)
    assert idx >= 0, f"missing {selector}"
    end = CSS.find("}", idx)
    assert end > idx
    return CSS[idx:end]


def test_edit_area_caret_uses_bubble_text():
    rule = _rule(_AREA + " {")
    assert "caret-color: var(--user-bubble-text)" in rule


def test_edit_area_selection_is_split_and_inverts_the_bubble():
    for pseudo in ("::selection {", "::-moz-selection {"):
        rule = _rule(_AREA + pseudo)
        assert "background: var(--user-bubble-text)" in rule
        assert "background-color: var(--user-bubble-text)" in rule
        assert "color: var(--user-bubble-bg)" in rule


def test_dark_edit_selection_uses_page_background():
    for pseudo in ("::selection {", "::-moz-selection {"):
        rule = _rule(":root.dark " + _AREA + pseudo)
        assert "background: var(--user-bubble-text)" in rule
        assert "background-color: var(--user-bubble-text)" in rule
        assert "color: var(--bg)" in rule
