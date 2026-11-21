"""POST /api/chat/skip-tool: skip the tool call the turn is blocked on, without stopping it.

A blocking tool call (a long ``wait``, a foreground command the user no longer wants)
parks the agent loop: the chat looks frozen while nothing reaches the model. Skipping
answers the model with a skipped result and keeps the turn running, so this is NOT the
Stop edge and NOT the background hand-over.

These tests cover the gates the frontend branches on and the ownership fence the
endpoint shares with Stop. The executor behaviour itself lives in hermes-agent
(``tests/agent/test_tool_skip.py``).
"""
import queue
import threading

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


def _agent(*, accepted: bool = True, supports_skip: bool = True):
    agent = type("Agent", (), {})()
    agent.calls = 0
    if supports_skip:
        def skip_current_tool():
            agent.calls += 1
            return accepted

        agent.skip_current_tool = skip_current_tool
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
    config.STREAMS.pop(stream_id, None)
    config.AGENT_INSTANCES.pop(stream_id, None)
    config.ACTIVE_RUNS.pop(stream_id, None)
    config.STREAM_SESSION_OWNERS.pop(stream_id, None)


def _post(sid: str | None):
    handler = _make_handler()
    body = {} if sid is None else {"session_id": sid}
    streaming._handle_chat_skip_tool(handler, body)
    return _captured_response(handler)


def test_skips_the_tool_call_of_a_running_turn(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("sid") == {"accepted": True, "reason": None, "stream_id": "stream"}
    assert agent.calls == 1


def test_refuses_when_no_tool_is_running(registries, monkeypatch):
    agent = _agent(accepted=False)
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("sid") == {"accepted": False, "reason": "no_tool_running", "stream_id": "stream"}
    assert agent.calls == 1


def test_refuses_an_agent_without_skip_support(registries, monkeypatch):
    agent = _agent(supports_skip=False)
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("sid") == {"accepted": False, "reason": "skip_unsupported", "stream_id": None}


def test_refuses_a_gateway_owned_run(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent, backend="gateway")

    assert _post("sid") == {"accepted": False, "reason": "gateway_run", "stream_id": "stream"}
    assert agent.calls == 0


def test_refuses_a_stream_owned_by_another_session(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("other")["reason"] == "stream_dead"
    assert agent.calls == 0


def test_refuses_after_stop_detached_the_stream(registries, monkeypatch):
    agent = _agent()
    _bind_local_run(monkeypatch, "sid", "stream", agent, worker=False)
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")
    _detach_after_stop("stream")

    assert _post("sid") == {"accepted": False, "reason": "stream_dead", "stream_id": None}
    assert agent.calls == 0


def test_refuses_when_no_stream_is_active(registries, monkeypatch):
    agent = _agent()
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")
    _bind_session(monkeypatch, None)

    assert _post("sid") == {"accepted": False, "reason": "not_running", "stream_id": None}
    assert agent.calls == 0


def test_refuses_missing_session(registries, monkeypatch):
    agent = _agent()
    config.SESSION_AGENT_CACHE["sid"] = (agent, "sig")

    def _missing(_sid):
        raise KeyError(_sid)

    monkeypatch.setattr(streaming, "get_session", _missing)

    assert _post("sid") == {"accepted": False, "reason": "session_not_found", "stream_id": None}
    assert agent.calls == 0


def test_requires_a_session_id(registries):
    assert "session_id" in str(_post(None))


def test_skip_is_delivered_under_the_stream_edge(registries, monkeypatch):
    edge = ObservedLock()
    monkeypatch.setattr(config, "STREAMS_LOCK", edge)
    agent = _agent()
    observed = []

    def skip_current_tool():
        observed.append((edge.owner == threading.get_ident(), "stream" in config.STREAMS))
        return True

    agent.skip_current_tool = skip_current_tool
    _bind_local_run(monkeypatch, "sid", "stream", agent)

    assert _post("sid")["accepted"] is True
    assert observed == [(True, True)]
