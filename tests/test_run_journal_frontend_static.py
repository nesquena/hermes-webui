from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text()
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text()


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


def test_reattach_path_uses_replay_when_status_reports_journal():
    reattach_pos = MESSAGES_SRC.index("let replayOnly=false;")
    block = MESSAGES_SRC[reattach_pos : reattach_pos + 1200]

    assert "st.replay_available" in block
    assert "replayOnly=true" in block
    assert "(reconnecting||replayOnly)?_runJournalReplayParams():''" in block
    assert "_clearOwnerInflightState()" in block


def test_error_reconnect_path_can_restore_from_journal():
    reconnect_pos = MESSAGES_SRC.index("setComposerStatus('Reconnecting")
    block = MESSAGES_SRC[reconnect_pos : reconnect_pos + 900]

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
    load_block = SESSIONS_SRC[load_pos : load_pos + 20000]

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
    helper = _function_body(SESSIONS_SRC, "function _inflightHasVisibleLiveState")

    assert "msg.role === 'user'" in helper or "msg.role==='user'" in helper
    assert "_messageComparableText(msg)" in helper or "msg.content" in helper


def test_pending_user_merge_dedupes_workspace_prefixed_replay_rows():
    """Pending user rows and replayed/checkpointed user rows share one turn.

    The model-facing current user message may carry the WebUI workspace sentinel,
    while pending_user_message stores only the human prompt.  Frontend transcript
    equality must normalize the same way the renderer and backend context
    identity do, otherwise active reload/reconnect can show two user bubbles for
    one submitted turn.
    """
    same = _function_body(SESSIONS_SRC, "function _sameTranscriptMessage")
    normalizer = _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText")
    forced = _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope")

    assert "_normalizeUserTranscriptText" in same
    assert "_stripWorkspaceDisplayPrefix" in normalizer
    assert "_stripAttachedFilesMarker" in normalizer
    assert "Workspace::v1" in normalizer
    assert "FORCED SKILL CONTEXT" in forced
    assert "USER OVERRIDE" in forced


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
