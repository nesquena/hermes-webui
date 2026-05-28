"""Regression tests for session id rotation URL sync."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_stream_completion_syncs_rotated_session_id_to_tab_state():
    """When compact/restore returns a new session id, the tab anchor follows it."""
    # #3018 inserted a carry-forward of ephemeral per-turn fields into both the
    # completion (_finishDone) and settled-restore assignments; match the new shapes.
    completion_marker = "S.session=d.session;S.messages=_carryForwardEphemeralTurnFields(S.messages||[], d.session.messages||[]);"
    settled_marker = "S.session=session;\n        const _nextMsgs3018=(session.messages||[]).filter(m=>m&&m.role);"

    # Both blocks end at the same sentinel statement. Slice to it rather
    # than a hardcoded character window so the assertions stay correct no
    # matter how many lines get inserted between the `S.session=`
    # reassignment and the URL-sync guard. A fixed `+ 500` window broke
    # when the realtime-todos work inserted a `_hydrateTodosFromSession`
    # line into both paths, pushing `_setActiveSessionUrl(...)` past the
    # window even though the call itself was untouched.
    end_sentinel = "const _markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages);"

    completion_pos = MESSAGES_JS.find(completion_marker)
    settled_pos = MESSAGES_JS.find(settled_marker)
    assert completion_pos != -1
    assert settled_pos != -1

    completion_end = MESSAGES_JS.find(end_sentinel, completion_pos)
    settled_end = MESSAGES_JS.find(end_sentinel, settled_pos)
    assert completion_end != -1, "completion-path block end sentinel not found"
    assert settled_end != -1, "settled-restore block end sentinel not found"

    completion_block = MESSAGES_JS[completion_pos:completion_end]
    settled_block = MESSAGES_JS[settled_pos:settled_end]

    for block in (completion_block, settled_block):
        assert "localStorage.setItem('hermes-webui-session',S.session.session_id);" in block
        assert "_setActiveSessionUrl(S.session.session_id)" in block
        assert "typeof _setActiveSessionUrl==='function'" in block
