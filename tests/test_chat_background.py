"""POST /api/chat/background: hand a running foreground command to the background.

A foreground terminal call parks the agent loop until it exits, so a long build
or a sleep freezes the chat. The endpoint asks the live agent to redirect: while
tools execute, ``AIAgent.redirect()`` steers the model and flags every tool
worker thread, so the terminal tool adopts its live process into the background
registry (``tools.interrupt.request_yield``) instead of killing it.

These tests cover the gates the frontend branches on and the ownership fence the
endpoint shares with Stop. A cached Agent is only a fallback for a live, owned
stream; it is never evidence that a turn is running. The adoption itself lives
in hermes-agent and is covered by ``tests/tools/test_terminal_yield_to_background.py``.
"""
import queue
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from api import config, streaming
from tests.test_real_steer import _captured_response, _make_handler
from tests.test_steer_cancel_ordering import ObservedLock


@pytest.fixture
def registries(monkeypatch):
    """Empty STREAMS / ACTIVE_RUNS / AGENT_INSTANCES / cache per test, restored after."""
    from api.config import ACTIVE_RUNS_LOCK, SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK

    with SESSION_AGENT_CACHE_LOCK:
        cache_snapshot = dict(config.SESSION_AGENT_CACHE)
        config.SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snapshot = dict(config.STREAMS)
        workers_snapshot = dict(config.AGENT_INSTANCES)
        config.STREAMS.clear()
        config.AGENT_INSTANCES.clear()
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
        config.AGENT_INSTANCES.clear()
        config.AGENT_INSTANCES.update(workers_snapshot)
    with ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update(runs_snapshot)


def _agent(*, executing: bool = True, accepted: bool = True):
    agent = type("Agent", (), {})()
    agent._executing_tools = executing
    agent.calls = []

    def redirect(text):
        agent.calls.append(text)
        return accepted

    agent.redirect = redirect
    return agent


def _bind_session(monkeypatch, stream_id):
    class _Session:
        active_stream_id = stream_id

    monkeypatch.setattr(streaming, "get_session", lambda _sid: _Session())


def _bind_local_run(monkeypatch, sid: str, stream_id: str, agent, *,
                    phase: str = "running", backend: str = "legacy", worker: bool = True):
    config.STREAMS[stream_id] = queue.Queue()
    if worker:
        config.AGENT_INSTANCES[stream_id] = agent
    config.ACTIVE_RUNS[stream_id] = {"session_id": sid, "backend": backend, "phase": phase}
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {stream_id: sid})
    _bind_session(monkeypatch, stream_id)


def _detach_after_stop(stream_id: str):
    """What cancel_stream leaves behind: registries dropped, session pointer stale."""
    config.STREAMS.pop(stream_id, None)
    config.AGENT_INSTANCES.pop(stream_id, None)
    config.ACTIVE_RUNS.pop(stream_id, None)
    config.STREAM_SESSION_OWNERS.pop(stream_id, None)


def _post(sid: str | None):
    handler = _make_handler()
    body = {} if sid is None else {"session_id": sid}
    streaming._handle_chat_background(handler, body)
    return _captured_response(handler)


def test_redirects_a_run_that_is_executing_tools(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("sid") == {"accepted": True, "reason": None, "stream_id": "stream"}
    assert agent.calls == [streaming.BACKGROUND_RUN_NOTE]


def test_refuses_when_no_tool_is_running(registries, monkeypatch):
    agent = _agent(executing=False)
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    body = _post("sid")

    assert body == {"accepted": False, "reason": "no_tool_running", "stream_id": "stream"}
    assert agent.calls == []


def test_refuses_a_gateway_owned_run(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent, backend="gateway")

    assert _post("sid") == {"accepted": False, "reason": "gateway_run", "stream_id": "stream"}
    assert agent.calls == []


def test_refuses_a_stream_owned_by_another_session(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("other")["reason"] == "stream_dead"
    assert agent.calls == []


def test_refuses_a_cancelling_run(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent, phase="cancelling")

    assert _post("sid")["reason"] == "stream_dead"
    assert agent.calls == []


def test_accepts_cached_agent_behind_a_live_owned_stream(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent, worker=False)
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")

    assert _post("sid") == {"accepted": True, "reason": None, "stream_id": "stream"}
    assert agent.calls == [streaming.BACKGROUND_RUN_NOTE]


def test_refuses_cached_agent_after_stop_detached_the_stream(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent, worker=False)
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")
    _detach_after_stop("stream")

    assert _post("sid") == {"accepted": False, "reason": "stream_dead", "stream_id": None}
    assert agent.calls == []


def test_refuses_cached_agent_when_no_stream_is_active(registries, monkeypatch):
    agent = _agent()
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")
    _bind_session(monkeypatch, None)

    assert _post("sid") == {"accepted": False, "reason": "not_running", "stream_id": None}
    assert agent.calls == []


def test_refuses_missing_session(registries, monkeypatch):
    agent = _agent()
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")

    def _missing(_sid):
        raise KeyError(_sid)

    monkeypatch.setattr(streaming, "get_session", _missing)

    assert _post("sid") == {"accepted": False, "reason": "session_not_found", "stream_id": None}
    assert agent.calls == []


def test_requires_a_session_id(registries):
    assert "session_id" in str(_post(None))


def test_redirect_is_delivered_under_the_stream_edge(registries, monkeypatch):
    edge = ObservedLock()
    monkeypatch.setattr(config, "STREAMS_LOCK", edge)
    agent = _agent()
    observed = []

    def redirect(text):
        observed.append((edge.owner == threading.get_ident(), "stream" in config.STREAMS))
        return True

    agent.redirect = redirect
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("sid")["accepted"] is True
    assert observed == [(True, True)]


@pytest.fixture
def cancel_scene(monkeypatch):
    """Registries a real Stop mutates, patched on both config and streaming."""
    edge = ObservedLock()
    agent = type("Agent", (), {})()
    agent.session_id = "sid"
    agent._executing_tools = True
    agent.redirected = []
    agent.interrupted = []
    agent.redirect = lambda text: agent.redirected.append(text) or True
    agent.interrupt = lambda reason: agent.interrupted.append(reason)
    maps = {
        "STREAMS": {"run": queue.Queue()},
        "CANCEL_FLAGS": {"run": threading.Event()},
        "AGENT_INSTANCES": {"run": agent},
        "STREAM_PARTIAL_TEXT": {"run": ""},
        "STREAM_REASONING_TEXT": {"run": ""},
        "STREAM_LIVE_TOOL_CALLS": {"run": []},
        "STREAM_SESSION_OWNERS": {"run": "sid"},
        "ACTIVE_RUNS": {"run": {"session_id": "sid", "backend": "legacy", "phase": "running"}},
        "SESSION_AGENT_CACHE": {"sid": (agent, "sig")},
    }
    for name, value in maps.items():
        monkeypatch.setattr(config, name, value)
        if hasattr(streaming, name):
            monkeypatch.setattr(streaming, name, value)
    monkeypatch.setattr(config, "STREAMS_LOCK", edge)
    monkeypatch.setattr(streaming, "STREAMS_LOCK", edge)
    monkeypatch.setattr(streaming, "get_session",
                        lambda _sid: SimpleNamespace(active_stream_id="run"))
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    monkeypatch.setattr(streaming, "_stream_writeback_is_current", lambda *args: False)
    from api import clarify
    monkeypatch.setattr(clarify, "clear_pending", lambda _sid: None)
    return agent


def test_refuses_after_real_stop_cancelled_the_run(cancel_scene):
    """A Stop detaches the stream; the cached Agent it leaves behind must never yield."""
    assert streaming.cancel_stream("run") is True

    body = _post("sid")

    assert body["accepted"] is False
    assert body == {"accepted": False, "reason": "stream_dead", "stream_id": None}
    assert cancel_scene.redirected == []

