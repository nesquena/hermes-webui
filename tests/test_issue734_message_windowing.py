from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_message_virtualization_switches_render_messages_to_scroll_driven_window():
    assert "function _messageVirtualWindow(opts)" in UI_JS
    assert "function _messageVirtualKeepTailCount()" in UI_JS
    assert "const virtualWindow=_currentMessageVirtualWindow(visWithIdx,_messageVirtualKeepTailCount())" in UI_JS
    assert "const renderHeadVisWithIdx=visWithIdx.slice(windowStart, windowEnd)" in UI_JS
    assert "const renderTailStart=virtualWindow.virtualized?Math.max(windowEnd, virtualWindow.tailStart):windowEnd" in UI_JS
    assert "const renderTailVisWithIdx=virtualWindow.virtualized&&renderTailStart<visWithIdx.length" in UI_JS
    assert "const renderVisWithIdx=renderHeadVisWithIdx.concat(renderTailVisWithIdx)" in UI_JS
    assert "if(virtualWindow.virtualized&&virtualWindow.bottomPad>0&&vi===headRenderCount)" in UI_JS


def test_load_earlier_only_pages_server_history_and_preserves_scroll():
    assert "function _wireMessageWindowLoadEarlierButton()" in UI_JS
    assert "if(typeof _loadOlderMessages==='function') _loadOlderMessages();" in UI_JS
    assert "if(hasServerOlder){" in UI_JS
    assert "if(virtualWindow.virtualized&&virtualWindow.topPad>0)" in UI_JS
    assert "_messageRenderWindowSize=_currentMessageRenderWindowSize()+Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT);" in SESSIONS_JS
    assert "renderMessages({ preserveScroll: true });" in SESSIONS_JS
    assert "_scheduleMessageVirtualizedRender();" in UI_JS


def test_windowed_render_keeps_streaming_and_tool_activity_anchored_to_rendered_messages():
    assert "_scrollAfterMessageRender(preserveScroll, scrollSnapshot);" in UI_JS
    assert "const assistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);" in UI_JS
    assert "if(aIdx<assistantIdxs[0]) continue;" in UI_JS
    assert "const renderedAssistantIdxs=[...assistantSegments.keys()].sort((a,b)=>a-b);" in UI_JS
    assert "const seg=assistantSegments.get(mi);" in UI_JS


def test_window_state_participates_in_cache_and_cached_button_is_rewired():
    assert "cached.renderWindowKey===renderWindowKey" in UI_JS
    assert "cached.signature===renderSignature" in UI_JS
    assert "_sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowKey,signature:renderSignature})" in UI_JS
    assert "_messageVirtualWindowKey=renderWindowKey" in UI_JS
    assert "function _wireMessageWindowLoadEarlierButton()" in UI_JS
    assert "_wireMessageWindowLoadEarlierButton();" in UI_JS
    assert UI_JS.count("_wireMessageWindowLoadEarlierButton();") >= 2


def test_virtualization_affordances_have_styling_hooks():
    assert "message-window-load-earlier" in UI_JS
    assert ".message-window-load-earlier" in CSS
    assert ".message-virtual-spacer" in CSS
    assert "border-radius:999px" in CSS


def test_measurement_rerenders_are_bounded_per_virtual_window_cycle():
    assert "const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS=2;" in UI_JS
    assert "function _messageVirtualMeasurementCycleKeyFor(windowMetrics)" in UI_JS
    assert "function _scheduleMessageVirtualMeasurementRefresh(windowMetrics)" in UI_JS
    assert "if(_messageVirtualMeasurementRetryCount>=MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS){" in UI_JS
    assert "_scheduleMessageVirtualMeasurementRefresh(virtualWindow);" in UI_JS
    assert "_markMessageVirtualMeasurementsSettled(virtualWindow);" in UI_JS


def test_measurement_retry_budget_not_reset_by_cycle_key_change():
    """#6654/#6717: the cycle-key branch of _scheduleMessageVirtualMeasurementRefresh
    must record the new key but never reset _messageVirtualMeasurementRetryCount.
    Resetting on key change lets WebKit's A->B->A->B metric oscillation renew
    the two-render budget forever, keeping the rAF/measure loop alive. The ONLY
    reset allowed inside the scheduler is the per-burst one: when a NEW
    externally initiated cycle begins (no burst active), guarded by
    if(!_messageVirtualMeasurementBurstActive)."""
    idx = UI_JS.index("function _scheduleMessageVirtualMeasurementRefresh(windowMetrics)")
    end = UI_JS.index("function _markMessageVirtualMeasurementsSettled", idx)
    body = UI_JS[idx:end]
    # The key-change branch must only assign the key.
    assert "if(_messageVirtualMeasurementCycleKey!==cycleKey){" in body
    key_branch_start = body.index("if(_messageVirtualMeasurementCycleKey!==cycleKey){")
    key_branch_end = body.index("}", key_branch_start)
    key_branch = body[key_branch_start:key_branch_end]
    assert "_messageVirtualMeasurementRetryCount=0;" not in key_branch, (
        "cycle-key change must not reset the retry budget (issue #6654)"
    )
    # The per-burst reset is allowed only at the start of a new external cycle.
    assert "if(!_messageVirtualMeasurementBurstActive){" in body
    guard_pos = body.index("if(!_messageVirtualMeasurementBurstActive){")
    reset_pos = body.index("_messageVirtualMeasurementRetryCount=0;")
    assert guard_pos < reset_pos, (
        "the budget reset must be guarded by the burst-active check "
        "(per-burst lifecycle, issue #6717)"
    )
    # The budget guard and increment must still be present.
    assert "if(_messageVirtualMeasurementRetryCount>=MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS){" in body
    assert "_messageVirtualMeasurementRetryCount++;" in body
