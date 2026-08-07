"""Tests for the gateway runs-API approval bridge (#4203)."""
from __future__ import annotations

import io
import json
import socket
import threading
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


_MISSING = object()
_TEST_ROUTE_SESSION_LOCK = threading.Lock()
_TEST_ROUTE_SESSION_PRIOR: dict[str, object] = {}
_TEST_ROUTE_SESSION_REFCOUNTS: dict[str, int] = {}


def _reset_gateway_run_start_state(stream_id: str) -> None:
    from api import gateway_chat

    gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)
    lifecycle = getattr(gateway_chat, "_STREAM_RUN_LIFECYCLE", None)
    if isinstance(lifecycle, dict):
        lifecycle.pop(stream_id, None)


def _invoke_gateway_approval_response(results: dict, key: str, session, body: dict) -> None:
    handler = MagicMock()
    handler.wfile = io.BytesIO()
    from api.models import Session, SESSIONS
    from api.routes import _handle_approval_respond

    sid = str(body.get("session_id") or "")
    route_session = Session(
        session_id=sid,
        workspace=".",
        model=getattr(session, "model", "test-model"),
        model_provider=getattr(session, "model_provider", "test-provider"),
        messages=list(getattr(session, "messages", []) or []),
        context_messages=list(getattr(session, "context_messages", []) or []),
        active_stream_id=getattr(session, "active_stream_id", None),
        profile=getattr(session, "profile", None),
    )
    with _TEST_ROUTE_SESSION_LOCK:
        refs = _TEST_ROUTE_SESSION_REFCOUNTS.get(sid, 0)
        if refs == 0:
            _TEST_ROUTE_SESSION_PRIOR[sid] = SESSIONS.get(sid, _MISSING)
            SESSIONS[sid] = route_session
        _TEST_ROUTE_SESSION_REFCOUNTS[sid] = refs + 1
    try:
        _handle_approval_respond(handler, body)
    finally:
        with _TEST_ROUTE_SESSION_LOCK:
            refs = _TEST_ROUTE_SESSION_REFCOUNTS.get(sid, 0) - 1
            if refs > 0:
                _TEST_ROUTE_SESSION_REFCOUNTS[sid] = refs
            else:
                _TEST_ROUTE_SESSION_REFCOUNTS.pop(sid, None)
                prior = _TEST_ROUTE_SESSION_PRIOR.pop(sid, _MISSING)
                if prior is _MISSING:
                    SESSIONS.pop(sid, None)
                else:
                    SESSIONS[sid] = prior
    results[key] = (
        handler.send_response.call_args.args[0],
        json.loads(handler.wfile.getvalue().decode("utf-8")),
    )


# ---------------------------------------------------------------------------
# 1. Capability detection
# ---------------------------------------------------------------------------

def test_gateway_capability_detection():
    """get_gateway_caps / gateway_supports_approval correctly parse /v1/capabilities."""
    from api.config import (
        gateway_approval_unavailable_reason,
        gateway_supports_approval,
        get_gateway_caps,
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
        caps = get_gateway_caps("http://fake:1234", "secret")
        assert caps["capabilities_reachable"] is True
        assert caps["probe_error"] is None
        assert gateway_approval_unavailable_reason("http://fake:1234", "secret") is None
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
        caps = get_gateway_caps("http://fake:5678")
        assert caps["capabilities_reachable"] is True
        assert caps["probe_error"] is None
        assert gateway_approval_unavailable_reason("http://fake:5678") == "unsupported"
        assert gateway_supports_approval("http://fake:5678") is False

    invalidate_gateway_caps()


def test_gateway_capability_detection_marks_probe_failures_unreachable():
    """Probe failures stay non-fatal but remain distinguishable from unsupported gateways."""
    from api.config import (
        gateway_approval_unavailable_reason,
        gateway_supports_approval,
        get_gateway_caps,
        invalidate_gateway_caps,
    )

    invalidate_gateway_caps()

    def _fake_urlopen_fail(req, *, timeout=None):
        assert req.full_url == "http://fake:9999/v1/capabilities"
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_fail):
        caps = get_gateway_caps("http://fake:9999", "secret")
        assert caps["capabilities_reachable"] is False
        assert caps["probe_error"]
        assert gateway_approval_unavailable_reason("http://fake:9999", "secret") == "unreachable"
        assert gateway_supports_approval("http://fake:9999", "secret") is False

    invalidate_gateway_caps()


def test_gateway_capability_detection_treats_timeout_probe_as_reachable_unsupported():
    """Slow probes should preserve the reachable-but-unsupported warning contract."""
    from api.config import (
        gateway_approval_unavailable_reason,
        gateway_supports_approval,
        get_gateway_caps,
        invalidate_gateway_caps,
    )

    invalidate_gateway_caps()

    def _fake_urlopen_timeout(req, *, timeout=None):
        assert req.full_url == "http://fake:8888/v1/capabilities"
        raise socket.timeout("timed out")

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_timeout):
        caps = get_gateway_caps("http://fake:8888", "secret")
        assert caps["capabilities_reachable"] is True
        assert caps["probe_error"]
        assert gateway_approval_unavailable_reason("http://fake:8888", "secret") == "unsupported"
        assert gateway_supports_approval("http://fake:8888", "secret") is False

    invalidate_gateway_caps()


def test_gateway_capability_detection_treats_404_probe_as_reachable_unsupported():
    """Older reachable gateways can 404 /v1/capabilities without becoming "offline"."""
    from api.config import (
        gateway_approval_unavailable_reason,
        gateway_supports_approval,
        get_gateway_caps,
        invalidate_gateway_caps,
    )

    invalidate_gateway_caps()

    def _fake_urlopen_404(req, *, timeout=None):
        assert req.full_url == "http://fake:7777/v1/capabilities"
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=io.BytesIO(b""))

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_404):
        caps = get_gateway_caps("http://fake:7777", "secret")
        assert caps["capabilities_reachable"] is True
        assert caps["probe_error"]
        assert gateway_approval_unavailable_reason("http://fake:7777", "secret") == "unsupported"
        assert gateway_supports_approval("http://fake:7777", "secret") is False

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
    captured = {}
    original_text = "hello from runs"

    def fake_runs_streaming(
        session_id,
        msg_text,
        model,
        workspace,
        stream_id,
        base_url,
        api_key,
        prefill_messages,
        body_extras,
        **kwargs,
    ):
        runs_called["called"] = True
        captured["body_extras"] = body_extras
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
                 patch("api.gateway_chat._gateway_reasoning_effort_for_request", return_value="high"), \
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
    assert captured["body_extras"]["reasoning_effort"] == "high"


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
    empty_id = _gateway_runs_approval_event({**payload, "approval_id": "", "id": ""})
    import api.route_approvals as approvals
    sid = "translation-empty-id"
    try:
        approvals.submit_gateway_pending_mirror(sid, empty_id)
        mirror = approvals.gateway_pending_mirror(sid, run_id="run-999")
        assert mirror["approval_id"]
        assert mirror.get("_gateway_agent_identity_v1") is not True
    finally:
        approvals._pending.pop(sid, None)

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
    assert run_body["session_id"] == "sess1"
    assert "messages" not in run_body

    assert final_text == "Hello"
    assert usage["input_tokens"] == 3
    assert usage["output_tokens"] == 1
    assert events[0][0] == "approval"
    assert events[0][1]["description"] == "Dangerous command approval"
    assert events[0][1]["approval_id"] == "appr-1"
    assert events[1] == ("reasoning", {"text": "thinking..."})
    assert events[2] == ("token", {"text": "Hello"})
    import api.route_approvals as approvals
    assert approvals.gateway_pending_mirror("sess1", run_id="run-abc") is None


def test_live_empty_ingress_id_stays_fifo_under_capability_v1():
    """A normalized fallback browser ID must not become authoritative Agent identity."""
    from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
    from api.gateway_chat import _STREAM_RUN_IDS, _gateway_runs_approval_event, _run_gateway_runs_api_streaming
    from api import routes
    import api.route_approvals as approvals

    sid = "sid-live-empty-ingress"
    stream_id = "stream-live-empty-ingress"
    events = []
    normalized = _gateway_runs_approval_event({
        "command": "rm -rf /tmp/x", "description": "Dangerous",
        "run_id": "run-empty-ingress", "approval_id": "", "id": "",
    })
    assert normalized["approval_id"]
    assert normalized["_gateway_raw_approval_id_present"] is False
    STREAM_PARTIAL_TEXT[stream_id] = ""
    STREAM_REASONING_TEXT[stream_id] = ""

    class _JsonResponse:
        def read(self, _limit=None):
            return b'{"run_id":"run-empty-ingress"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _SseResponse:
        def __iter__(self):
            return iter([
                b'data: {"event":"approval.request","command":"rm -rf /tmp/x","description":"Dangerous","run_id":"run-empty-ingress","approval_id":"","id":""}\n',
                b'\n',
                b'data: [DONE]\n',
                b'\n',
            ])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse()
        return _SseResponse()

    handler = MagicMock()
    handler.wfile = io.BytesIO()
    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=True):
            _run_gateway_runs_api_streaming(
                session_id=sid, msg_text="hi", model="test-model", workspace="/tmp",
                stream_id=stream_id, base_url="http://gw:8642", api_key="secret",
                prefill_messages=[], body_extras={}, put_gateway_event=lambda event, data: events.append((event, data)),
                cancel_event=threading.Event(),
            )
            mirror = approvals.gateway_pending_mirror(sid, run_id="run-empty-ingress")
            browser_id = mirror["approval_id"]
            with patch("api.routes.get_session", return_value=SimpleNamespace(active_stream_id=stream_id)), \
                 patch("api.config.gateway_supports_approval_identity_v1", return_value=True), \
                 patch("api.runner_client.HttpRunnerClient.respond_approval") as respond:
                routes._handle_approval_respond(handler, {
                    "session_id": sid, "choice": "once", "approval_id": browser_id,
                })
                respond.assert_called_once_with("run-empty-ingress", "", "once")
    finally:
        STREAM_PARTIAL_TEXT.pop(stream_id, None)
        STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)

    assert events[0][0] == "approval"
    assert events[0][1]["approval_id"]
    assert events[0][1]["_gateway_raw_approval_id_present"] is False
    assert events[0][1]["_gateway_agent_identity_v1"] is False
    assert mirror["approval_id"] == events[0][1]["approval_id"]
    assert mirror["_gateway_raw_approval_id_present"] is False
    assert mirror["_gateway_agent_identity_v1"] is False
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)


def test_live_authoritative_ingress_id_relays_exactly_under_capability_v1():
    """A raw Agent-issued approval ID becomes authoritative only through live ingress."""
    from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming
    from api import routes
    import api.route_approvals as approvals

    sid = "sid-live-authoritative-ingress"
    stream_id = "stream-live-authoritative-ingress"
    events = []
    STREAM_PARTIAL_TEXT[stream_id] = ""
    STREAM_REASONING_TEXT[stream_id] = ""

    class _JsonResponse:
        def read(self, _limit=None):
            return b'{"run_id":"run-authoritative-ingress"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _SseResponse:
        def __iter__(self):
            return iter([
                b'data: {"event":"approval.request","command":"echo hi","description":"Safe","run_id":"run-authoritative-ingress","approval_id":"agent-approval-1","id":"agent-approval-legacy"}\n',
                b'\n',
                b'data: [DONE]\n',
                b'\n',
            ])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse()
        return _SseResponse()

    handler = MagicMock()
    handler.wfile = io.BytesIO()
    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=True):
            _run_gateway_runs_api_streaming(
                session_id=sid, msg_text="hi", model="test-model", workspace="/tmp",
                stream_id=stream_id, base_url="http://gw:8642", api_key="secret",
                prefill_messages=[], body_extras={}, put_gateway_event=lambda event, data: events.append((event, data)),
                cancel_event=threading.Event(),
            )
            mirror = approvals.gateway_pending_mirror(
                sid, approval_id="agent-approval-1", run_id="run-authoritative-ingress"
            )
            with patch("api.routes.get_session", return_value=SimpleNamespace(active_stream_id=stream_id)), \
                 patch("api.config.gateway_supports_approval_identity_v1", return_value=True), \
                 patch("api.runner_client.HttpRunnerClient.respond_approval") as respond:
                routes._handle_approval_respond(handler, {
                    "session_id": sid, "choice": "once", "approval_id": "agent-approval-1",
                })
                respond.assert_called_once_with("run-authoritative-ingress", "agent-approval-1", "once")
    finally:
        STREAM_PARTIAL_TEXT.pop(stream_id, None)
        STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)

    assert events[0][0] == "approval"
    assert events[0][1]["approval_id"] == "agent-approval-1"
    assert events[0][1]["_gateway_agent_identity_v1"] is True
    assert mirror["approval_id"] == "agent-approval-1"
    assert mirror["_gateway_agent_identity_v1"] is True
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)


def test_gateway_runs_api_streaming_same_run_fifo_emits_head_and_promotes_successor():
    """Runs API approval events publish the reconciled FIFO head and count."""
    from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming
    from api import routes
    import api.route_approvals as approvals

    sid = "sess-same-run-producer"
    stream_id = "stream-same-run-producer"
    events = []
    STREAM_PARTIAL_TEXT[stream_id] = ""
    STREAM_REASONING_TEXT[stream_id] = ""

    class _JsonResponse:
        def read(self, _limit=None):
            return b'{"run_id":"run-fifo"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _SseResponse:
        def __iter__(self):
            return iter([
                b'data: {"event":"approval.request","command":"first","description":"First","run_id":"run-fifo","approval_id":"approval-first"}\n',
                b'\n',
                b'data: {"event":"approval.request","command":"second","description":"Second","run_id":"run-fifo","approval_id":"approval-second"}\n',
                b'\n',
                b'data: [DONE]\n',
                b'\n',
            ])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, *, timeout=None):
        return _JsonResponse() if req.full_url.endswith("/v1/runs") else _SseResponse()

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _run_gateway_runs_api_streaming(
                sid, "hi", "test", "/tmp", stream_id, "http://gw:8642", "", [], {},
                put_gateway_event=lambda event, data: events.append((event, data)),
                cancel_event=threading.Event(),
            )

        approval_events = [data for event, data in events if event == "approval"]
        assert [(data["approval_id"], data["pending_count"]) for data in approval_events] == [
            ("approval-first", 1),
            ("approval-first", 2),
        ]

        handler = MagicMock()
        handler.wfile = io.BytesIO()
        with patch("api.routes.get_session", return_value=SimpleNamespace(active_stream_id=stream_id)), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval:
            routes._handle_approval_respond(handler, {
                "session_id": sid,
                "choice": "deny",
                "approval_id": "approval-first",
            })

        handler.send_response.assert_called_with(200)
        respond_approval.assert_called_once_with("run-fifo", "", "deny")
        promoted = approvals.gateway_pending_mirror(sid, run_id="run-fifo")
        assert promoted is not None
        assert promoted["approval_id"] == "approval-second"
        with approvals._lock:
            assert len(approvals._pending[sid]) == 1
    finally:
        STREAM_PARTIAL_TEXT.pop(stream_id, None)
        STREAM_REASONING_TEXT.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_empty_id_runs_approval_reaches_real_response_lifecycle():
    """Runs API empty IDs are carried and resolved through the live mirror."""
    from api.gateway_chat import _run_gateway_runs_api_streaming
    import api.route_approvals as approvals
    import api.routes as routes

    for choice in ("once", "deny"):
        choice_bytes = choice.encode()
        sid = f"sess-real-{choice}"
        stream_id = f"stream-real-{choice}"
        events = []

        class _JsonResponse:
            def read(self, _limit=None, _choice_bytes=choice_bytes):
                return b'{"run_id":"run-real-' + _choice_bytes + b'"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None

        class _SseResponse:
            def __iter__(self):
                return iter([
                    b'data: {"event":"approval.request","command":"echo x",'
                    b'"description":"approval","approval_id":""}\n',
                    b'\n', b'data: [DONE]\n', b'\n'
                ])
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None

        def fake_urlopen(req, *, timeout=None):
            return _JsonResponse() if req.full_url.endswith("/v1/runs") else _SseResponse()

        handler = MagicMock()
        handler.wfile = io.BytesIO()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("api.routes.get_session", return_value=SimpleNamespace(active_stream_id=None)), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.runner_client.HttpRunnerClient._request_json", return_value={"ok": True}):
            _run_gateway_runs_api_streaming(
                sid, "hi", "test", "/tmp", stream_id, "http://gw:8642", "", [], {},
                put_gateway_event=lambda event, data, _events=events: _events.append((event, data)),
                cancel_event=threading.Event(),
            )
            approval_id = events[0][1]["approval_id"]
            assert approval_id
            mirror = approvals.gateway_pending_mirror(sid, approval_id=approval_id)
            assert mirror["run_id"] == f"run-real-{choice}"
            assert mirror["approval_id"] == approval_id
            routes._handle_approval_respond(handler, {
                "session_id": sid, "choice": choice, "approval_id": approval_id,
            })
        assert handler.send_response.call_args.args[0] == 200
        assert json.loads(handler.wfile.getvalue().decode())["ok"] is True
        assert approvals.gateway_pending_mirror(sid, approval_id=approval_id) is None
        approvals._pending.pop(sid, None)


def test_gateway_mirror_match_is_session_scoped_and_retires_one_run():
    import api.route_approvals as approvals

    sid = "sess-multi-run"
    other_sid = "sess-other-run"
    approvals._pending.pop(sid, None)
    approvals._pending.pop(other_sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-a", "command": "a"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-b", "command": "b"})
        first = approvals.gateway_pending_mirror(sid, run_id="run-a")
        second = approvals.gateway_pending_mirror(sid, run_id="run-b")
        assert first["approval_id"].startswith("gwrun:run-a:")
        assert second["approval_id"].startswith("gwrun:run-b:")
        assert approvals.gateway_pending_mirror(other_sid, run_id="run-a") is None
        assert approvals.retire_gateway_pending_mirror(sid, run_id="run-a") is True
        assert approvals.gateway_pending_mirror(sid, run_id="run-a") is None
        assert approvals.gateway_pending_mirror(sid, run_id="run-b") is not None
    finally:
        approvals._pending.pop(sid, None)
        approvals._pending.pop(other_sid, None)


def test_gateway_mirror_exact_pair_lookup_skips_same_id_other_run():
    import api.route_approvals as approvals

    sid = "sess-same-id-other-run"
    approvals._pending.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {
            "run_id": "run-old", "approval_id": "same-id", "command": "old"
        })
        approvals.submit_gateway_pending_mirror(sid, {
            "run_id": "run-live", "approval_id": "same-id", "command": "live"
        })
        assert approvals.gateway_pending_mirror(sid, approval_id="same-id") is None
        live = approvals.gateway_pending_mirror(sid, approval_id="same-id", run_id="run-live")
        assert live is not None
        assert live["run_id"] == "run-live"
        assert approvals.retire_gateway_pending_mirror(sid, approval_id="same-id", run_id="run-live")
        assert approvals.gateway_pending_mirror(sid, approval_id="same-id", run_id="run-live") is None
        assert approvals.gateway_pending_mirror(sid, approval_id="same-id", run_id="run-old") is not None
    finally:
        approvals._pending.pop(sid, None)


def test_gateway_approval_response_relay_cleans_only_selected_shared_id_run():
    from api.gateway_chat import _STREAM_RUN_IDS
    from api import routes
    import api.route_approvals as approvals

    sid = "sess-shared-id-relay"
    stream_id = "sid-shared-id-relay"
    _STREAM_RUN_IDS[stream_id] = "run-live"
    approvals._pending.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {
            "run_id": "run-old", "approval_id": "same-id", "command": "old"
        })
        approvals.submit_gateway_pending_mirror(sid, {
            "run_id": "run-live", "approval_id": "same-id", "command": "live"
        })

        mock_session = MagicMock()
        mock_session.active_stream_id = stream_id
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval, \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""):
            routes._handle_approval_respond(handler, {
                "session_id": sid, "choice": "once", "approval_id": "same-id",
            })

        handler.send_response.assert_called_with(200)
        respond_approval.assert_called_once_with("run-live", "", "once")
        assert approvals.gateway_pending_mirror(sid, approval_id="same-id", run_id="run-live") is None
        assert approvals.gateway_pending_mirror(sid, approval_id="same-id", run_id="run-old") is not None
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)


def test_gateway_approval_response_without_active_run_rejects_shared_upstream_id():
    from api import routes
    import api.route_approvals as approvals

    sid = "sess-shared-id-no-active-run"
    approvals._pending.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {
            "run_id": "run-a", "approval_id": "shared-upstream-id", "command": "a"
        })
        approvals.submit_gateway_pending_mirror(sid, {
            "run_id": "run-b", "approval_id": "shared-upstream-id", "command": "b"
        })

        mock_session = MagicMock()
        mock_session.active_stream_id = None
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=True), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval:
            routes._handle_approval_respond(handler, {
                "session_id": sid, "choice": "once", "approval_id": "shared-upstream-id",
            })

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.send_response.call_args.args[0] == 409
        assert payload["code"] == "gateway_run_unavailable"
        respond_approval.assert_not_called()
        assert approvals.gateway_pending_mirror(sid, approval_id="shared-upstream-id", run_id="run-a") is not None
        assert approvals.gateway_pending_mirror(sid, approval_id="shared-upstream-id", run_id="run-b") is not None
    finally:
        approvals._pending.pop(sid, None)


def test_gateway_approval_relay_does_not_unblock_unrelated_local_head():
    from api.gateway_chat import _STREAM_RUN_IDS
    from api import routes
    import api.route_approvals as approvals
    ta = pytest.importorskip(
        "tools.approval",
        reason="tools.approval not available in this environment",
    )

    sid = "sess-remote-relay-local-head"
    stream_id = "sid-remote-relay-local-head"
    _STREAM_RUN_IDS[stream_id] = "run-remote"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {
            "run_id": "run-remote", "approval_id": "remote-a", "command": "remote"
        })
        local_entry = ta._ApprovalEntry({
            "command": "local",
            "description": "local approval",
            "approval_id": "local-b",
        })
        with approvals._lock:
            approvals._gateway_queues[sid] = [local_entry]

        mock_session = MagicMock()
        mock_session.active_stream_id = stream_id
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval, \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""):
            routes._handle_approval_respond(handler, {
                "session_id": sid, "choice": "once", "approval_id": "remote-a",
            })

        handler.send_response.assert_called_with(200)
        respond_approval.assert_called_once_with("run-remote", "", "once")
        assert local_entry.event.is_set() is False
        assert local_entry.result is None
        assert approvals.gateway_pending_mirror(sid, approval_id="remote-a", run_id="run-remote") is None
        with approvals._lock:
            assert sid in approvals._gateway_queues
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_empty_id_approvals_same_run_keep_distinct_fifo_cards():
    import api.route_approvals as approvals

    sid = "sess-same-run"
    approvals._pending.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-a", "command": "first"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-a", "command": "second"})
        queue = approvals._pending[sid]
        assert [entry["command"] for entry in queue] == ["first", "second"]
        assert queue[0]["approval_id"] != queue[1]["approval_id"]
        assert approvals.retire_gateway_pending_mirror(sid, approval_id=queue[0]["approval_id"], run_id="run-a")
        assert approvals._pending[sid][0]["command"] == "second"
    finally:
        approvals._pending.pop(sid, None)


def test_live_gateway_head_gets_token_scoped_approval_id():
    import api.route_approvals as approvals

    sid = "sess-live-head"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [SimpleNamespace(data={"run_id": "run-live", "command": "head"})]
        with approvals._lock:
            head, total, changed = approvals.reconcile_gateway_pending_mirror_locked(sid)
        assert changed is True
        assert total == 1
        assert head["approval_id"].startswith("gwrun:run-live:")
        assert approvals._gateway_queues[sid][0].data["approval_id"] == head["approval_id"]
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_live_gateway_head_emits_the_stored_token_scoped_identity():
    import api.route_approvals as approvals

    sid = "sess-live-head-emitted-id"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [SimpleNamespace(data={"run_id": "run-live", "command": "head"})]
        approval_data = {"run_id": "run-live", "command": "head"}
        approvals.submit_gateway_pending_mirror(sid, approval_data)
        mirror = approvals.gateway_pending_mirror(sid, run_id="run-live")
        assert mirror is not None
        assert approval_data["approval_id"] == mirror["approval_id"]
        assert approvals._gateway_queues[sid][0].data["approval_id"] == mirror["approval_id"]
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_distinct_same_run_approval_does_not_overwrite_live_head_identity():
    import api.route_approvals as approvals

    sid = "sess-same-run-distinct-approval"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [
            SimpleNamespace(data={"run_id": "run-live", "approval_id": "appr-a", "command": "head"})
        ]
        approvals.submit_gateway_pending_mirror(
            sid,
            {"run_id": "run-live", "approval_id": "appr-b", "command": "second"},
        )
        assert approvals._gateway_queues[sid][0].data["approval_id"] == "appr-a"
        assert approvals.gateway_pending_mirror(sid, approval_id="appr-a", run_id="run-live") is not None
        assert approvals.gateway_pending_mirror(sid, approval_id="appr-b", run_id="run-live") is not None
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_direct_remote_approval_still_mirrors_with_local_gateway_head():
    import api.route_approvals as approvals

    sid = "sess-local-head-remote-mirror"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [SimpleNamespace(data={"command": "local-head"})]
        approvals.submit_gateway_pending_mirror(
            sid,
            {"run_id": "run-remote", "approval_id": "remote-a", "command": "remote"},
        )
        mirror = approvals.gateway_pending_mirror(sid, approval_id="remote-a", run_id="run-remote")
        assert mirror is not None
        assert mirror["run_id"] == "run-remote"
        assert mirror["approval_id"] == "remote-a"
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_legacy_gateway_approval_without_run_gets_browser_visible_id():
    import api.route_approvals as approvals

    sid = "sess-legacy-no-run-id"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [SimpleNamespace(data={"command": "legacy"})]
        approval = {"command": "legacy"}
        approvals.submit_gateway_pending_mirror(sid, approval)
        assert approval.get("approval_id")
        queue = approvals._pending.get(sid) or []
        assert queue[0]["approval_id"] == approval["approval_id"]
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_legacy_gateway_approval_without_run_keeps_its_own_id_beside_local_head():
    import api.route_approvals as approvals

    sid = "sess-legacy-no-run-local-head"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._pending[sid] = [{"approval_id": "local-id", "command": "local"}]
        approvals._gateway_queues[sid] = [SimpleNamespace(data={"command": "legacy"})]
        approval = {"command": "legacy"}
        approvals.submit_gateway_pending_mirror(sid, approval)
        assert approval.get("approval_id")
        assert approval["approval_id"] != "local-id"
        queue = approvals._pending.get(sid) or []
        mirrors = [
            entry for entry in queue
            if entry.get(approvals._GATEWAY_MIRROR_FLAG)
            and not str(entry.get("run_id") or "").strip()
        ]
        assert mirrors
        assert mirrors[-1]["approval_id"] == approval["approval_id"]
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_legacy_gateway_approval_without_run_keeps_its_own_id_beside_local_gateway_head():
    import api.route_approvals as approvals
    ta = pytest.importorskip(
        "tools.approval",
        reason="tools.approval not available in this environment",
    )

    sid = "sess-legacy-no-run-local-gateway-head"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [ta._ApprovalEntry({
            "approval_id": "local-id",
            "command": "local-head",
        })]
        approval = {"approval_id": "remote-id", "command": "legacy"}
        approvals.submit_gateway_pending_mirror(sid, approval)
        assert approval["approval_id"] == "remote-id"
        queue = approvals._pending.get(sid) or []
        mirrors = [
            entry for entry in queue
            if entry.get(approvals._GATEWAY_MIRROR_FLAG)
            and not str(entry.get("run_id") or "").strip()
            and entry.get("approval_id") == "remote-id"
        ]
        assert mirrors
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_identical_legacy_gateway_approvals_without_run_keep_distinct_ids():
    import api.route_approvals as approvals

    sid = "sess-legacy-no-run-identical"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        first = {"command": "legacy"}
        second = {"command": "legacy"}
        approvals.submit_gateway_pending_mirror(sid, first)
        approvals.submit_gateway_pending_mirror(sid, second)
        assert first["approval_id"] != second["approval_id"]
        queue = approvals._pending.get(sid) or []
        mirrored_ids = [
            entry.get("approval_id")
            for entry in queue
            if entry.get(approvals._GATEWAY_MIRROR_FLAG)
            and not str(entry.get("run_id") or "").strip()
        ]
        assert mirrored_ids == [first["approval_id"], second["approval_id"]]
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_identical_legacy_gateway_approvals_with_explicit_ids_keep_distinct_ids():
    import api.route_approvals as approvals

    sid = "sess-legacy-no-run-identical-explicit"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        first = {"approval_id": "upstream-a", "command": "legacy"}
        second = {"approval_id": "upstream-b", "command": "legacy"}
        approvals.submit_gateway_pending_mirror(sid, first)
        approvals.submit_gateway_pending_mirror(sid, second)
        assert first["approval_id"] == "upstream-a"
        assert second["approval_id"] == "upstream-b"
        queue = approvals._pending.get(sid) or []
        mirrored_ids = [
            entry.get("approval_id")
            for entry in queue
            if entry.get(approvals._GATEWAY_MIRROR_FLAG)
            and not str(entry.get("run_id") or "").strip()
        ]
        assert mirrored_ids == ["upstream-a", "upstream-b"]
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_reconcile_does_not_bind_live_token_by_approval_id_alone():
    import api.route_approvals as approvals

    sid = "sess-cross-run-shared-id"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._pending[sid] = [{
            approvals._GATEWAY_MIRROR_FLAG: True,
            "run_id": "run-stale",
            "approval_id": "appr-shared",
            "command": "stale",
        }]
        approvals._gateway_queues[sid] = [SimpleNamespace(data={
            "run_id": "run-live",
            "approval_id": "appr-shared",
            "command": "live",
        })]
        with approvals._lock:
            head, total, _changed = approvals.reconcile_gateway_pending_mirror_locked(sid)
            queue = list(approvals._pending[sid])
        assert total == 2
        assert head["run_id"] == "run-live"
        assert queue[0]["run_id"] == "run-live"
        assert queue[1]["run_id"] == "run-stale"
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_terminal_run_retirement_removes_all_same_run_mirrors():
    import api.route_approvals as approvals

    sid = "sess-terminal-run"
    approvals._pending.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-a", "command": "first"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-a", "command": "second"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-b", "command": "other"})
        assert approvals.retire_gateway_pending_mirror(sid, run_id="run-a")
        assert approvals.gateway_pending_mirror(sid, run_id="run-a") is None
        assert approvals.gateway_pending_mirror(sid, run_id="run-b") is not None
    finally:
        approvals._pending.pop(sid, None)


def test_terminal_run_retirement_clears_same_run_gateway_queue_state():
    import api.route_approvals as approvals

    sid = "sess-terminal-run-queue"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [
            SimpleNamespace(data={"run_id": "run-a", "approval_id": "appr-a", "command": "a"}),
            SimpleNamespace(data={"command": "local-head"}),
        ]
        approvals.submit_gateway_pending_mirror(
            sid,
            {"run_id": "run-a", "approval_id": "appr-a", "command": "a"},
        )
        assert approvals.retire_gateway_pending_mirror(sid, run_id="run-a")
        assert approvals.gateway_pending_mirror(sid, run_id="run-a") is None
        assert not any(
            str((getattr(entry, "data", None) or {}).get("run_id") or "").strip() == "run-a"
            for entry in approvals._gateway_queues.get(sid, [])
        )
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_terminal_run_retirement_clears_non_head_same_run_gateway_queue_state():
    import api.route_approvals as approvals

    sid = "sess-terminal-run-non-head-queue"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [
            SimpleNamespace(data={"command": "local-head"}),
            SimpleNamespace(data={"run_id": "run-a", "approval_id": "appr-a", "command": "a"}),
        ]
        assert approvals.retire_gateway_pending_mirror(sid, run_id="run-a")
        assert approvals.gateway_pending_mirror(sid, run_id="run-a") is None
        assert not any(
            str((getattr(entry, "data", None) or {}).get("run_id") or "").strip() == "run-a"
            for entry in approvals._gateway_queues.get(sid, [])
        )
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_exact_one_retirement_keeps_same_run_gateway_queue_state():
    import api.route_approvals as approvals

    sid = "sess-exact-one-queue"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._pending[sid] = [
            {
                approvals._GATEWAY_MIRROR_FLAG: True,
                "run_id": "run-a",
                "approval_id": "appr-a",
                "command": "a",
            },
            {
                approvals._GATEWAY_MIRROR_FLAG: True,
                "run_id": "run-a",
                "approval_id": "appr-b",
                "command": "b",
            },
        ]
        approvals._gateway_queues[sid] = [
            SimpleNamespace(data={"run_id": "run-a", "approval_id": "appr-b", "command": "b"})
        ]
        assert approvals.retire_gateway_pending_mirror(sid, approval_id="appr-a", run_id="run-a")
        assert approvals.gateway_pending_mirror(sid, approval_id="appr-b", run_id="run-a") is not None
        queue = approvals._gateway_queues.get(sid) or []
        assert len(queue) == 1
        assert queue[0].data["approval_id"] == "appr-b"
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_retire_gateway_pending_mirror_notifies_reconciled_successor_when_target_missing():
    import api.route_approvals as approvals

    sid = "sess-retire-notify-successor"
    approvals._pending.pop(sid, None)
    try:
        approvals._pending[sid] = [{
            approvals._GATEWAY_MIRROR_FLAG: True,
            "run_id": "run-a",
            "approval_id": "appr-b",
            "command": "b",
        }]
        notifications = []

        def fake_notify(_sid, head, total):
            notifications.append((head["approval_id"] if head else None, total))

        with patch.object(approvals, "_approval_sse_notify_locked", side_effect=fake_notify):
            assert approvals.retire_gateway_pending_mirror(sid, approval_id="appr-a", run_id="run-a") is False
        assert notifications[-1] == ("appr-b", 1)
    finally:
        approvals._pending.pop(sid, None)


def test_local_stop_uses_runs_api_stop_endpoint():
    from api.gateway_chat import _STREAM_RUN_IDS, stop_gateway_run

    stream_id = "stream-stop"
    _STREAM_RUN_IDS[stream_id] = "run-stop"

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "http://gw:8642/v1/runs/run-stop/stop"

    requests = []

    class _Opener:
        def open(self, req, *, timeout=None):
            requests.append((req, timeout))
            return _Response()

    def fake_build_opener(*handlers):
        assert handlers, "stop must install a no-redirect handler"
        return _Opener()

    try:
        with patch("urllib.request.build_opener", side_effect=fake_build_opener), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value="secret"):
            assert stop_gateway_run("run-stop") is True
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)

    request, timeout = requests[0]
    assert request.full_url == "http://gw:8642/v1/runs/run-stop/stop"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 10


def test_runs_api_publishes_run_id_before_events():
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    stream_id = "sid-run-create-cancel"
    cancel_event = threading.Event()
    requests = []

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
        requests.append(req.full_url)
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-pre-events-cancel"})
        if req.full_url.endswith("/events"):
            return _SseResponse([
                b'data: {"event":"run.completed","output":"Hello"}\n',
                b'\n',
            ])
        raise AssertionError(f"unexpected urlopen after pre-stream cancel: {req.full_url}")

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            final_text, usage = _run_gateway_runs_api_streaming(
                session_id="sess-run-create-cancel",
                msg_text="hi",
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[],
                body_extras={},
                put_gateway_event=lambda *_args, **_kwargs: None,
                cancel_event=cancel_event,
                cfg={},
            )
        assert final_text == "Hello"
        assert usage == {}
        assert requests == ["http://gw:8642/v1/runs", "http://gw:8642/v1/runs/run-pre-events-cancel/events"]
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_runs_api_does_not_own_pre_stream_stop_after_publication():
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming

    stream_id = "sid-run-create-cancel-stop-failed"
    cancel_event = threading.Event()
    requests = []
    events = []

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
        requests.append(req.full_url)
        if req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-pre-events-stop-failed"})
        if req.full_url.endswith("/events"):
            return _SseResponse([
                b'data: {"event":"message.delta","delta":"Hello"}\n',
                b'\n',
                b'data: {"event":"run.completed","output":"Hello"}\n',
                b'\n',
            ])
        raise AssertionError(f"unexpected urlopen: {req.full_url}")

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            final_text, usage = _run_gateway_runs_api_streaming(
                session_id="sess-run-create-cancel-stop-failed",
                msg_text="hi",
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[],
                body_extras={},
                put_gateway_event=lambda event, data: events.append((event, data)),
                cancel_event=cancel_event,
                cfg={},
            )
        assert final_text == "Hello"
        assert usage == {}
        assert requests == ["http://gw:8642/v1/runs", "http://gw:8642/v1/runs/run-pre-events-stop-failed/events"]
        assert [event for event, _data in events] == ["token"]
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_runs_api_marks_run_pending_before_request_preparation():
    from api.gateway_chat import (
        _mark_gateway_run_starting,
        _run_gateway_runs_api_streaming,
        gateway_run_id_pending,
    )

    stream_id = "sid-run-starting-prep"
    cancel_event = threading.Event()
    observed = {"pending_during_prepare": False, "pending_during_post": False}

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
                b'data: {"event":"run.completed","output":"Hello"}\n',
                b'\n',
            ])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_strip(text):
        observed["pending_during_prepare"] = gateway_run_id_pending(stream_id)
        return text

    def fake_urlopen(req, *, timeout=None):
        if req.full_url.endswith("/v1/runs"):
            observed["pending_during_post"] = gateway_run_id_pending(stream_id)
            return _JsonResponse({"run_id": "run-starting"})
        if req.full_url.endswith("/events"):
            return _SseResponse()
        raise AssertionError(f"unexpected urlopen: {req.full_url}")

    try:
        _mark_gateway_run_starting(stream_id)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("api.streaming._strip_oob_blocks", side_effect=fake_strip):
            final_text, usage = _run_gateway_runs_api_streaming(
                session_id="sess-run-starting-prep",
                msg_text="hi",
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
                base_url="http://gw:8642",
                api_key="secret",
                prefill_messages=[],
                body_extras={},
                put_gateway_event=lambda *_args, **_kwargs: None,
                cancel_event=cancel_event,
                cfg={},
                session=SimpleNamespace(context_messages=[{"role": "user", "content": "ctx"}]),
            )
        assert final_text == "Hello"
        assert usage == {}
        assert observed["pending_during_prepare"] is True
        assert observed["pending_during_post"] is True
        assert gateway_run_id_pending(stream_id) is False
    finally:
        _reset_gateway_run_start_state(stream_id)


def test_gateway_worker_marks_run_pending_before_runs_api_prelude():
    from api import gateway_chat
    from api.gateway_chat import (
        STREAMS,
        _mark_gateway_run_starting,
        _run_gateway_chat_streaming,
        gateway_run_id_pending,
    )

    stream_id = "sid-worker-run-starting"
    observed = {"pending_during_support_check": False, "pending_during_runs_call": False}
    publish_run_id = getattr(gateway_chat, "_publish_gateway_run_id", None)
    STREAMS[stream_id] = SimpleNamespace(put_nowait=lambda *_args, **_kwargs: None)
    session = SimpleNamespace(
        profile=None,
        workspace="/tmp",
        context_messages=[],
        messages=[],
        active_stream_id=stream_id,
        pending_user_source="webui",
        process_wakeup_pause={},
        save=lambda: None,
    )

    def fake_supports(_base_url, _api_key):
        observed["pending_during_support_check"] = gateway_run_id_pending(stream_id)
        return True

    def fake_runs(*_args, **_kwargs):
        observed["pending_during_runs_call"] = gateway_run_id_pending(stream_id)
        if publish_run_id:
            publish_run_id(stream_id, "run-worker-cleanup")
        return None, {}

    try:
        _mark_gateway_run_starting(stream_id)
        with patch("api.gateway_chat.RunJournalWriter", return_value=SimpleNamespace(append_sse_event=lambda *_a, **_k: None)), \
             patch("api.gateway_chat.get_session", return_value=session), \
             patch("api.config.get_config", return_value={}), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.gateway_chat._gateway_use_runs_api_enabled", return_value=True), \
             patch("api.gateway_chat.gateway_supports_approval", side_effect=fake_supports), \
             patch("api.gateway_chat._gateway_reasoning_effort_for_request", return_value=None), \
             patch("api.gateway_chat._run_gateway_runs_api_streaming", side_effect=fake_runs), \
             patch("api.streaming._load_webui_prefill_context", return_value={}), \
             patch("api.streaming._prefill_messages_with_webui_context", return_value=[]), \
             patch("api.streaming._normalize_prefill_messages_before_user_turn", side_effect=lambda messages: messages), \
             patch("api.streaming._public_prefill_context_status", return_value={}), \
             patch("api.streaming._webui_ephemeral_system_prompt", return_value="sys"):
            _run_gateway_chat_streaming(
                session_id="sess-worker-run-starting",
                msg_text="hi",
                model="test-model",
                workspace="/tmp",
                stream_id=stream_id,
            )
    finally:
        _reset_gateway_run_start_state(stream_id)

    assert observed["pending_during_support_check"] is True
    assert observed["pending_during_runs_call"] is True
    assert gateway_run_id_pending(stream_id) is False
    assert stream_id not in getattr(gateway_chat, "_STREAM_RUN_LIFECYCLE", {})


def test_start_chat_stream_marks_gateway_run_pending_before_thread_start(monkeypatch):
    from api import gateway_chat, routes

    recorded = {}
    session = SimpleNamespace(
        session_id="sess-route-start-pending",
        active_stream_id=None,
        pending_started_at=None,
        title="title",
        profile=None,
        process_wakeup_pause={},
    )

    class _NoopLock:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    class _FakeThread:
        def __init__(self, *, target=None, args=None, kwargs=None, daemon=None):
            self._target = target
            self._args = args or ()
            self._kwargs = kwargs or {}

        def start(self):
            stream_id = recorded["stream_id"]
            assert gateway_chat.gateway_run_id_pending(stream_id) is True
            gateway_chat._finish_gateway_run_starting(stream_id, result="fallback")
            gateway_chat._clear_gateway_run_starting(stream_id)

    def fake_prepare(session_obj, *, stream_id, **_kwargs):
        recorded["stream_id"] = stream_id
        session_obj.active_stream_id = stream_id
        session_obj.pending_started_at = 123.0

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda *_args, **_kwargs: _NoopLock())
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fake_prepare)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: SimpleNamespace())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "threading", SimpleNamespace(Thread=_FakeThread))

    with patch("api.turn_journal.append_turn_journal_event", return_value={}):
        response = routes._start_chat_stream_for_session(
            session,
            msg="hi",
            attachments=[],
            workspace="/tmp",
            model="test-model",
            external_runtime_owned=True,
        )

    assert response["stream_id"] == recorded["stream_id"]
    assert gateway_chat.gateway_run_id_pending(recorded["stream_id"]) is False


def test_start_chat_stream_clears_gateway_run_state_when_thread_start_fails(monkeypatch):
    from api import gateway_chat, routes

    recorded = {}
    session = SimpleNamespace(
        session_id="sess-route-start-fail",
        active_stream_id=None,
        pending_started_at=None,
        title="title",
        profile=None,
        process_wakeup_pause={},
    )

    class _NoopLock:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    class _BoomThread:
        def __init__(self, *, target=None, args=None, kwargs=None, daemon=None):
            self._target = target
            self._args = args or ()
            self._kwargs = kwargs or {}

        def start(self):
            stream_id = recorded["stream_id"]
            assert gateway_chat.gateway_run_id_pending(stream_id) is True
            raise RuntimeError("thread start failed")

    def fake_prepare(session_obj, *, stream_id, **_kwargs):
        recorded["stream_id"] = stream_id
        session_obj.active_stream_id = stream_id
        session_obj.pending_started_at = 123.0

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda *_args, **_kwargs: _NoopLock())
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fake_prepare)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: SimpleNamespace())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "threading", SimpleNamespace(Thread=_BoomThread))

    with patch("api.turn_journal.append_turn_journal_event", return_value={}):
        with pytest.raises(RuntimeError, match="thread start failed"):
            routes._start_chat_stream_for_session(
                session,
                msg="hi",
                attachments=[],
                workspace="/tmp",
                model="test-model",
                external_runtime_owned=True,
            )

    assert gateway_chat.gateway_run_id_pending(recorded["stream_id"]) is False
    assert recorded["stream_id"] not in getattr(gateway_chat, "_STREAM_RUN_LIFECYCLE", {})


@pytest.mark.parametrize(
    ("stop_result", "expected_status", "expected_payload", "expect_cancel"),
    [
        (
            False,
            502,
            {"ok": False, "cancelled": False, "stream_id": "stream-cancel-worker-pending", "error": "Gateway stop failed"},
            False,
        ),
        (
            True,
            200,
            {"ok": True, "cancelled": True, "stream_id": "stream-cancel-worker-pending"},
            True,
        ),
    ],
)
def test_chat_cancel_waits_for_worker_published_run_id_before_settlement(
    monkeypatch,
    stop_result,
    expected_status,
    expected_payload,
    expect_cancel,
):
    import time
    import urllib.parse

    from api import gateway_chat, routes
    import api.route_approvals as approvals
    from api.config import ACTIVE_RUNS, STREAMS, register_stream_owner, stream_owner_session_id, unregister_stream_owner

    sid = "sess-cancel-worker-pending"
    stream_id = "stream-cancel-worker-pending"
    support_gate = threading.Event()
    release_support = threading.Event()
    release_worker = threading.Event()
    captured = {}
    called = {"cancel": False, "stop": None}
    publish_run_id = gateway_chat._publish_gateway_run_id
    mark_starting = gateway_chat._mark_gateway_run_starting

    STREAMS[stream_id] = SimpleNamespace(put_nowait=lambda *_args, **_kwargs: None)
    register_stream_owner(stream_id, sid)
    approvals._pending.pop(sid, None)
    approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-stop/1", "command": "first"})
    session = SimpleNamespace(
        profile=None,
        workspace="/tmp",
        context_messages=[],
        messages=[],
        active_stream_id=stream_id,
        pending_user_source="webui",
        process_wakeup_pause={},
        pending_started_at=time.time(),
        save=lambda: None,
        title="title",
    )

    def fake_supports(_base_url, _api_key):
        support_gate.set()
        assert gateway_chat.gateway_run_id_pending(stream_id) is True
        release_support.wait(timeout=5)
        return True

    def fake_runs(*_args, **_kwargs):
        publish_run_id(stream_id, "run-stop/1")
        release_worker.wait(timeout=5)
        return None, {}

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda run_id: called.__setitem__("stop", run_id) or stop_result)
    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    request_thread = threading.Thread(target=routes.handle_get, args=(object(), parsed), daemon=True)
    worker_thread = threading.Thread(
        target=gateway_chat._run_gateway_chat_streaming,
        kwargs={
            "session_id": sid,
            "msg_text": "hi",
            "model": "test-model",
            "workspace": "/tmp",
            "stream_id": stream_id,
        },
        daemon=True,
    )

    try:
        mark_starting(stream_id)
        with patch("api.gateway_chat.RunJournalWriter", return_value=SimpleNamespace(append_sse_event=lambda *_a, **_k: None)), \
             patch("api.gateway_chat.get_session", return_value=session), \
             patch("api.config.get_config", return_value={}), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.gateway_chat._gateway_use_runs_api_enabled", return_value=True), \
             patch("api.gateway_chat.gateway_supports_approval", side_effect=fake_supports), \
             patch("api.gateway_chat._gateway_reasoning_effort_for_request", return_value=None), \
             patch("api.gateway_chat._run_gateway_runs_api_streaming", side_effect=fake_runs), \
             patch("api.streaming._load_webui_prefill_context", return_value={}), \
             patch("api.streaming._prefill_messages_with_webui_context", return_value=[]), \
             patch("api.streaming._normalize_prefill_messages_before_user_turn", side_effect=lambda messages: messages), \
             patch("api.streaming._public_prefill_context_status", return_value={}), \
             patch("api.streaming._webui_ephemeral_system_prompt", return_value="sys"):
            worker_thread.start()
            assert support_gate.wait(timeout=5)
            request_thread.start()
            time.sleep(0.05)
            assert request_thread.is_alive()
            assert called["cancel"] is False
            assert called["stop"] is None
            release_support.set()
            request_thread.join(timeout=5)
            assert not request_thread.is_alive()
            assert captured["status"] == expected_status
            assert captured["payload"] == expected_payload
            assert called["stop"] == "run-stop/1"
            assert called["cancel"] is expect_cancel
            if stop_result:
                assert approvals.gateway_pending_mirror(sid, run_id="run-stop/1") is None
            else:
                assert stream_id in STREAMS
                assert stream_owner_session_id(stream_id) == sid
                assert session.active_stream_id == stream_id
                assert stream_id in ACTIVE_RUNS
                assert approvals.gateway_pending_mirror(sid, run_id="run-stop/1") is not None
    finally:
        release_support.set()
        release_worker.set()
        request_thread.join(timeout=5)
        worker_thread.join(timeout=5)
        approvals._pending.pop(sid, None)
        STREAMS.pop(stream_id, None)
        ACTIVE_RUNS.pop(stream_id, None)
        unregister_stream_owner(stream_id)
        _reset_gateway_run_start_state(stream_id)


def test_gateway_worker_prelude_exception_retires_failed_start_after_waiter_consumes():
    import time

    from api import gateway_chat

    stream_id = "stream-start-prelude-exception"
    sid = "sess-start-prelude-exception"
    wait_for_gateway_run_id = gateway_chat.wait_for_gateway_run_id
    mark_starting = gateway_chat._mark_gateway_run_starting
    lifecycle = gateway_chat._STREAM_RUN_LIFECYCLE
    run_ids = gateway_chat._STREAM_RUN_IDS
    observed = {}
    waiter_thread = None
    session = SimpleNamespace(
        profile=None,
        workspace="/tmp",
        context_messages=[],
        messages=[],
        active_stream_id=stream_id,
        pending_user_source="webui",
        process_wakeup_pause={},
        pending_started_at=time.time(),
        save=lambda: None,
        title="title",
    )

    def waiter():
        observed["result"] = wait_for_gateway_run_id(stream_id, 1.0)

    worker_thread = threading.Thread(
        target=gateway_chat._run_gateway_chat_streaming,
        kwargs={
            "session_id": sid,
            "msg_text": "hi",
            "model": "test-model",
            "workspace": "/tmp",
            "stream_id": stream_id,
        },
        daemon=True,
    )

    try:
        gateway_chat.STREAMS[stream_id] = SimpleNamespace(put_nowait=lambda *_args, **_kwargs: None)
        mark_starting(stream_id)
        waiter_thread = threading.Thread(target=waiter, daemon=True)
        waiter_thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if int((lifecycle.get(stream_id) or {}).get("waiters") or 0) == 1:
                break
            time.sleep(0.01)
        assert int((lifecycle.get(stream_id) or {}).get("waiters") or 0) == 1
        with patch("api.gateway_chat.RunJournalWriter", return_value=SimpleNamespace(append_sse_event=lambda *_a, **_k: None)), \
             patch("api.gateway_chat.get_session", return_value=session), \
             patch("api.config.get_config", side_effect=RuntimeError("prelude boom")):
            worker_thread.start()
            worker_thread.join(timeout=5)
            assert not worker_thread.is_alive()
        waiter_thread.join(timeout=5)
        assert not waiter_thread.is_alive()
        assert observed["result"] == (True, None)
        assert stream_id not in lifecycle
        assert stream_id not in run_ids
    finally:
        if waiter_thread is not None:
            waiter_thread.join(timeout=5)
        worker_thread.join(timeout=5)
        gateway_chat.STREAMS.pop(stream_id, None)
        _reset_gateway_run_start_state(stream_id)


def test_gateway_failed_start_result_survives_waiter_until_consumed():
    import time

    from api import gateway_chat

    stream_id = "stream-start-failed-race"
    wait_for_gateway_run_id = gateway_chat.wait_for_gateway_run_id
    mark_starting = gateway_chat._mark_gateway_run_starting
    finish_starting = gateway_chat._finish_gateway_run_starting
    clear_starting = gateway_chat._clear_gateway_run_starting
    lifecycle = gateway_chat._STREAM_RUN_LIFECYCLE
    observed = {}

    def waiter():
        observed["result"] = wait_for_gateway_run_id(stream_id, 1.0)

    try:
        mark_starting(stream_id)
        waiter_thread = threading.Thread(target=waiter, daemon=True)
        waiter_thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if int((lifecycle.get(stream_id) or {}).get("waiters") or 0) == 1:
                break
            time.sleep(0.01)
        assert int((lifecycle.get(stream_id) or {}).get("waiters") or 0) == 1
        finish_starting(stream_id)
        clear_starting(stream_id)
        waiter_thread.join(timeout=5)
        assert not waiter_thread.is_alive()
        assert observed["result"] == (True, None)
        assert stream_id not in lifecycle
        assert wait_for_gateway_run_id(stream_id, 0.0) == (False, None)
    finally:
        _reset_gateway_run_start_state(stream_id)


def test_gateway_ready_run_state_retires_after_owner_and_waiter_finish():
    from api import gateway_chat

    stream_id = "stream-start-ready-race"
    mark_starting = gateway_chat._mark_gateway_run_starting
    publish_run_id = gateway_chat._publish_gateway_run_id
    clear_starting = gateway_chat._clear_gateway_run_starting
    retire_starting = gateway_chat._retire_gateway_run_starting_if_done
    lifecycle = gateway_chat._STREAM_RUN_LIFECYCLE
    run_ids = gateway_chat._STREAM_RUN_IDS

    try:
        mark_starting(stream_id)
        publish_run_id(stream_id, "run-ready/1")
        lifecycle[stream_id]["waiters"] = 1
        clear_starting(stream_id)
        assert lifecycle[stream_id]["phase"] == "ready"
        assert lifecycle[stream_id]["owner_done"] is True
        assert run_ids[stream_id] == "run-ready/1"
        lifecycle[stream_id]["waiters"] = 0
        assert retire_starting(stream_id) is True
        assert stream_id not in lifecycle
        assert stream_id not in run_ids
    finally:
        _reset_gateway_run_start_state(stream_id)


def test_gateway_missing_stream_releases_pending_start_state_for_local_cancel():
    from api import gateway_chat

    stream_id = "stream-start-missing-queue"
    mark_starting = gateway_chat._mark_gateway_run_starting
    wait_for_gateway_run_id = gateway_chat.wait_for_gateway_run_id
    lifecycle = gateway_chat._STREAM_RUN_LIFECYCLE

    try:
        mark_starting(stream_id)
        gateway_chat._run_gateway_chat_streaming(
            session_id="sess-missing-queue",
            msg_text="hi",
            model="test-model",
            workspace="/tmp",
            stream_id=stream_id,
        )
        assert wait_for_gateway_run_id(stream_id, 0.0) == (False, None)
        assert stream_id not in lifecycle
    finally:
        _reset_gateway_run_start_state(stream_id)


def test_local_stop_rejects_redirected_success():
    from api.gateway_chat import _STREAM_RUN_IDS, stop_gateway_run

    stream_id = "stream-stop-redirect"
    _STREAM_RUN_IDS[stream_id] = "run-stop"

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "http://gw:8642/login"

    class _Opener:
        def open(self, req, *, timeout=None):
            return _Response()

    try:
        with patch("urllib.request.build_opener", return_value=_Opener()), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=None):
            assert stop_gateway_run("run-stop") is False
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_chat_cancel_surfaces_redirected_gateway_stop(monkeypatch):
    import urllib.parse

    from api import routes
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-cancel-stop-redirect"
    stream_id = "stream-cancel-stop-redirect"
    _STREAM_RUN_IDS[stream_id] = "run-stop"
    approvals._pending.pop(sid, None)
    approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-stop", "command": "first"})
    captured = {}
    called = {"cancel": False}

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "stream_owner_session_id", lambda _stream_id: sid)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _stream_id: False)

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    try:
        routes.handle_get(object(), parsed)
        assert captured["status"] == 502
        assert captured["payload"] == {
            "ok": False,
            "cancelled": False,
            "stream_id": stream_id,
            "error": "Gateway stop failed",
        }
        assert called["cancel"] is False
        assert sid in approvals._pending
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)


def test_chat_cancel_retires_same_run_gateway_mirrors_after_stop(monkeypatch):
    import urllib.parse

    from api import routes
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-cancel-stop"
    stream_id = "stream-cancel-stop"
    _STREAM_RUN_IDS[stream_id] = "run-stop"
    approvals._pending.pop(sid, None)
    approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-stop", "command": "first"})
    approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-stop", "command": "second"})
    captured = {}

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "stream_owner_session_id", lambda _stream_id: sid)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _stream_id: True)

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    try:
        routes.handle_get(object(), parsed)
        assert captured["status"] == 200
        assert captured["payload"] == {"ok": True, "cancelled": True, "stream_id": stream_id}
        assert sid not in approvals._pending
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)


def test_chat_cancel_surfaces_gateway_stop_failure(monkeypatch):
    import urllib.parse

    from api import routes
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-cancel-stop-fail"
    stream_id = "stream-cancel-stop-fail"
    _STREAM_RUN_IDS[stream_id] = "run-stop"
    approvals._pending.pop(sid, None)
    approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-stop", "command": "first"})
    captured = {}
    called = {"cancel": False}

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "stream_owner_session_id", lambda _stream_id: sid)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _stream_id: False)

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    try:
        routes.handle_get(object(), parsed)
        assert captured["status"] == 502
        assert captured["payload"] == {
            "ok": False,
            "cancelled": False,
            "stream_id": stream_id,
            "error": "Gateway stop failed",
        }
        assert called["cancel"] is False
        assert sid in approvals._pending
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)


def test_chat_cancel_without_gateway_readiness_uses_local_cancel(monkeypatch):
    import urllib.parse

    from api import routes

    stream_id = "stream-cancel-local-only"
    captured = {}
    called = {"cancel": False, "stop": False}

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _run_id: called.__setitem__("stop", True))

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")

    routes.handle_get(object(), parsed)

    assert captured["status"] == 200
    assert captured["payload"] == {
        "ok": True,
        "cancelled": True,
        "stream_id": stream_id,
    }
    assert called["cancel"] is True
    assert called["stop"] is False


def test_chat_cancel_times_out_while_gateway_run_id_is_pending(monkeypatch):
    import urllib.parse

    from api import routes
    from api import gateway_chat

    stream_id = "stream-cancel-pending-run-id"
    captured = {}
    called = {"cancel": False, "stop": False}
    mark_starting = gateway_chat._mark_gateway_run_starting

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.GATEWAY_RUN_ID_WAIT_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _run_id: called.__setitem__("stop", True))

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    try:
        mark_starting(stream_id)
        routes.handle_get(object(), parsed)
        assert captured["status"] == 502
        assert captured["payload"] == {
            "ok": False,
            "cancelled": False,
            "stream_id": stream_id,
            "error": "Gateway stop failed",
        }
        assert called["cancel"] is False
        assert called["stop"] is False
    finally:
        _reset_gateway_run_start_state(stream_id)


def test_chat_cancel_uses_local_cancel_after_gateway_runs_api_falls_back(monkeypatch):
    import time
    import urllib.parse

    from api import routes
    from api import gateway_chat

    stream_id = "stream-cancel-runs-fallback"
    captured = {}
    called = {"cancel": False, "stop": False}
    mark_starting = gateway_chat._mark_gateway_run_starting
    finish_starting = gateway_chat._finish_gateway_run_starting

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _run_id: called.__setitem__("stop", True))

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    request_thread = threading.Thread(target=routes.handle_get, args=(object(), parsed))
    try:
        mark_starting(stream_id)
        request_thread.start()
        time.sleep(0.05)
        assert request_thread.is_alive()
        finish_starting(stream_id, result="fallback")
        request_thread.join(timeout=5)
        assert not request_thread.is_alive()
        assert captured["status"] == 200
        assert captured["payload"] == {
            "ok": True,
            "cancelled": True,
            "stream_id": stream_id,
        }
        assert called["cancel"] is True
        assert called["stop"] is False
    finally:
        if request_thread.is_alive():
            finish_starting(stream_id, result="fallback")
            request_thread.join(timeout=5)
        _reset_gateway_run_start_state(stream_id)


def test_chat_cancel_ignores_legacy_run_id_after_gateway_runs_api_falls_back(monkeypatch):
    import time
    import urllib.parse

    from api import routes
    from api import gateway_chat

    stream_id = "stream-cancel-runs-fallback-legacy-run-id"
    captured = {}
    called = {"cancel": False, "stop": False}
    mark_starting = gateway_chat._mark_gateway_run_starting
    finish_starting = gateway_chat._finish_gateway_run_starting
    run_ids = gateway_chat._STREAM_RUN_IDS

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _run_id: called.__setitem__("stop", True))

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
    request_thread = threading.Thread(target=routes.handle_get, args=(object(), parsed))
    try:
        mark_starting(stream_id)
        request_thread.start()
        time.sleep(0.05)
        assert request_thread.is_alive()
        finish_starting(stream_id, result="fallback")
        run_ids[stream_id] = "legacy-run/1"
        request_thread.join(timeout=5)
        assert not request_thread.is_alive()
        assert captured["status"] == 200
        assert captured["payload"] == {
            "ok": True,
            "cancelled": True,
            "stream_id": stream_id,
        }
        assert called["cancel"] is True
        assert called["stop"] is False
    finally:
        if request_thread.is_alive():
            finish_starting(stream_id, result="fallback")
            request_thread.join(timeout=5)
        _reset_gateway_run_start_state(stream_id)


def test_stale_gateway_card_does_not_relay_live_run():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-stale-gateway"
    stream_id = "sid-stale-live-run"
    _STREAM_RUN_IDS[stream_id] = "run-live"
    approvals._pending.pop(sid, None)
    approvals.submit_gateway_pending_mirror(sid, {
        "run_id": "run-live", "approval_id": "gwrun:run-live", "command": "echo live"
    })

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    handler = MagicMock()
    handler.wfile = io.BytesIO()

    with patch("api.routes.get_session", return_value=mock_session), \
         patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
         patch("api.gateway_chat._gateway_api_key", return_value=""), \
         patch("api.runner_client.HttpRunnerClient._request_json") as request_json:
        from api.routes import _handle_approval_respond
        _handle_approval_respond(handler, {
            "session_id": sid,
            "choice": "once",
            "approval_id": "gwrun:run-stale",
        })

    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.send_response.call_args.args[0] == 409
    assert payload["code"] == "gateway_run_unavailable"
    request_json.assert_not_called()
    assert approvals.gateway_pending_mirror(sid, run_id="run-live") is not None

    _STREAM_RUN_IDS.pop(stream_id, None)
    approvals._pending.pop(sid, None)


def test_retained_gateway_card_does_not_relay_different_active_run():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-retained-gateway"
    stream_id = "sid-retained-live-run"
    _STREAM_RUN_IDS[stream_id] = "run-live"
    approvals._pending.pop(sid, None)
    approvals.submit_gateway_pending_mirror(sid, {
        "run_id": "run-retained", "approval_id": "gwrun:run-retained", "command": "echo retained"
    })
    approvals.submit_gateway_pending_mirror(sid, {
        "run_id": "run-live", "approval_id": "gwrun:run-live", "command": "echo live"
    })

    mock_session = MagicMock()
    mock_session.active_stream_id = stream_id
    handler = MagicMock()
    handler.wfile = io.BytesIO()

    with patch("api.routes.get_session", return_value=mock_session), \
         patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
         patch("api.gateway_chat._gateway_api_key", return_value=""), \
         patch("api.runner_client.HttpRunnerClient._request_json") as request_json:
        from api.routes import _handle_approval_respond
        _handle_approval_respond(handler, {
            "session_id": sid,
            "choice": "once",
            "approval_id": "gwrun:run-retained",
        })

    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.send_response.call_args.args[0] == 409
    assert payload["code"] == "gateway_run_unavailable"
    request_json.assert_not_called()
    assert approvals.gateway_pending_mirror(sid, approval_id="gwrun:run-retained") is not None
    assert approvals.gateway_pending_mirror(sid, approval_id="gwrun:run-live") is not None

    _STREAM_RUN_IDS.pop(stream_id, None)
    approvals._pending.pop(sid, None)


def test_stale_same_run_gateway_card_does_not_relay_live_head():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-stale-same-run"
    stream_id = "sid-stale-same-run"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "first"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "second"})
        queue = approvals._pending[sid]
        stale_id = queue[0]["approval_id"]
        live_id = queue[1]["approval_id"]
        assert approvals.retire_gateway_pending_mirror(sid, approval_id=stale_id, run_id="run-shared")

        mock_session = MagicMock()
        mock_session.active_stream_id = stream_id
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.runner_client.HttpRunnerClient._request_json") as request_json:
            from api.routes import _handle_approval_respond
            _handle_approval_respond(handler, {
                "session_id": sid,
                "choice": "once",
                "approval_id": stale_id,
            })

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.send_response.call_args.args[0] == 409
        assert payload["code"] == "gateway_run_unavailable"
        request_json.assert_not_called()
        assert approvals.gateway_pending_mirror(sid, approval_id=live_id) is not None
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)


def test_stale_same_run_gateway_card_without_token_does_not_relay_live_head():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals
    ta = pytest.importorskip(
        "tools.approval",
        reason="tools.approval not available in this environment",
    )

    sid = "sess-stale-same-run-no-token"
    stream_id = "sid-stale-same-run-no-token"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._pending[sid] = [{
            approvals._GATEWAY_MIRROR_FLAG: True,
            "run_id": "run-shared",
            "approval_id": "stale-a",
            "command": "stale",
        }]
        approvals._gateway_queues[sid] = [ta._ApprovalEntry({
            "run_id": "run-shared",
            "approval_id": "live-b",
            "command": "live",
        })]

        mock_session = MagicMock()
        mock_session.active_stream_id = stream_id
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.runner_client.HttpRunnerClient._request_json") as request_json:
            from api.routes import _handle_approval_respond
            _handle_approval_respond(handler, {
                "session_id": sid,
                "choice": "once",
                "approval_id": "stale-a",
            })

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.send_response.call_args.args[0] == 409
        assert payload["code"] == "gateway_run_unavailable"
        request_json.assert_not_called()
        assert approvals.gateway_pending_mirror(sid, approval_id="live-b", run_id="run-shared") is not None
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_stale_same_run_gateway_card_without_token_after_teardown_does_not_relay_live_head():
    import api.route_approvals as approvals
    ta = pytest.importorskip(
        "tools.approval",
        reason="tools.approval not available in this environment",
    )

    sid = "sess-stale-same-run-no-token-teardown"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._pending[sid] = [{
            approvals._GATEWAY_MIRROR_FLAG: True,
            "run_id": "run-shared",
            "approval_id": "stale-a",
            "command": "stale",
        }]
        approvals._gateway_queues[sid] = [ta._ApprovalEntry({
            "run_id": "run-shared",
            "approval_id": "live-b",
            "command": "live",
        })]

        mock_session = MagicMock()
        mock_session.active_stream_id = None
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.runner_client.HttpRunnerClient._request_json") as request_json:
            from api.routes import _handle_approval_respond
            _handle_approval_respond(handler, {
                "session_id": sid,
                "choice": "once",
                "approval_id": "stale-a",
            })

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.send_response.call_args.args[0] == 409
        assert payload["code"] == "gateway_run_unavailable"
        request_json.assert_not_called()
        assert approvals.gateway_pending_mirror(sid, approval_id="live-b", run_id="run-shared") is not None
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_stale_same_run_gateway_card_without_token_does_not_relay_live_head_when_live_id_is_synthesized():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals
    ta = pytest.importorskip(
        "tools.approval",
        reason="tools.approval not available in this environment",
    )

    sid = "sess-stale-same-run-no-token-synthesized-live-id"
    stream_id = "sid-stale-same-run-no-token-synthesized-live-id"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._pending[sid] = [{
            approvals._GATEWAY_MIRROR_FLAG: True,
            "run_id": "run-shared",
            "approval_id": "stale-a",
            "command": "stale",
        }]
        approvals._gateway_queues[sid] = [ta._ApprovalEntry({
            "run_id": "run-shared",
            "command": "live",
        })]

        mock_session = MagicMock()
        mock_session.active_stream_id = stream_id
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.runner_client.HttpRunnerClient._request_json") as request_json:
            from api.routes import _handle_approval_respond
            _handle_approval_respond(handler, {
                "session_id": sid,
                "choice": "once",
                "approval_id": "stale-a",
            })

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.send_response.call_args.args[0] == 409
        assert payload["code"] == "gateway_run_unavailable"
        request_json.assert_not_called()
        live_mirror = approvals.gateway_pending_mirror(sid, run_id="run-shared")
        assert live_mirror is not None
        assert live_mirror["approval_id"] != "stale-a"
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_token_stale_same_run_gateway_card_does_not_relay_advanced_live_head():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-token-stale-same-run"
    stream_id = "sid-token-stale-same-run"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals._gateway_queues[sid] = [SimpleNamespace(data={
            "run_id": "run-shared",
            "approval_id": "gwrun:run-shared:a",
            "command": "first",
        })]
        approvals.submit_gateway_pending_mirror(sid, dict(approvals._gateway_queues[sid][0].data))
        stale_id = approvals._pending[sid][0]["approval_id"]

        approvals._gateway_queues[sid] = [SimpleNamespace(data={
            "run_id": "run-shared",
            "approval_id": "gwrun:run-shared:b",
            "command": "second",
        })]
        approvals.submit_gateway_pending_mirror(sid, dict(approvals._gateway_queues[sid][0].data))

        mock_session = MagicMock()
        mock_session.active_stream_id = stream_id
        handler = MagicMock()
        handler.wfile = io.BytesIO()

        with patch("api.routes.get_session", return_value=mock_session), \
             patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.runner_client.HttpRunnerClient._request_json") as request_json:
            from api.routes import _handle_approval_respond
            _handle_approval_respond(handler, {
                "session_id": sid,
                "choice": "once",
                "approval_id": stale_id,
            })

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.send_response.call_args.args[0] == 409
        assert payload["code"] == "gateway_run_unavailable"
        request_json.assert_not_called()
        assert approvals.gateway_pending_mirror(sid, approval_id="gwrun:run-shared:a") is None
        assert approvals.gateway_pending_mirror(sid, approval_id="gwrun:run-shared:b") is not None
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


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
    import api.route_approvals as approvals

    # Seed the mapping.
    _STREAM_RUN_IDS["sid-relay"] = "run abc/1"
    approvals.submit_gateway_pending_mirror("sess-relay", {
        "run_id": "run abc/1", "approval_id": "appr x/y", "command": "echo x"
    })

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
    assert captured["body"] == {"choice": "once", "approval_id": ""}
    handler.send_response.assert_called_with(200)

    # Cleanup.
    _STREAM_RUN_IDS.pop("sid-relay", None)
    approvals._pending.pop("sess-relay", None)


def test_synthetic_gateway_identity_stays_fifo_after_capability_upgrade():
    """A local gwrun ID never becomes remote authority after a capability refresh."""
    from api import routes
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    _STREAM_RUN_IDS["sid-upgrade"] = "run-upgrade"
    approvals.submit_gateway_pending_mirror("sess-upgrade", {
        "run_id": "run-upgrade", "approval_id": "gwrun:run-upgrade:local", "command": "echo x"
    })
    handler = MagicMock()
    handler.wfile = io.BytesIO()
    with patch("api.routes.get_session", return_value=SimpleNamespace(active_stream_id="sid-upgrade")), \
         patch("api.config.gateway_supports_approval_identity_v1", return_value=True), \
         patch("api.runner_client.HttpRunnerClient.respond_approval") as respond:
        routes._handle_approval_respond(handler, {
            "session_id": "sess-upgrade", "choice": "once", "approval_id": "gwrun:run-upgrade:local",
        })
    respond.assert_called_once_with("run-upgrade", "", "once")
    _STREAM_RUN_IDS.pop("sid-upgrade", None)
    approvals._pending.pop("sess-upgrade", None)


def test_agent_identity_v1_relays_the_ingress_identity_exactly():
    """An Agent-issued v1 identity is the only targeted relay authority."""
    from api import routes
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    _STREAM_RUN_IDS["sid-v1"] = "run-v1"
    approvals.submit_gateway_pending_mirror("sess-v1", {
        "run_id": "run-v1", "approval_id": "agent-v1", "command": "echo x",
        "_gateway_agent_identity_v1": True,
    })
    mirror = approvals.gateway_pending_mirror("sess-v1", approval_id="agent-v1", run_id="run-v1")
    assert mirror["_gateway_agent_identity_v1"] is True
    handler = MagicMock()
    handler.wfile = io.BytesIO()
    with patch("api.routes.get_session", return_value=SimpleNamespace(active_stream_id="sid-v1")), \
         patch("api.config.gateway_supports_approval_identity_v1", return_value=True), \
         patch("api.runner_client.HttpRunnerClient.respond_approval") as respond:
        routes._handle_approval_respond(handler, {
            "session_id": "sess-v1", "choice": "once", "approval_id": "agent-v1",
        })
    respond.assert_called_once_with("run-v1", "agent-v1", "once")
    _STREAM_RUN_IDS.pop("sid-v1", None)
    approvals._pending.pop("sess-v1", None)


def test_capability_v1_empty_ingress_identity_stays_fifo_only():
    """Capability alone cannot promote a locally synthesized gwrun identity."""
    from api import routes
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    _STREAM_RUN_IDS["sid-v1-empty"] = "run-v1-empty"
    approvals.submit_gateway_pending_mirror("sess-v1-empty", {
        "run_id": "run-v1-empty", "approval_id": "", "command": "echo x",
        "_gateway_agent_identity_v1": False,
    })
    mirror = approvals.gateway_pending_mirror("sess-v1-empty", run_id="run-v1-empty")
    assert mirror["approval_id"].startswith("gwrun:run-v1-empty:")
    assert mirror["_gateway_agent_identity_v1"] is False
    handler = MagicMock()
    handler.wfile = io.BytesIO()
    with patch("api.routes.get_session", return_value=SimpleNamespace(active_stream_id="sid-v1-empty")), \
         patch("api.config.gateway_supports_approval_identity_v1", return_value=True), \
         patch("api.runner_client.HttpRunnerClient.respond_approval") as respond:
        routes._handle_approval_respond(handler, {
            "session_id": "sess-v1-empty", "choice": "once", "approval_id": mirror["approval_id"],
        })
    respond.assert_called_once_with("run-v1-empty", "", "once")
    _STREAM_RUN_IDS.pop("sid-v1-empty", None)
    approvals._pending.pop("sess-v1-empty", None)


def test_gateway_approval_response_without_approval_id_409s():
    """Gateway relay requires the emitted per-approval id, not bare run state."""
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    _STREAM_RUN_IDS["sid-relay-empty-id"] = "run abc/1"
    approvals.submit_gateway_pending_mirror("sess-relay", {
        "run_id": "run abc/1", "approval_id": "", "command": "echo x"
    })

    mock_session = MagicMock()
    mock_session.active_stream_id = "sid-relay-empty-id"
    handler = MagicMock()
    handler.wfile = io.BytesIO()

    with patch("api.routes.get_session", return_value=mock_session), \
         patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
         patch("api.gateway_chat._gateway_api_key", return_value=""), \
         patch("api.runner_client.HttpRunnerClient._request_json") as request_json, \
         patch("api.routes._resolve_approval_legacy", return_value=True) as cleanup:
        from api.routes import _handle_approval_respond
        _handle_approval_respond(
            handler,
            {"session_id": "sess-relay", "choice": "once", "approval_id": ""},
        )

    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    status = handler.send_response.call_args.args[0]
    assert status == 409, f"status={status} payload={payload}"
    assert payload["ok"] is False
    assert payload["relayed"] is False
    assert payload["code"] == "gateway_run_unavailable"
    request_json.assert_not_called()
    cleanup.assert_not_called()

    _STREAM_RUN_IDS.pop("sid-relay-empty-id", None)
    approvals._pending.pop("sess-relay", None)


def test_gateway_approval_response_relay_failure_returns_502():
    """Gateway relay failures must surface as HTTP errors to the frontend."""
    from api.gateway_chat import _STREAM_RUN_IDS
    from api.runner_client import RunnerClientError
    import api.route_approvals as approvals

    _STREAM_RUN_IDS["sid-relay-fail"] = "run-abc"
    approvals.submit_gateway_pending_mirror("sess-relay", {
        "run_id": "run-abc", "approval_id": "appr-x", "command": "echo x"
    })

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
    approvals._pending.pop("sess-relay", None)


def test_gateway_approval_response_invalid_gateway_base_returns_502():
    """Misconfigured gateway bases must not fall through to the local approval path."""
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    _STREAM_RUN_IDS["sid-relay-invalid-base"] = "run-abc"
    approvals.submit_gateway_pending_mirror("sess-relay", {
        "run_id": "run-abc", "approval_id": "appr-x", "command": "echo x"
    })

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
    approvals._pending.pop("sess-relay", None)


def test_identityless_gateway_relay_duplicate_response_is_single_flight():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-relay-single-flight"
    stream_id = "sid-relay-single-flight"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "first"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "second"})
        first_id = approvals.gateway_pending_mirror(sid, run_id="run-shared")["approval_id"]
        second_id = approvals._pending[sid][1]["approval_id"]
        session = SimpleNamespace(active_stream_id=stream_id)
        relay_started = threading.Event()
        release_relay = threading.Event()
        results = {}
        calls = []

        def blocked_respond(run_id, approval_id, choice):
            calls.append((run_id, approval_id, choice))
            relay_started.set()
            assert release_relay.wait(timeout=5)

        with patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval", side_effect=blocked_respond):
            t1 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "first", session, {"session_id": sid, "choice": "once", "approval_id": first_id}),
                daemon=True,
            )
            t2 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "second", session, {"session_id": sid, "choice": "once", "approval_id": first_id}),
                daemon=True,
            )
            t1.start()
            assert relay_started.wait(timeout=5)
            t2.start()
            t2.join(timeout=5)
            assert not t2.is_alive()
            release_relay.set()
            t1.join(timeout=5)
            assert not t1.is_alive()

        assert calls == [("run-shared", "", "once")]
        assert results["first"][0] == 200
        assert results["first"][1]["relayed"] is True
        assert results["second"][0] == 409
        assert results["second"][1]["code"] == "gateway_approval_in_progress"
        promoted = approvals.gateway_pending_mirror(sid, run_id="run-shared")
        assert promoted is not None
        assert promoted["approval_id"] == second_id
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_identityless_gateway_relay_conflicting_duplicate_choice_returns_busy():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-relay-conflicting-choice"
    stream_id = "sid-relay-conflicting-choice"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "first"})
        first_id = approvals.gateway_pending_mirror(sid, run_id="run-shared")["approval_id"]
        session = SimpleNamespace(active_stream_id=stream_id)
        relay_started = threading.Event()
        release_relay = threading.Event()
        results = {}
        calls = []

        def blocked_respond(run_id, approval_id, choice):
            calls.append((run_id, approval_id, choice))
            relay_started.set()
            assert release_relay.wait(timeout=5)

        with patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval", side_effect=blocked_respond):
            t1 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "first", session, {"session_id": sid, "choice": "once", "approval_id": first_id}),
                daemon=True,
            )
            t2 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "second", session, {"session_id": sid, "choice": "deny", "approval_id": first_id}),
                daemon=True,
            )
            t1.start()
            assert relay_started.wait(timeout=5)
            t2.start()
            t2.join(timeout=5)
            assert not t2.is_alive()
            release_relay.set()
            t1.join(timeout=5)
            assert not t1.is_alive()

        assert calls == [("run-shared", "", "once")]
        assert results["first"][0] == 200
        assert results["second"][0] == 409
        assert results["second"][1]["code"] == "gateway_approval_in_progress"
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_identityless_gateway_relay_failure_releases_owner_for_retry():
    from api.gateway_chat import _STREAM_RUN_IDS
    from api.runner_client import RunnerClientError
    import api.route_approvals as approvals

    sid = "sess-relay-retry-after-failure"
    stream_id = "sid-relay-retry-after-failure"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "first"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "second"})
        first_id = approvals.gateway_pending_mirror(sid, run_id="run-shared")["approval_id"]
        second_id = approvals._pending[sid][1]["approval_id"]
        session = SimpleNamespace(active_stream_id=stream_id)
        relay_started = threading.Event()
        release_relay = threading.Event()
        results = {}
        calls = []

        def flaky_respond(run_id, approval_id, choice):
            calls.append((run_id, approval_id, choice))
            if len(calls) == 1:
                relay_started.set()
                assert release_relay.wait(timeout=5)
                raise RunnerClientError("relay failed")

        with patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval", side_effect=flaky_respond):
            t1 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "first", session, {"session_id": sid, "choice": "once", "approval_id": first_id}),
                daemon=True,
            )
            t2 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "second", session, {"session_id": sid, "choice": "once", "approval_id": first_id}),
                daemon=True,
            )
            t1.start()
            assert relay_started.wait(timeout=5)
            t2.start()
            t2.join(timeout=5)
            assert not t2.is_alive()
            release_relay.set()
            t1.join(timeout=5)
            assert not t1.is_alive()
            _invoke_gateway_approval_response(
                results,
                "retry",
                session,
                {"session_id": sid, "choice": "once", "approval_id": first_id},
            )

        assert calls == [
            ("run-shared", "", "once"),
            ("run-shared", "", "once"),
        ]
        assert results["first"][0] == 502
        assert results["second"][0] == 409
        assert results["second"][1]["code"] == "gateway_approval_in_progress"
        assert results["retry"][0] == 200
        promoted = approvals.gateway_pending_mirror(sid, run_id="run-shared")
        assert promoted is not None
        assert promoted["approval_id"] == second_id
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_identityless_gateway_relay_keeps_different_runs_parallel():
    import api.route_approvals as approvals

    sid = "sess-relay-parallel-runs"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-a", "approval_id": "appr-a", "command": "first"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-b", "approval_id": "appr-b", "command": "second"})
        session = SimpleNamespace(active_stream_id=None)
        first_started = threading.Event()
        second_started = threading.Event()
        release_relays = threading.Event()
        results = {}
        calls = []

        def parallel_respond(run_id, approval_id, choice):
            calls.append((run_id, approval_id, choice))
            if run_id == "run-a":
                first_started.set()
            elif run_id == "run-b":
                second_started.set()
            assert release_relays.wait(timeout=5)

        with patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval", side_effect=parallel_respond):
            t1 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "first", session, {"session_id": sid, "choice": "once", "approval_id": "appr-a"}),
                daemon=True,
            )
            t2 = threading.Thread(
                target=_invoke_gateway_approval_response,
                args=(results, "second", session, {"session_id": sid, "choice": "deny", "approval_id": "appr-b"}),
                daemon=True,
            )
            t1.start()
            t2.start()
            assert first_started.wait(timeout=5)
            assert second_started.wait(timeout=5)
            release_relays.set()
            t1.join(timeout=5)
            t2.join(timeout=5)
            assert not t1.is_alive()
            assert not t2.is_alive()

        assert sorted(calls) == [("run-a", "", "once"), ("run-b", "", "deny")]
        assert results["first"][0] == 200
        assert results["second"][0] == 200
        assert approvals.gateway_pending_mirror(sid, run_id="run-a") is None
        assert approvals.gateway_pending_mirror(sid, run_id="run-b") is None
    finally:
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


def test_identityless_gateway_relay_revalidates_head_after_claim_and_releases_owner():
    from api.gateway_chat import _STREAM_RUN_IDS
    import api.route_approvals as approvals

    sid = "sess-relay-head-advance-after-claim"
    stream_id = "sid-relay-head-advance-after-claim"
    _STREAM_RUN_IDS[stream_id] = "run-shared"
    approvals._pending.pop(sid, None)
    approvals._gateway_queues.pop(sid, None)
    try:
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "first"})
        approvals.submit_gateway_pending_mirror(sid, {"run_id": "run-shared", "command": "second"})
        first_id = approvals.gateway_pending_mirror(sid, run_id="run-shared")["approval_id"]
        second_id = approvals._pending[sid][1]["approval_id"]
        session = SimpleNamespace(active_stream_id=stream_id)
        real_gateway_pending_mirror = approvals.gateway_pending_mirror
        advanced = {"done": False}

        def advance_head_after_claim(*args, **kwargs):
            mirror = real_gateway_pending_mirror(*args, **kwargs)
            approval_id = kwargs.get("approval_id") if kwargs else args[1] if len(args) > 1 else ""
            run_id = kwargs.get("run_id") if kwargs else args[2] if len(args) > 2 else ""
            if not advanced["done"] and approval_id == first_id and run_id == "run-shared":
                advanced["done"] = True
                assert approvals.retire_gateway_pending_mirror(sid, approval_id=first_id, run_id="run-shared")
            return mirror

        with patch("api.gateway_chat._gateway_base_url", return_value="http://gw:8642"), \
             patch("api.gateway_chat._gateway_api_key", return_value=""), \
             patch("api.config.gateway_supports_approval_identity_v1", return_value=False), \
             patch("api.routes.gateway_pending_mirror", side_effect=advance_head_after_claim), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond:
            results = {}
            _invoke_gateway_approval_response(
                results,
                "stale",
                session,
                {"session_id": sid, "choice": "once", "approval_id": first_id},
            )

            assert results["stale"][0] == 409
            assert results["stale"][1]["code"] == "gateway_run_unavailable"
            respond.assert_not_called()
            live = approvals.gateway_pending_mirror(sid, run_id="run-shared")
            assert live is not None
            assert live["approval_id"] == second_id

            _invoke_gateway_approval_response(
                results,
                "retry",
                session,
                {"session_id": sid, "choice": "once", "approval_id": second_id},
            )

        assert results["retry"][0] == 200
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        approvals._pending.pop(sid, None)
        approvals._gateway_queues.pop(sid, None)


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
