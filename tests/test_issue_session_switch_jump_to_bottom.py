"""Regression checks for session-switch scroll reset behavior.

These tests enforce that cross-session navigation uses a named helper to clear
sticky scroll state before rendering the new transcript, while keeping
same-session force-reload preserveScroll behavior intact.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _extract_load_session_block():
    start = SESSIONS_JS.index("async function loadSession(sid){")
    end = SESSIONS_JS.index("  // Sync context usage indicator from session data", start)
    return SESSIONS_JS[start:end]


def test_session_switch_uses_named_scroll_reset_helper():
    """Fresh cross-session switches must call a named helper to reset sticky state."""
    assert "function _resetSessionSwitchScrollState" in UI_JS, (
        "ui.js should expose a dedicated _resetSessionSwitchScrollState() helper "
        "for session-switch fresh-load scroll resets."
    )
    assert "window._resetSessionSwitchScrollState=_resetSessionSwitchScrollState" in UI_JS, (
        "ui.js should export _resetSessionSwitchScrollState for cross-module use."
    )
    block = _extract_load_session_block()
    assert "_resetSessionSwitchScrollState();" in block, (
        "loadSession() should invoke _resetSessionSwitchScrollState() on real session "
        "switches (currentSid !== sid)."
    )


def test_session_switch_renders_new_session_without_preserve_scroll():
    """Fresh session renders must stay on bottom unless same-session preserveScroll is requested."""
    block = _extract_load_session_block()
    explicit_preserve_calls = block.count("renderMessages({preserveScroll:true})")
    assert explicit_preserve_calls == 0, (
        "renderMessages for session switch must not force preserveScroll for new sessions."
    )
    assert "freshSessionSwitch?{freshSessionLoad:true}:undefined" in block, (
        "fresh session switches should render with freshSessionLoad:true so the "
        "post-render scroll path explicitly lands at the bottom."
    )
    assert "sameSessionForceReload?{preserveScroll:true}:" in block, (
        "same-session force reload behavior should still pass preserveScroll:true when "
        "sameSessionForceReload is true."
    )


def test_fresh_session_render_forces_bottom_without_snapshot_restore():
    """freshSessionLoad must bypass stale snapshot restoration and force bottom."""
    assert "const forceBottom=!!(options&&options.freshSessionLoad);" in UI_JS
    assert "if(forceBottom){\n    scrollToBottom();\n    return;\n  }" in UI_JS
    assert "const scrollSnapshot=(!forceBottom&&(preserveScroll||_messageUserUnpinned))?_captureMessageScrollSnapshot():null;" in UI_JS


def test_message_scroll_owner_prefers_effective_scrollable_transcript_element():
    """Mobile layouts can make #msgInner the actual scroller while #messages is fixed."""
    helper_start = UI_JS.index("function _messageScrollElement")
    helper_end = UI_JS.index("function _cancelBottomSettle", helper_start)
    helper = UI_JS[helper_start:helper_end]

    assert "const messages=$('messages');" in helper
    assert "const msgInner=$('msgInner');" in helper
    assert "messages.scrollHeight>messages.clientHeight+1" in helper
    assert "msgInner.scrollHeight>msgInner.clientHeight+1" in helper
    assert "if(messagesScrollable) return messages;" in helper
    assert "if(msgInnerScrollable) return msgInner;" in helper
    assert "return msgInner||messages;" in helper


def test_bottom_helpers_use_canonical_message_scroll_owner():
    """Force-bottom and near-bottom logic must target the real mobile/desktop scroller."""
    for signature in (
        "function _setMessageScrollToBottom",
        "function _isMessagePaneNearBottom",
        "function _messageBottomDistance",
        "function _captureMessageScrollSnapshot",
        "function _restoreMessageScrollSnapshot",
    ):
        start = UI_JS.index(signature)
        end = UI_JS.index("function ", start + len(signature))
        body = UI_JS[start:end]
        assert "_messageScrollElement()" in body, f"{signature} should use the canonical scroll owner"


def test_scroll_listener_binds_both_possible_scroll_owners():
    """User scroll intent must be heard from the same element that bottom-follow scrolls."""
    assert "const bindableScrollElements=[_messageScrollElement(),$('messages'),$('msgInner')]" in UI_JS
    assert "bindableScrollElements.forEach(el=>" in UI_JS
    assert "el.addEventListener('scroll',function()" in UI_JS


def test_session_switch_scroll_helpers_are_scoped_to_real_switches():
    """Scroll reset helper call should remain conditional on currentSid !== sid."""
    block = _extract_load_session_block()
    marker = "if (currentSid !== sid)"
    helper_idx = block.find("_resetSessionSwitchScrollState()")
    assert helper_idx >= 0, "expected reset helper invocation in loadSession()"
    assert block.rfind(marker, 0, helper_idx) < helper_idx, (
        "_resetSessionSwitchScrollState() must be inside a real-switch guard (currentSid !== sid)."
    )
