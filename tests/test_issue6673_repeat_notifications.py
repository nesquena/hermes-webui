"""Focused page and producer regressions for #6673."""

import json
import shutil
import subprocess
from pathlib import Path

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")


def test_identity_capture_uses_only_canonical_journal_ids():
    capture = extract_function(MESSAGES_JS, "_captureNotificationEventIdentity")
    assert "return null" in capture
    assert "_notificationEventFallbackId" not in MESSAGES_JS
    assert "event.lastEventId" in capture
    assert "lastEventId.length>_NOTIFICATION_IDENTITY_MAX_LENGTH" in capture


def test_page_notification_path_has_no_claim_ledger_and_keeps_delivery_fallback():
    assert "indexedDB.open(" not in MESSAGES_JS
    assert "hermes.notification.present" in MESSAGES_JS
    assert "status==='shown'||status==='duplicate'?status:deliverDirect()" in MESSAGES_JS
    assert "renotify:true" in MESSAGES_JS


def test_producers_leave_delivery_frames_without_journal_ids_unkeyed():
    streaming = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    gateway = (ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")
    assert "fallback_event_seq" not in streaming
    assert "fallback_event_seq" not in gateway
    assert ":fallback:" not in streaming
    assert ":fallback:" not in gateway
    assert "queue_item = (event, data, event_id) if hasattr(q, \"subscribe_with_snapshot\")" in streaming
    assert "queue_item = (event, data, event_id) if hasattr(q, \"subscribe_with_snapshot\")" in gateway


def test_notification_present_protocol_carries_canonical_event_data():
    assert "protocolVersion:1" in MESSAGES_JS
    assert "eventId:identity.lastEventId" in MESSAGES_JS
    assert "data:{url}" in MESSAGES_JS


def test_journal_less_sse_frames_reset_sticky_eventsource_ids():
    assert "def _sse_with_reset_id" in ROUTES_PY
    assert "event_id = queued_event_id if has_queued_event_id else" in ROUTES_PY
    assert "_sse_with_reset_id(handler, event, data)" in ROUTES_PY


def test_worker_presentation_queues_release_settled_tag_state():
    assert "const trackedOperation = operation.catch(() => {});" in SW_JS
    assert "notificationPresentationByTag.delete(tag);" in SW_JS


def test_replay_cursor_accepts_only_current_canonical_positive_ids():
    node = shutil.which("node")
    if node is None:
        return
    cursor = extract_function(MESSAGES_JS, "_rememberRunJournalCursor")
    script = f"""
const cursor = {cursor};
let _lastRunJournalSeq = 0;
let _lastRunJournalEventId = '';
const streamId = 'stream-6673';
const activeSid = 'session-6673';
const INFLIGHT = {{[activeSid]: {{}}}};
const _throttledPersist = () => {{}};
for (const value of ['stream-6673:2', 'stream-6673:fallback:3', 'other:4', 'stream-6673:-1', 'stream-6673:NaN', 'stream-6673:4']) {{
  cursor({{lastEventId: value}});
}}
console.log(JSON.stringify({{seq:_lastRunJournalSeq, id:_lastRunJournalEventId}}));
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"seq": 4, "id": "stream-6673:4"}
