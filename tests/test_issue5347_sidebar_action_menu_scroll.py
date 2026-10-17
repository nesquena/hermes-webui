"""Regression coverage for #5347: chat scroll must not dismiss sidebar action menu."""
from pathlib import Path

SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")


def _scroll_listener_block() -> str:
    marker = "document.addEventListener('scroll',e=>{"
    start = SESSIONS_JS.find(marker)
    assert start != -1, "session action menu scroll listener not found"
    end = SESSIONS_JS.find("}, true);", start)
    assert end != -1, "session action menu scroll listener did not close"
    return SESSIONS_JS[start : end + len("}, true);")]


def test_chat_scroll_is_ignored_while_session_action_menu_is_open():
    """Streaming/manual chat scroll must not close the sidebar three-dot menu (#5347)."""
    block = _scroll_listener_block()

    assert "_sessionActionMenuShouldIgnoreScrollTarget(e.target)" in block
    assert "#messages" in SESSIONS_JS
    assert "#msgInner" in SESSIONS_JS


def test_session_list_scroll_repositions_instead_of_closing_action_menu():
    """Sidebar list scroll should keep the menu aligned with its anchor row."""
    block = _scroll_listener_block()

    assert "_sessionActionMenuShouldRepositionOnScroll(e.target)" in block
    assert block.index("_sessionActionMenuShouldRepositionOnScroll(e.target)") < block.index(
        "closeSessionActionMenu();"
    )
    assert "_positionSessionActionMenu(_sessionActionAnchor);" in block


def test_pointer_opened_menu_closes_on_sidebar_scroll_instead_of_jumping():
    """Right-click menus are placed at the pointer. Sidebar scroll must close
    them instead of re-anchoring to the row, which jumps the menu off the click.
    """
    block = _scroll_listener_block()
    pointer_at = block.index("if(_sessionActionPointer)")
    reposition_at = block.index("_positionSessionActionMenu(_sessionActionAnchor);")
    assert pointer_at < reposition_at
    assert "closeSessionActionMenu();" in block[pointer_at:reposition_at]
