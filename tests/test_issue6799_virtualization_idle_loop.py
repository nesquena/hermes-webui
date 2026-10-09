"""Regression coverage for the settled-transcript virtualization loop (#6799)."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _scroll_listener_body() -> str:
    start = UI_JS.index("el.addEventListener('scroll',()=>{")
    end = UI_JS.index("  },{passive:true});", start)
    return UI_JS[start:end]


def test_programmatic_scroll_cannot_schedule_virtualized_rerender():
    listener = _scroll_listener_body()

    guard_pos = listener.index("if(_freshProgrammaticScrollActive()) return;")
    schedule_pos = listener.index("_scheduleMessageVirtualizedRender();")

    assert guard_pos < schedule_pos


def test_settled_full_window_is_restored_to_default_after_render():
    assert "function _restoreMessageRenderWindowAfterSettledRender()" in UI_JS

    first_expand = MESSAGES_JS.index(
        "_messageRenderWindowSize=Math.max(",
        MESSAGES_JS.index("// Expand render window to cover all messages"),
    )
    first_restore = MESSAGES_JS.index(
        "_restoreMessageRenderWindowAfterSettledRender();",
        first_expand,
    )
    first_block_end = MESSAGES_JS.index("loadDir('.', { preservePreview: true });", first_expand)
    assert first_expand < first_restore < first_block_end

    second_expand = MESSAGES_JS.index(
        "_messageRenderWindowSize=Math.max(",
        MESSAGES_JS.index("// Expand render window so the settled render"),
    )
    second_render = MESSAGES_JS.index("renderMessages({preserveScroll:true});", second_expand)
    second_restore = MESSAGES_JS.index(
        "_restoreMessageRenderWindowAfterSettledRender();",
        second_render,
    )
    assert second_expand < second_render < second_restore
