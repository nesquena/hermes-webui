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
