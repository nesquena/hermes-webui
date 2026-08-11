"""
Tests for session queue persistence across page refresh and tab restore.

#660 introduced sessionStorage persistence. #3108 hardens it by mirroring queue
state to localStorage and restoring from the durable copy when sessionStorage is
missing after browser tab/process restore.
"""
import pathlib

UI_JS = pathlib.Path(__file__).parent.parent / 'static' / 'ui.js'
SESSIONS_JS = pathlib.Path(__file__).parent.parent / 'static' / 'sessions.js'

ui_src = UI_JS.read_text(encoding='utf-8')
sess_src = SESSIONS_JS.read_text(encoding='utf-8')


class TestQueuePersistence:
    """queueSessionMessage persists through the shared dual-storage helper."""

    def test_queue_storage_helpers_exist(self):
        """Queue persistence must be centralized so write/delete paths stay symmetric."""
        assert "function _queueStorageKey(sid)" in ui_src
        assert "function _persistSessionQueueStorage(sid, queue)" in ui_src
        assert "function _readPersistedSessionQueue(sid)" in ui_src
        assert "function _clearPersistedSessionQueue(sid)" in ui_src

    def test_queue_writes_to_session_and_local_storage(self):
        """queueSessionMessage must mirror queue state to sessionStorage and localStorage."""
        helper_start = ui_src.find("function _persistSessionQueueStorage(sid, queue)")
        helper_end = ui_src.find("function _readPersistedSessionQueue(sid)", helper_start)
        assert helper_start != -1 and helper_end != -1, "_persistSessionQueueStorage helper not found"
        helper = ui_src[helper_start:helper_end]
        assert "sessionStorage.setItem(key,payload)" in helper
        assert "localStorage.setItem(key,payload)" in helper

    def test_queue_stamps_queued_at_timestamp(self):
        """Each queue entry must have a _queued_at timestamp for stale-entry detection."""
        assert '_queued_at' in ui_src

    def test_shift_uses_shared_persist_and_clear_helpers(self):
        """shiftQueuedSessionMessage must update/remove both storage layers through helpers."""
        start = ui_src.find("function shiftQueuedSessionMessage(sid)")
        end = ui_src.find("function getQueuedSessionCount(sid)", start)
        assert start != -1 and end != -1, "shiftQueuedSessionMessage block not found"
        body = ui_src[start:end]
        assert "_clearPersistedSessionQueue(sid)" in body
        assert "_persistSessionQueueStorage(sid,q)" in body

    def test_queue_card_edit_paths_use_shared_helpers(self):
        """Queue edit/combine/delete paths must not leave localStorage stale."""
        assert "_saveAndRefresh()" in ui_src
        assert "_persistSessionQueueStorage(sid,liveQ)" in ui_src
        assert "_clearPersistedSessionQueue(sid)" in ui_src


class TestQueueRestore:
    """Persisted optimistic state is reconciled with backend queue authority."""

    def test_restore_reads_shared_helper(self):
        """Session load must invoke the authoritative backend reconciliation helper."""
        assert "syncBackendSessionQueue(S.session.session_id)" in sess_src

    def test_read_helper_falls_back_to_local_storage(self):
        """The helper must fall back to localStorage and re-mirror sessionStorage."""
        start = ui_src.find("function _readPersistedSessionQueue(sid)")
        end = ui_src.find("function queueSessionMessage(sid", start)
        assert start != -1 and end != -1, "_readPersistedSessionQueue block not found"
        body = ui_src[start:end]
        assert "const sessionValue=read(sessionStorage)" in body
        assert "if(sessionValue&&sessionValue.length) return sessionValue;" in body
        assert "const localValue=read(localStorage)" in body
        assert "if(localValue&&localValue.length)" in body
        assert "sessionStorage.setItem(key,JSON.stringify(localValue))" in body

    def test_restore_uses_timestamp_guard(self):
        """Reconciliation hydrates the persisted ACK-in-flight cache before matching IDs."""
        start = ui_src.find("async function syncBackendSessionQueue(sid)")
        end = ui_src.find("function _compressionSessionLock()", start)
        assert start != -1 and end != -1
        body = ui_src[start:end]
        assert "const persisted=_readPersistedSessionQueue(sid)" in body
        assert "byClientId" in body

    def test_restore_shows_toast(self):
        """A failed authoritative reconciliation must remain visible to the user."""
        assert "Could not reconcile queued messages" in sess_src

    def test_restore_puts_text_in_composer(self):
        """Recovered queue intent remains a queue chip and is never duplicated as a draft."""
        assert "_msg.value=_first.text" not in sess_src
        assert "syncBackendSessionQueue(S.session.session_id)" in sess_src

    def test_restore_clears_stale_storage(self):
        """An empty authoritative snapshot clears the optimistic recovery cache."""
        start = ui_src.find("async function syncBackendSessionQueue(sid)")
        end = ui_src.find("function _compressionSessionLock()", start)
        body = ui_src[start:end]
        assert "_clearPersistedSessionQueue(sid)" in body

    def test_restore_wrapped_in_try_catch(self):
        """Session load contains reconciliation failure without discarding the cache."""
        sync_pos = sess_src.find("await syncBackendSessionQueue(S.session.session_id)")
        catch_pos = sess_src.find("catch(err){", sync_pos)
        assert sync_pos != -1 and catch_pos > sync_pos

    def test_delete_session_clears_persisted_queue_after_success(self):
        """Deleting a session must clear localStorage-backed queue state after the API succeeds."""
        start = sess_src.find("async function deleteSession(sid, beforeDelete=null)")
        end = sess_src.find("// ── Project helpers", start)
        assert start != -1 and end != -1, "deleteSession block not found"
        body = sess_src[start:end]
        clear_pos = body.find("if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);")
        error_pos = body.find("if(deleteResult&&deleteResult.error){")
        success_pos = body.find("const response=deleteResult&&deleteResult.response;")
        assert error_pos != -1 and success_pos != -1 and clear_pos != -1
        assert success_pos < clear_pos, "queue cleanup should run only after delete success"

    def test_active_session_not_restored_as_draft(self):
        """Active sessions reconcile queue chips without restoring intent into Composer."""
        load_start = sess_src.find("async function loadSession(")
        load_end = sess_src.find("// ── Handoff hint logic", load_start)
        load_body = sess_src[load_start:load_end]
        active_stream_pos = load_body.find("let activeStreamId=S.session.active_stream_id")
        sync_pos = load_body.find("syncBackendSessionQueue(S.session.session_id)")
        assert active_stream_pos != -1 and sync_pos > active_stream_pos
        assert "_msg.value=_first.text" not in load_body
