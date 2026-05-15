from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text()


def test_reattach_path_uses_replay_when_status_reports_journal():
    reattach_pos = MESSAGES_SRC.index("let replayOnly=false;")
    block = MESSAGES_SRC[reattach_pos : reattach_pos + 1200]

    assert "st.replay_available" in block
    assert "replayOnly=true" in block
    assert "replay=1&after_seq=0" in block
    assert "_clearOwnerInflightState()" in block


def test_error_reconnect_path_can_restore_from_journal():
    reconnect_pos = MESSAGES_SRC.index("setComposerStatus('Reconnecting")
    block = MESSAGES_SRC[reconnect_pos : reconnect_pos + 900]

    assert "st.active" in block
    assert "st.replay_available" in block
    assert "Restoring stream" in block
    assert "replay=1&after_seq=0" in block
