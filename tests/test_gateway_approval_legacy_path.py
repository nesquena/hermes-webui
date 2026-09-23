"""Tests for approval event handling on the gateway legacy /v1/chat/completions path (#4549).

The legacy path is the default when HERMES_WEBUI_GATEWAY_USE_RUNS_API is not set.
PR #4495 fixed the runs API path but left the legacy path without approval handling.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.gateway_chat import _gateway_runs_approval_event

REPO_ROOT = Path(__file__).parent.parent
GATEWAY_CHAT_SRC = (REPO_ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")

_LEGACY_MARKER = 'url = f"{base_url}/v1/chat/completions"'
_NEXT_FUNC_RE = "\ndef "


def _extract_legacy_sse_loop():
    """Extract the legacy /v1/chat/completions SSE relay function body."""
    start = GATEWAY_CHAT_SRC.find(_LEGACY_MARKER)
    assert start >= 0, "Legacy chat/completions path not found in gateway_chat.py"
    end = GATEWAY_CHAT_SRC.find(_NEXT_FUNC_RE, start)
    if end < 0:
        end = len(GATEWAY_CHAT_SRC)
    return GATEWAY_CHAT_SRC[start:end]


def _drain_queue(q):
    import queue

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


def _make_legacy_gateway_urlopen(approval_payload: str, after_approval=None):
    def fake_urlopen(req, *, timeout=None):
        del req, timeout

        def _iter():
            yield b"event: approval.request"
            yield f"data: {approval_payload}".encode("utf-8")
            yield b""
            if after_approval is not None:
                after_approval()
            yield b'data: {"choices":[{"delta":{"content":"Done"}}]}'
            yield b""
            yield b"data: [DONE]"
            yield b""

        resp = MagicMock()
        resp.__iter__ = lambda s: _iter()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    return fake_urlopen


def test_legacy_loop_checks_approval_request_event():
    """Legacy SSE loop must handle `approval.request` events."""
    loop = _extract_legacy_sse_loop()
    assert '"approval.request"' in loop, (
        "Legacy SSE loop must check for approval.request event name"
    )


def test_legacy_loop_checks_hermes_approval_request_event():
    """Legacy SSE loop must handle `hermes.approval.request` events."""
    loop = _extract_legacy_sse_loop()
    assert '"hermes.approval.request"' in loop, (
        "Legacy SSE loop must check for hermes.approval.request event name"
    )


def test_legacy_loop_derives_event_from_payload():
    """Legacy SSE loop must derive event type from JSON payload fields."""
    loop = _extract_legacy_sse_loop()
    assert 'payload.get("event")' in loop or "payload.get('event')" in loop, (
        "Legacy SSE loop must check payload JSON 'event' field"
    )


def test_legacy_loop_calls_put_gateway_event_approval():
    """Legacy SSE loop must relay approval via put_gateway_event('approval', ...)."""
    loop = _extract_legacy_sse_loop()
    assert 'put_gateway_event("approval"' in loop, (
        "Legacy SSE loop must call put_gateway_event with 'approval' event type"
    )


def test_legacy_loop_calls_submit_gateway_pending_mirror():
    """Legacy SSE loop must mirror approval to polling state."""
    loop = _extract_legacy_sse_loop()
    assert "submit_gateway_pending_mirror" in loop, (
        "Legacy SSE loop must call submit_gateway_pending_mirror for polling fallback"
    )


def test_legacy_loop_reuses_gateway_runs_approval_event():
    """Legacy SSE loop must reuse _gateway_runs_approval_event, not duplicate the mapping."""
    loop = _extract_legacy_sse_loop()
    assert "_gateway_runs_approval_event" in loop, (
        "Legacy SSE loop must call _gateway_runs_approval_event to map the payload"
    )


def test_legacy_loop_resets_sse_event_after_approval():
    """Legacy SSE loop must reset sse_event to 'message' after handling approval."""
    loop = _extract_legacy_sse_loop()
    approval_idx = loop.find('"hermes.approval.request"')
    assert approval_idx >= 0
    # Window sized to cover the approval handling block including the run_id
    # recording added in the #4549 follow-up (reset lands ~1360 chars in).
    block_after = loop[approval_idx:approval_idx + 2200]
    assert 'sse_event = "message"' in block_after, (
        "Must reset sse_event to 'message' after approval handling to prevent bleed"
    )


def test_approval_event_mapping_complete_payload():
    """_gateway_runs_approval_event correctly maps a full approval payload."""
    result = _gateway_runs_approval_event({
        "command": "rm -rf /tmp/x",
        "description": "Dangerous command approval",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": "appr-leg-1",
        "choices": ["once", "session", "always", "deny"],
    })
    assert result is not None
    assert result["tool"] == "dangerous_command"
    assert result["command"] == "rm -rf /tmp/x"
    assert result["description"] == "Dangerous command approval"
    assert result["approval_id"] == "appr-leg-1"
    assert result["allow_permanent"] is True
    assert result["risk_level"] == "high"


def test_approval_event_mapping_rejects_empty():
    """Incomplete payload returns None."""
    assert _gateway_runs_approval_event({"risk_level": "high"}) is None
    assert _gateway_runs_approval_event({}) is None


# ---------------------------------------------------------------------------
# Behavioral regression test — fails on base, passes on head
# ---------------------------------------------------------------------------

def test_legacy_sse_loop_relays_approval_event():
    """Legacy /v1/chat/completions SSE loop must relay approval events to the frontend.

    This is the primary regression test for #4549. On the base branch (before
    the fix), the approval SSE event falls through the delta parser and never
    produces an ("approval", ...) event, so this test fails. On head (after the
    fix), the approval handler catches it and emits the event.
    """
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-legacy-approval"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    approval_payload = json.dumps({
        "command": "rm -rf /tmp/test",
        "description": "Delete temporary files",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": "appr-legacy-1",
        "choices": ["once", "session", "always", "deny"],
    })
    sse_body = (
        f"event: approval.request\ndata: {approval_payload}\n\n"
        'data: {"choices":[{"delta":{"content":"Done"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None

    def fake_urlopen(req, *, timeout=None):
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(sse_body.split(b"\n"))
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=False), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-legacy-approval",
                    msg_text="do something risky",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    approval_events = [
        e for e in events
        if isinstance(e, tuple) and e[0] == "approval"
    ]
    assert approval_events, (
        f"Legacy SSE loop must relay approval events to the frontend. "
        f"Got events: {[e[0] if isinstance(e, tuple) else e for e in events]}"
    )
    assert approval_events[0][1]["command"] == "rm -rf /tmp/test"
    assert approval_events[0][1]["approval_id"] == "appr-legacy-1"
    assert approval_events[0][1]["description"] == "Delete temporary files"


def test_legacy_approval_records_run_id_for_response_relay():
    """#4549 follow-up: a legacy approval event carrying a run_id must populate
    _STREAM_RUN_IDS so /api/approval/respond can relay the choice back to the
    gateway and resume the parked run.

    Without recording the run_id, the approval card renders but approve/deny
    falls through to the local path (no remote gateway agent to resume) and the
    response is {"ok": false}. This regression test fails on the pre-fix head
    (run_id never stored) and passes on the fixed head.
    """
    import io
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_chat_streaming

    events = []
    # Capture _STREAM_RUN_IDS at the instant the approval event is emitted.
    # In production the legacy SSE connection stays open (blocked on the gateway
    # stream) while the run is parked for approval, so _STREAM_RUN_IDS is still
    # populated when the user responds. This test completes the stream
    # synchronously, after which the function's finally-block pops the mapping —
    # so we snapshot the live value mid-stream rather than after return.
    run_id_at_approval = {}

    def _record(item):
        events.append(item)
        if isinstance(item, tuple) and item[0] == "approval":
            run_id_at_approval["value"] = _STREAM_RUN_IDS.get(stream_id)

    q = MagicMock()
    q.put_nowait = _record

    stream_id = "sid-legacy-runid"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q
    _STREAM_RUN_IDS.pop(stream_id, None)

    approval_payload = json.dumps({
        "command": "rm -rf /tmp/test",
        "description": "Delete temporary files",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": "appr-legacy-runid",
        "run_id": "run-legacy-1",
        "choices": ["once", "session", "always", "deny"],
    })
    sse_body = (
        f"event: approval.request\ndata: {approval_payload}\n\n"
        'data: {"choices":[{"delta":{"content":"Done"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None

    def fake_urlopen(req, *, timeout=None):
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(sse_body.split(b"\n"))
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=False), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-legacy-runid",
                    msg_text="do something risky",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )

        # The run_id from the approval payload must have been recorded at the
        # moment the approval event was emitted (before the synchronous stream
        # completed and the finally-block popped it).
        assert run_id_at_approval.get("value") == "run-legacy-1", (
            "Legacy approval event must record run_id in _STREAM_RUN_IDS so the "
            "approval response can relay to the gateway and resume the run. "
            f"Got {run_id_at_approval.get('value')!r}"
        )

        # And /api/approval/respond must actually relay to the gateway runs API
        # when the mapping is live. Use a fresh session whose active_stream_id is
        # still set + re-seed _STREAM_RUN_IDS to model the production state
        # (connection still open, run parked) — this test's stream already ran to
        # completion, which cleared active_stream_id and popped the mapping.
        _STREAM_RUN_IDS[stream_id] = "run-legacy-1"
        from types import SimpleNamespace
        from api import route_approvals as ra
        with ra._lock:
            ra._gateway_queues["sess-legacy-runid"] = [SimpleNamespace(data={
                "command": "rm -rf /tmp/test",
                "description": "Delete temporary files",
                "pattern_key": "dangerous_command",
                "pattern_keys": ["dangerous_command"],
                "approval_id": "appr-legacy-runid",
                "run_id": "run-legacy-1",
            })]
            live_entry = ra._gateway_queues["sess-legacy-runid"][0]
        ra.submit_gateway_pending_mirror("sess-legacy-runid", live_entry.data)
        relay_session = MagicMock()
        relay_session.active_stream_id = stream_id
        captured = {}

        def fake_request_json(self, req):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return {"ok": True}

        handler = MagicMock()
        handler.wfile = io.BytesIO()
        body = {"session_id": "sess-legacy-runid", "choice": "once",
                "approval_id": "appr-legacy-runid"}

        with patch("api.routes.get_session", return_value=relay_session), \
             patch("api.runner_client.HttpRunnerClient._request_json", new=fake_request_json), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""):
            from api.routes import _handle_approval_respond
            _handle_approval_respond(handler, body)

        # No mirror/session profile is bound, so the relay must use the unscoped
        # owner URL rather than inventing "/p/default" (which could target the
        # wrong profile).
        assert captured.get("url", "") == "http://gw:8642/v1/runs/run-legacy-1/approval", (
            f"approval respond must relay to the gateway run; got {captured.get('url')!r}"
        )
        assert captured["body"] == {"choice": "once", "approval_id": ""}
        handler.send_response.assert_called_with(200)
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_legacy_teardown_clears_stale_gateway_mirror_and_notifies_empty_state():
    """Legacy teardown must remove a stale gateway mirror and publish empty SSE state."""
    from types import SimpleNamespace
    from api import route_approvals as ra
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    session_id = "sess-legacy-teardown-stale"
    stream_id = "sid-legacy-teardown-stale"
    approval_data = {
        "command": "rm -rf /tmp/test",
        "description": "Delete temporary files",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": "appr-legacy-teardown-stale",
        "choices": ["once", "session", "always", "deny"],
        "run_id": "run-legacy-teardown-stale",
    }
    approval_payload = json.dumps(approval_data)

    subscriber = ra._approval_sse_subscribe(session_id)
    q = MagicMock()
    q.put_nowait = lambda item: None

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None
    mock_session.pending_user_source = None

    try:
        with ra._lock:
            ra._pending.pop(session_id, None)
            ra._gateway_queues[session_id] = [SimpleNamespace(data=dict(approval_data))]
        with STREAMS_LOCK:
            STREAMS[stream_id] = q

        def clear_gateway_queue():
            with ra._lock:
                ra._gateway_queues.pop(session_id, None)

        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=False), \
                 patch("urllib.request.urlopen", side_effect=_make_legacy_gateway_urlopen(approval_payload, clear_gateway_queue)), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id=session_id,
                    msg_text="do something risky",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )

        payloads = _drain_queue(subscriber)
        assert payloads, "Expected approval SSE notifications from mirror and teardown"
        assert payloads[0]["pending"]["_gateway_mirror"] is True
        assert payloads[0]["pending_count"] == 1
        assert payloads[-1]["pending"] is None
        assert payloads[-1]["pending_count"] == 0
        with ra._lock:
            assert session_id not in ra._pending
    finally:
        ra._approval_sse_unsubscribe(session_id, subscriber)
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        with ra._lock:
            ra._pending.pop(session_id, None)
            ra._gateway_queues.pop(session_id, None)


def test_legacy_teardown_retires_live_gateway_head_mirror():
    """A mapped legacy run retires its live gateway mirror during outer teardown."""
    from types import SimpleNamespace
    from api import route_approvals as ra
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    session_id = "sess-legacy-teardown-live"
    stream_id = "sid-legacy-teardown-live"
    approval_data = {
        "command": "rm -rf /tmp/test",
        "description": "Delete temporary files",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": "appr-legacy-teardown-live",
        "choices": ["once", "session", "always", "deny"],
        "run_id": "run-legacy-teardown-live",
    }
    approval_payload = json.dumps(approval_data)

    subscriber = ra._approval_sse_subscribe(session_id)
    q = MagicMock()
    q.put_nowait = lambda item: None

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None
    mock_session.pending_user_source = None

    try:
        with ra._lock:
            ra._pending.pop(session_id, None)
            ra._gateway_queues[session_id] = [SimpleNamespace(data=dict(approval_data))]
        with STREAMS_LOCK:
            STREAMS[stream_id] = q

        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=False), \
                 patch("urllib.request.urlopen", side_effect=_make_legacy_gateway_urlopen(approval_payload)), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id=session_id,
                    msg_text="do something risky",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )

        payloads = _drain_queue(subscriber)
        assert payloads, "Expected mirrored approval notifications"
        assert payloads[-1]["pending"] is None
        assert payloads[-1]["pending_count"] == 0
        with ra._lock:
            pending = ra._pending.get(session_id)
            assert pending is None
    finally:
        ra._approval_sse_unsubscribe(session_id, subscriber)
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        with ra._lock:
            ra._pending.pop(session_id, None)
            ra._gateway_queues.pop(session_id, None)


def test_legacy_teardown_preserves_local_pending_entry():
    """Legacy gateway teardown must not remove non-gateway pending approvals."""
    from api import route_approvals as ra
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    session_id = "sess-legacy-teardown-local"
    stream_id = "sid-legacy-teardown-local"
    local_pending = {
        "command": "echo local",
        "description": "Local approval",
        "pattern_key": "local_command",
        "pattern_keys": ["local_command"],
        "approval_id": "appr-legacy-local",
    }
    approval_data = {
        "command": "rm -rf /tmp/test",
        "description": "Delete temporary files",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": "appr-legacy-teardown-local",
        "choices": ["once", "session", "always", "deny"],
        "run_id": "run-legacy-teardown-local",
    }
    approval_payload = json.dumps(approval_data)

    subscriber = ra._approval_sse_subscribe(session_id)
    q = MagicMock()
    q.put_nowait = lambda item: None

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None
    mock_session.pending_user_source = None

    try:
        with ra._lock:
            ra._gateway_queues.pop(session_id, None)
            ra._pending[session_id] = [dict(local_pending)]
        with STREAMS_LOCK:
            STREAMS[stream_id] = q

        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=False), \
                 patch("urllib.request.urlopen", side_effect=_make_legacy_gateway_urlopen(approval_payload)), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id=session_id,
                    msg_text="do something risky",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )

        payloads = _drain_queue(subscriber)
        assert payloads, "Expected local pending approval notifications"
        assert any(
            payload["pending"] and payload["pending"]["approval_id"] == local_pending["approval_id"]
            for payload in payloads
        )
        with ra._lock:
            pending = ra._pending.get(session_id)
            assert isinstance(pending, list)
            assert pending[0]["approval_id"] == local_pending["approval_id"]
            assert pending[0].get(ra._GATEWAY_MIRROR_FLAG) is not True
    finally:
        ra._approval_sse_unsubscribe(session_id, subscriber)
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        with ra._lock:
            ra._pending.pop(session_id, None)
            ra._gateway_queues.pop(session_id, None)


def test_mirrored_run_id_survives_active_stream_loss():
    """A mirrored gateway approval must still relay after active_stream_id is lost."""
    import io
    import threading
    from types import SimpleNamespace
    from api import route_approvals as ra
    from api import routes

    sid = "sess-legacy-stream-loss"
    approval_id = "appr-legacy-stream-loss"
    run_id = "run-legacy-stream-loss"

    with ra._lock:
        ra._gateway_queues.pop(sid, None)
        ra._pending.pop(sid, None)

    entry = SimpleNamespace(
        data={
            "command": "rm -rf /tmp/test",
            "description": "Delete temporary files",
            "pattern_key": "dangerous_command",
            "pattern_keys": ["dangerous_command"],
            "approval_id": approval_id,
            "run_id": run_id,
            "choices": ["once", "session", "always", "deny"],
        },
        event=threading.Event(),
        result=None,
    )
    with ra._lock:
        ra._gateway_queues.setdefault(sid, []).append(entry)
    ra.submit_gateway_pending_mirror(sid, entry.data)

    with ra._lock:
        mirrored = ra._pending[sid][0]
    assert mirrored["approval_id"] == approval_id
    assert mirrored["run_id"] == run_id
    assert mirrored.get(ra._GATEWAY_MIRROR_FLAG) is True

    relay_session = MagicMock()
    relay_session.active_stream_id = None
    captured = {}

    def fake_request_json(self, req):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return {"ok": True}

    def fake_resolve_gateway_approval(session_key, choice, resolve_all=False):
        del resolve_all
        with ra._lock:
            queue = ra._gateway_queues.get(session_key) or []
            if not queue:
                return 0
            queued_entry = queue.pop(0)
            queued_entry.result = choice
            queued_entry.event.set()
            if not queue:
                ra._gateway_queues.pop(session_key, None)
            return 1

    handler = MagicMock()
    handler.wfile = io.BytesIO()
    body = {"session_id": sid, "choice": "once", "approval_id": approval_id}

    try:
        with patch("api.routes.get_session", return_value=relay_session), \
             patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=True), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.config.get_config", return_value={}), \
             patch("api.routes.resolve_gateway_approval", new=fake_resolve_gateway_approval), \
             patch("api.runner_client.HttpRunnerClient._request_json", new=fake_request_json):
            routes._handle_approval_respond(handler, body)

        # Unbound mirror + session: unscoped owner URL, never an invented
        # "/p/default" prefix.
        assert captured.get("url", "") == f"http://gw:8642/v1/runs/{run_id}/approval", (
            f"approval respond must relay to the mirrored gateway run; got {captured.get('url')!r}"
        )
        assert captured["body"] == {"choice": "once", "approval_id": ""}
        handler.send_response.assert_called_with(200)
        assert entry.event.is_set(), "mirrored gateway approval was not resolved"
        assert entry.result == "once"
        with ra._lock:
            assert sid not in ra._pending, "mirrored pending card was not cleared"
            assert sid not in ra._gateway_queues, "parked gateway entry was not drained"
        assert handler.wfile.getvalue()
        assert json.loads(handler.wfile.getvalue().decode("utf-8")) == {
            "ok": True,
            "choice": "once",
            "relayed": True,
        }
    finally:
        with ra._lock:
            ra._gateway_queues.pop(sid, None)
            ra._pending.pop(sid, None)


def test_gateway_mode_no_pending_click_stays_non_409():
    """Gateway mode must still fall through when nothing is pending."""
    from api import route_approvals as ra
    from api import routes

    sid = "sess-legacy-no-pending"
    approval_id = "appr-legacy-no-pending"

    with ra._lock:
        ra._gateway_queues.pop(sid, None)
        ra._pending.pop(sid, None)

    mock_session = MagicMock()
    mock_session.active_stream_id = None
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None

    captured = {}

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}), \
         patch("api.routes.get_session", return_value=mock_session), \
         patch("api.routes.j", new=fake_j), \
         patch("api.runtime_adapter.runtime_adapter_enabled", return_value=False):
        routes._handle_approval_respond(
            object(),
            {"session_id": sid, "choice": "once", "approval_id": approval_id},
        )

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["choice"] == "once"
    assert captured["payload"]["stale_cleared"] is True
    assert captured["payload"].get("code") != "gateway_run_unavailable"


def test_legacy_approval_without_run_id_retires_locally():
    """A no-run legacy mirror retires locally instead of becoming a ghost card."""
    from types import SimpleNamespace
    from api import routes as r
    from api import route_approvals as ra
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_chat_streaming

    stream_id = "sid-legacy-no-run"
    session_id = "sess-legacy-no-run"
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    with STREAMS_LOCK:
        STREAMS[stream_id] = q
    _STREAM_RUN_IDS.pop(stream_id, None)

    approval_payload = json.dumps({
        "command": "rm -rf /tmp/test",
        "description": "Delete temporary files",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": "appr-legacy-no-run",
        "choices": ["once", "session", "always", "deny"],
    })
    sse_body = (
        f"event: approval.request\ndata: {approval_payload}\n\n"
        'data: {"choices":[{"delta":{"content":"Done"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    mock_session.workspace = "/tmp"
    mock_session.model = "test"
    mock_session.model_provider = None
    mock_session.profile = None
    mock_session.context_messages = []
    mock_session.messages = []
    mock_session.pending_user_message = None
    mock_session.pending_attachments = None
    mock_session.pending_started_at = None

    def fake_urlopen(req, *, timeout=None):
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(sse_body.split(b"\n"))
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    captured = {}

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=False), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id=session_id,
                    msg_text="do something risky",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )

        assert _STREAM_RUN_IDS.get(stream_id) is None
        approval_events = [
            item for item in events
            if isinstance(item, tuple) and item[0] == "approval"
        ]
        assert approval_events
        approval_data = approval_events[0][1]
        local_entry = SimpleNamespace(data=dict(approval_data), event=threading.Event(), result=None)
        with ra._lock:
            r._gateway_queues[session_id] = [local_entry]
        ra.submit_gateway_pending_mirror(session_id, approval_data)
        with ra._lock:
            pending_queue = r._pending.get(session_id)
            assert isinstance(pending_queue, list)
            approval_id = pending_queue[0]["approval_id"]

        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}), \
             patch("api.routes.get_session", return_value=mock_session), \
             patch("api.routes.j", new=fake_j):
            r._handle_approval_respond(
                object(),
                {"session_id": session_id, "choice": "once", "approval_id": approval_id},
            )

        assert captured["status"] == 200
        assert captured["payload"] == {"ok": True, "choice": "once", "local_retired": True}
        assert local_entry.event.is_set()
        assert local_entry.result == "once"
        with ra._lock:
            assert session_id not in r._pending
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        with ra._lock:
            r._pending.pop(session_id, None)
            r._gateway_queues.pop(session_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_route_deny_settles_exact_non_head_run_producer():
    """The response route must settle the exact producer whose card was denied.

    The route is the only caller that supplies a target's ``(run_id,
    approval_id)`` to the legacy resolver, so a regression in that wiring is
    invisible to helper-level coverage: the producer would be dropped from the
    queue without its waiter ever being woken, leaving the agent thread parked
    until the approval timeout.

    Unlike the route-level test in ``test_approval_unblock.py`` (which needs the
    installed agent's ``tools.approval`` and is skipped without it), this one
    runs wherever the suite runs.
    """
    from types import SimpleNamespace
    from api import route_approvals as ra
    from api import routes as r

    sid = "sess-route-non-head-deny"
    run_id = "run-route-non-head-deny"
    head_id = "approval-route-head"
    target_id = "approval-route-target"
    sibling_id = "approval-route-sibling"
    other_run_id = "run-route-unrelated"
    head = {
        "approval_id": head_id,
        "run_id": run_id,
        "command": "echo head",
        "description": "Head approval on the run",
        "_gateway_agent_identity_v1": True,
    }
    sibling = {
        "approval_id": sibling_id,
        "run_id": run_id,
        "command": "echo sibling",
        "description": "Non-head sibling approval on the same run",
        "_gateway_agent_identity_v1": True,
    }
    target = {
        "approval_id": target_id,
        "run_id": run_id,
        "command": "echo target",
        "description": "Non-head approval on the same run",
        "_gateway_agent_identity_v1": True,
    }
    other_run = {
        "approval_id": "approval-route-other",
        "run_id": other_run_id,
        "command": "echo else",
        "description": "Approval on an unrelated run",
    }

    def producer(payload):
        """The agent's real producer entry when installed, an equivalent one otherwise.

        The suite runs without hermes-agent in CI, where
        ``tools.approval._ApprovalEntry`` does not exist; the stand-in carries the
        same ``data`` / ``event`` / ``result`` / ``reason`` contract the resolution
        path touches, so the assertions below mean the same thing in both shapes.
        """
        try:
            from tools.approval import _ApprovalEntry
        except Exception:
            _ApprovalEntry = None
        if _ApprovalEntry is not None:
            return _ApprovalEntry(dict(payload))
        return SimpleNamespace(
            data=dict(payload), event=threading.Event(), result=None, reason=None
        )

    head_entry = producer(head)
    sibling_entry = producer(sibling)
    target_entry = producer(target)
    other_run_entry = producer(other_run)
    relayed = []
    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured.update(payload=data, status=status)
        return data

    def fake_respond(_self, got_run_id, got_approval_id, choice):
        relayed.append((got_run_id, got_approval_id, choice))
        return {"resolved": 1}

    try:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues[sid] = [head_entry, sibling_entry, target_entry, other_run_entry]
        ra.submit_gateway_pending_mirror(sid, dict(target_entry.data))
        mirror = ra.gateway_pending_mirror(sid, approval_id=target_id, run_id=run_id)
        assert mirror is not None, "the non-head approval must be mirrored before it is answered"

        with patch("api.routes.j", new=fake_j), \
             patch("api.runner_client.HttpRunnerClient.respond_approval", new=fake_respond), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=True):
            r._handle_approval_respond(
                object(),
                {
                    "session_id": sid,
                    "choice": "deny",
                    "approval_id": target_id,
                    "run_id": run_id,
                    "mirror_token": mirror[ra._GATEWAY_MIRROR_TOKEN],
                },
            )

        assert captured == {
            "payload": {"ok": True, "choice": "deny", "relayed": True},
            "status": 200,
        }
        assert relayed == [(run_id, target_id, "deny")]

        assert target_entry.event.is_set(), "the denied approval's waiter must be woken"
        assert target_entry.result == "deny"
        with ra._lock:
            remaining = list(ra._gateway_queues.get(sid) or [])
        assert target_entry not in remaining, "the denied approval's producer must be consumed"
        assert head_entry in remaining, (
            "the run's own head approval must not be consumed by answering another approval"
        )
        assert not head_entry.event.is_set()
        assert sibling_entry in remaining, (
            "a sibling approval on the same run must not be consumed"
        )
        assert not sibling_entry.event.is_set()
        assert other_run_entry in remaining, (
            "an unrelated run's producer must not be consumed"
        )
        assert not other_run_entry.event.is_set()

        # These are the observations made by subsequent HTTP polling.
        for _ in range(2):
            with ra._lock:
                ra.reconcile_gateway_pending_mirror_locked(sid)
            assert ra.gateway_pending_mirror(
                sid, approval_id=target_id, run_id=run_id
            ) is None, "reconciliation must not resurrect the denied approval's mirror"
    finally:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues.pop(sid, None)


def test_deny_settles_run_producer_so_reconciliation_cannot_resurrect_mirror():
    """Deny must settle the producer as well as its WebUI mirror.

    Selection is the behaviour under test, so the queue carries more than the
    one entry being resolved: a sibling approval for the same run, and a producer
    belonging to a different run. Resolving one approval must consume exactly
    that producer, wake its waiter, and leave both others queued.
    """
    from types import SimpleNamespace
    from api import route_approvals as ra

    sid = "sess-deny-no-resurrection"
    approval_id = "approval-deny-no-resurrection"
    run_id = "run-deny-no-resurrection"
    sibling_approval_id = "approval-deny-sibling"
    other_run_id = "run-deny-unrelated"
    approval = {
        "approval_id": approval_id,
        "run_id": run_id,
        "command": "echo pricing",
        "description": "Run pricing probe",
    }
    sibling_approval = {
        "approval_id": sibling_approval_id,
        "run_id": run_id,
        "command": "echo sibling",
        "description": "Sibling approval on the same run",
    }
    other_run_approval = {
        "approval_id": "approval-deny-other-run",
        "run_id": other_run_id,
        "command": "echo else",
        "description": "Approval on an unrelated run",
    }

    def producer(payload):
        return SimpleNamespace(data=dict(payload), event=threading.Event(), result=None)

    target = producer(approval)
    same_run_sibling = producer(sibling_approval)
    other_run = producer(other_run_approval)
    try:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues[sid] = [target, same_run_sibling, other_run]
            queued_before = list(ra._gateway_queues[sid])
        ra.submit_gateway_pending_mirror(sid, dict(approval))
        mirror = ra.gateway_pending_mirror(sid, approval_id=approval_id, run_id=run_id)
        assert mirror is not None

        resolved, _head, _total = ra.resolve_gateway_pending_run(
            sid,
            approval_id=approval_id,
            run_id=run_id,
            choice="deny",
        )
        assert resolved == 1

        assert queued_before == [target, same_run_sibling, other_run], (
            "the queue should have held all three producers before retirement"
        )
        with ra._lock:
            remaining = list(ra._gateway_queues.get(sid) or [])
        assert target not in remaining, "the denied approval's producer must be consumed"
        assert target.event.is_set(), "the denied approval's waiter must be woken"
        assert target.result == "deny"
        assert same_run_sibling in remaining, (
            "a sibling approval on the same run must not be consumed by retiring another approval"
        )
        assert other_run in remaining, (
            "an unrelated run's producer must not be consumed"
        )

        # These are the same observations made by subsequent HTTP polling: the
        # denied approval must not come back, and the survivors must stay live.
        # `gateway_pending_mirror` acquires `_lock` itself, so it must be called
        # outside the locked section.
        for _ in range(2):
            with ra._lock:
                ra.reconcile_gateway_pending_mirror_locked(sid)
            assert ra.gateway_pending_mirror(
                sid, approval_id=approval_id, run_id=run_id
            ) is None, "reconciliation must not resurrect the denied approval's mirror"
            with ra._lock:
                refreshed = list(ra._gateway_queues.get(sid) or [])
            assert target not in refreshed
            assert same_run_sibling in refreshed
            assert other_run in refreshed
    finally:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues.pop(sid, None)


@pytest.mark.parametrize(
    "failure_point",
    ["reconcile_gateway_pending_mirror_locked", "_approval_sse_notify_locked"],
)
def test_exact_run_resolution_settles_before_projection_failure(failure_point):
    """Projection failures must not strand an already-consumed producer."""
    from types import SimpleNamespace
    from api import route_approvals as ra

    sid = f"sess-exact-projection-failure-{failure_point}"
    run_id = "run-exact-projection-failure"
    target = SimpleNamespace(
        data={"run_id": run_id, "approval_id": "approval-target"},
        event=threading.Event(),
        result=None,
        reason=None,
    )
    sibling = SimpleNamespace(
        data={"run_id": run_id, "approval_id": "approval-sibling"},
        event=threading.Event(),
        result=None,
        reason=None,
    )
    try:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues[sid] = [target, sibling]

        with patch.object(ra, failure_point, side_effect=RuntimeError("projection failed")):
            with pytest.raises(RuntimeError, match="projection failed"):
                ra.resolve_gateway_pending_run(
                    sid,
                    approval_id="approval-target",
                    run_id=run_id,
                    choice="deny",
                    reason="user denied",
                )

        assert target.result == "deny"
        assert target.reason == "user denied"
        assert target.event.is_set(), "the consumed producer must be woken before projection work"
        assert sibling.result is None
        assert sibling.reason is None
        assert not sibling.event.is_set()
        with ra._lock:
            assert ra._gateway_queues[sid] == [sibling]
    finally:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues.pop(sid, None)


@pytest.mark.parametrize(
    "terminal_reason",
    [
        "Gateway run completed before approval resolution",
        "Gateway run failed before approval resolution",
        "Gateway run was cancelled before approval resolution",
        "Gateway run ended during teardown before approval resolution",
    ],
)
def test_terminal_run_settlement_denies_all_run_producers_and_preserves_other_runs(
    terminal_reason,
):
    """Every terminal exit fails closed without consuming another run's producer."""
    from types import SimpleNamespace
    from api import route_approvals as ra

    sid = f"sess-terminal-run-{terminal_reason.split()[2]}"
    run_id = "run-terminal"

    def producer(entry_run_id, approval_id):
        return SimpleNamespace(
            data={"run_id": entry_run_id, "approval_id": approval_id},
            event=threading.Event(),
            result=None,
            reason=None,
        )

    targets = [producer(run_id, "approval-a"), producer(run_id, "approval-b")]
    survivor = producer("run-other", "approval-other")
    try:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues[sid] = [targets[0], survivor, targets[1]]

        settled, _head, _total = ra.settle_gateway_pending_run(
            sid, run_id, reason=terminal_reason
        )

        assert settled == 2
        for target in targets:
            assert target.result == "deny"
            assert target.reason == terminal_reason
            assert target.event.is_set()
        assert survivor.result is None
        assert survivor.reason is None
        assert not survivor.event.is_set()
        with ra._lock:
            assert ra._gateway_queues[sid] == [survivor]
    finally:
        with ra._lock:
            ra._pending.pop(sid, None)
            ra._gateway_queues.pop(sid, None)
