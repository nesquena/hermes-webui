"""Regression coverage for the first jump-to-answer click after session load."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    assert NODE is not None
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
"""


def _extract_message_scroll_listener_script(js: str) -> str:
    marker = "el.addEventListener('scroll',()=>{"
    marker_idx = js.index(marker)
    start = js.rfind("(function(){", 0, marker_idx)
    end = js.index("})();", marker_idx) + len("})();")
    assert start >= 0
    return f"const messageScrollListenerIife = {js[start:end]!r};\n"


def test_first_jump_to_answer_cancels_pending_load_time_bottom_settle():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _bottomSettleToken = 0;
let _settleRAF = 0;
let _settleRO = null;
let _settleTimer = 0;
let _settleFinalTimer = 0;
let targetScrolls = 0;
let snappedBackToBottom = false;

const container = {
  scrollHeight: 900,
  clientHeight: 500,
  scrollTop: 400,
  getBoundingClientRect(){ return {top: 100, height: 500}; },
  querySelectorAll(){ return [assistantSegment]; },
};
const assistantSegment = {
  getClientRects(){ return [{}]; },
  getBoundingClientRect(){ return {top: -300, height: 40}; },
  scrollIntoView(){ targetScrolls += 1; container.scrollTop = 0; },
};
function $(id){ return id === 'messages' ? container : null; }
function _userMessageDomId(rawIdx){ return 'msg-user-' + rawIdx; }
function _highlightQuestionRow(){}
function _getVisibleMessagesWithIdx(){ throw new Error('visible target should use fast path'); }
function cancelAnimationFrame(){}

eval(extractFunc('_cancelBottomSettle'));
eval(extractFunc('_messageJumpSessionId'));
eval(extractFunc('_scheduleMessageJumpScrollReconcile'));
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
eval(extractFunc('jumpToTurnQuestion'));

(async () => {
  _settleFinalTimer = setTimeout(() => {
    if (_scrollPinned && !_messageUserUnpinned) snappedBackToBottom = true;
  }, 15);

  await jumpToTurnQuestion(4, 5);
  await new Promise(resolve => setTimeout(resolve, 340));

  console.log(JSON.stringify({
    targetScrolls,
    snappedBackToBottom,
    scrollPinned: _scrollPinned,
    messageUserUnpinned: _messageUserUnpinned,
    bottomSettleToken: _bottomSettleToken,
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    result = json.loads(_run_node(source))

    assert result == {
        "targetScrolls": 1,
        "snappedBackToBottom": False,
        "scrollPinned": False,
        "messageUserUnpinned": True,
        "bottomSettleToken": 1,
    }


@pytest.mark.parametrize(
    ("scroll_range", "expect_reader_owned"),
    [
        (0, False),
        (79, False),
        (81, True),
        (400, True),
    ],
)
def test_jump_geometry_controls_active_session_refresh_deferral(
    scroll_range: int, expect_reader_owned: bool
):
    ui_js = UI_JS_PATH.read_text(encoding="utf-8")
    sessions_js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(ui_js + "\n" + sessions_js) + rf"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _pendingSessionUpdatedCounts = null;
function _drainSessionUpdatedPendingCount(){{}}
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _bottomSettleToken = 0;
let _settleRAF = 0;
let _settleRO = null;
let _settleTimer = 0;
let _settleFinalTimer = 0;
let _activeSessionExternalRefreshInFlight = false;
let _deferredActiveSessionExternalRefreshReason = '';
let apiCalls = 0;

const scrollRange = {scroll_range};
const container = {{
  scrollHeight: 500 + scrollRange,
  clientHeight: 500,
  scrollTop: scrollRange,
  getBoundingClientRect(){{ return {{top: 100, height: 500}}; }},
  querySelectorAll(){{ return [assistantSegment]; }},
}};
const assistantSegment = {{
  getClientRects(){{ return [{{}}]; }},
  getBoundingClientRect(){{ return {{top: 100 - scrollRange, height: 40}}; }},
  scrollIntoView(){{ container.scrollTop = 0; }},
}};
const document = {{hidden: false, getElementById(){{ return null; }}}};
const window = {{}};
const S = {{
  session: {{session_id: 'active', message_count: 1, last_message_at: 1}},
  messages: [{{role: 'user'}}],
  busy: false,
  activeStreamId: null,
}};

function $(id){{ return id === 'messages' ? container : null; }}
function _userMessageDomId(rawIdx){{ return 'msg-user-' + rawIdx; }}
function _highlightQuestionRow(){{}}
function _getVisibleMessagesWithIdx(){{ throw new Error('visible target should use fast path'); }}
function cancelAnimationFrame(){{}}
function _isMessageReaderUnpinned(){{ return _messageUserUnpinned; }}
function _deferActiveSessionExternalRefresh(reason){{
  _deferredActiveSessionExternalRefreshReason = reason || 'poll';
}}
function _isExternalSession(){{ return false; }}
async function api(){{
  apiCalls += 1;
  return {{session: {{message_count: 1, last_message_at: 1}}}};
}}

eval(extractFunc('_cancelBottomSettle'));
eval(extractFunc('_messageJumpSessionId'));
eval(extractFunc('_scheduleMessageJumpScrollReconcile'));
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
eval(extractFunc('jumpToTurnQuestion'));
eval(extractFunc('refreshActiveSessionIfExternallyUpdated'));

(async () => {{
  await jumpToTurnQuestion(4, 5);
  await new Promise(resolve => setTimeout(resolve, 340));
  const refreshResult = await refreshActiveSessionIfExternallyUpdated('idle-reconcile', {{ignoreStreamJustFinished:true}});
  console.log(JSON.stringify({{
    scrollRange,
    scrollTop: container.scrollTop,
    scrollPinned: _scrollPinned,
    messageUserUnpinned: _messageUserUnpinned,
    refreshResult,
    deferredReason: _deferredActiveSessionExternalRefreshReason,
    apiCalls,
  }}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""
    result = json.loads(_run_node(source))

    assert result["scrollRange"] == scroll_range
    assert result["scrollTop"] == 0
    assert result["scrollPinned"] is (not expect_reader_owned)
    assert result["messageUserUnpinned"] is expect_reader_owned
    assert result["refreshResult"] == ("skipped" if expect_reader_owned else "unchanged")
    assert result["deferredReason"] == ("idle-reconcile" if expect_reader_owned else "")
    assert result["apiCalls"] == (0 if expect_reader_owned else 1)


@pytest.mark.parametrize("jump_path", ["assistant", "user", "virtualized"])
@pytest.mark.parametrize(
    ("scroll_range", "expect_reader_owned"),
    [(0, False), (79, False), (80, False), (81, True), (400, True)],
)
def test_response_jump_owns_native_smooth_scroll_until_final_reconciliation(
    jump_path: str, scroll_range: int, expect_reader_owned: bool
):
    ui_js = UI_JS_PATH.read_text(encoding="utf-8")
    sessions_js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = (
        _extract_func_script(ui_js + "\n" + sessions_js)
        + _extract_message_scroll_listener_script(ui_js)
        + rf"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _pendingSessionUpdatedCounts = null;
function _drainSessionUpdatedPendingCount(){{}}
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _lastScrollTop = {scroll_range};
let _lastMessageClientHeight = 500;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _programmaticScrollResetTimer = 0;
let _bottomSettleToken = 0;
let _settleRAF = 0;
let _settleRO = null;
let _settleTimer = 0;
let _settleFinalTimer = 0;
let _activeSessionExternalRefreshInFlight = false;
let _deferredActiveSessionExternalRefreshReason = '';
let _scrollbarDragActive = false;
let _newMessageCueVisible = false;
let _messageVirtualWindowKey = '';
let _messageRenderWindowSize = 0;
let _messagesTruncated = false;
let rendered = {str(jump_path != "virtualized").lower()};
let cueShown = null;
let apiCalls = 0;
const scrollListeners = [];
const jumpPath = {jump_path!r};
const scrollRange = {scroll_range};

const dispatchScroll = () => scrollListeners.forEach(listener => listener());
let scrollTopValue = scrollRange;
const container = {{
  scrollHeight: 500 + scrollRange,
  clientHeight: 500,
  clientWidth: 320,
  contains(){{ return false; }},
  matches(){{ return false; }},
  addEventListener(type, listener){{ if(type === 'scroll') scrollListeners.push(listener); }},
  querySelectorAll(selector){{
    if(selector.includes('data-msg-idx') && rendered && jumpPath !== 'user') return [assistantSegment];
    return [];
  }},
  getBoundingClientRect(){{ return {{top: 100, bottom: 600, height: 500}}; }},
  get scrollTop(){{ return scrollTopValue; }},
  set scrollTop(value){{ scrollTopValue = value; dispatchScroll(); }},
}};
function animateToTop(){{
  if(scrollRange === 0) return;
  setTimeout(() => {{ scrollTopValue = Math.ceil(scrollRange / 2); dispatchScroll(); }}, 180);
  setTimeout(() => {{ scrollTopValue = 0; dispatchScroll(); }}, 230);
}}
const assistantSegment = {{
  getClientRects(){{ return [{{}}]; }},
  getBoundingClientRect(){{ return {{top: 100 - scrollTopValue, height: 40}}; }},
  scrollIntoView(){{ animateToTop(); }},
}};
const userRow = {{
  getClientRects(){{ return [{{}}]; }},
  getBoundingClientRect(){{ return {{top: 100 - scrollTopValue, height: 40}}; }},
  scrollIntoView(){{ animateToTop(); }},
  classList: {{remove(){{}}, add(){{}}}},
  offsetWidth: 1,
}};
const document = {{
  hidden: false,
  visibilityState: 'visible',
  activeElement: null,
  getElementById(id){{
    if(id === 'messages') return container;
    if(id === 'msg-user-4' && jumpPath === 'user') return userRow;
    return null;
  }},
  addEventListener(){{}},
}};
const window = {{addEventListener(){{}}, setTimeout}};
function requestAnimationFrame(callback){{ return setTimeout(callback, 0); }}
function cancelAnimationFrame(handle){{ clearTimeout(handle); }}
const S = {{
  session: {{session_id: 'active', message_count: 1, last_message_at: 1}},
  messages: [{{role: 'user'}}],
  busy: false,
  activeStreamId: null,
}};

function $(id){{ return id === 'messages' ? container : null; }}
function _userMessageDomId(rawIdx){{ return 'msg-user-' + rawIdx; }}
function _getVisibleMessagesWithIdx(){{ return [{{rawIdx: 4}}]; }}
function _messageVisibleIndexForRawIdx(){{ return 0; }}
function _messageVirtualScrollTopForVisibleIdx(){{ return 0; }}
function _messageHiddenBeforeCount(){{ return 0; }}
function _currentMessageRenderWindowSize(){{ return 1; }}
function _messageRenderableMessageCount(){{ return 1; }}
function renderMessages(){{ rendered = true; }}
function _deferClearProgrammaticScroll(ms){{
  clearTimeout(_programmaticScrollResetTimer);
  _programmaticScrollResetTimer = setTimeout(() => {{ _programmaticScroll = false; }}, ms || 80);
}}
function _scheduleMessageVirtualizedRender(){{}}
function _markMessageVirtualScrollActive(){{}}
function _recentMessageRenderArtifactWindow(){{ return false; }}
function _recentMessageTouchScrollIntent(){{ return false; }}
function _recentNonMessageScrollIntent(){{ return false; }}
function _recentMessageWheelIntent(){{ return false; }}
function _recentMessageKeyScrollIntent(){{ return false; }}
function _clearNewMessageScrollCue(){{}}
function _syncScrollToBottomCue(show){{ cueShown = show; }}
function _updateSessionStartJumpButton(){{}}
function _isSessionEndlessScrollEnabled(){{ return false; }}
function _isMessageReaderUnpinned(){{ return _messageUserUnpinned; }}
function _deferActiveSessionExternalRefresh(reason){{
  _deferredActiveSessionExternalRefreshReason = reason || 'poll';
}}
function _isExternalSession(){{ return false; }}
async function api(){{
  apiCalls += 1;
  return {{session: {{message_count: 1, last_message_at: 1}}}};
}}

eval(extractFunc('_cancelBottomSettle'));
eval(extractFunc('_messageJumpSessionId'));
eval(extractFunc('_scheduleMessageJumpScrollReconcile'));
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
eval(extractFunc('_highlightQuestionRow'));
eval(extractFunc('jumpToTurnQuestion'));
eval(extractFunc('refreshActiveSessionIfExternallyUpdated'));
eval(messageScrollListenerIife);

(async () => {{
  await jumpToTurnQuestion(4, jumpPath === 'user' ? -1 : 5);
  await new Promise(resolve => setTimeout(resolve, 560));
  const refreshResult = await refreshActiveSessionIfExternallyUpdated(
    'idle-reconcile', {{ignoreStreamJustFinished:true}}
  );
  console.log(JSON.stringify({{
    jumpPath,
    scrollRange,
    bottomDistance: container.scrollHeight - container.scrollTop - container.clientHeight,
    scrollPinned: _scrollPinned,
    messageUserUnpinned: _messageUserUnpinned,
    nearBottomCount: _nearBottomCount,
    cueShown,
    refreshResult,
    deferredReason: _deferredActiveSessionExternalRefreshReason,
    apiCalls,
  }}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""
    )
    result = json.loads(_run_node(source))

    assert result["jumpPath"] == jump_path
    assert result["bottomDistance"] == scroll_range
    assert result["scrollPinned"] is (not expect_reader_owned)
    assert result["messageUserUnpinned"] is expect_reader_owned
    assert result["nearBottomCount"] == (0 if expect_reader_owned else 2)
    assert result["cueShown"] is expect_reader_owned
    assert result["refreshResult"] == ("skipped" if expect_reader_owned else "unchanged")
    assert result["deferredReason"] == ("idle-reconcile" if expect_reader_owned else "")
    assert result["apiCalls"] == (0 if expect_reader_owned else 1)


def test_jump_owner_replacement_and_session_change_cancel_stale_generation():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _lastScrollTop = 400;
let _lastMessageClientHeight = 500;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _newMessageCueVisible = false;
let currentSid = 'active';
const S = {session:{session_id:'active'}};
const container = {scrollHeight:900,clientHeight:500,scrollTop:0};
function _syncScrollToBottomCue(){}

eval(extractFunc('_messageJumpSessionId'));
eval(extractFunc('_scheduleMessageJumpScrollReconcile'));
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));

const first = _beginMessageJumpScroll(container);
const second = _beginMessageJumpScroll(container);
_finishMessageJumpScroll(first);
const staleIgnored = _messageJumpScrollOwner && _messageJumpScrollOwner.generation === second;
const preservedFromFirst = _messageJumpScrollOwner.preserved.scrollPinned === true
  && _messageJumpScrollOwner.preserved.messageUserUnpinned === false
  && _messageJumpScrollOwner.preserved.nearBottomCount === 2;
S.session.session_id = 'other';
_finishMessageJumpScroll(second);
console.log(JSON.stringify({
  staleIgnored,
  preservedFromFirst,
  ownerCleared: _messageJumpScrollOwner === null,
  programmaticCleared: _programmaticScroll === false,
  scrollPinned: _scrollPinned,
  messageUserUnpinned: _messageUserUnpinned,
  nearBottomCount: _nearBottomCount,
}));
"""
    result = json.loads(_run_node(source))

    assert result == {
        "staleIgnored": True,
        "preservedFromFirst": True,
        "ownerCleared": True,
        "programmaticCleared": True,
        "scrollPinned": True,
        "messageUserUnpinned": False,
        "nearBottomCount": 2,
    }


def test_near_tail_jump_preserves_preexisting_reader_unpin():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = false;
let _messageUserUnpinned = true;
let _nearBottomCount = 0;
let _lastScrollTop = 79;
let _lastMessageClientHeight = 500;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _newMessageCueVisible = false;
let currentSid = 'active';
const S = {session:{session_id:'active'}};
const container = {scrollHeight:579,clientHeight:500,scrollTop:0};
let cueShown = null;
function _syncScrollToBottomCue(show){ cueShown = show; }

eval(extractFunc('_messageJumpSessionId'));
eval(extractFunc('_scheduleMessageJumpScrollReconcile'));
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));

const generation = _beginMessageJumpScroll(container);
_finishMessageJumpScroll(generation);
console.log(JSON.stringify({
  scrollPinned: _scrollPinned,
  messageUserUnpinned: _messageUserUnpinned,
  nearBottomCount: _nearBottomCount,
  cueShown,
}));
"""
    result = json.loads(_run_node(source))

    assert result == {
        "scrollPinned": False,
        "messageUserUnpinned": True,
        "nearBottomCount": 0,
        "cueShown": False,
    }


def test_manual_wheel_input_cancels_active_jump_owner():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
const PROGRAMMATIC_SCROLL_VALID_MS = 150;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _bottomSettleToken = 0;
let _settleRAF = 0;
let _settleRO = null;
let _settleTimer = 0;
let _settleFinalTimer = 0;
let _lastNonMessageScrollIntentMs = -Infinity;
let _lastMessageScrollIntentMs = -Infinity;
let _lastMessageWheelIntentMs = -Infinity;
let _newMessageCueVisible = false;
let currentSid = 'active';
const S = {session:{session_id:'active'}};
const child = {};
const container = {
  scrollHeight:900,
  clientHeight:500,
  scrollTop:0,
  contains(node){ return node === child; },
};
const document = {getElementById(id){ return id === 'messages' ? container : null; }};
function cancelAnimationFrame(){}

eval(extractFunc('_messageJumpSessionId'));
eval(extractFunc('_scheduleMessageJumpScrollReconcile'));
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
eval(extractFunc('_cancelBottomSettle'));
eval(extractFunc('_freshProgrammaticScrollActive'));
eval(extractFunc('_recordNonMessageScrollIntent'));

_beginMessageJumpScroll(container);
_recordNonMessageScrollIntent({target:child,type:'wheel',deltaY:-5});
console.log(JSON.stringify({
  ownerCleared: _messageJumpScrollOwner === null,
  programmaticCleared: _programmaticScroll === false,
  bottomSettleToken: _bottomSettleToken,
  scrollPinned: _scrollPinned,
  messageUserUnpinned: _messageUserUnpinned,
  intentRecorded: Number.isFinite(_lastMessageScrollIntentMs),
}));
"""
    result = json.loads(_run_node(source))

    assert result == {
        "ownerCleared": True,
        "programmaticCleared": True,
        "bottomSettleToken": 1,
        # Current master treats even a low-delta upward wheel as reader takeover
        # when it interrupts an active programmatic scroll (#6414).
        "scrollPinned": False,
        "messageUserUnpinned": True,
        "intentRecorded": True,
    }


def test_streaming_frame_during_jump_owner_does_not_snap_to_bottom():
    """#6621 (Fable finding ii): a streaming render frame that fires INSIDE the
    jump-owner window must not let scrollIfPinned() reclaim the bottom. Reproduces
    the exact live-token snap the fix prevents."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _messageUserUnpinned = false;
let _nearBottomCount = 0;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _lastScrollTop = null;
let _lastMessageClientHeight = 0;
let _newMessageCueVisible = false;
let bottomWrites = 0;
const performance = { now: () => Date.now() };
const TARGET_TOP = 707;
const container = { scrollHeight: 3510, clientHeight: 745, scrollTop: 2765, contains: () => true };
const window = { _autoScrollFollow: true };
function _messageJumpSessionId(){ return 'S1'; }
function clearTimeout(){} function setTimeout(){ return 0; }
function _scheduleMessageJumpScrollReconcile(){}
function _syncScrollToBottomCue(){} function _updateSessionStartJumpButton(){}
function _flushDeferredActiveSessionExternalRefresh(){}
function _recentNonMessageScrollIntent(){ return false; }
function _recentMessageScrollIntent(){ return false; }
function _recentMessageTouchScrollIntent(){ return false; }
function _recentMessageWheelIntent(){ return false; }
function _recentMessageKeyScrollIntent(){ return false; }
function _messageBottomDistance(){ return container.scrollHeight - container.scrollTop - container.clientHeight; }
function _setMessageScrollToBottom(){ bottomWrites++; container.scrollTop = container.scrollHeight - container.clientHeight; }
function _settleMessageScrollToBottom(){ if (_scrollPinned){ bottomWrites++; container.scrollTop = container.scrollHeight - container.clientHeight; } }

eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
eval(extractFunc('scrollIfPinned'));

_beginMessageJumpScroll(container);
container.scrollTop = TARGET_TOP;
const before = container.scrollTop;
scrollIfPinned();
scrollIfPinned();
const after = container.scrollTop;
console.log(JSON.stringify({ before, after, bottomWrites }));
"""
    result = json.loads(_run_node(source))
    assert result == {"before": 707, "after": 707, "bottomWrites": 0}, (
        "A streaming frame inside the jump-owner window must not snap the reader "
        "back to the bottom (#6621)."
    )


def test_wheel_up_during_jump_owner_takes_reader_ownership_not_pinned_midtranscript():
    """#6621 (Fable robustness): a gentle upward wheel during the jump-owner window
    (after the programmatic latch stales) must leave the reader explicitly unpinned
    at their position, NOT pinned mid-transcript where the next frame would yank them."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _lastScrollTop = null;
let _lastMessageClientHeight = 0;
let _newMessageCueVisible = false;
let _messageScrollInputGeneration = 0;
let _lastMessageWheelIntentMs = -Infinity;
let _lastMessageScrollIntentMs = -Infinity;
let _lastNonMessageScrollIntentMs = -Infinity;
let _touchStartY = null;
const performance = { now: () => 1000 };
const PROGRAMMATIC_SCROLL_VALID_MS = 150;
const container = { scrollHeight: 3510, clientHeight: 745, scrollTop: 707, contains: (t) => t === container };
const messages = container;
const document = { getElementById: (id) => (id === 'messages' ? messages : null) };
const window = { _autoScrollFollow: true };
function _messageJumpSessionId(){ return 'S1'; }
function _cancelBottomSettle(){ _cancelMessageJumpScroll(); }
function clearTimeout(){} function setTimeout(){ return 0; }
function _scheduleMessageJumpScrollReconcile(){}
function _syncScrollToBottomCue(){} function _updateSessionStartJumpButton(){}
function _flushDeferredActiveSessionExternalRefresh(){}
function _markMessageTouchScrollIntent(){}
function _freshProgrammaticScrollActive(){
  if (!_programmaticScroll) return false;
  const a = performance.now() - _programmaticScrollSetAt;
  if (!Number.isFinite(a) || a < 0 || a > PROGRAMMATIC_SCROLL_VALID_MS){ _programmaticScroll = false; return false; }
  return true;
}
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
eval(extractFunc('_recordNonMessageScrollIntent'));

_beginMessageJumpScroll(container);
container.scrollTop = 707;
_programmaticScrollSetAt = performance.now() - 200; // latch stale
_recordNonMessageScrollIntent({ type: 'wheel', target: messages, deltaY: -5 });
console.log(JSON.stringify({
  ownerCleared: _messageJumpScrollOwner === null,
  messageUserUnpinned: _messageUserUnpinned,
  scrollPinned: _scrollPinned,
  scrollTop: container.scrollTop,
}));
"""
    result = json.loads(_run_node(source))
    assert result == {
        "ownerCleared": True,
        "messageUserUnpinned": True,
        "scrollPinned": False,
        "scrollTop": 707,
    }, "A wheel-up during the jump owner window must hand ownership to the reader unpinned, not leave them pinned mid-transcript (#6621)."


def test_wheel_down_during_jump_owner_also_takes_reader_ownership():
    """#6621 (Fable finding S3): a wheel-DOWN (or touch scroll) during the owner
    window must ALSO hand ownership to the reader unpinned — not restore the
    pinned snapshot and let the next streaming token yank to the bottom."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = true;
let _loadingSessionId = null;
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _messageJumpScrollGeneration = 0;
let _messageJumpScrollOwner = null;
let _messageJumpScrollSettleTimer = 0;
let _lastScrollTop = null;
let _lastMessageClientHeight = 0;
let _newMessageCueVisible = false;
let _messageScrollInputGeneration = 0;
let _lastMessageWheelIntentMs = -Infinity;
let _lastMessageScrollIntentMs = -Infinity;
let _lastNonMessageScrollIntentMs = -Infinity;
let _touchStartY = null;
const performance = { now: () => 1000 };
const PROGRAMMATIC_SCROLL_VALID_MS = 150;
const container = { scrollHeight: 3510, clientHeight: 745, scrollTop: 707, contains: (t) => t === container };
const messages = container;
const document = { getElementById: (id) => (id === 'messages' ? messages : null) };
const window = { _autoScrollFollow: true };
function _messageJumpSessionId(){ return 'S1'; }
function _cancelBottomSettle(){ _cancelMessageJumpScroll(); }
function clearTimeout(){} function setTimeout(){ return 0; }
function _scheduleMessageJumpScrollReconcile(){}
function _syncScrollToBottomCue(){} function _updateSessionStartJumpButton(){}
function _flushDeferredActiveSessionExternalRefresh(){}
function _markMessageTouchScrollIntent(){}
function _freshProgrammaticScrollActive(){
  if (!_programmaticScroll) return false;
  const a = performance.now() - _programmaticScrollSetAt;
  if (!Number.isFinite(a) || a < 0 || a > PROGRAMMATIC_SCROLL_VALID_MS){ _programmaticScroll = false; return false; }
  return true;
}
eval(extractFunc('_beginMessageJumpScroll'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
eval(extractFunc('_recordNonMessageScrollIntent'));

_beginMessageJumpScroll(container);
container.scrollTop = 707;
_programmaticScrollSetAt = performance.now() - 200; // latch stale
_recordNonMessageScrollIntent({ type: 'wheel', target: messages, deltaY: 5 }); // DOWNWARD
console.log(JSON.stringify({
  ownerCleared: _messageJumpScrollOwner === null,
  messageUserUnpinned: _messageUserUnpinned,
  scrollPinned: _scrollPinned,
}));
"""
    result = json.loads(_run_node(source))
    assert result == {
        "ownerCleared": True,
        "messageUserUnpinned": True,
        "scrollPinned": False,
    }, "A wheel-down during the jump owner window must also unpin the reader (#6621 S3)."
