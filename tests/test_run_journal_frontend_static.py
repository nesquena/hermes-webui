from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text()


def test_reattach_path_uses_replay_when_status_reports_journal():
    reattach_pos = MESSAGES_SRC.index("let replayOnly=false;")
    block = MESSAGES_SRC[reattach_pos : reattach_pos + 1200]

    assert "st.replay_available" in block
    assert "replayOnly=true" in block
    assert "replayOnly?_runJournalReplayParams():''" in block
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

    assert "updateThinking(" in wire_block, "reasoning replay should use the live Thinking card path"
    assert "appendLiveToolCard(tc)" in wire_block, "tool replay should use live tool-card rendering"
    assert "setCompressionUi({" in wire_block, "compression replay should use the compression card path"
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
