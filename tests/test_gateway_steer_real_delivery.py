"""Gateway runs get real mid-turn steering via the Runs API.

The gateway's ``POST /v1/runs/{id}/steer`` accepts guidance for a RUNNING run
(applied at the next tool-result boundary). The WebUI previously never called
it and always fell back to the client-side queue; these tests pin the new
contract: attempt real delivery first, queue only as fallback.
"""
import io
import json
import queue
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api import config, gateway_chat, streaming
from tests.test_real_steer import _captured_response, _make_handler


# ── gateway_chat.gateway_run_id_for_stream ──────────────────────────────


def test_gateway_run_id_for_stream_maps_trims_and_misses():
    with gateway_chat._STREAM_RUN_STARTING_CONDITION:
        gateway_chat._STREAM_RUN_IDS["stream-1"] = " run-1 "
    try:
        assert gateway_chat.gateway_run_id_for_stream("stream-1") == "run-1"
    finally:
        with gateway_chat._STREAM_RUN_STARTING_CONDITION:
            gateway_chat._STREAM_RUN_IDS.pop("stream-1", None)
    assert gateway_chat.gateway_run_id_for_stream("missing") is None
    assert gateway_chat.gateway_run_id_for_stream("") is None


# ── gateway_chat.steer_gateway_run (HTTP client) ─────────────────────────


def test_steer_gateway_run_posts_runs_api_with_auth_and_input(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=10):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["auth"] = req.headers.get("Authorization")
        seen["method"] = req.get_method()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            status = 200

            def read(self, n=-1):
                return b"{}"

        return _Resp()

    monkeypatch.setattr(gateway_chat, "gateway_run_endpoint", lambda rid: ("http://gw", "key-1"))
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    out = gateway_chat.steer_gateway_run("run-1", "change course")
    assert out == {"accepted": True, "error": None}
    assert seen["url"] == "http://gw/v1/runs/run-1/steer"
    assert seen["method"] == "POST"
    assert seen["body"] == {"input": "change course"}
    assert seen["auth"] == "Bearer key-1"


def test_steer_gateway_run_409_is_not_accepted(monkeypatch):
    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 409, "not accepting", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(gateway_chat, "gateway_run_endpoint", lambda rid: ("http://gw", None))
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    out = gateway_chat.steer_gateway_run("run-1", "hello")
    assert out == {"accepted": False, "error": "gateway_steer_status_409"}


def test_steer_gateway_run_unreachable_reports_gateway_unreachable(monkeypatch):
    def fake_urlopen(req, timeout=10):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(gateway_chat, "gateway_run_endpoint", lambda rid: ("http://gw", None))
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    out = gateway_chat.steer_gateway_run("run-1", "hello")
    assert out == {"accepted": False, "error": "gateway_unreachable"}


@pytest.mark.parametrize("run_id,text", [("", "hi"), ("run-1", "   "), (None, "hi")])
def test_steer_gateway_run_rejects_empty_input_without_http(run_id, text, monkeypatch):
    def fail_urlopen(req, timeout=10):
        raise AssertionError("HTTP must not be attempted for empty input")

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fail_urlopen)
    assert gateway_chat.steer_gateway_run(run_id, text) == {
        "accepted": False,
        "error": "invalid_request",
    }


# ── streaming._steer_gateway_or_queue (delivery vs queue fallback) ───────


def test_or_queue_delivers_when_gateway_accepts(monkeypatch):
    monkeypatch.setattr(gateway_chat, "gateway_run_id_for_stream", lambda st: "run-9")
    monkeypatch.setattr(
        gateway_chat, "steer_gateway_run",
        lambda rid, text: {"accepted": True, "error": None})
    assert streaming._steer_gateway_or_queue("s1", "stream-1", "hi") == {
        "accepted": True, "fallback": None, "stream_id": "stream-1",
    }


def test_or_queue_queues_when_gateway_refuses(monkeypatch):
    monkeypatch.setattr(gateway_chat, "gateway_run_id_for_stream", lambda st: "run-9")
    monkeypatch.setattr(
        gateway_chat, "steer_gateway_run",
        lambda rid, text: {"accepted": False, "error": "gateway_steer_status_409"})
    assert streaming._steer_gateway_or_queue("s1", "stream-1", "hi") == {
        "accepted": False, "fallback": "gateway_steer_queued", "stream_id": "stream-1",
    }


def test_or_queue_queues_when_unreachable(monkeypatch):
    monkeypatch.setattr(gateway_chat, "gateway_run_id_for_stream", lambda st: "run-9")
    monkeypatch.setattr(
        gateway_chat, "steer_gateway_run",
        lambda rid, text: {"accepted": False, "error": "gateway_unreachable"})
    out = streaming._steer_gateway_or_queue("s1", "stream-1", "hi")
    assert out["accepted"] is False
    assert out["fallback"] == "gateway_steer_queued"


def test_or_queue_queues_without_run_id_and_never_calls_http(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway_chat, "gateway_run_id_for_stream", lambda st: None)
    monkeypatch.setattr(gateway_chat, "steer_gateway_run", lambda *a: calls.append(a))
    out = streaming._steer_gateway_or_queue("s1", "stream-1", "hi")
    assert out == {"accepted": False, "fallback": "gateway_steer_queued",
                   "stream_id": "stream-1"}
    assert not calls


# ── end-to-end through _handle_chat_steer (gateway-owned stream) ─────────


@pytest.fixture
def gateway_scene(monkeypatch):
    monkeypatch.setattr(config, "STREAMS", {"st": queue.Queue()})
    monkeypatch.setattr(config, "AGENT_INSTANCES", {})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {"st": "s1"})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {
        "st": {"session_id": "s1", "phase": "running", "backend": "gateway"},
    })
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    monkeypatch.setattr(streaming, "get_session", lambda sid: SimpleNamespace(active_stream_id="st"))


def test_handler_attempts_real_gateway_delivery(gateway_scene, monkeypatch):
    sent = {}
    monkeypatch.setattr(gateway_chat, "gateway_run_id_for_stream", lambda st: "run-77")
    monkeypatch.setattr(
        gateway_chat, "steer_gateway_run",
        lambda rid, text: sent.update(run=rid, text=text) or {"accepted": True, "error": None})
    h = _make_handler()
    streaming._handle_chat_steer(h, {"session_id": "s1", "text": "steer guidance"})
    assert _captured_response(h) == {"accepted": True, "fallback": None, "stream_id": "st"}
    assert sent == {"run": "run-77", "text": "steer guidance"}


def test_handler_queues_when_gateway_refuses(gateway_scene, monkeypatch):
    monkeypatch.setattr(gateway_chat, "gateway_run_id_for_stream", lambda st: "run-77")
    monkeypatch.setattr(
        gateway_chat, "steer_gateway_run",
        lambda rid, text: {"accepted": False, "error": "gateway_steer_status_409"})
    h = _make_handler()
    streaming._handle_chat_steer(h, {"session_id": "s1", "text": "steer guidance"})
    assert _captured_response(h) == {
        "accepted": False, "fallback": "gateway_steer_queued", "stream_id": "st",
    }


def test_handler_queues_when_run_id_unknown(gateway_scene, monkeypatch):
    """Pre-admission steers (no run id yet) keep the historical queue fallback."""
    calls = []
    monkeypatch.setattr(gateway_chat, "gateway_run_id_for_stream", lambda st: None)
    monkeypatch.setattr(gateway_chat, "steer_gateway_run", lambda *a: calls.append(a))
    h = _make_handler()
    streaming._handle_chat_steer(h, {"session_id": "s1", "text": "steer guidance"})
    assert _captured_response(h) == {
        "accepted": False, "fallback": "gateway_steer_queued", "stream_id": "st",
    }
    assert not calls


def test_handler_local_worker_path_untouched(monkeypatch):
    """A registered local worker still steers in-process; no gateway HTTP."""
    agent = MagicMock()
    agent.steer.return_value = True
    monkeypatch.setattr(config, "STREAMS", {"st": queue.Queue()})
    monkeypatch.setattr(config, "AGENT_INSTANCES", {"st": agent})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {"st": "s1"})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {
        "st": {"session_id": "s1", "phase": "running",
               "backend": streaming.WEBUI_LOCAL_CHAT_BACKEND},
    })
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    monkeypatch.setattr(streaming, "get_session", lambda sid: SimpleNamespace(active_stream_id="st"))

    def fail_urlopen(req, timeout=10):
        raise AssertionError("local steer must not hit the gateway HTTP API")

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fail_urlopen)
    h = _make_handler()
    streaming._handle_chat_steer(h, {"session_id": "s1", "text": "hi"})
    assert _captured_response(h) == {"accepted": True, "fallback": None, "stream_id": "st"}
    agent.steer.assert_called_once_with("hi")
