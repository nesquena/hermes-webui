"""Regression coverage for #2458 Kanban board selector long-key layout."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_kanban_board_menu_renders_dedicated_slug_column():
    """Long board keys should not share the same flex cell as the title."""
    assert "kanban-board-switcher-item-slug" in PANELS
    assert 'title="${esc(b.slug || \'\')}"' in PANELS
    assert "${esc(b.slug || '')}</span>" in PANELS
    assert "kanban-board-switcher-item-name" in PANELS
    assert 'title="${esc(b.name || b.slug)}"' in PANELS


def test_kanban_board_menu_uses_separate_truncated_grid_columns():
    """Slug, title, and count stay in separate columns with truncation."""
    assert ".kanban-board-switcher-item{" in STYLE
    assert "display:grid" in STYLE
    assert "grid-template-columns:18px minmax(72px,96px) minmax(0,1fr) auto" in STYLE
    assert ".kanban-board-switcher-item-slug" in STYLE
    assert "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" in STYLE
    assert "font-family:'SF Mono',ui-monospace,Menlo,monospace" in STYLE
