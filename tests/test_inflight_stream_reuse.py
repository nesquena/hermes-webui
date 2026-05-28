"""Regression tests for preserving live streams across session switches."""
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
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
    assert "existingLive.source.readyState!==EventSource.CLOSED" in body
    assert "return" in body[reuse_pos:close_pos]


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
    inflight_block = body[inflight_idx : inflight_idx + 2400]
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
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_load_session_rebuilds_live_tail_before_dropping_stale_dom_snapshot():
    body = _function_body(SESSIONS_JS, "loadSession")
    ensure_pos = body.find("_ensureInflightLiveAssistantMessage(INFLIGHT[sid]);")
    inflight_pos = body.find("const inflightMessages=_projectInflightMessagesForActivityBursts(INFLIGHT[sid]);")
    prepare_pos = body.find("const liveTailPrepared=_prepareRunningLiveTail(S.messages,inflightMessages);")
    drop_dom_pos = body.find("delete INFLIGHT[sid].liveTurnHtml;")
    drop_assistant_pos = body.find("S.messages=_dropCurrentTurnAssistantMessages(S.messages);")
    merge_pos = body.find("S.messages=_mergeInflightTailMessages(S.messages,inflightMessages);")
    restore_pos = body.find("restoreLiveTurnHtmlForSession(sid)")
    assert ensure_pos != -1 and inflight_pos != -1
    assert prepare_pos != -1 and drop_dom_pos != -1
    assert drop_assistant_pos != -1 and merge_pos != -1 and restore_pos != -1
    assert ensure_pos < inflight_pos < prepare_pos < drop_dom_pos < drop_assistant_pos < merge_pos < restore_pos


def test_load_session_does_not_advance_replay_cursor_from_session_journal_summary():
    body = _function_body(SESSIONS_JS, "loadSession")
    assert "INFLIGHT[sid].lastRunJournalSeq=journalSeq;" not in body
    assert "const journalSeq=_runJournalSeqFromSession(S.session);" not in body
    assert "function _runJournalSeqFromSession" not in SESSIONS_JS


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
