"""Tests for approval event handling on the gateway legacy /v1/chat/completions path (#4549).

The legacy path is the default when HERMES_WEBUI_GATEWAY_USE_RUNS_API is not set.
PR #4495 fixed the runs API path but left the legacy path without approval handling.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    block_after = loop[approval_idx:approval_idx + 1500]
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

        assert captured.get("url", "") == "http://gw:8642/v1/runs/run-legacy-1/approval", (
            f"approval respond must relay to the gateway run; got {captured.get('url')!r}"
        )
        assert captured["body"] == {"choice": "once", "approval_id": "appr-legacy-runid"}
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


def test_legacy_teardown_preserves_live_gateway_head_mirror():
    """A live gateway head must still be mirrored after legacy teardown runs."""
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
        assert payloads[-1]["pending"]["_gateway_mirror"] is True
        assert payloads[-1]["pending_count"] == 1
        with ra._lock:
            pending = ra._pending.get(session_id)
            assert isinstance(pending, list)
            assert len(pending) == 1
            assert pending[0]["approval_id"] == approval_data["approval_id"]
            assert pending[0]["_gateway_mirror"] is True
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

        assert captured.get("url", "") == f"http://gw:8642/v1/runs/{run_id}/approval", (
            f"approval respond must relay to the mirrored gateway run; got {captured.get('url')!r}"
        )
        assert captured["body"] == {"choice": "once", "approval_id": approval_id}
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


def test_legacy_approval_without_run_id_stays_actionable():
    """Legacy approvals without a run_id must fail explicitly and keep the mirror live."""
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
        with ra._lock:
            r._gateway_queues[session_id] = [SimpleNamespace(data=dict(approval_data))]
        ra.submit_gateway_pending_mirror(session_id, approval_data)
        with ra._lock:
            pending_queue = r._pending.get(session_id)
            assert isinstance(pending_queue, list)
            approval_id = pending_queue[0]["approval_id"]

        # The relay-unavailable 409 is only meaningful when the WebUI is
        # actually running the gateway chat backend. On a gateway deployment
        # the backend env is process-wide (not just during the stream), so
        # assert the 409 with HERMES_WEBUI_CHAT_BACKEND=gateway active at
        # respond time. Without this scope the handler now (correctly) treats
        # a mirrored approval on the default LOCAL backend as locally
        # resolvable and falls through instead of 409ing — see
        # test_issue4771_local_approval_regression.py (#4771 follow-up).
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}), \
             patch("api.routes.get_session", return_value=mock_session), \
             patch("api.routes.j", new=fake_j):
            r._handle_approval_respond(
                object(),
                {"session_id": session_id, "choice": "once", "approval_id": approval_id},
            )

        assert captured["status"] == 409
        assert captured["payload"]["code"] == "gateway_run_unavailable"
        assert captured["payload"]["error"] == r._GATEWAY_APPROVAL_RELAY_UNAVAILABLE
        with ra._lock:
            pending_queue = r._pending.get(session_id)
            assert isinstance(pending_queue, list)
            assert pending_queue[0]["approval_id"] == approval_id
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        with ra._lock:
            r._pending.pop(session_id, None)
            r._gateway_queues.pop(session_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)
