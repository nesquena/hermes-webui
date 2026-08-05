"""
Tests for the server-owned queue and the legacy browser-only restore fallback.
"""
import pathlib

UI_JS = pathlib.Path(__file__).parent.parent / 'static' / 'ui.js'
SESSIONS_JS = pathlib.Path(__file__).parent.parent / 'static' / 'sessions.js'

ui_src = UI_JS.read_text(encoding='utf-8')
sess_src = SESSIONS_JS.read_text(encoding='utf-8')


class TestQueuePersistence:
    """The runtime queue is a server-backed render cache."""

    def test_queue_storage_helpers_exist(self):
        """Queue persistence must be centralized so write/delete paths stay symmetric."""
        assert "function _queueStorageKey(sid)" in ui_src
        assert "function _persistSessionQueueStorage(sid, queue)" in ui_src
        assert "function _readPersistedSessionQueue(sid)" in ui_src
        assert "function _clearPersistedSessionQueue(sid)" in ui_src

    def test_queue_posts_to_server_after_uploading_files(self):
        """queueSessionMessage must upload browser files and call the queue API."""
        start = ui_src.index("async function queueSessionMessage(sid, payload)")
        end = ui_src.index("async function mutateSessionQueue", start)
        body = ui_src[start:end]
        assert "uploadPendingFiles({files:browserFiles,sessionId:sid,clearPending:false})" in body
        assert "_postSessionQueue(sid,{action:'enqueue'" in body
        assert "model_provider:payload.model_provider||null" in body

    def test_queue_stamps_queued_at_timestamp(self):
        """Each queue entry must have a _queued_at timestamp for stale-entry detection."""
        assert '_queued_at' in ui_src

    def test_runtime_drain_was_removed(self):
        """The browser must not pop or submit a queue item on idle."""
        assert "function shiftQueuedSessionMessage" not in ui_src
        assert "_queueDrainSid" not in ui_src

    def test_queue_card_edit_paths_use_server_actions(self):
        """Queue controls must be accepted by the server before the cache changes."""
        assert "return _postSessionQueue(sid,{action,...extra})" in ui_src
        assert "_saveAndRefresh('combine')" in ui_src
        assert "_saveAndRefresh('reorder',{item_ids:itemIds})" in ui_src
        assert "_saveAndRefresh('edit',{item_id:_entryId,text:newText})" in ui_src
        assert "_saveAndRefresh('delete',{item_id:_entryId})" in ui_src


class TestQueueRestore:
    """Legacy browser entries are restored for review, never auto-submitted."""

    def test_restore_reads_shared_helper(self):
        """sessions.js must use the shared helper so localStorage fallback is reachable."""
        assert "_readPersistedSessionQueue(sid)" in sess_src

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
        """Stale legacy entries (created before the last response) are dropped."""
        assert '_queued_at' in sess_src
        assert '_lastAsst' in sess_src

    def test_restore_shows_toast(self):
        """User must see a toast notification when a queue is restored."""
        assert 'queued message' in sess_src.lower() and 'restored' in sess_src.lower()

    def test_restore_puts_text_in_composer(self):
        """First queued message goes into the composer input, not auto-sent."""
        assert "_msg.value=_first.text" in sess_src

    def test_restore_clears_legacy_storage_after_review_restore(self):
        """Legacy storage is cleared after its first item is copied for review."""
        assert "_clearPersistedSessionQueue(sid)" in sess_src

    def test_restore_wrapped_in_try_catch(self):
        """Storage access must be wrapped in try/catch (private browsing may block it)."""
        assert "catch(_){if(typeof _clearPersistedSessionQueue==='function') _clearPersistedSessionQueue(sid);}" in sess_src

    def test_delete_session_clears_legacy_queue_after_success(self):
        """Deleting a session must clear any leftover legacy browser queue."""
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
        """When agent is active (INFLIGHT), queue restore must NOT run."""
        # The restore block must be inside the else branch (idle path), not the INFLIGHT branch
        inflight_pos = sess_src.find("if(INFLIGHT[sid]){")
        restore_pos = sess_src.find("_readPersistedSessionQueue(sid)")
        else_pos = sess_src.find("}else{", inflight_pos)
        assert restore_pos > else_pos, \
            "Queue restore must be inside the else (idle) branch, not the INFLIGHT branch"
