"""Regression: sidebar unread dot must clear when visiting a session.

Visiting must clear local unread markers, sync polling snapshots so deferred
list refreshes cannot re-flag the session, patch stale sidebar DOM, and repaint
after the async message-load gap — not only on sidebar row clicks.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _load_session_block():
    start = SESSIONS_JS.index("async function loadSession(sid)")
    end = SESSIONS_JS.index("function _resolveSessionModelForDisplaySoon", start)
    return SESSIONS_JS[start:end]


def test_acknowledge_session_visit_helper_exists():
    assert "function _acknowledgeSessionVisit(sid, messageCount = 0, lastMessageAt = 0)" in SESSIONS_JS
    assert "function _isSessionOpenInChatPane(sid)" in SESSIONS_JS
    assert "function _patchSidebarUnreadIndicatorsForSession(sid)" in SESSIONS_JS
    assert "function _syncSessionListSnapshotOnVisit(sid, messageCount, lastMessageAt)" in SESSIONS_JS


def test_load_session_acknowledges_visit_before_and_after_message_load():
    block = _load_session_block()
    first_ack = block.find("_acknowledgeSessionVisit(")
    loading_clear = block.find("if (_loadingSessionId === sid) _loadingSessionId = null;")
    second_ack = block.find("_acknowledgeSessionVisit(", loading_clear)

    assert first_ack != -1, "loadSession must acknowledge visit when metadata arrives"
    assert loading_clear != -1, "loadSession must clear in-flight marker before final acknowledge"
    assert second_ack != -1 and first_ack < loading_clear < second_ack, (
        "loadSession must re-acknowledge after the async message-load gap so deferred "
        "sidebar polls cannot leave a sticky unread dot"
    )


def test_same_session_reselect_clears_stale_unread():
    block = _load_session_block()
    guard = block.find("if(currentSid===sid && !forceReload && !_loadingSessionId)")
    unread_check = block.find("_sessionVisitHasUnreadState(sid)", guard)
    acknowledge = block.find("_acknowledgeSessionVisit(", unread_check)

    assert guard != -1
    assert unread_check != -1 and guard < unread_check < acknowledge, (
        "re-selecting the already-open session must still clear stale unread markers"
    )


def test_polling_completion_syncs_viewed_for_open_pane_without_focus_gate():
    start = SESSIONS_JS.index("function _markPollingCompletionUnreadTransitions(sessions)")
    end = SESSIONS_JS.index("let _newSessionInFlight", start)
    block = SESSIONS_JS[start:end]

    assert "_isSessionOpenInChatPane(sid)" in block
    assert "!_isSessionActivelyViewedForList(sid)" not in block, (
        "completion polling must not require document focus to treat the open "
        "chat pane as visited — that re-introduces sticky unread dots"
    )


def test_background_completion_if_background_uses_open_pane_not_focus():
    start = SESSIONS_JS.index("function _markSessionCompletionUnreadIfBackground(sid")
    end = SESSIONS_JS.index("function _clearSessionCompletionUnread", start)
    block = SESSIONS_JS[start:end]

    assert "_isSessionOpenInChatPane(sid)" in block
    assert "_isSessionActivelyViewedForList(sid)" not in block
