import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"could not extract function body for {signature!r}")


def _run_session_identity_probe() -> dict:
    prompt = "same submitted prompt\nwith a second line"
    workspace_prompt = f"[Workspace::v1: /tmp/hermes-webui]\n{prompt}"
    legacy_workspace_prompt = f"[Workspace: /tmp/hermes-webui]\n{prompt}"
    attached_prompt = f"{prompt}\n\n[Attached files: /tmp/a.txt]"
    forced_prompt = (
        "[USER OVERRIDE] You MUST follow the skill 'hermes-webui-coordinator' "
        "content provided below before responding to the next message.\n\n"
        "[FORCED SKILL CONTEXT: hermes-webui-coordinator]\n"
        "skill body that should not make a second user bubble\n"
        "[/FORCED SKILL CONTEXT]\n\n"
        f"{prompt}"
    )
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _stripWorkspaceDisplayPrefix"),
            _function_body(SESSIONS_SRC, "function _messageComparableText"),
            _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
            _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
            _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
            _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
            _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
            _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
            _function_body(SESSIONS_SRC, "function _inflightHasVisibleLiveState"),
        ]
    )
    script = f"""
{helpers}
const plain = {{role:'user', content:{json.dumps(prompt)}}};
const workspace = {{role:'user', content:{json.dumps(workspace_prompt)}}};
const legacyWorkspace = {{role:'user', content:{json.dumps(legacy_workspace_prompt)}}};
const attached = {{role:'user', content:{json.dumps(attached_prompt)}}};
const forced = {{role:'user', content:{json.dumps(forced_prompt)}}};
const different = {{role:'user', content:'a different submitted prompt'}};
process.stdout.write(JSON.stringify({{
  workspaceDedupe: _sameTranscriptMessage(plain, workspace),
  legacyWorkspaceDedupe: _sameTranscriptMessage(plain, legacyWorkspace),
  attachedDedupe: _sameTranscriptMessage(plain, attached),
  forcedSkillDedupe: _sameTranscriptMessage(plain, forced),
  differentUserNotDedupe: !_sameTranscriptMessage(plain, different),
  roleMismatchNotDedupe: !_sameTranscriptMessage(plain, {{role:'assistant', content:{json.dumps(prompt)}}}),
  userOnlyInflightVisible: _inflightHasVisibleLiveState({{messages:[plain]}}),
  emptyUserOnlyInflightNotVisible: !_inflightHasVisibleLiveState({{messages:[{{role:'user', content:'   '}}]}}),
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _run_current_turn_scope_probe() -> dict:
    prompt = "repeat me"
    historical_workspace_prompt = f"[Workspace::v1: /tmp/old]\n{prompt}"
    current_workspace_prompt = f"[Workspace::v1: /tmp/current]\n{prompt}"
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _stripWorkspaceDisplayPrefix"),
            _function_body(UI_SRC, "function msgContent"),
            _function_body(UI_SRC, "function _isContextCompactionText"),
            _function_body(UI_SRC, "function _isContextCompactionMessage"),
            _function_body(SESSIONS_SRC, "function _messageComparableText"),
            _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
            _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
            _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
            _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
            _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
            _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
            _function_body(SESSIONS_SRC, "function _mergePendingSessionMessage"),
            _function_body(SESSIONS_SRC, "function _mergeInflightTailMessages"),
        ]
    )
    script = f"""
{helpers}
function getPendingSessionMessage(session, messages){{
  const text=String(session&&session.pending_user_message||'').trim();
  if(!text) return null;
  return {{
    role:'user',
    content:text,
    _ts:session.pending_started_at||10,
    _pending:true,
  }};
}}
const historical = {{role:'user', content:{json.dumps(historical_workspace_prompt)}, _ts:1}};
const historicalAnswer = {{role:'assistant', content:'done', _ts:2}};
const liveAssistant = {{role:'assistant', content:'working', _live:true, _ts:4}};
const pendingSession = {{pending_user_message:{json.dumps(prompt)}, pending_started_at:3}};

const pendingAfterHistory = [historical, historicalAnswer];
const insertedAfterHistory = _mergePendingSessionMessage(pendingSession, pendingAfterHistory);

const pendingBeforeLive = [historical, historicalAnswer, liveAssistant];
const insertedBeforeLive = _mergePendingSessionMessage(pendingSession, pendingBeforeLive);

const optimisticCurrent = {{role:'user', content:{json.dumps(current_workspace_prompt)}, _ts:3}};
const pendingWithCurrent = [historical, historicalAnswer, optimisticCurrent, liveAssistant];
const insertedWithCurrent = _mergePendingSessionMessage(pendingSession, pendingWithCurrent);

const inflightAfterHistory = _mergeInflightTailMessages(
  [historical, historicalAnswer],
  [{{role:'user', content:{json.dumps(prompt)}, _ts:3}}, liveAssistant]
);

const inflightWithCurrent = _mergeInflightTailMessages(
  [historical, historicalAnswer, optimisticCurrent],
  [{{role:'user', content:{json.dumps(prompt)}, _ts:3}}, liveAssistant]
);

const compaction = {{
  role:'user',
  content:'[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.',
  _ts:3.5,
}};
const compactionBase = [historical, historicalAnswer, optimisticCurrent, compaction];
const compactionCandidate = {{role:'user', content:{json.dumps(prompt)}, _ts:3}};
const compactionCurrentTail = _currentTailUserMessage(compactionBase);
const compactionTailDuplicate = _hasCurrentTailUserDuplicate(compactionBase, compactionCandidate);
const compactionMerged = _mergeInflightTailMessages(
  compactionBase,
  [compactionCandidate, liveAssistant]
);
const insertedAfterCompaction = _mergePendingSessionMessage(pendingSession, compactionMerged);
const compactionPromptCount = compactionMerged.filter(
  m=>m&&m.role==='user'&&m._ts===3&&_normalizeUserTranscriptText(m.content)==={json.dumps(prompt)}
).length;
const compactionMarkerRetained = compactionMerged.some(m=>_isContextCompactionMessage(m));
const compactionLiveAssistantRetained = compactionMerged.some(
  m=>m&&m.role==='assistant'&&m._live&&m.content==='working'
);
const completedBoundaryDedupe = _hasCurrentTailUserDuplicate(
  [historical, historicalAnswer, compaction],
  {{role:'user', content:{json.dumps(prompt)}, _ts:3}}
);
const distinctCompletedTurnPromptCount = inflightAfterHistory.filter(
  m=>m&&m.role==='user'&&_normalizeUserTranscriptText(m.content)==={json.dumps(prompt)}
).length;

process.stdout.write(JSON.stringify({{
  insertedAfterHistory,
  pendingAfterHistoryRoles: pendingAfterHistory.map(m=>m.role),
  insertedBeforeLive,
  pendingBeforeLiveRoles: pendingBeforeLive.map(m=>m.role),
  insertedWithCurrent,
  pendingWithCurrentRoles: pendingWithCurrent.map(m=>m.role),
  inflightAfterHistoryRoles: inflightAfterHistory.map(m=>m.role),
  inflightWithCurrentRoles: inflightWithCurrent.map(m=>m.role),
  insertedAfterCompaction,
  compactionCurrentTailContent: compactionCurrentTail&&compactionCurrentTail.content,
  compactionTailDuplicate,
  compactionPromptCount,
  compactionMarkerRetained,
  compactionLiveAssistantRetained,
  completedBoundaryDedupe,
  distinctCompletedTurnPromptCount,
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _run_pending_session_message_probe() -> dict:
    prompt = "repeat me"
    historical_workspace_prompt = f"[Workspace::v1: /tmp/old]\n{prompt}"
    current_workspace_prompt = f"[Workspace::v1: /tmp/current]\n{prompt}"
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _stripWorkspaceDisplayPrefix"),
            _function_body(UI_SRC, "function msgContent"),
            _function_body(SESSIONS_SRC, "function _messageComparableText"),
            _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
            _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
            _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
            _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
            _function_body(UI_SRC, "function _pendingCurrentTailUserMessage"),
            _function_body(UI_SRC, "function _isContextCompactionText"),
            _function_body(UI_SRC, "function _isContextCompactionMessage"),
            _function_body(UI_SRC, "function getPendingSessionMessage"),
        ]
    )
    script = f"""
{helpers}
const prompt = {json.dumps(prompt)};
const historical = {{role:'user', content:prompt, _ts:1}};
const historicalWorkspace = {{role:'user', content:{json.dumps(historical_workspace_prompt)}, _ts:1}};
const historicalAnswer = {{role:'assistant', content:'done', _ts:2}};
const currentTail = {{role:'user', content:prompt, _ts:3}};
const currentWorkspaceTail = {{role:'user', content:{json.dumps(current_workspace_prompt)}, _ts:3}};
const currentTailForCompaction = {{role:'user', content:prompt, _ts:3}};
const liveAssistant = {{role:'assistant', content:'working', _live:true, _ts:4}};
const attachments = [{{name:'note.txt', path:'note.txt', mime:'text/plain'}}];
const compactionMarker = {{
  role:'user',
  content:'[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.',
  _ts:3.5,
}};
const repeatedPromptTurnOne = {{role:'user', content:prompt, _ts:1}};
const repeatedPromptAnswerOne = {{role:'assistant', content:'done', _ts:2}};
const repeatedPromptTurnTwo = {{role:'user', content:prompt, _ts:3}};
const repeatedPromptAnswerTwo = {{role:'assistant', content:'done', _ts:4}};
const repeatedCompletedBase = [
  repeatedPromptTurnOne,
  repeatedPromptAnswerOne,
  repeatedPromptTurnTwo,
  repeatedPromptAnswerTwo,
];

const fromHistoricalSameText = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:3}},
  [historical, historicalAnswer]
);
const fromHistoricalWorkspace = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:3}},
  [historicalWorkspace, historicalAnswer]
);
const exactCurrentMessages = [historical, historicalAnswer, currentTail];
const exactCurrentResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4, pending_attachments:attachments}},
  exactCurrentMessages
);
const workspaceCurrentResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4}},
  [historical, historicalAnswer, currentWorkspaceTail]
);
const liveAfterCurrentResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4}},
  [historical, historicalAnswer, currentWorkspaceTail, liveAssistant]
);
const differentTailResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4}},
  [historical, historicalAnswer, {{role:'user', content:'different prompt', _ts:3}}]
);
const compactionTailResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4, pending_attachments:attachments}},
  [historical, historicalAnswer, currentTailForCompaction, compactionMarker]
);
const repeatedCompletedResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:5}},
  repeatedCompletedBase
);
const repeatedCompletedMessages = repeatedCompletedResult
  ? [...repeatedCompletedBase, repeatedCompletedResult]
  : repeatedCompletedBase;
const repeatedCompletedPromptCount = repeatedCompletedMessages.filter(
  m=>m&&m.role==='user'&&_normalizeUserTranscriptText(m.content)===prompt
).length;
const compactionCurrentTail = _pendingCurrentTailUserMessage([historical, historicalAnswer, currentTailForCompaction, compactionMarker]);

process.stdout.write(JSON.stringify({{
  historicalSameTextSurvives: !!fromHistoricalSameText && fromHistoricalSameText.content===prompt && fromHistoricalSameText._pending===true,
  historicalWorkspaceSurvives: !!fromHistoricalWorkspace && fromHistoricalWorkspace.content===prompt && fromHistoricalWorkspace._pending===true,
  exactCurrentTailDedupe: exactCurrentResult===null,
  exactCurrentTailAttachmentsCopied: Array.isArray(currentTail.attachments) && currentTail.attachments[0].name==='note.txt',
  workspaceCurrentTailDedupe: workspaceCurrentResult===null,
  liveAfterCurrentTailDedupe: liveAfterCurrentResult===null,
  differentCurrentTailSurvives: !!differentTailResult && differentTailResult.content===prompt && differentTailResult._pending===true,
  compactionBoundaryDedupe: compactionTailResult===null,
  compactionBoundaryCurrentTail: compactionCurrentTail&&compactionCurrentTail.role==='user'&&compactionCurrentTail.content===prompt,
  compactionCurrentTailAttachmentsCopied: Array.isArray(currentTailForCompaction.attachments) && currentTailForCompaction.attachments[0].name==='note.txt',
  repeatedCompletedPromptsRemainValid: repeatedCompletedPromptCount===3,
  isContextCompactionText: _isContextCompactionText(compactionMarker.content),
  isContextCompactionMessage: _isContextCompactionMessage(compactionMarker),
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_reattach_path_uses_replay_when_status_reports_journal():
    reattach_pos = MESSAGES_SRC.index("let replayOnly=false;")
    # Window widened to 2200: the SSE-recovery follow-restore fix (the
    # _wasFollowingAtReconnectDead guard + its sticky-unpin check) inserted lines
    # into the reconnect-dead cleanup block between this anchor and the
    # replay-params assertion below, pushing the target string past the old slice.
    block = MESSAGES_SRC[reattach_pos : reattach_pos + 2200]

    assert "st.replay_available" in block
    assert "replayOnly=true" in block
    assert "(reconnecting||replayOnly)?_runJournalReplayParams():''" in block
    assert "_clearOwnerInflightState()" in block


def test_error_reconnect_path_can_restore_from_journal():
    # Anchor on the reconnect block's stable entry point rather than the exact
    # composer-status string: the first status was changed to a template literal
    # `Reconnecting… (1/${_retryDelays.length})` (staged-probe counter), so the
    # old single-quoted "setComposerStatus('Reconnecting" anchor no longer exists.
    reconnect_pos = MESSAGES_SRC.index("_reconnectAttempted=true;")
    block = MESSAGES_SRC[reconnect_pos : reconnect_pos + 1100]

    assert "st.active" in block
    assert "st.replay_available" in block
    assert "Restoring stream" in block
    assert "_runJournalReplayParams()" in block


def test_frontend_replay_cursor_uses_eventsource_last_event_id():
    cursor_pos = MESSAGES_SRC.index("function _rememberRunJournalCursor")
    block = MESSAGES_SRC[cursor_pos : cursor_pos + 1000]

    assert "e.lastEventId" in block
    assert "lastIndexOf(':')" in block
    assert "_lastRunJournalSeq=seq" in block
    assert "source.addEventListener(_runJournalEventName,_rememberRunJournalCursor)" in MESSAGES_SRC
    assert "after_seq=${encodeURIComponent(String(_runJournalReplayAfterSeq()))}" in MESSAGES_SRC
    assert "after_seq=0" not in MESSAGES_SRC


def test_replayed_long_task_events_enter_the_same_live_timeline_handlers():
    """Run-journal replay must not grow a parallel long-task renderer.

    The run-state consistency contract depends on replayed journal events
    flowing through the same EventSource handlers as live streams.  Otherwise a
    live long task can render as Thinking -> progress text -> tool cards, while
    the same journaled event sequence replays as a flattened or reordered scene.
    """
    wire_pos = MESSAGES_SRC.index("function _wireSSE(source)")
    wire_block = MESSAGES_SRC[wire_pos : MESSAGES_SRC.index("async function _restoreSettledSession", wire_pos)]
    replay_events = [
        "reasoning",
        "interim_assistant",
        "tool",
        "tool_complete",
        "compressing",
        "compressed",
        "metering",
        "done",
        "apperror",
    ]

    for event_name in replay_events:
        assert f"source.addEventListener('{event_name}'" in wire_block, (
            f"{event_name} must be handled by the shared live/replay SSE pipeline"
        )

    thinking_helper = MESSAGES_SRC[
        MESSAGES_SRC.index("function _updateLiveThinkingCard") :
        MESSAGES_SRC.index("// Split a content string", MESSAGES_SRC.index("function _updateLiveThinkingCard"))
    ]
    assert "_updateLiveThinkingCard(" in wire_block, "reasoning replay should use the live Thinking card path"
    assert "updateThinking(text, opts)" in thinking_helper and "appendThinking(text, opts)" in thinking_helper, (
        "the shared Thinking helper should still route replay/live reasoning into the Worklog Thinking card path"
    )
    assert "appendLiveToolCard(tc" in wire_block, "tool replay should use live tool-card rendering"
    # Compression replay must dispatch through setCompressionUi(...). The handler
    # body may build the state object inline (`setCompressionUi({...})`) or hoist
    # it into a `state` variable first (`setCompressionUi(state)`) — both forms
    # use the same compression-card path, so accept either. Pinning the literal
    # `{` after the open-paren was over-specific and broke in v0.51.76 when
    # PR #2347 hoisted the state object to share it with `appendLiveCompressionCard`.
    assert ("setCompressionUi({" in wire_block) or ("setCompressionUi(state)" in wire_block), (
        "compression replay should use the compression card path"
    )
    assert "_runJournalReplayParams()" in MESSAGES_SRC, "replay attachments should enter _wireSSE via EventSource"


def test_run_journal_cursor_tracks_every_long_task_timeline_event():
    """Every user-visible long-task event needs cursor tracking for parity replay."""
    cursor_loop_pos = MESSAGES_SRC.index("for(const _runJournalEventName of [")
    cursor_loop = MESSAGES_SRC[cursor_loop_pos : MESSAGES_SRC.index("]", cursor_loop_pos)]
    timeline_events = [
        "token",
        "interim_assistant",
        "reasoning",
        "tool",
        "tool_complete",
        "compressing",
        "compressed",
        "metering",
        "done",
        "apperror",
        "cancel",
    ]

    for event_name in timeline_events:
        assert f"'{event_name}'" in cursor_loop, (
            f"{event_name} must advance the replay cursor to avoid duplicate timeline replay"
        )


def test_server_runtime_journal_snapshot_restores_structured_inflight_state():
    helper_pos = SESSIONS_SRC.index("function _serverLiveSnapshotToolId")
    helper_block = SESSIONS_SRC[helper_pos : helper_pos + 3600]
    load_pos = SESSIONS_SRC.index("async function loadSession")
    load_end = SESSIONS_SRC.index("// ── Handoff hint logic", load_pos)
    load_block = SESSIONS_SRC[load_pos:load_end]

    assert "runtime_journal_snapshot" in load_block
    assert "_serverLiveSnapshotInflight(S.session.runtime_journal_snapshot" in load_block
    assert "!_inflightHasVisibleLiveState(INFLIGHT[sid])" in load_block
    assert "journalSnapshot:true" in helper_block
    assert "lastRunJournalSeq" in helper_block
    assert "last_assistant_text" in helper_block
    assert "activity_burst_anchors" in helper_block
    for key in ("tid", "id", "tool_call_id", "tool_use_id", "call_id"):
        assert key in helper_block


def test_active_reload_keeps_user_only_inflight_visible_until_pending_dedupe():
    """A just-submitted user row is visible live state before first assistant text.

    On an active first-turn reload, the sidecar can still have messages=[] while
    pending_user_message and the submitted turn journal record the same prompt.
    The browser must not discard the user-only optimistic INFLIGHT entry as a
    cursor-only snapshot before pending/live replay reconciliation runs.
    """
    result = _run_session_identity_probe()

    assert result["userOnlyInflightVisible"] is True
    assert result["emptyUserOnlyInflightNotVisible"] is True


def test_pending_user_merge_dedupes_user_turn_variants_by_behavior():
    """Pending user rows and replayed/checkpointed user rows share one turn.

    Execute the same JavaScript helpers the browser uses so the regression test
    catches regex/order/trim mistakes, not just identifier wiring.
    """
    result = _run_session_identity_probe()

    assert result["workspaceDedupe"] is True
    assert result["legacyWorkspaceDedupe"] is True
    assert result["attachedDedupe"] is True
    assert result["forcedSkillDedupe"] is True
    assert result["differentUserNotDedupe"] is True
    assert result["roleMismatchNotDedupe"] is True


def test_user_turn_dedupe_is_scoped_to_current_turn_by_behavior():
    result = _run_current_turn_scope_probe()

    assert result["insertedAfterHistory"] is True
    assert result["pendingAfterHistoryRoles"] == ["user", "assistant", "user"]
    assert result["insertedBeforeLive"] is True
    assert result["pendingBeforeLiveRoles"] == ["user", "assistant", "user", "assistant"]

    assert result["insertedWithCurrent"] is False
    assert result["pendingWithCurrentRoles"] == ["user", "assistant", "user", "assistant"]

    assert result["inflightAfterHistoryRoles"] == ["user", "assistant", "user", "assistant"]
    assert result["inflightWithCurrentRoles"] == ["user", "assistant", "user", "assistant"]

    assert result["insertedAfterCompaction"] is False
    assert result["compactionCurrentTailContent"] == "[Workspace::v1: /tmp/current]\nrepeat me"
    assert result["compactionTailDuplicate"] is True
    assert result["compactionPromptCount"] == 1
    assert result["compactionMarkerRetained"] is True
    assert result["compactionLiveAssistantRetained"] is True
    assert result["completedBoundaryDedupe"] is False
    assert result["distinctCompletedTurnPromptCount"] == 2


def test_get_pending_session_message_keeps_deferred_repeat_prompt_by_behavior():
    """Deferred active reload must not hide a current repeat prompt.

    In the default deferred save mode, chat start persists only
    pending_user_message before the worker appends the display row.  If an older
    turn has the same visible user text, getPendingSessionMessage still has to
    return the current pending row; downstream merge code then dedupes only
    against the current tail.
    """
    result = _run_pending_session_message_probe()

    assert result["historicalSameTextSurvives"] is True
    assert result["historicalWorkspaceSurvives"] is True
    assert result["exactCurrentTailDedupe"] is True
    assert result["exactCurrentTailAttachmentsCopied"] is True
    assert result["workspaceCurrentTailDedupe"] is True
    assert result["liveAfterCurrentTailDedupe"] is True
    assert result["differentCurrentTailSurvives"] is True
    assert result["compactionBoundaryDedupe"] is True
    assert result["compactionBoundaryCurrentTail"] is True
    assert result["compactionCurrentTailAttachmentsCopied"] is True
    assert result["repeatedCompletedPromptsRemainValid"] is True
    assert result["isContextCompactionText"] is True
    assert result["isContextCompactionMessage"] is True


def test_live_tool_matching_uses_the_same_aliases_as_live_card_dedup():
    live_tid_pos = MESSAGES_SRC.index("function _liveToolTid")
    live_tid_block = MESSAGES_SRC[live_tid_pos : live_tid_pos + 450]
    find_pos = MESSAGES_SRC.index("function _findPendingLiveToolCallIndex")
    find_block = MESSAGES_SRC[find_pos : find_pos + 900]
    upsert_pos = MESSAGES_SRC.index("function upsertLiveToolCall")
    upsert_block = MESSAGES_SRC[upsert_pos : upsert_pos + 600]

    for key in ("tid", "id", "tool_call_id", "tool_use_id", "call_id"):
        assert key in live_tid_block
        assert key in find_block
        assert key in upsert_block
