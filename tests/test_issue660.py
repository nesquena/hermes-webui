"""
Tests for #660: session queue persistence across page refresh.

The queue is stored through shared persistence helpers that mirror state to
sessionStorage and localStorage, and restored on session load when the agent
is idle.
"""
import pathlib

UI_JS = pathlib.Path(__file__).parent.parent / 'static' / 'ui.js'
SESSIONS_JS = pathlib.Path(__file__).parent.parent / 'static' / 'sessions.js'

ui_src = UI_JS.read_text(encoding='utf-8')
sess_src = SESSIONS_JS.read_text(encoding='utf-8')


class TestQueuePersistence:
    """queueSessionMessage persists through shared storage helpers."""

    def test_queue_writes_via_shared_storage_helpers(self):
        """queueSessionMessage must write through the persistence helper after enqueueing."""
        assert "function _persistSessionQueueStorage(sid, queue){" in ui_src
        assert "sessionStorage.setItem(key,payload);" in ui_src
        assert "localStorage.setItem(key,payload);" in ui_src
        assert "_persistSessionQueueStorage(sid,q);" in ui_src

    def test_queue_stamps_queued_at_timestamp(self):
        """Each queue entry must have a _queued_at timestamp for stale-entry detection."""
        assert '_queued_at' in ui_src

    def test_shift_removes_via_shared_storage_helper(self):
        """shiftQueuedSessionMessage must remove/update persisted queue state on dequeue."""
        assert "function _removeSessionQueueStorage(sid){" in ui_src
        assert "_removeSessionQueueStorage(sid);" in ui_src

    def test_shift_updates_persisted_storage_when_items_remain(self):
        """When queue still has items after shift, persisted storage is updated (not removed)."""
        assert "_persistSessionQueueStorage(sid,q);" in ui_src


class TestQueueRestore:
    """Queue is restored from shared storage helpers on session load when agent is idle."""

    def test_restore_reads_via_shared_storage_helper(self):
        """sessions.js must restore queue state via the shared read helper in the idle-session load path."""
        assert "typeof _readPersistedSessionQueue==='function' ? _readPersistedSessionQueue(sid) : []" in sess_src

    def test_restore_uses_timestamp_guard(self):
        """Stale entries (created before last assistant response) must be dropped."""
        assert '_queued_at' in sess_src
        assert '_lastAsst' in sess_src

    def test_restore_shows_toast(self):
        """User must see a toast notification when a queue is restored."""
        assert 'queued message' in sess_src.lower() and 'restored' in sess_src.lower()

    def test_restore_puts_text_in_composer(self):
        """First queued message goes into the composer input, not auto-sent."""
        assert "_msg.value=_first.text" in sess_src

    def test_restore_clears_stale_storage_via_helper(self):
        """On timestamp mismatch, stale persisted queue state is removed."""
        assert "_removeSessionQueueStorage(sid);" in sess_src

    def test_restore_wrapped_in_try_catch(self):
        """Storage restore must be wrapped in try/catch and clear persisted state on failure."""
        assert "}catch(_){" in sess_src
        assert "if(typeof _removeSessionQueueStorage==='function') _removeSessionQueueStorage(sid);" in sess_src

    def test_active_session_not_restored_as_draft(self):
        """When agent is active (INFLIGHT), queue restore must NOT run."""
        inflight_pos = sess_src.find("if(INFLIGHT[sid]){")
        restore_pos = sess_src.find("_readPersistedSessionQueue(sid)")
        else_pos = sess_src.find("}else{", inflight_pos)
        assert restore_pos > else_pos, \
            "Queue restore must be inside the else (idle) branch, not the INFLIGHT branch"
