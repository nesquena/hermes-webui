"""Regression tests for session id rotation URL sync."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_stream_completion_syncs_rotated_session_id_to_tab_state():
    """When compact/restore returns a new session id, the tab anchor follows it."""
    # Session replacement is owned by each handler; URL/tab synchronization is
    # delegated to the shared _setActiveSessionUrl chokepoint.
    done_pos = MESSAGES_JS.find("source.addEventListener('done'")
    stream_end_pos = MESSAGES_JS.find("source.addEventListener('stream_end'", done_pos)
    restore_pos = MESSAGES_JS.find("async function _restoreSettledSession(source")
    error_pos = MESSAGES_JS.find("function _handleStreamError(source)", restore_pos)
    assert done_pos != -1
    assert stream_end_pos > done_pos
    assert restore_pos != -1
    assert error_pos > restore_pos

    completion_block = MESSAGES_JS[done_pos:stream_end_pos]
    settled_block = MESSAGES_JS[restore_pos:error_pos]

    for block in (completion_block, settled_block):
        assert "localStorage.setItem('hermes-webui-session',S.session.session_id);" in block
        assert "_setActiveSessionUrl(S.session.session_id)" in block
        assert "typeof _setActiveSessionUrl==='function'" in block
