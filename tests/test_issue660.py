"""Tests for the server-owned queue and the runner-local fail-closed boundary."""
import pathlib

UI_JS = pathlib.Path(__file__).parent.parent / 'static' / 'ui.js'
SESSIONS_JS = pathlib.Path(__file__).parent.parent / 'static' / 'sessions.js'
ROUTES_PY = pathlib.Path(__file__).parent.parent / 'api' / 'routes.py'

ui_src = UI_JS.read_text(encoding='utf-8')
sess_src = SESSIONS_JS.read_text(encoding='utf-8')
routes_src = ROUTES_PY.read_text(encoding='utf-8')


class TestQueuePersistence:
    """The runtime queue is a server-backed render cache."""

    def test_queue_send_does_not_use_legacy_browser_storage(self):
        """New queue acceptance must not write the legacy browser draft store."""
        start = ui_src.index("async function queueSessionMessage(sid, payload)")
        end = ui_src.index("async function mutateSessionQueue", start)
        body = ui_src[start:end]
        assert "_persistSessionQueueStorage" not in body
        assert "_readPersistedSessionQueue" not in body

    def test_queue_posts_to_server_after_uploading_files(self):
        """queueSessionMessage must upload browser files and call the queue API."""
        start = ui_src.index("async function queueSessionMessage(sid, payload)")
        end = ui_src.index("async function mutateSessionQueue", start)
        body = ui_src[start:end]
        assert "uploadPendingFiles({files:browserFiles,sessionId:sid,clearPending:false})" in body
        assert "const body={action:'enqueue',text:String(payload.text||''),files," in body
        assert "body.intent=JSON.parse(JSON.stringify(payload.intent));" in body
        assert "body.intent.attachments=[...files];" in body
        assert "const response=await _postSessionQueue(sid,body);" in body
        assert "model_provider:payload.model_provider||null" in body

    def test_queue_stamps_queued_at_timestamp(self):
        """The server assigns each queue entry's stale-entry timestamp."""
        assert '"_queued_at": queued_at if queued_at is not None' in routes_src

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


class TestRunnerLocalQueueBoundary:
    """Runner-local has no browser-owned queue acceptance path."""

    def test_browser_queue_state_is_absent_but_legacy_draft_restore_remains(self):
        assert "_serverQueueOwned" not in ui_src
        assert "_browserQueue" not in ui_src
        assert "_readPersistedSessionQueue" in ui_src
        assert "_readPersistedSessionQueue" in sess_src
        assert "review and send when ready" in sess_src

    def test_clear_does_not_touch_removed_browser_queue_state(self):
        assert "_clearBrowserOwnedSessionQueue" not in ui_src

    def test_runner_local_guard_precedes_file_upload(self):
        start = ui_src.index("async function queueSessionMessage(sid, payload)")
        end = ui_src.index("async function mutateSessionQueue", start)
        body = ui_src[start:end]
        assert body.index("queue_capability") < body.index("uploadPendingFiles")
        assert "error.status=501" in body
