from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = SESSIONS_JS.find(start_marker)
    assert start != -1, f"{start_marker} not found"
    end = SESSIONS_JS.find(end_marker, start)
    assert end != -1, f"{end_marker} not found after {start_marker}"
    return SESSIONS_JS[start:end]


def test_session_list_disables_browser_scroll_anchoring():
    session_list_rule_start = STYLE_CSS.find(".session-list{")
    assert session_list_rule_start != -1
    session_list_rule = STYLE_CSS[session_list_rule_start:STYLE_CSS.find("}", session_list_rule_start)]
    assert "overflow-anchor:none" in session_list_rule, (
        "Firefox/Waterfox scroll anchoring can fight virtualized sidebar DOM "
        "replacement and rubber-band the session list."
    )


def test_polling_payloads_are_deferred_while_user_scrolls_sidebar():
    render_block = _block("async function renderSessionList", "// ── Gateway session SSE")
    apply_block = _block("function _applySessionListPayload", "async function renderSessionList")

    assert "function _isSessionListUserInteracting()" in SESSIONS_JS
    assert "async function renderSessionList(opts={})" in render_block
    assert "const deferWhileInteracting=Boolean(opts&&opts.deferWhileInteracting);" in render_block
    assert "if(deferWhileInteracting&&_isSessionListUserInteracting())" in render_block
    assert "_pendingSessionListPayload={gen:_gen,sessData,projData};" in render_block
    assert "_schedulePendingSessionListApply();" in render_block
    assert "_applySessionListPayload(sessData,projData);" in render_block
    assert "_markPollingCompletionUnreadTransitions(_allSessions);" in apply_block, (
        "deferring sidebar refreshes must preserve background-completion unread semantics"
    )


def test_deferred_payloads_keep_generation_stale_response_guard():
    schedule_apply_block = _block("function _schedulePendingSessionListApply", "function _applySessionListPayload")
    render_block = _block("async function renderSessionList", "// ── Gateway session SSE")

    assert "if(!deferWhileInteracting) _pendingSessionListPayload=null;" in render_block, (
        "explicit/user-initiated refreshes must clear older deferred background payloads"
    )
    assert "payload.gen!==_renderSessionListGen" in schedule_apply_block, (
        "a deferred polling response must not apply after a newer renderSessionList generation starts"
    )


def test_only_background_refreshes_defer_while_sidebar_is_interacting():
    streaming_poll_block = _block("function startStreamingPoll", "function stopStreamingPoll")
    gateway_poll_block = _block("function startGatewayPollFallback", "function stopGatewayPollFallback")
    gateway_sse_block = _block("function startGatewaySSE", "function stopGatewaySSE")

    assert "renderSessionList({deferWhileInteracting:true})" in streaming_poll_block
    assert "renderSessionList({deferWhileInteracting:true})" in gateway_poll_block
    assert "renderSessionList({deferWhileInteracting:true}); // re-fetch and re-render" in gateway_sse_block
    assert "pfToggle.onclick=()=>{_showAllProfiles=true;renderSessionList();};" in SESSIONS_JS
    assert "pfToggle.onclick=()=>{_showAllProfiles=false;renderSessionList();};" in SESSIONS_JS

def test_session_list_pointer_hover_and_scroll_activity_are_tracked():
    interaction_block = _block("function _isSessionListUserInteracting()", "function _schedulePendingSessionListApply")
    schedule_block = _block("function _scheduleSessionVirtualizedRender()", "function _ensureSessionVirtualScrollHandler")
    ensure_block = _block("function _ensureSessionVirtualScrollHandler", "function renderSessionListFromCache")

    assert "list.matches(':hover')" in interaction_block
    assert "list.matches(':focus-within')" in interaction_block
    assert "_sessionListLastScrollAt=Date.now();" in schedule_block
    for event_name in ["pointerdown", "pointerup", "pointercancel", "pointerleave"]:
        assert event_name in ensure_block
    assert "_sessionListPointerActive=true;" in ensure_block
    assert "_sessionListPointerActive=false;" in ensure_block


def test_virtual_scroll_skips_dom_rebuild_when_window_is_unchanged():
    schedule_block = _block("function _scheduleSessionVirtualizedRender()", "function _ensureSessionVirtualScrollHandler")
    assert "nextWindow.start===currentStart" in schedule_block
    assert "nextWindow.end===currentEnd" in schedule_block
    assert "return;" in schedule_block
    assert "renderSessionListFromCache();" in schedule_block
