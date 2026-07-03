"""Regression coverage for #3479: iOS Safari must not see a top-scroll frame.

The live token path writes into an existing streaming-markdown DOM node. The
jump came from discrete transcript rebuilds and card replacement paths, so these
tests pin those call sites rather than the per-token renderer.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
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


def test_live_anchor_worklog_rebuild_restores_snapshot_before_follow_settle():
    body = _function_body(UI_JS, "renderLiveAnchorActivityScene")

    capture_idx = body.index("const scrollSnapshot=_captureMessageScrollSnapshot();")
    guard_idx = body.index("const scrollRebuildGuard=_prepareLiveAnchorScrollRebuildGuard(scrollSnapshot);")
    remove_idx = body.index("blocks.querySelectorAll('[data-anchor-scene-owner=\"1\"],[data-anchor-scene-row=\"1\"]')")
    restore_detail_idx = body.index("_restoreWorklogDetailDisclosureState(blocks, liveDisclosureState);")
    dedupe_idx = body.index("_dedupeLiveProcessedWorklogAnchors(turn);")
    move_status_idx = body.index("_moveLiveRunStatusToTurnEnd();")
    restore_idx = body.index("_restoreMessageScrollSnapshotSameFrame(scrollSnapshot);")
    release_idx = body.index("if(scrollRebuildGuard&&scrollRebuildGuard.release)")
    settle_idx = body.index("if(!scrollRebuildGuard.readerAwayFromBottom&&typeof scrollIfPinned==='function') scrollIfPinned();")

    assert capture_idx < guard_idx < remove_idx < restore_detail_idx < dedupe_idx < move_status_idx < restore_idx < release_idx < settle_idx


def test_live_anchor_worklog_rebuild_guards_height_for_unpinned_reader():
    guard = _function_body(UI_JS, "_prepareLiveAnchorScrollRebuildGuard")
    compact = _compact(guard)

    assert "constbeforeBottomDistance=Math.max(0,messagesEl.scrollHeight-messagesEl.scrollTop-messagesEl.clientHeight);" in compact
    assert "beforeBottomDistance>250&&(_messageUserUnpinned||_scrollPinned===false)" in compact
    assert "scrollSnapshot.pinned=false;" in compact
    assert "scrollSnapshot.userUnpinned=true;" in compact
    assert "scrollSnapshot.bottom=beforeBottomDistance;" in compact
    assert "_messageUserUnpinned=true;" in compact
    assert "_scrollPinned=false;" in compact
    assert "_nearBottomCount=0;" in compact
    assert "msgInner.style.minHeight=`${guardHeight}px`;" in compact


def test_same_frame_snapshot_preserves_bottom_distance_and_unpinned_state():
    capture = _function_body(UI_JS, "_captureMessageScrollSnapshot")
    restore = _function_body(UI_JS, "_restoreMessageScrollSnapshotSameFrame")
    wrapper = _function_body(UI_JS, "_renderMessagesWithScrollSnapshot")

    assert "bottom" in capture
    assert "readerAwayFromBottom?false:_shouldFollowMessagesOnDomReplace()" in _compact(capture)
    assert "readerAwayFromBottom?true:_messageUserUnpinned" in _compact(capture)
    assert "maxTop-Math.max(0,bottom)" in restore
    assert "_messageUserUnpinned=true" in restore
    assert "_scrollPinned=false" in restore
    assert "renderMessages({...(options||{}),preserveScroll:true});" in wrapper
    assert "_restoreMessageScrollSnapshotSameFrame(scrollSnapshot);" in wrapper


def test_preserve_scroll_restores_reader_away_from_bottom_before_following():
    body = _function_body(UI_JS, "_scrollAfterMessageRender")
    compact = _compact(body)

    reader_idx = compact.index("constreaderAwayFromBottom=")
    follow_idx = compact.index("if(!readerAwayFromBottom&&!_messageUserUnpinned&&_followMessagesAfterDomReplace())return;")
    restore_idx = compact.index("_restoreMessageScrollSnapshot(scrollSnapshot);")

    assert "Number(scrollSnapshot.bottom)>250" in compact
    assert reader_idx < follow_idx < restore_idx


def test_scroll_snapshot_restore_reinstates_unpinned_state_when_reader_is_mid_answer():
    restore = _function_body(UI_JS, "_restoreMessageScrollSnapshot")
    compact = _compact(restore)

    assert "constbottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;" in compact
    assert "if(bottomDistance>250)" in compact
    assert "_messageUserUnpinned=true" in compact
    assert "_scrollPinned=false" in compact


def test_same_session_force_refresh_does_not_reset_scroll_direction_tracker():
    body = _function_body(SESSIONS_JS, "loadSession")
    compact = _compact(body)

    assert "constcurrentSid=S.session?S.session.session_id:null;" in compact
    assert "if(currentSid!==sid&&typeofwindow!=='undefined'&&typeofwindow._resetScrollDirectionTracker==='function')" in compact


def test_clarify_card_is_height_clamped_and_scrollable_on_mobile_viewports():
    compact = _compact(STYLE_CSS)

    assert ".clarify-card" in STYLE_CSS
    assert "max-height:clamp(180px,min(68vh,calc(100vh-220px)),420px)" in compact
    assert "@supports(height:100dvh)" in compact
    assert "max-height:clamp(180px,min(62dvh,calc(100dvh-180px)),360px)" in compact
    assert "overflow-y:auto" in compact
    assert "-webkit-overflow-scrolling:touch" in compact
    assert "overscroll-behavior:contain" in compact
