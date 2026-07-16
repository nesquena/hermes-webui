"""Regression coverage for #3479: iOS Safari must not see a top-scroll frame.

The live token path writes into an existing streaming-markdown DOM node. The
jump came from discrete transcript rebuilds and card replacement paths, so these
tests pin those call sites rather than the per-token renderer.
"""

import json
import subprocess
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


def _run_node(source: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", source],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


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
    release_idx = body.index("_scheduleLiveAnchorScrollRebuildGuardRelease(scrollRebuildGuard,scrollSnapshot)")
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
    assert "consttoken=_pinWipeMinHeight(msgInner,guardHeight);" in compact
    assert "release:token?()=>_releaseWipeMinHeight(msgInner,token):null" in compact


def test_live_anchor_delayed_release_cannot_restore_after_newer_owner_wins():
    helpers = "\n".join(
        _function_body(UI_JS, name)
        for name in (
            "_pinWipeMinHeight",
            "_releaseWipeMinHeight",
            "_prepareLiveAnchorScrollRebuildGuard",
            "_scheduleLiveAnchorScrollRebuildGuardRelease",
        )
    )
    source = f"""
const messages={{scrollHeight:5000,scrollTop:3000,clientHeight:800}};
const inner={{scrollHeight:5000,style:{{minHeight:'17px'}},dataset:{{}}}};
function $(id){{return id==='messages'?messages:inner;}}
let _messageUserUnpinned=true,_scrollPinned=false,_nearBottomCount=0;
let restores=0,raf=[];
function _restoreMessageScrollSnapshotSameFrame(){{restores++;}}
function requestAnimationFrame(cb){{raf.push(cb);}}
let _wipeGuardSeq=0;
{helpers}
const oldGuard=_prepareLiveAnchorScrollRebuildGuard({{scrollHeight:5000}});
const oldToken=inner.dataset.wipeGuardToken;
const newerToken=_pinWipeMinHeight(inner,6000);
_scheduleLiveAnchorScrollRebuildGuardRelease(oldGuard,{{top:3000}});
while(raf.length) raf.shift()();
const afterStale={{minHeight:inner.style.minHeight,token:inner.dataset.wipeGuardToken,restores}};
const newerReleased=_releaseWipeMinHeight(inner,newerToken);
console.log(JSON.stringify({{oldToken,newerToken,afterStale,newerReleased,finalMinHeight:inner.style.minHeight,finalToken:inner.dataset.wipeGuardToken||null}}));
"""
    result = _run_node(source)
    assert result["oldToken"] != result["newerToken"]
    assert result["afterStale"] == {
        "minHeight": "6000px",
        "token": result["newerToken"],
        "restores": 0,
    }
    assert result["newerReleased"] is True
    assert result["finalMinHeight"] == "17px"
    assert result["finalToken"] is None


def test_measurement_render_exception_releases_owner_token_immediately():
    helpers = "\n".join(
        _function_body(UI_JS, name)
        for name in (
            "_pinWipeMinHeight",
            "_releaseWipeMinHeight",
            "_compensateScrollForMeasurementDelta",
        )
    )
    source = f"""
const classes=new Set();
const container={{
  scrollTop:3000,scrollHeight:5000,clientHeight:800,
  classList:{{add:x=>classes.add(x),remove:x=>classes.delete(x)}},
  querySelector:()=>null,
}};
const inner={{scrollHeight:5000,style:{{minHeight:'17px'}},dataset:{{}}}};
function $(id){{return id==='messages'?container:inner;}}
function _captureMessageViewportAnchor(){{return null;}}
let _messageUserUnpinned=true;
let _wipeGuardSeq=0;
{helpers}
let threw=false;
try{{_compensateScrollForMeasurementDelta(()=>{{throw new Error('render failure');}});}}catch(_e){{threw=true;}}
console.log(JSON.stringify({{threw,minHeight:inner.style.minHeight,token:inner.dataset.wipeGuardToken||null,measuring:classes.has('vscroll-measuring')}}));
"""
    assert _run_node(source) == {
        "threw": True,
        "minHeight": "17px",
        "token": None,
        "measuring": False,
    }


def test_delayed_settle_restore_yields_to_fresh_user_scroll():
    # Maintainer gate: if the reader keeps scrolling during the two-frame settle
    # window, the delayed release must NOT snap them back to the pre-render anchor —
    # and it must not be fooled by _programmaticScroll (which the sync compensation
    # sets true for the whole window). The render below is a REAL one that measures a
    # shifted row and latches _programmaticScroll=true, exercising the true window.
    helpers = "\n".join(
        _function_body(UI_JS, name)
        for name in (
            "_pinWipeMinHeight",
            "_releaseWipeMinHeight",
            "_compensateScrollForMeasurementDelta",
        )
    )
    # scenario: "before" moves scrollTop BEFORE frame one; "between" moves it between
    # the two frames; "still" never moves. rafQueue lets us interleave the user scroll.
    def _run(scenario):
        return f"""
const rafQueue=[];
function requestAnimationFrame(cb){{ rafQueue.push(cb); return rafQueue.length; }}
let _programmaticScrollResetTimer=0;
function clearTimeout(){{}}
function setTimeout(cb){{ if(typeof cb==='function') cb(); return 1; }}
function _deferClearProgrammaticScroll(){{ _programmaticScroll=false; }}
let _programmaticScroll=false;
let _programmaticScrollSetAt=0;
let _lastScrollTop=0;
let _messageUserUnpinned=true;
let _wipeGuardSeq=0;
let restoredAnchor=null;
function _restoreMessageViewportAnchor(a){{ restoredAnchor=a; container.scrollTop=200; return true; }}
const container={{
  scrollTop:200,scrollHeight:5000,clientHeight:800,
  classList:{{add(){{}},remove(){{}}}},
  getBoundingClientRect:()=>({{top:0}}),
  querySelector:(sel)=>rowEl,
}};
// A measured row whose top matches the captured anchor offset, so the sync
// compensation runs its real branch (sets _programmaticScroll=true) with delta 0
// — i.e. it does NOT itself move scrollTop, isolating user movement.
const rowEl={{ getBoundingClientRect:()=>({{top:0}}), style:{{height:'0'}} }};
const inner={{scrollHeight:5000,style:{{minHeight:'17px'}},dataset:{{}}}};
function $(id){{return id==='messages'?container:inner;}}
function _captureMessageViewportAnchor(){{return {{rawIdx:5,sessionIdx:5,topOffset:0,topPadBefore:0}};}}
{helpers}
// A real render that latches _programmaticScroll for the whole window.
function realRender(){{ _programmaticScroll=true; _programmaticScrollSetAt=1; }}
_compensateScrollForMeasurementDelta(realRender);
// "before" = user scrolls AFTER sync compensation settles but BEFORE frame one
// (the window the old baseline-inside-frame-one capture missed).
{'container.scrollTop=260;' if scenario=='before' else ''}
rafQueue.shift()();   // frame 1
{'container.scrollTop=260;' if scenario=='between' else ''}
rafQueue.shift()();   // frame 2 -> release
console.log(JSON.stringify({{scrollTop:container.scrollTop, restored:restoredAnchor!==null, prog:_programmaticScroll}}));
"""
    before = _run_node(_run("before"))
    between = _run_node(_run("between"))
    still = _run_node(_run("still"))
    # _programmaticScroll must be TRUE at release time (proves the latch is armed and
    # that the yield decision ignores it rather than being masked by a no-op render).
    assert before["prog"] is True and between["prog"] is True
    # Movement before frame one OR between frames must cancel the restore.
    assert before == {"scrollTop": 260, "restored": False, "prog": True}
    assert between == {"scrollTop": 260, "restored": False, "prog": True}
    # No movement: the semantic anchor restore still runs.
    assert still["restored"] is True


def test_all_guarded_render_paths_have_exception_cleanup_and_owned_restore():
    render = _function_body(UI_JS, "renderMessages")
    cache = render[render.index("const _cacheGuardToken="):render.index("// Mid-stream flicker fix")]
    compact = _function_body(UI_JS, "renderLiveAnchorActivityScene")
    transparent = _function_body(UI_JS, "_renderLiveAnchorActivitySceneTransparent")
    settle = _function_body(UI_JS, "_compensateScrollForMeasurementDelta")

    assert "finally" in render and "if(!_mainGuardCompleted&&_mainWipeGuardToken)" in render
    assert "finally" in cache and "if(_cacheGuardCleanupArmed&&_cacheGuardToken)" in cache
    assert "finally" in compact and "scrollRebuildGuard.release();" in compact
    assert "finally" in transparent and "scrollRebuildGuard.release();" in transparent
    assert "if(!_releaseWipeMinHeight(_inner,_settleToken)) return;" in settle
    schedule = _function_body(UI_JS, "_scheduleLiveAnchorScrollRebuildGuardRelease")
    assert "if(released&&_messageUserUnpinned)" in schedule


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
