"""Regression tests for session id rotation URL sync."""
from pathlib import Path
import re

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_stream_completion_syncs_rotated_session_id_to_tab_state():
    """When compact/restore returns a new session id, the tab anchor follows it."""
    # #3018 inserted a carry-forward of ephemeral per-turn fields into both the
    # completion (_finishDone) and settled-restore assignments; match the new shapes.
    completion_marker = re.compile(
        r"S\.session=d\.session;\s*"
        r"S\.messages=_carryForwardEphemeralTurnFields\(S\.messages\|\|\[\], d\.session\.messages\|\|\[\]\);"
    )
    settled_marker = "S.session=session;\n        const _nextMsgs3018=(session.messages||[]).filter(m=>m&&m.role);"

    completion_match = completion_marker.search(MESSAGES_JS)
    completion_pos = completion_match.start() if completion_match else -1
    settled_pos = MESSAGES_JS.find(settled_marker)
    assert completion_pos != -1
    assert settled_pos != -1

    # Proximity window scoping "the completion/settled handler block near the
    # session assignment". #4720 added a documented `_oldestIdx` reset statement
    # into the completion block (between the marker and the localStorage sync),
    # growing it past the original 800; widen to 1000 — still well within the
    # single done/settled handler, so the guard's intent (the rotated session id
    # is synced to tab state right after the session assignment) is preserved.
    completion_block = MESSAGES_JS[completion_pos : completion_pos + 1000]
    settled_block = MESSAGES_JS[settled_pos : settled_pos + 1000]

    for block in (completion_block, settled_block):
        assert "localStorage.setItem('hermes-webui-session',S.session.session_id);" in block
        assert "_setActiveSessionUrl(S.session.session_id)" in block
        assert "typeof _setActiveSessionUrl==='function'" in block
