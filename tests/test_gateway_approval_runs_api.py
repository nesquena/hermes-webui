"""Tests for the gateway runs-API approval bridge (#4203)."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# 1. Capability detection
# ---------------------------------------------------------------------------

def test_gateway_capability_detection():
    """get_gateway_caps / gateway_supports_approval correctly parse /v1/capabilities."""
    from api.config import (
        gateway_supports_approval,
        invalidate_gateway_caps,
    )

    # Clear any leftover cache state.
    invalidate_gateway_caps()

    def _fake_urlopen_capable(req, *, timeout=None):
        assert req.full_url == "http://fake:1234/v1/capabilities"
        assert req.get_header("Authorization") == "Bearer secret"
        body = json.dumps({
            "features": {
                "approval_events": True,
                "run_approval_response": True,
            },
        }).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_capable):
        assert gateway_supports_approval("http://fake:1234", "secret") is True

    invalidate_gateway_caps()

    def _fake_urlopen_incapable(req, *, timeout=None):
        assert req.full_url == "http://fake:5678/v1/capabilities"
        body = json.dumps({}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_incapable):
        assert gateway_supports_approval("http://fake:5678") is False

    invalidate_gateway_caps()


def test_gateway_capability_cache_keeps_fresher_success_on_probe_race():
    """A slower failed probe must not overwrite a fresher successful capability result."""
    from api.config import gateway_supports_approval, invalidate_gateway_caps

    invalidate_gateway_caps()
    first_probe_release = threading.Event()
    second_probe_done = threading.Event()
    call_count = {"value": 0}

    class _JsonResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self, _limit=None):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        call_count["value"] += 1
        if call_count["value"] == 1:
            second_probe_done.wait(timeout=5)
            first_probe_release.wait(timeout=5)
            raise urllib.error.URLError("slow probe failed")
        second_probe_done.set()
        return _JsonResponse({
            "features": {
                "approval_events": True,
                "run_approval_response": True,
            },
        })

    results = []

    def worker():
        results.append(gateway_supports_approval("http://fake:9999", "secret"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        second_probe_done.wait(timeout=5)
        t2.start()
        t2.join(timeout=5)
        first_probe_release.set()
        t1.join(timeout=5)

        assert gateway_supports_approval("http://fake:9999", "secret") is True

    assert results.count(True) == 2
    invalidate_gateway_caps()


# ---------------------------------------------------------------------------
# 2. Runs-API submission path
# ---------------------------------------------------------------------------

def test_gateway_runs_api_submission():
    """When gateway_supports_approval returns True, the runs-API path is used."""
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-test-runs"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    runs_called = {"called": False}
    original_text = "hello from runs"

    def fake_runs_streaming(*args, **kwargs):
        runs_called["called"] = True
        return (original_text, {"input_tokens": 10, "output_tokens": 5})

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

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway", "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1"}):
            with patch("api.gateway_chat.gateway_supports_approval", lambda *_args, **_kwargs: True), \
                 patch("api.gateway_chat._run_gateway_runs_api_streaming", fake_runs_streaming), \
                 patch("api.gateway_chat.get_session", return_value=mock_session), \
                 patch("api.gateway_chat._stream_writeback_is_current", return_value=True), \
                 patch("api.gateway_chat.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess1",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    assert runs_called["called"], "The runs-API streaming path should have been invoked"


# ---------------------------------------------------------------------------
# 3. Approval event translation
# ---------------------------------------------------------------------------

def test_gateway_approval_event_translation():
    """_gateway_runs_approval_event maps actual gateway approval fields."""
    from api.gateway_chat import _gateway_runs_approval_event

    payload = {
        "command": "rm -rf /tmp/x",
        "description": "Dangerous command approval",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "run_id": "run-999",
        "approval_id": "appr-1",
        "choices": ["once", "session", "always", "deny"],
    }
    result = _gateway_runs_approval_event(payload)
    assert result is not None
    assert result["tool"] == "dangerous_command"
    assert result["command"] == "rm -rf /tmp/x"
    assert result["description"] == "Dangerous command approval"
    assert result["pattern_key"] == "dangerous_command"
    assert result["pattern_keys"] == ["dangerous_command"]
    assert result["choices"] == ["once", "session", "always", "deny"]
    assert result["allow_permanent"] is True
    assert result["risk_level"] == "high"
    assert result["run_id"] == "run-999"
    assert result["approval_id"] == "appr-1"

    downgraded = _gateway_runs_approval_event({
        "command": "rm -rf /tmp/x",
        "description": "Dangerous command approval",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "allow_permanent": False,
        "choices": ["once", "session", "always", "deny"],
    })
    assert downgraded is not None
    assert downgraded["allow_permanent"] is False

    # Missing command/description/tool should return None.
    assert _gateway_runs_approval_event({"risk_level": "high"}) is None
    assert _gateway_runs_approval_event({}) is None


def test_gateway_runs_api_streaming_parses_real_run_events():
    """The runs-API bridge must parse the real gateway event payloads."""
    from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    events = []
    requests = []
    stream_id = "sid-real-runs"
    STREAM_PARTIAL_TEXT[stream_id] = ""
    STREAM_REASONING_TEXT[stream_id] = ""

    class _JsonResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self, _limit=None):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _SseResponse:
        def __init__(self, lines):
            self._lines = lines

        def __iter__(self):
            return iter(self._lines)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        requests.append(req)
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-abc"})
        return _SseResponse([
            b'data: {"event":"approval.request","command":"rm -rf /tmp/x","description":"Dangerous command approval","pattern_key":"dangerous_command","pattern_keys":["dangerous_command"],"choices":["once","session","always","deny"],"run_id":"run-abc","approval_id":"appr-1"}\n',
            b'\n',
            b'data: {"event":"reasoning.available","text":"thinking..."}\n',
            b'\n',
            b'data: {"event":"message.delta","delta":"Hello"}\n',
            b'\n',
            b'data: {"event":"run.completed","output":"Hello","usage":{"input_tokens":3,"output_tokens":1}}\n',
            b'\n',
        ])

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            final_text, usage = _run_gateway_runs_api_streaming(
                session_id="sess1",
                msg_text="hi",
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[
                    {"role": "system", "content": "system prompt"},
                    {"role": "assistant", "content": "earlier reply"},
                ],
                body_extras={"provider": "anthropic"},
                put_gateway_event=lambda event, data: events.append((event, data)),
                cancel_event=threading.Event(),
            )
    finally:
        STREAM_PARTIAL_TEXT.pop(stream_id, None)
        STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)

    run_req = requests[0]
    run_body = json.loads(run_req.data.decode("utf-8"))
    assert run_req.full_url == "http://gw:8642/v1/runs"
    assert run_req.get_header("Authorization") == "Bearer secret"
    assert run_body["input"] == "hi"
    assert run_body["instructions"] == "system prompt"
    assert run_body["conversation_history"] == [{"role": "assistant", "content": "earlier reply"}]
    assert run_body["provider"] == "anthropic"
    assert "messages" not in run_body

    assert final_text == "Hello"
    assert usage["input_tokens"] == 3
    assert usage["output_tokens"] == 1
    assert events[0][0] == "approval"
    assert events[0][1]["description"] == "Dangerous command approval"
    assert events[0][1]["approval_id"] == "appr-1"
    assert events[1] == ("reasoning", {"text": "thinking..."})
    assert events[2] == ("token", {"text": "Hello"})


def test_gateway_runs_api_streaming_preserves_multimodal_input():
    """Attachment-backed runs requests must keep multimodal content lists."""
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    requests = []
    multimodal_content = [
        {"type": "input_text", "text": "describe this"},
        {"type": "input_image", "image_url": "file:///tmp/demo.png"},
    ]

    class _JsonResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self, _limit=None):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _SseResponse:
        def __iter__(self):
            return iter([
                b'data: {"event":"run.completed","output":"done","usage":{"input_tokens":1,"output_tokens":1}}\n',
                b'\n',
            ])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        requests.append(req)
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-mm"})
        return _SseResponse()

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("api.streaming._build_native_multimodal_message", return_value=multimodal_content):
            _run_gateway_runs_api_streaming(
                session_id="sess-mm",
                msg_text="describe this",
                model="test-model",
                workspace="/tmp",
                stream_id="sid-mm",
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[],
                body_extras={},
                put_gateway_event=lambda *_args, **_kwargs: None,
                cancel_event=threading.Event(),
                attachments=[{"name": "demo.png"}],
                cfg={},
            )
    finally:
        _STREAM_RUN_IDS.pop("sid-mm", None)

    run_body = json.loads(requests[0].data.decode("utf-8"))
    assert run_body["input"] == [{"role": "user", "content": multimodal_content}]
    assert run_body["input"][0]["role"] == "user"
    assert run_body["input"][0]["content"] == multimodal_content


# ---------------------------------------------------------------------------
# 4. Cancelled runs path should not emit gateway_empty_response
# ---------------------------------------------------------------------------

def test_gateway_runs_api_cancel_does_not_emit_empty_response():
    """Cancelled runs-API turns should stop cleanly without empty-response errors."""
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-cancel"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

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

    def fake_runs_streaming(*args, **kwargs):
        kwargs["put_gateway_event"]("cancel", {"message": "Cancelled by gateway"})
        return None, {}

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway", "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1"}):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=True), \
                 patch("api.gateway_chat._run_gateway_runs_api_streaming", side_effect=fake_runs_streaming), \
                 patch("api.gateway_chat.get_session", return_value=mock_session):
                _run_gateway_chat_streaming(
                    session_id="sess-cancel",
                    msg_text="stop",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    assert any(e[0] == "cancel" for e in events if isinstance(e, tuple)), events
    assert not any(
        e[0] == "apperror" and isinstance(e[1], dict) and e[1].get("type") == "gateway_empty_response"
        for e in events if isinstance(e, tuple)
    ), events


# ---------------------------------------------------------------------------
# 5. Approval response relay
# ---------------------------------------------------------------------------

def test_gateway_approval_response_relay():
    """_handle_approval_respond relays the real gateway approval body."""
    from api.gateway_chat import _STREAM_RUN_IDS

    # Seed the mapping.
    _STREAM_RUN_IDS["sid-relay"] = "run abc/1"

    mock_session = MagicMock()
    mock_session.active_stream_id = "sid-relay"

    captured = {}

    def fake_request_json(self, req):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return {"ok": True}

    handler = MagicMock()
    handler.wfile = io.BytesIO()

    body = {"session_id": "sess-relay", "choice": "once", "approval_id": "appr x/y"}

    with patch("api.routes.get_session", return_value=mock_session), \
         patch("api.runner_client.HttpRunnerClient._request_json", new=fake_request_json), \
         patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
         patch("api.gateway_chat._gateway_api_key", return_value=""):
        from api.routes import _handle_approval_respond
        _handle_approval_respond(handler, body)

    assert captured.get("url", "") == "http://gw:8642/v1/runs/run%20abc%2F1/approval"
    assert captured["body"] == {"choice": "once", "approval_id": "appr x/y"}
    handler.send_response.assert_called_with(200)

    # Cleanup.
    _STREAM_RUN_IDS.pop("sid-relay", None)


def test_gateway_approval_response_relay_failure_returns_502():
    """Gateway relay failures must surface as HTTP errors to the frontend."""
    from api.gateway_chat import _STREAM_RUN_IDS
    from api.runner_client import RunnerClientError

    _STREAM_RUN_IDS["sid-relay-fail"] = "run-abc"

    mock_session = MagicMock()
    mock_session.active_stream_id = "sid-relay-fail"

    handler = MagicMock()
    handler.wfile = io.BytesIO()

    body = {"session_id": "sess-relay", "choice": "once", "approval_id": "appr-x"}

    with patch("api.routes.get_session", return_value=mock_session), \
         patch("api.runner_client.HttpRunnerClient.respond_approval", side_effect=RunnerClientError("relay failed")), \
         patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
         patch("api.gateway_chat._gateway_api_key", return_value=""):
        from api.routes import _handle_approval_respond
        _handle_approval_respond(handler, body)

    handler.send_response.assert_called_with(502)
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert payload["ok"] is False
    assert payload["relayed"] is True
    assert "relay failed" in payload["error"]

    _STREAM_RUN_IDS.pop("sid-relay-fail", None)


def test_gateway_approval_response_invalid_gateway_base_returns_502():
    """Misconfigured gateway bases must not fall through to the local approval path."""
    from api.gateway_chat import _STREAM_RUN_IDS

    _STREAM_RUN_IDS["sid-relay-invalid-base"] = "run-abc"

    mock_session = MagicMock()
    mock_session.active_stream_id = "sid-relay-invalid-base"

    handler = MagicMock()
    handler.wfile = io.BytesIO()

    body = {"session_id": "sess-relay", "choice": "once", "approval_id": "appr-x"}

    with patch("api.routes.get_session", return_value=mock_session), \
         patch("api.gateway_chat._gateway_base_url", return_value="file:///tmp/not-http"), \
         patch("api.gateway_chat._gateway_api_key", return_value=""):
        from api.routes import _handle_approval_respond
        _handle_approval_respond(handler, body)

    handler.send_response.assert_called_with(502)
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert payload["ok"] is False
    assert payload["relayed"] is True
    assert "runner base_url must be http(s)" in payload["error"]

    _STREAM_RUN_IDS.pop("sid-relay-invalid-base", None)


# ---------------------------------------------------------------------------
# 6. Empty chat/completions response emits gateway_empty_response (not a
#    misleading approval-unsupported banner)
# ---------------------------------------------------------------------------

def test_gateway_empty_response_no_approval_banner():
    """Empty response from chat/completions path emits gateway_empty_response, not gateway_approval_unsupported."""
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-fb"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    # Simulate an SSE stream that returns only [DONE] with no content.
    sse_body = b"data: [DONE]\n\n"

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
                 patch("api.gateway_chat.get_session", return_value=MagicMock(
                     active_stream_id=stream_id, workspace="/tmp",
                     profile=None, context_messages=[], messages=[],
                 )):
                _run_gateway_chat_streaming(
                    session_id="sess-fb",
                    msg_text="do something risky",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    apperrors = [e for e in events if isinstance(e, tuple) and e[0] == "apperror"]
    # The misleading gateway_approval_unsupported banner should no longer fire;
    # the generic gateway_empty_response handler covers this case correctly.
    assert not any(
        isinstance(ev[1], dict) and ev[1].get("type") == "gateway_approval_unsupported"
        for ev in apperrors
    ), f"gateway_approval_unsupported should not fire for generic empty responses: {apperrors}"
    assert any(
        isinstance(ev[1], dict) and ev[1].get("type") == "gateway_empty_response"
        for ev in apperrors
    ), f"Expected gateway_empty_response apperror, got events: {apperrors}"


# ---------------------------------------------------------------------------
# 7. Chat/completions path unchanged for normal responses
# ---------------------------------------------------------------------------

def test_gateway_chat_completions_path_unchanged():
    """Non-stalling chat/completions turn completes without apperror events."""
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-ok"

    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    # Simulate a normal SSE response with content.
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

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
                    session_id="sess-ok",
                    msg_text="hello",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    apperrors = [e for e in events if isinstance(e, tuple) and e[0] == "apperror"]
    assert not apperrors, f"No apperror expected for a normal response, got: {apperrors}"
    tokens = [e for e in events if isinstance(e, tuple) and e[0] == "token"]
    assert tokens, "Expected at least one token event"
