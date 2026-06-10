"""Regression tests for preserving live streams across session switches."""
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    brace = src.find("){", start)
    assert brace != -1, f"{name}() body not found"
    brace += 1
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[brace + 1 : i - 1]


def _function_decl(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    brace = src.find("){", start)
    assert brace != -1, f"{name}() body not found"
    brace += 1
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[start:i]


def test_attach_live_stream_reuses_existing_same_stream_transport():
    """Returning to a running session must not tear down its same SSE stream.

    The server-side stream queue is not a replay log. If a sidebar switch back
    to the running session closes and reopens the same EventSource, there is a
    narrow window where stream events can be consumed by the old transport but
    no longer represented in the pane/cache. The same session/stream pair should
    therefore reuse the existing transport.
    """
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    close_pos = body.find("\n  closeLiveStream(activeSid);\n")
    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    assert reuse_pos != -1, "attachLiveStream() should check for an existing live stream"
    assert close_pos != -1, "attachLiveStream() should still close stale/different streams"
    assert reuse_pos < close_pos, "same-stream reuse must run before closeLiveStream(activeSid)"
    assert "existingLive.streamId===streamId" in body
    assert "existingLive.source.readyState===EventSource.OPEN" in body
    assert "(!reconnecting&&existingLive.source.readyState===EventSource.CONNECTING)" in body
    assert "return" in body[reuse_pos:close_pos]


def test_attach_live_stream_reconnect_does_not_reuse_connecting_transport():
    """Explicit reattach must reopen a stale CONNECTING EventSource.

    A page can keep a same-stream EventSource object in CONNECTING while the
    server has no SSE subscriber. Reconnect paths from loadSession() should not
    treat that object as healthy, or the live pane remains blank despite the
    backend stream still emitting events.
    """
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    close_pos = body.find("\n  closeLiveStream(activeSid);\n")
    assert reuse_pos != -1
    assert close_pos != -1
    reuse_block = body[reuse_pos:close_pos]
    compact = re.sub(r"\s+", "", reuse_block)
    assert "existingLive.source.readyState===EventSource.OPEN" in reuse_block
    assert "(!reconnecting&&existingLive.source.readyState===EventSource.CONNECTING)" in compact
    assert "existingLive.source.readyState!==EventSource.CLOSED" not in reuse_block


def test_attach_live_stream_closes_other_session_streams_before_opening_new_one():
    """Only the selected conversation pane should hold an open chat SSE transport."""
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    helper = _function_body(MESSAGES_JS, "closeOtherLiveStreams")

    helper_compact = helper.replace(" ", "")
    assert "Object.keys(LIVE_STREAMS)" in helper
    assert "if(sid!==activeSid)closeLiveStream(sid)" in helper_compact

    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    close_other_pos = body.find("closeOtherLiveStreams(activeSid)")
    close_current_pos = body.find("\n  closeLiveStream(activeSid);\n")
    assert close_other_pos != -1, "attachLiveStream() should prune background chat EventSources"
    assert reuse_pos < close_other_pos < close_current_pos, (
        "same-stream reuse should happen before pruning, and pruning should happen "
        "before replacing the active session transport"
    )


def test_attach_live_stream_updates_uploads_before_same_stream_reuse():
    """Reusing transport must not skip per-session uploaded attachment state."""
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    upload_pos = body.find("if(uploaded.length) INFLIGHT[activeSid].uploaded=[...uploaded]")
    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    close_pos = body.find("\n  closeLiveStream(activeSid);\n")
    assert upload_pos != -1
    assert reuse_pos != -1
    assert close_pos != -1
    assert upload_pos < reuse_pos < close_pos


def test_attach_live_stream_different_stream_still_reopens_transport():
    """A new stream id for the same session must not reuse the old transport."""
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    close_pos = body.find("\n  closeLiveStream(activeSid);\n")
    assert reuse_pos != -1
    assert close_pos != -1
    reuse_block = body[reuse_pos:close_pos]
    assert "existingLive.streamId===streamId" in reuse_block
    assert "existingLive.streamId!==streamId" not in reuse_block
    assert "return" in reuse_block
    assert reuse_pos < close_pos


def test_load_session_reattach_path_uses_attach_live_stream_for_running_sessions():
    """The session switch-back path should still route through attachLiveStream()."""
    body = _function_body(SESSIONS_JS, "loadSession")
    active_pos = body.find("const activeStreamId=S.session.active_stream_id||null")
    reattach_pos = body.find("attachLiveStream(sid, activeStreamId")
    assert active_pos != -1
    assert reattach_pos != -1
    assert active_pos < reattach_pos
    assert "{reconnecting:true}" in body[reattach_pos : reattach_pos + 200]


def test_load_session_same_sid_noop_does_not_mask_pending_switch_back():
    """Clicking back to the prior session during a pending switch must reload it.

    loadSession() clears S.messages before the metadata fetch for the target
    session returns. During that small window S.session still points at the
    previous session. A fast click back to that previous sid used to hit the
    same-session no-op guard and leave the pane empty/Loading forever.
    """
    body = _function_body(SESSIONS_JS, "loadSession")
    compact = re.sub(r"\s+", "", body)
    guard = "if(currentSid===sid&&!forceReload&&!_loadingSessionId)return;"
    assert guard in compact, (
        "same-session no-op must be disabled while another loadSession() call "
        "is in flight, otherwise switching away and immediately back can keep "
        "the previous session's cleared transcript"
    )
    assert compact.find(guard) < compact.find("_loadingSessionId=sid;")


def test_load_session_preserves_existing_worklog_content_without_destructive_fallback():
    """Switching back to an active stream with live Worklog content should be treated as restored.

    If loadSession() sees .wl-reason or .tool-card-row already in #liveAssistantTurn,
    the destructive fallback must not call clearLiveToolCards() and rebuild a blank
    Running shell over the preserved timeline.
    """
    body = _function_body(SESSIONS_JS, "loadSession")
    content_pos = body.find("const hasCurrentWorklogContent=")
    clear_pos = body.find("clearLiveToolCards();", content_pos)
    assert content_pos != -1
    assert clear_pos != -1
    between = body[content_pos:clear_pos]
    compact = re.sub(r"\s+", "", between)
    assert "if(hasCurrentWorklogContent)restoredLiveTurn=true" in compact, (
        "Existing live Worklog content must mark the turn restored before the "
        "clearLiveToolCards() fallback runs."
    )


def test_tool_events_are_guarded_against_stale_session_and_stream():
    """Delayed tool events from an old EventSource must not mutate the current session DOM."""
    tool_handler = MESSAGES_JS.split("source.addEventListener('tool',e=>{", 1)[1].split("source.addEventListener('tool_complete'", 1)[0]
    complete_handler = MESSAGES_JS.split("source.addEventListener('tool_complete',e=>{", 1)[1].split("source.addEventListener('approval'", 1)[0]
    for handler in (tool_handler, complete_handler):
        assert "_terminalStateReached||_streamFinalized" in handler
        assert "S.session.session_id!==activeSid" in handler
        assert "S.activeStreamId!==streamId" in handler
        assert "appendLiveToolCard(tc,{sessionId:activeSid,streamId})" in handler


def test_close_live_stream_marks_inflight_for_reattach_on_return():
    """When closeLiveStream() tears down a still-active SSE transport (e.g. the
    user switched to another session), the corresponding INFLIGHT entry must be
    flagged so loadSession() reopens the SSE on return.

    Without this flag the in-memory INFLIGHT entry stays as it was (no
    `reattach:true`, which is only set on the storage-load path), so
    loadSession()'s reattach branch is skipped — the SSE is never reopened and
    the user sees no streamed tokens until the LLM finishes and a metadata
    refresh swaps in the final reply.
    """
    body = _function_body(MESSAGES_JS, "closeLiveStream")
    assert "INFLIGHT" in body, (
        "closeLiveStream() must touch INFLIGHT so loadSession() reattaches the "
        "SSE when the user switches back to a still-streaming session"
    )
    snapshot_pos = body.find("snapshotLiveTurnHtmlForSession(sessionId)")
    hide_pos = body.find("hideLiveRunStatus")
    assert snapshot_pos != -1, "closeLiveStream() must snapshot the visible Worklog before tearing down the pane"
    assert hide_pos != -1 and snapshot_pos < hide_pos
    assert re.search(r"INFLIGHT\[\w+\]\s*&&\s*\(?INFLIGHT\[\w+\]\.reattach\s*=\s*true", body) \
           or re.search(r"if\s*\(\s*INFLIGHT\[\w+\]\s*\)\s*INFLIGHT\[\w+\]\.reattach\s*=\s*true", body) \
           or re.search(r"if\s*\(\s*INFLIGHT\[\w+\]\s*\)\s*\{[^}]*INFLIGHT\[\w+\]\.reattach\s*=\s*true", body, re.DOTALL), (
        "closeLiveStream() must set INFLIGHT[sessionId].reattach = true "
        "(guarded by an existence check) so loadSession()'s reattach branch fires"
    )


def test_close_other_live_streams_triggers_reattach_for_backgrounded_sessions():
    """closeOtherLiveStreams() during session switch must mark every closed
    background session for reattach. Otherwise switching back to a session whose
    stream was closed during the switch leaves the SSE permanently disconnected.
    """
    helper_body = _function_body(MESSAGES_JS, "closeOtherLiveStreams")
    close_body = _function_body(MESSAGES_JS, "closeLiveStream")
    # closeOtherLiveStreams delegates per-session teardown to closeLiveStream,
    # so the reattach flag must be set inside closeLiveStream itself for the
    # chain to work — this guards the indirection.
    assert "closeLiveStream(sid)" in helper_body.replace(" ", ""), (
        "closeOtherLiveStreams() must delegate teardown to closeLiveStream()"
    )
    assert "reattach" in close_body, (
        "closeLiveStream() must set the reattach flag so closeOtherLiveStreams() "
        "propagates the reattach intent to every backgrounded session"
    )


def test_load_session_reattaches_when_inflight_is_in_memory_and_marked_for_reattach():
    """The session-switch return path must hit attachLiveStream() even when
    INFLIGHT[sid] is already in memory (i.e. wasn't loaded from storage).

    Before the fix, only the storage-load path set `reattach:true` on INFLIGHT,
    so a switch-back through an in-memory INFLIGHT entry skipped the reattach
    branch. Once closeLiveStream() also sets reattach=true, the existing
    `INFLIGHT[sid].reattach && activeStreamId` gate is enough — this test
    pins the gate's shape so future refactors don't drop the flag check.
    """
    body = _function_body(SESSIONS_JS, "loadSession")
    inflight_idx = body.find("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = body[inflight_idx : inflight_idx + 4200]
    assert "INFLIGHT[sid].reattach" in inflight_block, (
        "loadSession()'s INFLIGHT branch must gate the SSE reattach on the "
        "reattach flag so closeLiveStream()'s marking flows through"
    )
    reattach_gate = re.search(
        r"if\(INFLIGHT\[sid\]\.reattach\s*&&\s*activeStreamId.*?attachLiveStream\(sid, activeStreamId",
        inflight_block,
        re.DOTALL,
    )
    assert reattach_gate, (
        "loadSession() must reattach via attachLiveStream() when "
        "INFLIGHT[sid].reattach && activeStreamId"
    )


def test_load_session_attaches_sse_before_auxiliary_work():
    """Live SSE reattach is the primary recovery path.

    Rendering, workspace refresh, badges, and side-channel pollers must not run
    before attachLiveStream(), because any synchronous failure in those paths
    would otherwise leave the backend stream active with no browser subscriber.
    """
    body = _function_body(SESSIONS_JS, "loadSession")
    active_branch = body[body.find("if(activeStreamId){") : body.find("}else{", body.find("if(activeStreamId){"))]
    active_attach = active_branch.find("attachLiveStream(sid, activeStreamId")
    assert active_attach != -1
    for marker in (
        "updateSendBtn();",
        "syncTopbar();renderMessages();appendThinking();loadDir('.');",
        "updateQueueBadge(sid);",
        "startApprovalPolling(sid)",
    ):
        pos = active_branch.find(marker)
        assert pos != -1, f"{marker} not found in active-stream branch"
        assert active_attach < pos, f"attachLiveStream() must run before {marker}"


def test_running_reattach_refreshes_single_live_assistant_from_server_progress():
    """Switching back to a running session should keep one visible assistant
    source for the active turn.

    The server transcript can already contain interim assistant progress while
    INFLIGHT also holds the live assistant tail. Reattach must refresh the live
    tail from the server copy, drop the server's active-turn assistant rows, and
    render one `_live` assistant instead of duplicating or deleting progress.
    """
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

let base = [
  {{role:'user', content:'go'}},
  {{role:'assistant', content:'First progress.'}},
  {{role:'tool', content:'{{}}'}},
  {{role:'assistant', content:'Second progress.'}},
];
let inflight = [
  {{role:'user', content:'go'}},
  {{role:'assistant', _live:true, content:'First progress.\\n\\nSecond progress.\\n\\nSecond progress.'}},
];
assert.strictEqual(_prepareRunningLiveTail(base, inflight), true);
assert.strictEqual(inflight[1].content, 'First progress.\\n\\nSecond progress.');
base = _dropCurrentTurnAssistantMessages(base);
let merged = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(merged.filter(m => m.role === 'assistant').length, 1);
assert.strictEqual(merged[merged.length - 1]._live, true);
assert.strictEqual(merged[merged.length - 1].content, 'First progress.\\n\\nSecond progress.');

base = [
  {{role:'user', content:'go'}},
  {{role:'assistant', content:'First progress.'}},
  {{role:'tool', content:'{{}}'}},
  {{role:'assistant', content:'Second progress.'}},
];
inflight = [
  {{role:'user', content:'go'}},
  {{role:'assistant', _live:true, content:'First progress.'}},
];
assert.strictEqual(_prepareRunningLiveTail(base, inflight), true);
assert.strictEqual(inflight[1].content, 'First progress.\\n\\nSecond progress.');
base = _dropCurrentTurnAssistantMessages(base);
merged = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(merged.filter(m => m.role === 'assistant').length, 1);
assert.strictEqual(merged[merged.length - 1]._live, true);
assert.strictEqual(merged[merged.length - 1].content, 'First progress.\\n\\nSecond progress.');
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_running_reattach_rebuilds_live_assistant_from_last_text_before_activity():
    """A fast session switch can happen after INFLIGHT.lastAssistantText was
    updated but before the live assistant message/DOM snapshot caught up.

    Reattach must rebuild the structured `_live` assistant before restoring
    Activity, otherwise the UI can show only the Activity group until another
    switch or token causes the text segment to reappear.
    """
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

let base = [{{role:'user', content:'go'}}];
let inflightState = {{
  lastAssistantText:'Recovered progress text.',
  lastReasoningText:'',
  messages:[{{role:'user', content:'go'}}],
}};
assert.strictEqual(_ensureInflightLiveAssistantMessage(inflightState), true);
assert.strictEqual(inflightState.messages.length, 2);
assert.strictEqual(inflightState.messages[1]._live, true);
assert.strictEqual(inflightState.messages[1].content, 'Recovered progress text.');
assert.strictEqual(_prepareRunningLiveTail(base, inflightState.messages), true);
base = _dropCurrentTurnAssistantMessages(base);
const merged = _mergeInflightTailMessages(base, inflightState.messages);
assert.strictEqual(merged.filter(m => m.role === 'assistant').length, 1);
assert.strictEqual(merged[merged.length - 1]._live, true);
assert.strictEqual(merged[merged.length - 1].content, 'Recovered progress text.');
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_running_reattach_projects_live_text_into_activity_burst_segments():
    """Fallback reattach should rebuild the same process-text/tool-burst
    timeline even when the DOM snapshot is unavailable.
    """
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

const inflight = {{
  currentActivityBurstId: 2,
  activityBurstAnchors: [
    {{id: 1, textEnd: 'First progress.'.length}},
    {{id: 2, textEnd: 'First progress.\\n\\nSecond progress.'.length}},
  ],
  messages: [
    {{role:'user', content:'go'}},
    {{role:'assistant', _live:true, content:'First progress.\\n\\nSecond progress.\\n\\nTail progress.'}},
  ],
}};
const projected = _projectInflightMessagesForActivityBursts(inflight);
assert.strictEqual(projected.length, 4);
assert.strictEqual(projected[1].content, 'First progress.');
assert.strictEqual(projected[1]._activityBurstId, 1);
assert.strictEqual(projected[2].content, 'Second progress.');
assert.strictEqual(projected[2]._activityBurstId, 2);
assert.strictEqual(projected[3].content, 'Tail progress.');
assert.strictEqual(projected[3]._activityBurstId, 2);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_running_reattach_reprojects_segmented_live_tail_without_duplicate_prefix():
    """A reconnect write can leave a segmented live tail plus a full accumulator.

    syncInflightAssistantMessage() updates the last `_live` message from the
    full assistant accumulator.  On the next session switch,
    _projectInflightMessagesForActivityBursts() must replace the whole live
    tail with one projection from the accumulator; keeping the earlier
    projected segments and also splitting the full accumulator repeats already
    visible process text.
    """
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

const fullText = 'First progress.\\n\\nSecond progress.\\n\\nTail progress.';
const inflight = {{
  currentActivityBurstId: 2,
  currentLiveSegmentSeq: 2,
  activityBurstAnchors: [
    {{id: 1, textEnd: 'First progress.'.length}},
    {{id: 2, textEnd: 'First progress.\\n\\nSecond progress.'.length}},
  ],
  messages: [
    {{role:'user', content:'go'}},
    {{role:'assistant', _live:true, content:'First progress.', _activityBurstId:1, _liveSegmentSeq:1}},
    {{role:'assistant', _live:true, content:fullText, _activityBurstId:2, _liveSegmentSeq:2}},
  ],
}};
const projected = _projectInflightMessagesForActivityBursts(inflight);
assert.deepStrictEqual(
  projected.filter(m => m.role === 'assistant').map(m => m.content),
  ['First progress.', 'Second progress.', 'Tail progress.']
);
assert.deepStrictEqual(
  projected.filter(m => m.role === 'assistant').map(m => m._liveSegmentSeq),
  [1, 2, 3]
);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_running_reattach_keeps_segmented_tail_when_last_segment_is_not_accumulator():
    """Normal segmented live tails must not be collapsed from the last segment.

    The duplicate-prefix repair only applies when the last live message already
    contains earlier live segment text. If the last segment is only its own tail,
    the prior live segments are still the source of truth and must be preserved.
    """
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

const inflight = {{
  currentActivityBurstId: 2,
  currentLiveSegmentSeq: 2,
  activityBurstAnchors: [
    {{id: 1, textEnd: 'First progress.'.length}},
    {{id: 2, textEnd: 'First progress.\\n\\nSecond progress.'.length}},
  ],
  messages: [
    {{role:'user', content:'go'}},
    {{role:'assistant', _live:true, content:'First progress.', _activityBurstId:1, _liveSegmentSeq:1}},
    {{role:'assistant', _live:true, content:'Second progress.', _activityBurstId:2, _liveSegmentSeq:2}},
  ],
}};
const projected = _projectInflightMessagesForActivityBursts(inflight);
assert.deepStrictEqual(
  projected.filter(m => m.role === 'assistant').map(m => m.content),
  ['First progress.', 'Second progress.']
);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_running_reattach_aliases_empty_activity_bursts_to_previous_text_segment():
    """Duplicate boundaries with no new text should not leave tool activity
    attached to a burst id that has no visible assistant segment.
    """
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

const inflight = {{
  currentActivityBurstId: 2,
  activityBurstAnchors: [
    {{id: 1, textEnd: 'First progress.'.length}},
    {{id: 2, textEnd: 'First progress.'.length}},
  ],
  toolCalls: [
    {{name:'read_file', activityBurstId: 2}},
  ],
  messages: [
    {{role:'user', content:'go'}},
    {{role:'assistant', _live:true, content:'First progress.'}},
  ],
}};
    const projected = _projectInflightMessagesForActivityBursts(inflight);
    assert.strictEqual(projected.length, 2);
    assert.strictEqual(projected[1].content, 'First progress.');
    assert.strictEqual(projected[1]._activityBurstId, 1);
    assert.strictEqual(inflight.toolCalls[0].activityBurstId, 1);
    assert.strictEqual(inflight.toolCalls[0].activitySegmentSeq, 1);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_running_reattach_backfills_tool_segment_seq_for_burst_anchors():
    """When reattaching a running stream, persisted tool calls without
    activitySegmentSeq should be rebound to the projected live segment sequence
    so tool cards land next to their triggering text, not at the tail.
    """
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

const inflight = {{
  currentActivityBurstId: 3,
  activityBurstAnchors: [
    {{id: 1, textEnd: 'First progress.'.length}},
    {{id: 2, textEnd: 'First progress.\\n\\nSecond progress.'.length}},
  ],
  toolCalls: [
    {{name:'read_file', activityBurstId: 1, activitySegmentSeq: undefined}},
    {{name:'search', activityBurstId: 2, activitySegmentSeq: undefined}},
  ],
  messages: [
    {{role:'user', content:'go'}},
    {{role:'assistant', _live:true, content:'First progress.\\n\\nSecond progress.\\n\\nTail progress.'}},
  ],
}};
const projected = _projectInflightMessagesForActivityBursts(inflight);
assert.strictEqual(projected.length, 4);
assert.strictEqual(projected[1]._liveSegmentSeq, 1);
    assert.strictEqual(projected[2]._liveSegmentSeq, 2);
    assert.strictEqual(projected[3]._liveSegmentSeq, 3);
    assert.strictEqual(inflight.toolCalls[0].activitySegmentSeq, 1);
    assert.strictEqual(inflight.toolCalls[1].activitySegmentSeq, 2);
    """
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_upsert_live_tool_call_preserves_start_seq_for_complete():
    """tool_complete should inherit the seq captured by the matching tool_start.

    This guarantees a single in-flight tool row per call and keeps Activity
    placement stable even when complete arrives on a different segment.
    """
    assert NODE, "node not on PATH"
    helper_defs = "\n".join([
        _function_decl(MESSAGES_JS, "_stableStringify"),
        _function_decl(MESSAGES_JS, "_hashString"),
        _function_decl(MESSAGES_JS, "_toolCallSignature"),
        _function_decl(MESSAGES_JS, "_liveToolTid"),
        _function_decl(MESSAGES_JS, "_coerceLiveToolCallSignature"),
        _function_decl(MESSAGES_JS, "_coerceLiveToolCallSeq"),
        _function_decl(MESSAGES_JS, "_currentLiveToolAnchor"),
        _function_decl(MESSAGES_JS, "_findPendingLiveToolCallIndex"),
        _function_decl(MESSAGES_JS, "upsertLiveToolCall"),
    ])
    script = (
        "const assert = require('assert');\n"
        f"{helper_defs}\n\n"
        "const uploaded=[];\n"
        "let activeSid='sid';\n"
        "const INFLIGHT={};\n"
        "const S={\"toolCalls\":[],\"messages\":[]};\n"
        "let assistantRow={getAttribute:()=>\"7\"};\n"
        "let _assistantSegmentSeq=7;\n"
        "let _currentLiveSegmentSeq=7;\n"
        "let _currentActivityBurstId=1;\n"
        "const assistantBody=null;\n"
        "global.persistInflightState=()=>{};\n"
        "global.S=S;\n"
        "global.INFLIGHT=INFLIGHT;\n"
        "global.activeSid=activeSid;\n"
        "global.uploaded=uploaded;\n"
        "global.assistantRow=assistantRow;\n"
        "global.assistantBody=assistantBody;\n"
        "global._assistantSegmentSeq=_assistantSegmentSeq;\n"
        "global._currentLiveSegmentSeq=_currentLiveSegmentSeq;\n"
        "global._currentActivityBurstId=_currentActivityBurstId;\n\n"
        "const start=upsertLiveToolCall({\"name\":\"read_file\",\"args\":{\"path\":\"/tmp/a\"},\"preview\":\"start\"}, 'start');\n"
        "assert(start);\n"
        "start.started_at=111;\n"
        "assert.strictEqual(start.activitySegmentSeq, 7);\n"
        "assert.strictEqual(start._toolCallStartSeq, 7);\n"
        "_currentLiveSegmentSeq=11;\n"
        "_assistantSegmentSeq=11;\n"
        "const complete=upsertLiveToolCall({\"name\":\"read_file\",\"args\":{\"path\":\"/tmp/a\"},\"duration\":2}, 'complete');\n"
        "assert(complete);\n"
        "assert.strictEqual(complete.activitySegmentSeq, 7);\n"
        "assert.strictEqual(complete._toolCallStartSeq, 7);\n"
        "assert.strictEqual(complete===start, true);\n"
    )
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_upsert_live_tool_call_complete_matches_by_name_burst_without_tid():
    """A complete event without tid must still match the in-flight tool by name+burst.

    This is needed when the provider's complete stream payload does not carry a
    stable tool call id.
    """
    assert NODE, "node not on PATH"
    helper_defs = "\n".join([
        _function_decl(MESSAGES_JS, "_stableStringify"),
        _function_decl(MESSAGES_JS, "_hashString"),
        _function_decl(MESSAGES_JS, "_toolCallSignature"),
        _function_decl(MESSAGES_JS, "_liveToolTid"),
        _function_decl(MESSAGES_JS, "_coerceLiveToolCallSignature"),
        _function_decl(MESSAGES_JS, "_coerceLiveToolCallSeq"),
        _function_decl(MESSAGES_JS, "_currentLiveToolAnchor"),
        _function_decl(MESSAGES_JS, "_findPendingLiveToolCallIndex"),
        _function_decl(MESSAGES_JS, "upsertLiveToolCall"),
    ])
    script = (
        "const assert = require('assert');\n"
        f"{helper_defs}\n\n"
        "const uploaded=[];\n"
        "let activeSid='sid';\n"
        "const INFLIGHT={\"sid\":{\"toolCalls\":[{\"name\":\"search\",\"activityBurstId\":3,\"activitySegmentSeq\":4,\"_toolCallStartSeq\":4,\"_liveToolCallSignature\":\"search|3|4|{\\\"query\\\":\\\"x\\\"}\",\"done\":false}],\"messages\":[],\"uploaded\":[]}};\n"
        "const S={\"toolCalls\":[],\"messages\":[]};\n"
        "let _assistantSegmentSeq=9;\n"
        "let _currentLiveSegmentSeq=9;\n"
        "let _currentActivityBurstId=3;\n"
        "let assistantRow={getAttribute:()=>\"7\"};\n"
        "let assistantBody=null;\n"
        "global.persistInflightState=()=>{};\n"
        "global.S=S;\n"
        "global.INFLIGHT=INFLIGHT;\n"
        "global.activeSid=activeSid;\n"
        "global.uploaded=uploaded;\n"
        "global.assistantRow=assistantRow;\n"
        "global.assistantBody=assistantBody;\n"
        "global._assistantSegmentSeq=_assistantSegmentSeq;\n"
        "global._currentLiveSegmentSeq=_currentLiveSegmentSeq;\n"
        "global._currentActivityBurstId=_currentActivityBurstId;\n\n"
        "const complete=upsertLiveToolCall({\"name\":\"search\",\"args\":{\"query\":\"x\"}}, 'complete');\n"
        "assert(complete);\n"
        "assert.strictEqual(complete.activitySegmentSeq, 4);\n"
        "assert.strictEqual(complete._toolCallStartSeq, 4);\n"
        "assert.strictEqual(INFLIGHT[activeSid].toolCalls.length, 1);\n"
    )
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_upsert_flags_orphan_complete_but_not_normal_start_complete():
    """`_createdByComplete` must be set ONLY when a tool_complete creates a fresh
    record with no matching tool_start (orphan completion). The SSE handler uses
    this flag to decide whether to force a fresh segment: an orphan completion is
    a real tail boundary, but a normal in-place start->complete update must leave
    the active segment untouched (otherwise interleaved completions fragment the
    streaming text into spurious empty segments)."""
    assert NODE, "node not on PATH"
    helper_defs = "\n".join([
        _function_decl(MESSAGES_JS, "_stableStringify"),
        _function_decl(MESSAGES_JS, "_hashString"),
        _function_decl(MESSAGES_JS, "_toolCallSignature"),
        _function_decl(MESSAGES_JS, "_liveToolTid"),
        _function_decl(MESSAGES_JS, "_coerceLiveToolCallSignature"),
        _function_decl(MESSAGES_JS, "_coerceLiveToolCallSeq"),
        _function_decl(MESSAGES_JS, "_currentLiveToolAnchor"),
        _function_decl(MESSAGES_JS, "_findPendingLiveToolCallIndex"),
        _function_decl(MESSAGES_JS, "upsertLiveToolCall"),
    ])
    script = (
        "const assert = require('assert');\n"
        f"{helper_defs}\n\n"
        "const uploaded=[];\n"
        "let activeSid='sid';\n"
        "const INFLIGHT={};\n"
        "const S={\"toolCalls\":[],\"messages\":[]};\n"
        "let assistantRow={getAttribute:()=>\"7\"};\n"
        "let assistantBody=null;\n"
        "let _assistantSegmentSeq=7;\n"
        "let _currentLiveSegmentSeq=7;\n"
        "let _currentActivityBurstId=1;\n"
        "global.persistInflightState=()=>{};\n"
        "global.S=S;\n"
        "global.INFLIGHT=INFLIGHT;\n"
        "global.activeSid=activeSid;\n"
        "global.uploaded=uploaded;\n"
        "global.assistantRow=assistantRow;\n"
        "global.assistantBody=assistantBody;\n"
        "global._assistantSegmentSeq=_assistantSegmentSeq;\n"
        "global._currentLiveSegmentSeq=_currentLiveSegmentSeq;\n"
        "global._currentActivityBurstId=_currentActivityBurstId;\n\n"
        # Case A: normal start -> complete. The start record must NOT be flagged,
        # and the matching complete must reuse it without setting the flag.
        "const start=upsertLiveToolCall({\"name\":\"read_file\",\"args\":{\"path\":\"/tmp/a\"},\"tid\":\"T1\"}, 'start');\n"
        "assert(start);\n"
        "assert.strictEqual(!!start._createdByComplete, false, 'tool_start must not be flagged');\n"
        "const completeMatched=upsertLiveToolCall({\"name\":\"read_file\",\"args\":{\"path\":\"/tmp/a\"},\"tid\":\"T1\"}, 'complete');\n"
        "assert.strictEqual(completeMatched===start, true, 'complete must reuse the start record');\n"
        "assert.strictEqual(!!completeMatched._createdByComplete, false, 'in-place complete must not be flagged');\n"
        "assert.strictEqual(INFLIGHT[activeSid].toolCalls.length, 1, 'no duplicate record');\n\n"
        # Case B: orphan complete (no prior start). The freshly created record
        # MUST be flagged so the handler forces a fresh segment.
        "const orphan=upsertLiveToolCall({\"name\":\"write_file\",\"args\":{\"path\":\"/tmp/b\"},\"tid\":\"T2\"}, 'complete');\n"
        "assert(orphan);\n"
        "assert.strictEqual(orphan===start, false);\n"
        "assert.strictEqual(orphan._createdByComplete, true, 'orphan complete must be flagged');\n"
        "assert.strictEqual(orphan.done, true);\n"
        "assert.strictEqual(INFLIGHT[activeSid].toolCalls.length, 2);\n"
    )
    result = subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_tool_complete_handler_gates_segment_reset_on_orphan_flag():
    """The tool_complete SSE handler must only force a fresh segment for orphan
    completions (`_createdByComplete`), updating the card in place otherwise."""
    handler_start = MESSAGES_JS.find("source.addEventListener('tool_complete'")
    assert handler_start != -1
    handler_end = MESSAGES_JS.find("source.addEventListener('approval'", handler_start)
    assert handler_end != -1
    handler = MESSAGES_JS[handler_start:handler_end]
    # The reset trio must live behind the orphan-flag branch.
    guard_pos = handler.find("if(tc._createdByComplete)")
    reset_pos = handler.find("_resetAssistantSegment()")
    assert guard_pos != -1, "tool_complete must branch on tc._createdByComplete"
    assert reset_pos != -1 and guard_pos < reset_pos, (
        "segment reset must be gated behind the orphan-completion branch"
    )
    # The non-orphan branch must still place the card (in place).
    assert handler.count("appendLiveToolCard(tc,{sessionId:activeSid,streamId})") >= 2, (
        "both orphan and in-place branches must append/update the tool card"
    )


def test_project_inflight_with_no_visible_anchor_maps_tools_to_run_anchor_segment():
    """Without a visible burst anchor, in-flight tools should still map to the first
    segment instead of falling back to the last segment in render order."""
    assert NODE, "node not on PATH"
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    helper_src = SESSIONS_JS[start:end]
    script = f"""
const assert = require('assert');
{helper_src}

const inflight = {{
  currentActivityBurstId: 2,
  activityBurstAnchors: [
    {{ id: 1, textEnd: 0 }},
  ],
  toolCalls: [
    {{name:'read_file', activityBurstId:0}},
  ],
  messages: [
    {{role:'user', content:'go'}},
    {{role:'assistant', _live:true, _activityBurstId: 2, content:'First progress line'}},
  ],
}};
const projected = _projectInflightMessagesForActivityBursts(inflight);
    assert.strictEqual(projected.length, 2);
assert.strictEqual(projected[1].content, 'First progress line');
assert.strictEqual(projected[1]._liveSegmentSeq, 1);
assert.strictEqual(inflight.toolCalls[0].activitySegmentSeq, 1);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_load_session_rebuilds_live_tail_before_snapshot_fallback():
    body = _function_body(SESSIONS_JS, "loadSession")
    ensure_pos = body.find("_ensureInflightLiveAssistantMessage(INFLIGHT[sid]);")
    inflight_pos = body.find("const inflightMessages=_projectInflightMessagesForActivityBursts(INFLIGHT[sid]);")
    prepare_pos = body.find("const liveTailPrepared=_prepareRunningLiveTail(S.messages,inflightMessages);")
    drop_assistant_pos = body.find("S.messages=_dropCurrentTurnAssistantMessages(S.messages);")
    merge_pos = body.find("S.messages=_mergeInflightTailMessages(S.messages,inflightMessages);")
    restore_pos = body.find("restoreLiveTurnHtmlForSession(sid)")
    assert ensure_pos != -1 and inflight_pos != -1
    assert prepare_pos != -1
    assert drop_assistant_pos != -1 and merge_pos != -1 and restore_pos != -1
    assert "delete INFLIGHT[sid].liveTurnHtml" not in body
    assert ensure_pos < inflight_pos < prepare_pos < drop_assistant_pos < merge_pos < restore_pos


def test_load_session_prefers_structured_inflight_state_over_live_turn_snapshot():
    """Structured INFLIGHT state is authoritative during reattach.

    The memory-only liveTurnHtml snapshot can be stale across session switches.
    If loadSession restores that DOM after renderMessages() rebuilt the
    per-burst live tail, old snapshots can alternately erase progress text and
    leave Activity groups piled at the bottom of the turn.
    """
    body = _function_body(SESSIONS_JS, "loadSession")
    structured_pos = body.find("const hasStructuredLiveState=!!(INFLIGHT[sid]&&(")
    restore_pos = body.find("restoreLiveTurnHtmlForSession(sid)")
    fallback_pos = body.find("if(!restoredLiveTurn){", restore_pos)
    assert structured_pos != -1, "loadSession must compute structured live-state presence"
    assert restore_pos != -1, "loadSession must still retain DOM snapshot fallback"
    assert fallback_pos != -1
    assert structured_pos < restore_pos < fallback_pos
    guard_block = body[structured_pos:fallback_pos]
    assert "lastAssistantText" in guard_block
    assert "lastReasoningText" in guard_block
    assert "activityBurstAnchors" in guard_block
    assert "toolCalls" in guard_block
    assert "if(!hasStructuredLiveState)" in guard_block
    assert "hasCurrentWorklogContent" in guard_block
    assert "if(hasCurrentWorklogContent) restoredLiveTurn=true;" in guard_block
    assert "else restoredLiveTurn=restoreLiveTurnHtmlForSession(sid);" in guard_block


def test_load_session_restores_worklog_shell_before_reattach_replay():
    """Reattaching before replay/new SSE should not leave the active stream blank."""
    body = _function_body(SESSIONS_JS, "loadSession")
    fallback_pos = body.find("if(!restoredLiveTurn){")
    assert fallback_pos != -1, "loadSession must have a live-turn fallback branch"
    fallback_block = body[fallback_pos:body.find("loadDir('.')", fallback_pos)]
    clear_pos = fallback_block.find("clearLiveToolCards();")
    shell_pos = fallback_block.find("ensureLiveWorklogShell()")
    legacy_pos = fallback_block.find("else appendThinking();")
    replay_pos = fallback_block.find("replayPersistedLiveToolCards();")
    invariant_pos = fallback_block.find("!liveTurn||!liveTurn.querySelector")
    assert clear_pos != -1, "fallback must clear stale live tool DOM first"
    assert shell_pos != -1, "fallback must restore a quiet live Worklog shell"
    assert legacy_pos != -1, "fallback should retain legacy thinking-card behavior"
    assert replay_pos != -1, "fallback must still replay persisted live tools"
    assert invariant_pos != -1, "reattach must enforce a Worklog shell even after an empty restored snapshot"
    assert clear_pos < shell_pos < replay_pos
    assert replay_pos < invariant_pos


def test_restore_succeeded_reconnect_replays_tool_cards():
    """When reconnect replay succeeds in restoring the live turn HTML, tool cards
    are still repainted from the persisted live-call list instead of waiting for a
    future SSE event to reintroduce them."""
    body = _function_body(SESSIONS_JS, "loadSession")
    replay_fn = body.find("const replayPersistedLiveToolCards=(opts)=>{")
    reattach_pos = body.find("if(INFLIGHT[sid].reattach&&activeStreamId&&typeof attachLiveStream==='function')")
    restore_pos = body.find("if(typeof restoreLiveTurnHtmlForSession==='function'){", reattach_pos if reattach_pos != -1 else 0)
    fallback_pos = body.find("if(!restoredLiveTurn){", restore_pos)
    restore_replay_pos = body.find("if(restoredLiveTurn&&didReconnect){", restore_pos)
    restore_replay_block = body[restore_replay_pos:fallback_pos]
    helper_replay_call = restore_replay_block.find("replayPersistedLiveToolCards({skipUnkeyedRestoredDuplicates:true});")
    assert reattach_pos != -1, "loadSession must keep the reconnect reattach branch"
    assert replay_fn != -1, "loadSession should extract live tool replay into a helper"
    assert restore_pos != -1, "loadSession must still execute restoreLiveTurnHtmlForSession"
    assert reattach_pos > replay_fn, "live-tool replay helper must be defined before reattach branch"
    assert restore_pos > reattach_pos, "restore/fallback branch should be after reattach handling in INFLIGHT flow"
    assert restore_replay_pos != -1, "restored live turns must explicitly replay tools on reconnect"
    assert helper_replay_call != -1, "replay helper must be executed so reconnect can repopulate tool cards"
    assert replay_fn < restore_replay_pos < fallback_pos, "restore+reconnect replay should run before fallback"
    assert restore_replay_block.strip().startswith("if(restoredLiveTurn&&didReconnect){")
    assert (
        "if(restoredLiveTurn&&didReconnect){"
        "replayPersistedLiveToolCards({skipUnkeyedRestoredDuplicates:true});"
        "}"
    ) in re.sub(r"\s+", "", restore_replay_block)


def test_restore_succeeded_reconnect_skips_unkeyed_restored_tool_duplicates():
    """Restored snapshots can already contain legacy tool rows without live tids.

    Replaying an unkeyed persisted tool over that restored DOM would append a
    duplicate, so the restore-success reconnect path should only replay unkeyed
    tools when the restored turn has no visible tool rows to preserve.
    """
    body = _function_body(SESSIONS_JS, "loadSession")
    replay_fn = body.find("const replayPersistedLiveToolCards=(opts)=>{")
    restore_replay_pos = body.find("if(restoredLiveTurn&&didReconnect){")
    fallback_pos = body.find("if(!restoredLiveTurn){", restore_replay_pos)
    assert replay_fn != -1, "loadSession should keep replay options on the helper"
    assert "const liveToolReplayId=(tc)=>" in body
    assert "tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id" in body
    helper_block = body[replay_fn:restore_replay_pos]
    assert "skipUnkeyedRestoredDuplicates" in helper_block
    assert "restoredLiveTurn.querySelector('.tool-card-row')" in helper_block
    assert "hasRestoredLiveToolRows&&!liveToolReplayId(tc)" in helper_block
    restore_block = body[restore_replay_pos:fallback_pos]
    assert "replayPersistedLiveToolCards({skipUnkeyedRestoredDuplicates:true});" in restore_block
    assert "replayPersistedLiveToolCards();" in body[fallback_pos:body.find("loadDir('.')", fallback_pos)]


def test_merge_inflight_tail_preserves_all_segmented_live_progress():
    """The reattach merge must keep every projected live progress segment.

    _projectInflightMessagesForActivityBursts() can split one live assistant
    accumulator into multiple _live messages.  If the merge starts at the last
    _live segment, the earlier process-text anchors disappear and Activity
    groups whose burst ids point to those anchors pile up at the bottom.
    """
    assert NODE, "node not on PATH"
    fn_start = SESSIONS_JS.index("function _mergeInflightTailMessages")
    fn_end = SESSIONS_JS.index("// Load older messages", fn_start)
    merge_fn = SESSIONS_JS[fn_start:fn_end]
    script = f"""
const assert = require('assert');
function _messageComparableText(m) {{ return String((m&&m.content)||'').trim(); }}
function _sameTranscriptMessage(a,b) {{
  return !!(a&&b&&a.role===b.role&&_messageComparableText(a)===_messageComparableText(b));
}}
{merge_fn}
const base = [{{role:'user', content:'go'}}];
const inflight = [
  {{role:'user', content:'go'}},
  {{role:'assistant', _live:true, content:'first progress', _activityBurstId:1}},
  {{role:'assistant', _live:true, content:'second progress', _activityBurstId:2}},
  {{role:'assistant', _live:true, content:'third progress', _activityBurstId:3}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.deepStrictEqual(
  merged.filter(m => m.role === 'assistant').map(m => m.content),
  ['first progress', 'second progress', 'third progress']
);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_load_session_does_not_advance_replay_cursor_from_session_journal_summary():
    body = _function_body(SESSIONS_JS, "loadSession")
    assert "INFLIGHT[sid].lastRunJournalSeq=journalSeq;" not in body
    assert "const journalSeq=_runJournalSeqFromSession(S.session);" not in body
    assert "function _runJournalSeqFromSession" not in SESSIONS_JS


def test_session_switch_reattach_discards_tail_cache_for_full_journal_replay():
    close_body = _function_body(MESSAGES_JS, "closeLiveStream")
    load_body = _function_body(SESSIONS_JS, "loadSession")
    compact_body = _function_body(UI_JS, "_compactInflightState")

    assert "INFLIGHT[sessionId].journalReplayFromStart=true" in close_body
    assert "journalReplayFromStart:true" in close_body
    assert "journalReplayFromStart:!!state.journalReplayFromStart" in compact_body
    assert "journalReplayFromStart:!!stored.journalReplayFromStart" in load_body
    assert "delete INFLIGHT[sid]" in load_body
    assert "clearInflightState(sid)" in load_body


def test_load_session_discards_cursor_only_inflight_before_reattach():
    """A cursor-only INFLIGHT cache must not skip historical journal replay.

    Real active sessions can have an empty sidecar transcript while the durable
    run journal has the full prose/tool timeline. If the browser kept only a
    lastRunJournalSeq cursor but lost visible INFLIGHT content, reattaching from
    that cursor makes the session look blank after switching away and back.
    """
    load_body = _function_body(SESSIONS_JS, "loadSession")
    helper_start = SESSIONS_JS.index("function _inflightHasVisibleLiveState")
    helper_body = SESSIONS_JS[
        helper_start : SESSIONS_JS.index("function _rememberRenderedSessionSnapshot", helper_start)
    ]

    assert "function _inflightHasVisibleLiveState" in SESSIONS_JS
    assert "lastAssistantText" in helper_body
    assert "lastReasoningText" in helper_body
    assert "liveTurnHtml" in helper_body
    assert "toolCalls" in helper_body
    assert "activityBurstAnchors" in helper_body
    assert "msg.role !== 'assistant'" in helper_body

    compact_load = re.sub(r"\s+", "", load_body)
    guard = "if(activeStreamId&&INFLIGHT[sid]&&!_inflightHasVisibleLiveState(INFLIGHT[sid]))"
    assert guard in compact_load
    guard_pos = compact_load.find(guard)
    inflight_branch_pos = compact_load.find("if(INFLIGHT[sid]){")
    assert 0 <= guard_pos < inflight_branch_pos


def test_reconnect_prefers_trimmed_live_message_over_stale_full_assistant_cache():
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    live_msg_pos = body.find("const _liveInflightAssistant")
    last_text_pos = body.find("const _lastLiveAssistant")
    assert live_msg_pos != -1 and last_text_pos != -1
    assert live_msg_pos < last_text_pos
    assistant_block = body[last_text_pos:body.find("const _lastLiveReasoning", last_text_pos)]
    assert "_liveInflightAssistant.content" in assistant_block
    assert "_fullInflightAssistant" in assistant_block
    assert "lastAssistantText" in body[live_msg_pos:last_text_pos]


def test_reconnect_uses_full_accumulator_when_live_tail_is_segmented():
    """When reattach projection splits the live assistant into multiple
    visible process-text segments, reconnect must resume from the full
    accumulator instead of the last segment.

    Otherwise the next syncInflightAssistantMessage() write truncates
    lastAssistantText to only the latest visible segment, so earlier process
    text anchors disappear on the next session switch and Activity groups fall
    back to the end of the turn.
    """
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    helper_pos = body.find("const _liveInflightAssistantMessages")
    last_text_pos = body.find("const _lastLiveAssistant")
    assert helper_pos != -1, (
        "attachLiveStream() should collect all live assistant segments before "
        "choosing reconnect text"
    )
    assert helper_pos < last_text_pos
    assistant_block = body[last_text_pos:body.find("const _lastLiveReasoning", last_text_pos)]
    assert "_liveInflightAssistantMessages.length>1" in assistant_block.replace(" ", "")
    assert "_fullInflightAssistant" in assistant_block
    assert "lastAssistantText" in body[helper_pos:last_text_pos]


def test_reconnect_seeds_segment_start_from_last_burst_anchor():
    """On reattach, segmentStart must align with the last burst anchor's textEnd.

    Without this, _doRender at segmentStart===0 uses the full visible text as
    displayText, so the smd parser (after _smdReconnect clears assistantBody)
    rewrites the entire accumulated text into the first live assistant segment.
    The per-burst segments rendered by _projectInflightMessagesForActivityBursts
    are left stale, Activity groups end up visually marooned among duplicate
    text, and the user sees Activity cards pile up at the tail of the turn.
    """
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    seg_start_pos = body.find("let segmentStart=(()=>{")
    assert seg_start_pos != -1, (
        "segmentStart must be initialized via a reconnect-aware IIFE that reads "
        "INFLIGHT.activityBurstAnchors so the smd parser rewrites only the "
        "tail-burst segment, not the full text."
    )
    seg_end_pos = body.find("})();", seg_start_pos)
    assert seg_end_pos != -1, "segmentStart IIFE must close with })();"
    seg_block = body[seg_start_pos:seg_end_pos]
    assert "activityBurstAnchors" in seg_block
    assert "reconnecting" in seg_block, "segmentStart should only shift when reconnecting"
    assert "textEnd" in seg_block


def test_ensure_assistant_row_reattaches_to_last_live_segment():
    """ensureAssistantRow must pick the LAST live segment, not the first.

    After session-switch reattach, the projected DOM holds one
    [data-live-assistant="1"] per recorded burst anchor plus a tail.  New
    tokens belong to the tail segment.  querySelector returns the first
    match, which would funnel all post-reattach tokens into segment 1,
    leaving the per-burst segments stale and Activity anchors visually
    detached.
    """
    body = _function_body(MESSAGES_JS, "ensureAssistantRow")
    assert "querySelectorAll('[data-live-assistant=\"1\"]')" in body, (
        "must enumerate every live segment so the tail can be selected"
    )
    # Sanity: still has the fresh-segment guard so post-tool turns don't
    # reuse the previous text segment that sits above the new tool card.
    assert "if(!_freshSegment)" in body
    # The selected segment must be the last entry, not the first.
    assert "liveSegments[liveSegments.length-1]" in body


def test_reconnect_without_tail_forces_fresh_segment_after_activity():
    """If reconnect resumes at the last recorded boundary, no tail segment exists.

    The next token should create a new segment after the previous Activity group
    instead of reusing the last burst's text segment above that Activity.
    """
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    fresh_pos = body.find("let _freshSegment=")
    seg_pos = body.find("let segmentStart=(()=>{")
    assert seg_pos != -1 and fresh_pos != -1
    assert seg_pos < fresh_pos
    fresh_line = body[fresh_pos:body.find(";", fresh_pos)]
    assert "reconnecting" in fresh_line
    assert "segmentStart>0" in fresh_line
    assert "segmentStart>=String(assistantText||'').length" in fresh_line
