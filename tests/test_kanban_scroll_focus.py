"""Regression check for PR #7660 — kanban horizontal scroll + focus mode.

P1: in normal-board mode the horizontal scroll container is the WRAPPER
(.kanban-board-wrap, overflow:auto), not #kanbanBoard. _kanbanScrollBoards()
used to return the bare board, so scrollBy/scrollTo hit a non-scrolling
element and the normal board never moved. Lane mode was already correct
(.kanban-board-in-lane has overflow-x:auto).

P2: the scroll/focus controls and the board region had hardcoded English
aria-labels; applyTranslations() only updates accessible names when
data-i18n-aria-label is present.

Verified at the source level (repo convention for JS behavior — see
test_3845_keyboard_session_nav.py) so this stays fast and dependency-free.
"""
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def _func_body(name):
    start = PANELS_JS.index(f"function {name}(")
    # brace-match to the closing brace of the function
    i = PANELS_JS.index("{", start)
    depth = 0
    for j in range(i, len(PANELS_JS)):
        if PANELS_JS[j] == "{":
            depth += 1
        elif PANELS_JS[j] == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_scroll_owner_is_wrapper_in_normal_mode():
    """Normal mode must return .kanban-board-wrap, not the bare #kanbanBoard."""
    body = _func_body("_kanbanScrollBoards")
    assert "closest('.kanban-board-wrap')" in body, (
        "_kanbanScrollBoards must target the .kanban-board-wrap wrapper in "
        "normal mode (the element that actually has overflow:auto)"
    )
    # Lane mode still returns the inner lane boards.
    assert ".kanban-board-in-lane" in body


def test_end_key_uses_per_owner_range():
    """End must scroll each owner to its OWN end, not the widest lane's range."""
    body = _func_body("kanbanBoardKeydown")
    assert "b.scrollWidth - b.clientWidth" in body, (
        "End key must compute each owner's own range (b.scrollWidth - "
        "b.clientWidth) rather than applying the max range to every lane"
    )
    assert "Math.max(...boards.map" not in body, (
        "End key must not apply the widest lane's range to every lane"
    )


def test_controls_wire_localized_aria_labels():
    """Scroll/focus buttons + board region use data-i18n-aria-label."""
    for el_id, key in [
        ("btnKanbanScrollLeft", "kanban_scroll_left"),
        ("btnKanbanScrollRight", "kanban_scroll_right"),
        ("btnKanbanFocus", "kanban_focus_mode"),
    ]:
        m = re.search(rf'id="{el_id}"[^>]*', INDEX_HTML)
        assert m, f"{el_id} not found in index.html"
        tag = m.group(0)
        assert f'data-i18n-aria-label="{key}"' in tag, (
            f"{el_id} must wire data-i18n-aria-label=\"{key}\" so the "
            "accessible name localizes"
        )
    # The board region (role=region) must be localized too.
    region = re.search(r'id="kanbanBoard"[^>]*', INDEX_HTML)
    assert region and 'data-i18n-aria-label="kanban_board_region"' in region.group(0), (
        "#kanbanBoard region must wire data-i18n-aria-label=\"kanban_board_region\""
    )


def test_board_region_key_in_all_locales():
    """kanban_board_region exists in every locale block (parity)."""
    count = len(re.findall(r"kanban_board_region:\s*'", I18N_JS))
    # 15 locale blocks (en it ja ru es de zh pt ko fr cs tr pl vi + zh-Hant).
    assert count == 15, f"kanban_board_region found in {count} blocks, expected 15"
