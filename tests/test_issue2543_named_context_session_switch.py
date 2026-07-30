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


def test_selection_id_counter_resets_when_all_chips_cleared():
    # #5929: the module-level _selectionIdCounter only ever incremented, so
    # clearing all context chips and selecting again started at "Context N+1"
    # instead of "Context 1". It must reset to 0 when the last chip is removed
    # AND on _clearPendingSelections (clear-all / session switch).
    remove_fn = MESSAGES_JS[
        MESSAGES_JS.index("function _removeNamedContextBlock(id){"):
        MESSAGES_JS.index("function _clearPendingSelections(){")
    ]
    assert "if(!_pendingSelections.length)_selectionIdCounter=0;" in remove_fn, (
        "_removeNamedContextBlock must reset _selectionIdCounter to 0 once the "
        "last chip is removed so the next selection restarts at Context 1"
    )
    clear_fn = MESSAGES_JS[MESSAGES_JS.index("function _clearPendingSelections(){"):]
    clear_fn = clear_fn[:clear_fn.index("\n}") + 2]
    assert "_selectionIdCounter=0;" in clear_fn, (
        "_clearPendingSelections must reset _selectionIdCounter to 0 (clear-all "
        "and session-switch path) so context chip numbering restarts at 1"
    )
