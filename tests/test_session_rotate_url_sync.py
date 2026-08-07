"""Regression tests for session id rotation URL sync."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_stream_completion_syncs_rotated_session_id_to_tab_state():
    """When compact/restore returns a new session id, the tab anchor follows it."""
    # #3018 inserted a carry-forward of ephemeral per-turn fields into both the
    # completion (_finishDone) and settled-restore assignments; match the new shapes.
    completion_marker = "S.session=completedSession;"
    settled_marker = "S.session=session;\n        const _nextMsgs3018=Array.isArray(session.messages)?session.messages:[];"

    completion_pos = MESSAGES_JS.find(completion_marker)
    settled_pos = MESSAGES_JS.find(settled_marker)
    assert completion_pos != -1
    assert settled_pos != -1

    # Proximity window scoping "the completion/settled handler block near the
    # session assignment". The settled restore block now includes the terminal
    # stale-prefix guard before the tab-state sync, so keep the assertion local
    # to the handler while widening the slice enough to cover the new helper
    # state and the unchanged localStorage/update-url writes.
    completion_block = MESSAGES_JS[completion_pos : completion_pos + 1800]
    settled_block = MESSAGES_JS[settled_pos : settled_pos + 2400]

    for block in (completion_block, settled_block):
        assert "localStorage.setItem('hermes-webui-session',S.session.session_id);" in block
        assert "_setActiveSessionUrl(S.session.session_id)" in block
        assert "typeof _setActiveSessionUrl==='function'" in block
