"""Regression: sticky manual unpin during streaming (supersedes #3250 timeout tuning).

After the user scrolls up to read earlier content, streaming tokens, tool cards,
and layout growth must not re-pin the viewport until the user scrolls back to
the bottom or clicks the scroll-to-bottom control.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _scroll_listener_block() -> str:
    anchor = "el.addEventListener('scroll'"
    start = UI_JS.index(anchor)
    raf_start = UI_JS.index("requestAnimationFrame", start)
    brace = UI_JS.index("{", raf_start)
    depth = 0
    for i in range(brace, len(UI_JS)):
        ch = UI_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[brace : i + 1]
    raise AssertionError("scroll listener rAF callback not found")


def test_scroll_if_pinned_respects_sticky_user_unpin():
    fn = UI_JS[UI_JS.index("function scrollIfPinned"): UI_JS.index("function scrollToBottom")]
    compact = fn.replace(" ", "")
    assert "if(_messageUserUnpinned)return" in compact, (
        "scrollIfPinned() must not fight a sticky manual unpin during streaming"
    )


def test_sticky_unpin_blocks_near_bottom_repin_without_downward_scroll():
    block = _scroll_listener_block()
    assert "_recentMessageUpwardIntent()" not in block
    compact = block.replace(" ", "")
    assert "elseif(!_messageUserUnpinned)" in compact, (
        "Near-bottom hysteresis re-pin must be gated off while the user is manually unpinned"
    )
    assert "elseif(movedDown&&nearBottom)" in compact, (
        "Re-follow must require explicit downward scroll into the near-bottom zone"
    )


def test_new_stream_resets_follow_state():
    attach = MESSAGES_JS[MESSAGES_JS.index("function attachLiveStream"):]
    assert "_resetStreamScrollFollow" in attach, (
        "A fresh live stream should default to following the tail until the user scrolls up"
    )


def test_scroll_to_bottom_clears_sticky_unpin():
    fn = UI_JS[UI_JS.index("function scrollToBottom"): UI_JS.index("function _fmtOllamaLabel")]
    assert "_messageUserUnpinned=false" in fn.replace(" ", "")
