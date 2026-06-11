"""Regression coverage for #2554 — workspace tree file rows align with directories."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _render_tree_item_toggle_block() -> str:
    start = UI_JS.index("if(item.type==='dir'){\n      // Toggle arrow for directories")
    end = UI_JS.index("\n\n    // Icon", start)
    return UI_JS[start:end]


def test_file_rows_get_toggle_placeholder_before_icon():
    block = _render_tree_item_toggle_block()

    assert "spacer=document.createElement('span')" in block
    assert "spacer.className='file-tree-toggle-placeholder'" in block
    assert "spacer.setAttribute('aria-hidden','true')" in block
    assert "el.appendChild(spacer);" in block

    spacer_idx = UI_JS.index("spacer.className='file-tree-toggle-placeholder'")
    icon_idx = UI_JS.index("const iconEl=document.createElement('span');", spacer_idx)
    assert spacer_idx < icon_idx, "file-row spacer must be appended before the file icon"


def test_placeholder_matches_directory_toggle_slot_width():
    assert ".file-tree-toggle{" in STYLE_CSS
    assert ".file-tree-toggle-placeholder{" in STYLE_CSS
    assert "--file-tree-toggle-width:10px" in STYLE_CSS

    toggle_start = STYLE_CSS.index(".file-tree-toggle{")
    toggle_end = STYLE_CSS.index("}", toggle_start)
    toggle = STYLE_CSS[toggle_start:toggle_end]

    placeholder_start = STYLE_CSS.index(".file-tree-toggle-placeholder{")
    placeholder_end = STYLE_CSS.index("}", placeholder_start)
    placeholder = STYLE_CSS[placeholder_start:placeholder_end]

    assert "width:var(--file-tree-toggle-width)" in toggle
    assert "width:var(--file-tree-toggle-width)" in placeholder
    assert "flex:0 0 var(--file-tree-toggle-width)" in placeholder
    assert "display:inline-block" in placeholder
