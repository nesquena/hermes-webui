from pathlib import Path


MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")
SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")


def test_named_context_clear_helper_is_exported_for_session_switches():
    assert "function _clearPendingSelections(){" in MESSAGES_JS
    assert "window._clearPendingSelections=_clearPendingSelections;" in MESSAGES_JS


def test_loadsession_clears_pending_named_context_before_saving_old_draft():
    start = SESSIONS_JS.index("if (currentSid && currentSid !== sid) {")
    end = SESSIONS_JS.index("if (currentSid !== sid || forceReload) {", start)
    block = SESSIONS_JS[start:end]

    clear_idx = block.find("window._clearPendingSelections()")
    save_idx = block.find("await _saveComposerDraftNow(currentSid")

    assert clear_idx != -1, "loadSession() must clear pending named context blocks on real session switches"
    assert save_idx != -1, "loadSession() switch block must still persist the old draft before leaving"
    assert clear_idx < save_idx, "pending named context blocks should disappear before the switch draft save yields"


def test_newsession_clears_pending_named_context():
    """New Chat (newSession) must also clear pending named context blocks — it
    replaces S.session WITHOUT going through loadSession(), so without an explicit
    clear, blocks selected in the previous conversation would leak into the new
    chat and be flushed on the first send (#2543)."""
    start = SESSIONS_JS.index("async function newSession(")
    end = SESSIONS_JS.index("updateQueueBadge();", start)
    head = SESSIONS_JS[start:end]
    assert "window._clearPendingSelections()" in head, (
        "newSession() must clear pending named context blocks before replacing S.session"
    )
