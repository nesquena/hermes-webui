"""Source-level regression tests for the Kanban board UI."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _css_rule(selector: str) -> str:
    start = STYLE_CSS.find(selector + "{")
    assert start != -1, f"missing CSS selector: {selector}"
    end = STYLE_CSS.find("}", start)
    assert end != -1, f"unterminated CSS selector: {selector}"
    return STYLE_CSS[start : end + 1]


def test_kanban_columns_render_scrollable_card_lists():
    assert 'class="kanban-column-body"' in PANELS_JS
    assert "tasks.map(task => _kanbanCard(task, col.name)).join('')" in PANELS_JS

    rule = _css_rule(".kanban-column-body")
    assert "max-height:min(68vh,720px)" in rule
    assert "overflow-y:auto" in rule
    assert "overscroll-behavior:contain" in rule
    assert "scrollbar-gutter:stable" in rule
