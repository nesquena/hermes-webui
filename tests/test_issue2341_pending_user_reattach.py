from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _load_session_inflight_branch() -> str:
    start = SESSIONS_JS.find("if(INFLIGHT[sid]){")
    assert start != -1, "loadSession INFLIGHT branch not found"
    end = SESSIONS_JS.find("}else{", start)
    assert end != -1, "loadSession INFLIGHT branch end not found"
    return SESSIONS_JS[start:end]


def test_load_session_inflight_reattach_merges_pending_user_message_before_render():
    """#2341: Reattaching to an active stream must show the initiating user turn.

    A reload or session switch can hydrate a running turn from INFLIGHT while the
    backend still carries the user prompt only as pending_user_message. Without
    merging that pending row before renderMessages(), the user sees assistant
    thinking/tool activity without the prompt that started it.
    """
    block = _load_session_inflight_branch()

    merge_pos = block.find("_mergePendingSessionMessage")
    render_pos = block.find("renderMessages();appendThinking();")

    assert merge_pos != -1, (
        "loadSession's INFLIGHT reattach branch must merge pending_user_message "
        "into S.messages before rendering the running turn"
    )
    assert render_pos != -1, "INFLIGHT branch render call not found"
    assert merge_pos < render_pos, (
        "The pending user row must be present before renderMessages() rebuilds "
        "the active transcript"
    )
    assert "INFLIGHT[sid].messages=S.messages;" in block, (
        "After merging the pending user row, the INFLIGHT cache should be updated "
        "so later session switches keep the same visible turn"
    )
    assert "messages.findIndex(m=>m&&m.role==='assistant'&&m._live)" in SESSIONS_JS
    assert "messages.splice(liveAssistantIdx,0,pendingMsg)" in SESSIONS_JS


def test_pending_user_message_dedup_checks_current_message_array():
    """#2341: Pending merge must not duplicate an already-visible user row."""
    assert "function getPendingSessionMessage(session, messagesOverride=null)" in UI_JS
    helper_start = UI_JS.find("function getPendingSessionMessage(session, messagesOverride=null)")
    assert helper_start != -1, "getPendingSessionMessage helper not found"
    helper_end = UI_JS.find("async function checkInflightOnBoot", helper_start)
    assert helper_end != -1, "getPendingSessionMessage helper end not found"
    helper = UI_JS[helper_start:helper_end]

    assert "messagesOverride" in helper
    assert "Array.isArray(messagesOverride)?messagesOverride" in helper.replace(" ", ""), (
        "Pending-message dedup must inspect the current S.messages/INFLIGHT "
        "array, not only session.messages from the metadata response"
    )
    assert "lastText===text" in helper, (
        "Pending-message merge must suppress duplicates when the last user row "
        "already matches pending_user_message"
    )
