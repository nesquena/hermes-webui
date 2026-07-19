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
    assert empty_id["approval_id"] == ""

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
        respond_approval.assert_called_once_with("run-fifo", "approval-first", "deny")
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
            assert approval_id.startswith(f"gwrun:run-real-{choice}:")
            assert approvals.gateway_pending_mirror(sid, approval_id=approval_id)["run_id"] == f"run-real-{choice}"
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
        respond_approval.assert_called_once_with("run-live", "same-id", "once")
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
        respond_approval.assert_called_once_with("run-remote", "remote-a", "once")
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
            assert stop_gateway_run(stream_id) is True
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)

    request, timeout = requests[0]
    assert request.full_url == "http://gw:8642/v1/runs/run-stop/stop"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 10


def test_runs_api_cancel_after_run_creation_stops_remote_run_before_events():
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

    def fake_urlopen(req, *, timeout=None):
        requests.append(req.full_url)
        if req.full_url.endswith("/v1/runs"):
            cancel_event.set()
            return _JsonResponse({"run_id": "run-pre-events-cancel"})
        raise AssertionError(f"unexpected urlopen after pre-stream cancel: {req.full_url}")

    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("api.gateway_chat.stop_gateway_run", return_value=True) as stop_run:
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
        assert final_text is None
        assert usage == {}
        stop_run.assert_called_once_with(stream_id)
        assert requests == ["http://gw:8642/v1/runs"]
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_runs_api_cancel_after_run_creation_keeps_stream_open_when_stop_fails():
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
            cancel_event.set()
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
        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("api.gateway_chat.stop_gateway_run", return_value=False) as stop_run:
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
        stop_run.assert_called_once_with(stream_id)
        assert requests == [
            "http://gw:8642/v1/runs",
            "http://gw:8642/v1/runs/run-pre-events-stop-failed/events",
        ]
        assert [event for event, _data in events] == ["token"]
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_runs_api_marks_run_pending_before_request_preparation():
    from api.gateway_chat import _STREAM_RUN_IDS, _run_gateway_runs_api_streaming, gateway_run_id_pending

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
        _STREAM_RUN_IDS.pop(stream_id, None)


def test_gateway_worker_marks_run_pending_before_runs_api_prelude():
    from api.gateway_chat import STREAMS, _run_gateway_chat_streaming, gateway_run_id_pending

    stream_id = "sid-worker-run-starting"
    observed = {"pending_during_support_check": False, "pending_during_runs_call": False}
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
        return None, {}

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

    assert observed["pending_during_support_check"] is True
    assert observed["pending_during_runs_call"] is True
    assert gateway_run_id_pending(stream_id) is False


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
            assert stop_gateway_run(stream_id) is False
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
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True))
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
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True))
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


def test_chat_cancel_blocks_while_gateway_run_id_is_pending(monkeypatch):
    import urllib.parse

    from api import routes

    stream_id = "stream-cancel-pending-run-id"
    captured = {}
    called = {"cancel": False, "stop": False}

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True))
    monkeypatch.setattr("api.gateway_chat.gateway_run_id_pending", lambda _stream_id: True)
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _stream_id: called.__setitem__("stop", True))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    parsed = urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}")
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


def test_chat_cancel_waits_for_pending_gateway_run_id_then_stops_remote_run(monkeypatch):
    import urllib.parse

    from api import routes
    from api.gateway_chat import _STREAM_RUN_IDS

    stream_id = "stream-cancel-pending-wait"
    captured = {}
    called = {"cancel": False, "stop": False, "pending": True}

    def fake_sleep(_seconds):
        _STREAM_RUN_IDS[stream_id] = "run-stop"
        called["pending"] = False

    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: called.__setitem__("cancel", True) or True)
    monkeypatch.setattr("api.gateway_chat.gateway_run_id_pending", lambda _stream_id: called["pending"])
    monkeypatch.setattr("api.gateway_chat.stop_gateway_run", lambda _stream_id: called.__setitem__("stop", True) or True)
    monkeypatch.setattr("time.sleep", fake_sleep)

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
        assert called["stop"] is True
        assert called["cancel"] is True
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)


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
    assert captured["body"] == {"choice": "once", "approval_id": "appr x/y"}
    handler.send_response.assert_called_with(200)

    # Cleanup.
    _STREAM_RUN_IDS.pop("sid-relay", None)
    approvals._pending.pop("sess-relay", None)


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
