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


def test_kanban_consolidated_board_fills_viewport_height():
    """The consolidated (single-board) view must stretch to fill the viewport
    instead of leaving empty space below the columns on desktop.

    Both the board element and its column bodies drop their capped heights and
    take height:100% so the columns extend to the bottom of the board wrap.
    """
    board_rule = _css_rule(".kanban-board.kanban-board-consolidated")
    assert "height:100%" in board_rule

    body_rule = _css_rule(".kanban-board-consolidated .kanban-column-body")
    assert "height:100%" in body_rule
    assert "max-height:unset" in body_rule
    # The consolidated view owns its own full-height scroll container, so it opts
    # out of the scroll-chaining lock the capped lane view uses.
    assert "overscroll-behavior:auto" in body_rule


def test_kanban_consolidated_class_toggled_by_lane_mode():
    """The consolidated-height CSS only takes effect when the board element
    carries the `kanban-board-consolidated` class, which the renderer adds only
    in the single-board (non-lanes) view. Pin the wiring so a render refactor
    can't silently strip the class and reintroduce the empty-space regression.
    """
    assert "classList.toggle('kanban-board-consolidated'" in PANELS_JS
