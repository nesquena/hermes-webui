"""Steer must use the active worker, not a compression-sensitive cache key."""
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api import config, streaming
from tests.test_real_steer import _make_handler, _captured_response


@pytest.fixture
def scene(monkeypatch):
    agent = MagicMock()
    agent.session_id = "compressed-child"
    agent.steer.return_value = True
    other = MagicMock()
    other.session_id = "other-session"
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {"original": (agent, "sig")})
    monkeypatch.setattr(config, "STREAMS", {"run": queue.Queue(), "other-run": queue.Queue()})
    monkeypatch.setattr(config, "AGENT_INSTANCES", {"run": agent, "other-run": other})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {"run": "original", "other-run": "other-session"})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {
        "run": {"session_id": "original", "phase": "running", "backend": "legacy"},
        "other-run": {"session_id": "other-session", "phase": "running", "backend": "legacy"},
    })
    sess = SimpleNamespace(active_stream_id="run")
    monkeypatch.setattr(streaming, "get_session", lambda sid: sess)
    return agent, other, sess


def steer():
    h = _make_handler()
    streaming._handle_chat_steer(h, {"session_id": "original", "text": "updated guidance"})
    return _captured_response(h)


@pytest.mark.parametrize("cache", ["rotated", "missing", "different"])
def test_active_worker_accepts_after_compression_without_cache_mutation(scene, cache):
    agent, other, _ = scene
    if cache == "missing":
        config.SESSION_AGENT_CACHE.clear()
    elif cache == "different":
        config.SESSION_AGENT_CACHE["original"] = (other, "sig")
    before = dict(config.SESSION_AGENT_CACHE)
    assert steer() == {"accepted": True, "fallback": None, "stream_id": "run"}
    agent.steer.assert_called_once_with("updated guidance")
    other.steer.assert_not_called()
    agent._session_db.close.assert_not_called()
    other._session_db.close.assert_not_called()
    agent.interrupt.assert_not_called()
    assert config.SESSION_AGENT_CACHE == before


@pytest.mark.parametrize("bad", ["owner", "run-owner", "missing-owner", "missing-run", "cancelling", "dead", "gateway"])
def test_ambiguous_or_inactive_worker_is_never_steered_or_closed(scene, bad):
    agent, other, _ = scene
    # Matching cache must not bypass conflicting active-run ownership.
    agent.session_id = "original"
    if bad == "owner":
        config.STREAM_SESSION_OWNERS["run"] = "other-session"
    elif bad == "run-owner":
        config.ACTIVE_RUNS["run"]["session_id"] = "other-session"
    elif bad == "missing-owner":
        config.STREAM_SESSION_OWNERS.pop("run")
    elif bad == "missing-run":
        config.ACTIVE_RUNS.pop("run")
    elif bad == "cancelling":
        config.ACTIVE_RUNS["run"]["phase"] = "cancelling"
    elif bad == "dead":
        config.STREAMS.pop("run")
    elif bad == "gateway":
        config.ACTIVE_RUNS["run"]["backend"] = "gateway"
    assert steer()["accepted"] is False
    agent.steer.assert_not_called()
    other.steer.assert_not_called()
    agent._session_db.close.assert_not_called()


def test_cache_only_mismatch_does_not_close_an_agent_owned_by_another_run(scene):
    agent, other, _ = scene
    config.AGENT_INSTANCES.pop("run")
    config.SESSION_AGENT_CACHE["original"] = (other, "sig")
    assert steer()["accepted"] is False
    other._session_db.close.assert_not_called()
    other.steer.assert_not_called()


def test_http_response_is_written_after_stream_lock_release(scene, monkeypatch):
    from api import helpers

    def respond(handler, payload):
        assert config.STREAMS_LOCK.acquire(blocking=False)
        config.STREAMS_LOCK.release()
        return payload

    monkeypatch.setattr(helpers, "j", respond)
    result = streaming._handle_chat_steer(None, {"session_id": "original", "text": "hint"})
    assert result["accepted"] is True


@pytest.mark.parametrize("behavior", ["reject", "raise", "unsupported"])
def test_live_worker_reports_real_acceptance(scene, behavior):
    agent, _, _ = scene
    if behavior == "reject":
        agent.steer.return_value = False
    elif behavior == "raise":
        agent.steer.side_effect = RuntimeError("fixture")
    else:
        agent.steer = None
    assert steer()["accepted"] is False
    agent.interrupt.assert_not_called()
