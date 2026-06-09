"""Regression coverage for #3479: iOS Safari must not see a top-scroll frame.

The live token path writes into an existing streaming-markdown DOM node. The
jump came from discrete transcript rebuilds and card replacement paths, so these
tests pin those call sites rather than the per-token renderer.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for pos in range(brace, len(src)):
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : pos + 1]
    raise AssertionError(f"{name} body did not terminate")


def _compact(text: str) -> str:
    return "".join(text.split())


def test_refresh_session_uses_same_frame_scroll_snapshot_restore():
    body = _function_body(UI_JS, "refreshSession")

    assert "syncTopbar(); _renderMessagesWithScrollSnapshot();" in body
    assert "syncTopbar(); renderMessages();" not in body


def test_handoff_rebuilds_use_same_frame_scroll_snapshot_restore():
    clear_body = _function_body(UI_JS, "clearHandoffUi")
    set_body = _function_body(UI_JS, "setHandoffUi")

    assert "_renderMessagesWithScrollSnapshot();" in clear_body
    assert "_renderMessagesWithScrollSnapshot();" in set_body
    assert "renderMessages();" not in clear_body
    assert "renderMessages();" not in set_body


def test_live_compression_card_replacement_restores_snapshot_before_follow_settle():
    body = _function_body(UI_JS, "appendLiveCompressionCard")

    capture_idx = body.index("const scrollSnapshot=_captureMessageScrollSnapshot();")
    replace_idx = body.index("if(existing) existing.replaceWith(node);")
    restore_idx = body.index("_restoreMessageScrollSnapshotSameFrame(scrollSnapshot);")
    settle_idx = body.index("if(typeof scrollIfPinned==='function') scrollIfPinned();")

    assert capture_idx < replace_idx < restore_idx < settle_idx


def test_same_frame_snapshot_preserves_bottom_distance_and_unpinned_state():
    capture = _function_body(UI_JS, "_captureMessageScrollSnapshot")
    restore = _function_body(UI_JS, "_restoreMessageScrollSnapshotSameFrame")
    wrapper = _function_body(UI_JS, "_renderMessagesWithScrollSnapshot")

    assert "bottom" in capture
    assert "pinned:_shouldFollowMessagesOnDomReplace()" in _compact(capture)
    assert "userUnpinned:_messageUserUnpinned" in _compact(capture)
    assert "maxTop-Math.max(0,bottom)" in restore
    assert "_messageUserUnpinned=true" in restore
    assert "_scrollPinned=false" in restore
    assert "renderMessages({...(options||{}),preserveScroll:true});" in wrapper
    assert "_restoreMessageScrollSnapshotSameFrame(scrollSnapshot);" in wrapper


def test_clarify_card_is_height_clamped_and_scrollable_on_mobile_viewports():
    compact = _compact(STYLE_CSS)

    assert ".clarify-card" in STYLE_CSS
    assert "max-height:clamp(180px,min(68vh,calc(100vh-220px)),420px)" in compact
    assert "@supports(height:100dvh)" in compact
    assert "max-height:clamp(180px,min(62dvh,calc(100dvh-180px)),360px)" in compact
    assert "overflow-y:auto" in compact
    assert "-webkit-overflow-scrolling:touch" in compact
    assert "overscroll-behavior:contain" in compact
