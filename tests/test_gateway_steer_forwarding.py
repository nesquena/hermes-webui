"""Gateway-owned Steer is forwarded to POST /v1/runs/{run_id}/steer.

Before this, every Gateway-backend steer returned ``gateway_steer_queued`` and
the browser queued the text as a new turn instead of steering the live run.
"""
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from api import config, gateway_chat, streaming
from tests.test_real_steer import _captured_response, _make_handler


class _GatewayStub(BaseHTTPRequestHandler):
    status = 200
    body = {"object": "hermes.run.steer", "run_id": "run_1", "accepted": True}
    calls: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        type(self).calls.append((self.path, json.loads(self.rfile.read(length) or b"{}"),
                                 self.headers.get("Authorization")))
        raw = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def gateway(monkeypatch):
    _GatewayStub.calls = []
    _GatewayStub.status = 200
    _GatewayStub.body = {"object": "hermes.run.steer", "run_id": "run_1", "accepted": True}
    srv = HTTPServer(("127.0.0.1", 0), _GatewayStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(gateway_chat, "gateway_run_endpoint", lambda run_id: (base, "k3y"))
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    monkeypatch.setattr(config, "STREAMS", {"run": queue.Queue()})
    monkeypatch.setattr(config, "AGENT_INSTANCES", {})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {"run": "sid"})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {
        "run": {"session_id": "sid", "phase": "running", "backend": "gateway"}})
    monkeypatch.setattr(streaming, "get_session", lambda sid: SimpleNamespace(active_stream_id="run"))
    gateway_chat._mark_gateway_run_starting("run")
    gateway_chat._publish_gateway_run_id("run", "run_1")
    yield _GatewayStub
    gateway_chat._STREAM_RUN_IDS.pop("run", None)
    gateway_chat._STREAM_RUN_LIFECYCLE.pop("run", None)
    srv.shutdown()


def steer():
    h = _make_handler()
    streaming._handle_chat_steer(h, {"session_id": "sid", "text": "go left"})
    return _captured_response(h)


def test_gateway_steer_is_delivered_to_the_live_run(gateway):
    assert steer() == {"accepted": True, "fallback": None, "stream_id": "run"}
    assert gateway.calls == [("/v1/runs/run_1/steer", {"input": "go left"}, "Bearer k3y")]


@pytest.mark.parametrize("status,body", [
    (409, {"error": {"code": "run_not_accepting_steer"}}),
    (200, {"accepted": False}),
    (404, {"error": {"code": "not_found"}}),
])
def test_gateway_refusal_keeps_the_queue_fallback(gateway, status, body):
    gateway.status, gateway.body = status, body
    assert steer() == {"accepted": False, "fallback": "gateway_steer_queued", "stream_id": "run"}
    assert len(gateway.calls) == 1


def test_unknown_run_id_is_not_forwarded(gateway):
    gateway_chat._STREAM_RUN_IDS.pop("run", None)
    gateway_chat._STREAM_RUN_LIFECYCLE.pop("run", None)
    assert steer() == {"accepted": False, "fallback": "gateway_steer_queued", "stream_id": "run"}
    assert gateway.calls == []


def test_accept_that_answers_late_is_uncertain_not_queued(gateway, monkeypatch):
    """The Gateway took the text but replied after the timeout: re-queueing would deliver it twice."""
    release = threading.Event()
    real_post = _GatewayStub.do_POST

    def slow_post(self):
        release.wait(5)
        real_post(self)

    monkeypatch.setattr(_GatewayStub, "do_POST", slow_post)
    monkeypatch.setattr(gateway_chat, "GATEWAY_STEER_TIMEOUT_SECS", 0.3)
    try:
        assert steer() == {"accepted": False, "fallback": "gateway_steer_uncertain", "stream_id": "run"}
    finally:
        release.set()


def test_connect_failure_before_sending_keeps_the_queue_fallback(gateway, monkeypatch):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens: refused before any byte is sent
    monkeypatch.setattr(gateway_chat, "gateway_run_endpoint", lambda run_id: (f"http://127.0.0.1:{port}", ""))
    assert steer() == {"accepted": False, "fallback": "gateway_steer_queued", "stream_id": "run"}


@pytest.mark.parametrize("exc", [ConnectionResetError(104, "reset"), TimeoutError("timed out")])
def test_send_phase_failure_wrapped_in_urlerror_is_uncertain(gateway, monkeypatch, exc):
    """urllib wraps errors raised while WRITING the request in URLError; the Gateway may
    already have the steer, so it must not be re-queued as a new turn."""
    import http.client

    real_request = http.client.HTTPConnection.request

    def request_then_fail(self, *a, **kw):
        real_request(self, *a, **kw)  # the full POST reaches the Gateway ...
        raise exc  # ... then the socket fails before the reply is read

    monkeypatch.setattr(http.client.HTTPConnection, "request", request_then_fail)
    assert steer() == {"accepted": False, "fallback": "gateway_steer_uncertain", "stream_id": "run"}
    import time
    deadline = time.monotonic() + 5
    while not gateway.calls and time.monotonic() < deadline:  # stub thread records asynchronously
        time.sleep(0.01)
    assert gateway.calls[0][1] == {"input": "go left"}


def test_dns_failure_keeps_the_queue_fallback(gateway, monkeypatch):
    monkeypatch.setattr(gateway_chat, "gateway_run_endpoint", lambda run_id: ("http://no-such-host.invalid", ""))
    assert steer() == {"accepted": False, "fallback": "gateway_steer_queued", "stream_id": "run"}


def test_uncertain_steer_keeps_the_draft_in_the_browser():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    js = (root / "static" / "commands.js").read_text(encoding="utf-8")
    fn = js[js.index("async function _trySteer("):js.index("async function cmdTitle(")]
    queue_guard = fn[:fn.index("queueSessionMessage(ownerSid")].rsplit("if(", 1)[1]
    assert "gateway_steer_queued" in queue_guard and "uncertain" not in queue_guard  # never auto-queued
    assert "steer_fail_gateway_steer_uncertain:" in (root / "static" / "i18n.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("event", ["run.completed", "run.failed", "run.cancelled"])
def test_live_relay_replays_unconsumed_gateway_steer(event):
    events = []
    gateway_chat._relay_gateway_pending_steer(
        "sid", {"event": event, "pending_steer": "go left"}, lambda e, d: events.append((e, d)))
    gateway_chat._relay_gateway_pending_steer("sid", {"event": event}, lambda e, d: events.append((e, d)))
    assert events == [("pending_steer_leftover", {"session_id": "sid", "text": "go left"})]
    src = __import__("inspect").getsource(gateway_chat._relay_gateway_run_events)
    block = src[src.index(f'if payload_event == "{event}":'):]
    assert block.split("\n")[1].strip() == "_relay_gateway_pending_steer(session_id, payload, put_gateway_event)"


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_reattach_status_replays_unconsumed_gateway_steer(monkeypatch, state):
    events = []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status",
                        lambda *a: {"status": state, "output": "ok", "pending_steer": "go left"})
    monkeypatch.setattr(gateway_chat, "update_active_run", lambda *a, **k: None)
    monkeypatch.setattr("api.route_approvals.settle_gateway_pending_run", lambda *a, **k: None)
    # The cancelled lane also persists the turn (covered by test_gateway_events_watchdog_7978).
    monkeypatch.setattr(gateway_chat, "_settle_gateway_cancelled_turn", lambda *a, **k: None)
    try:
        gateway_chat._await_gateway_run_result(
            "sid", "rs", "run_9", "http://x", "", put_gateway_event=lambda e, d: events.append((e, d)),
            cancel_event=threading.Event())
    except RuntimeError:
        assert state == "failed"
    finally:
        gateway_chat._STREAM_RUN_IDS.pop("rs", None)
        gateway_chat._STREAM_RUN_LIFECYCLE.pop("rs", None)
    assert events[0] == ("pending_steer_leftover", {"session_id": "sid", "text": "go left"})


def test_uncertain_steer_warning_is_translated_in_every_non_english_locale():
    import re
    from pathlib import Path
    src = (Path(__file__).parents[1] / "static" / "i18n.js").read_text(encoding="utf-8")
    english = "Steer may have reached the agent (no reply from Gateway); your text is kept in the composer"
    locales = {}
    current = None
    for line in src.splitlines():
        m = re.match(r"^  '?([A-Za-z-]+)'?: \{", line)
        if m:
            current = m.group(1)
        m = re.match(r"^\s+steer_fail_gateway_steer_uncertain: '(.*)',$", line)
        if m:
            locales[current] = m.group(1)
    assert locales.pop("en") == english
    assert len(locales) >= 14
    assert {k: v for k, v in locales.items() if v == english} == {}
