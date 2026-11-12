"""POST /api/chat/background: hand a running foreground command to the background.

A foreground terminal call parks the agent loop until it exits, so a long build
or a sleep freezes the chat. The endpoint asks the live agent to redirect: while
tools execute, ``AIAgent.redirect()`` steers the model and flags every tool
worker thread, so the terminal tool adopts its live process into the background
registry (``tools.interrupt.request_yield``) instead of killing it.

These tests cover the gates the frontend branches on. The adoption itself lives
in hermes-agent and is covered by ``tests/tools/test_terminal_yield_to_background.py``.
"""
import queue

import pytest

from api import config, streaming
from tests.test_real_steer import _captured_response, _make_handler


@pytest.fixture
def registries(monkeypatch):
    """Empty STREAMS / ACTIVE_RUNS / cache per test, restored afterwards."""
    from api.config import ACTIVE_RUNS_LOCK, SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK

    with SESSION_AGENT_CACHE_LOCK:
        cache_snapshot = dict(config.SESSION_AGENT_CACHE)
        config.SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snapshot = dict(config.STREAMS)
        config.STREAMS.clear()
    with ACTIVE_RUNS_LOCK:
        runs_snapshot = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {})
    yield
    with SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()
        config.SESSION_AGENT_CACHE.update(cache_snapshot)
    with STREAMS_LOCK:
        config.STREAMS.clear()
        config.STREAMS.update(streams_snapshot)
    with ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update(runs_snapshot)


def _bind_local_run(monkeypatch, sid: str, stream_id: str, agent, *, phase: str = "running"):
    config.STREAMS[stream_id] = queue.Queue()
    config.AGENT_INSTANCES[stream_id] = agent
    config.ACTIVE_RUNS[stream_id] = {"session_id": sid, "backend": "legacy", "phase": phase}
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {stream_id: sid})

    class _Session:
        active_stream_id = stream_id

    monkeypatch.setattr(streaming, "get_session", lambda _sid: _Session())


def _post(sid: str | None):
    handler = _make_handler()
    body = {} if sid is None else {"session_id": sid}
    streaming._handle_chat_background(handler, body)
    return _captured_response(handler)


def test_redirects_a_run_that_is_executing_tools(registries, monkeypatch):
    agent = type("Agent", (), {})()
    agent._executing_tools = True
    calls = []
    agent.redirect = lambda text: calls.append(text) or True
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("sid") == {"accepted": True, "reason": None, "stream_id": "stream"}
    assert calls == [streaming.BACKGROUND_RUN_NOTE]


def test_refuses_when_no_tool_is_running(registries, monkeypatch):
    agent = type("Agent", (), {})()
    agent._executing_tools = False
    calls = []
    agent.redirect = lambda text: calls.append(text) or True
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    body = _post("sid")

    assert body == {"accepted": False, "reason": "no_tool_running", "stream_id": "stream"}
    assert calls == []


def test_refuses_a_gateway_owned_run(registries, monkeypatch):
    agent = type("Agent", (), {})()
    agent._executing_tools = True
    agent.redirect = lambda text: True
    _bind_local_run(monkeypatch, "sid", "stream", agent)
    config.ACTIVE_RUNS["stream"]["backend"] = "gateway"

    assert _post("sid") == {"accepted": False, "reason": "gateway_run", "stream_id": "stream"}


def test_refuses_a_stream_owned_by_another_session(registries, monkeypatch):
    agent = type("Agent", (), {})()
    agent._executing_tools = True
    agent.redirect = lambda text: True
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("other")["reason"] == "stream_dead"


def test_falls_back_to_the_cached_agent(registries):
    agent = type("Agent", (), {})()
    agent._executing_tools = True
    calls = []
    agent.redirect = lambda text: calls.append(text) or True
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")

    assert _post("sid") == {"accepted": True, "reason": None, "stream_id": None}
    assert calls == [streaming.BACKGROUND_RUN_NOTE]


def test_requires_a_session_id(registries):
    assert "session_id" in str(_post(None))
