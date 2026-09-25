"""Stop and Steer must share a deterministic stream-ownership edge."""
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import config, streaming
from tests.test_real_steer import _captured_response, _make_handler


class ObservedLock:
    """Report a contender before acquisition, without timing-based guesses."""

    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self.contender = threading.Event()
        # Optional unlocked hooks: before acquisition and after release.
        self.before_acquire = None
        self.after_release = None

    def __enter__(self):
        if self.before_acquire is not None:
            self.before_acquire()
        if self.owner is not None and self.owner != threading.get_ident():
            self.contender.set()
        assert self.lock.acquire(timeout=5), "stream lock acquisition timed out"
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *args):
        self.owner = None
        self.lock.release()
        if self.after_release is not None:
            self.after_release()


@pytest.fixture
def scene(monkeypatch):
    lock = ObservedLock()
    agent = Mock(session_id="original")
    agent.steer.return_value = True
    maps = {
        "STREAMS": {"run": queue.Queue()},
        "CANCEL_FLAGS": {"run": threading.Event()},
        "AGENT_INSTANCES": {"run": agent},
        "STREAM_PARTIAL_TEXT": {"run": "partial answer"},
        "STREAM_REASONING_TEXT": {"run": "partial reasoning"},
        "STREAM_LIVE_TOOL_CALLS": {"run": [{"name": "example"}]},
        "STREAM_SESSION_OWNERS": {"run": "original"},
        "ACTIVE_RUNS": {"run": {"session_id": "original", "backend": "legacy", "phase": "running"}},
        "SESSION_AGENT_CACHE": {"original": (agent, "sig")},
    }
    for name, value in maps.items():
        monkeypatch.setattr(config, name, value)
        if hasattr(streaming, name):
            monkeypatch.setattr(streaming, name, value)
    monkeypatch.setattr(config, "STREAMS_LOCK", lock)
    monkeypatch.setattr(streaming, "STREAMS_LOCK", lock)
    monkeypatch.setattr(streaming, "get_session", lambda sid: SimpleNamespace(active_stream_id="run", messages=[]))
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda sid: nullcontext())
    # Persistence is outside this ordering test; do not touch real sessions.
    monkeypatch.setattr(streaming, "_stream_writeback_is_current", lambda *args: False)
    from api import clarify
    monkeypatch.setattr(clarify, "clear_pending", lambda sid: None)
    return lock, agent


def steer():
    handler = _make_handler()
    streaming._handle_chat_steer(handler, {"session_id": "original", "text": "guidance"})
    return _captured_response(handler)


@pytest.mark.parametrize("registered", [True, False])
def test_cancel_snapshot_claims_stream_before_later_steer(scene, monkeypatch, registered):
    """Pause at the exact old snapshot -> cancellation publication gap."""
    lock, agent = scene
    if not registered:
        config.AGENT_INSTANCES.clear()  # Matching-cache compatibility path.
    reached = threading.Event()
    release = threading.Event()
    update = streaming.update_active_run
    held_at_publication = []

    def publish(*args, **kwargs):
        held_at_publication.append(lock.owner == threading.get_ident())
        reached.set()
        assert release.wait(5), "cancel publication barrier timed out"
        return update(*args, **kwargs)

    monkeypatch.setattr(streaming, "update_active_run", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel = pool.submit(streaming.cancel_stream, "run")
        try:
            assert reached.wait(5)
            guidance = pool.submit(steer)
            # On the old code there is no held lock: force Steer to complete
            # within the publication gap, proving the original accepted=True.
            if held_at_publication == [False]:
                assert guidance.result(timeout=5)["accepted"] is False
            else:
                assert lock.contender.wait(5)
                assert not guidance.done()
        finally:
            release.set()
        assert cancel.result(timeout=5) == {"cancelled": True, "persistence_failed": False, "stream_id": "run"}
        assert guidance.result(timeout=5) == {"accepted": False, "fallback": "stream_dead", "stream_id": None}
    assert held_at_publication == [True]
    agent.steer.assert_not_called()
    agent.interrupt.assert_called_once()
    assert config.ACTIVE_RUNS["run"]["phase"] == "cancelling"
    assert "run" not in config.STREAMS
    assert "run" not in config.AGENT_INSTANCES
    # Buffers are still owned by worker teardown, not eager cancellation.
    assert config.STREAM_PARTIAL_TEXT["run"] == "partial answer"
    assert config.STREAM_REASONING_TEXT["run"] == "partial reasoning"
    assert config.STREAM_LIVE_TOOL_CALLS["run"] == [{"name": "example"}]


@pytest.mark.parametrize("registered", [True, False])
def test_steer_claimed_first_enqueues_before_cancel(scene, registered):
    lock, agent = scene
    if not registered:
        config.AGENT_INSTANCES.clear()  # Matching-cache compatibility path.
    reached = threading.Event()
    release = threading.Event()
    order = []

    def enqueue(text):
        assert lock.owner == threading.get_ident()
        reached.set()
        assert release.wait(5), "steer enqueue barrier timed out"
        order.append("steer")
        return True

    def interrupt(reason):
        assert lock.owner != threading.get_ident(), "interrupt must be outside registry lock"
        assert config.ACTIVE_RUNS["run"]["phase"] == "cancelling"
        assert "run" not in config.STREAMS
        assert "run" not in config.AGENT_INSTANCES
        order.append("cancel")

    agent.steer.side_effect = enqueue
    agent.interrupt.side_effect = interrupt
    with ThreadPoolExecutor(max_workers=2) as pool:
        guidance = pool.submit(steer)
        try:
            assert reached.wait(5)
            cancel = pool.submit(streaming.cancel_stream, "run")
            assert lock.contender.wait(5)
            assert not cancel.done()
        finally:
            release.set()
        assert guidance.result(timeout=5)["accepted"] is True
        assert cancel.result(timeout=5) == {"cancelled": True, "persistence_failed": False, "stream_id": "run"}
    assert order == ["steer", "cancel"]


@pytest.mark.parametrize("gap", ["before_acquire", "after_release"])
def test_cache_only_steer_is_atomic_with_stop(scene, monkeypatch, gap):
    """Cache-only Steer (no AGENT_INSTANCES entry) must never enqueue after Stop claims.

    Steer selects the matching cached worker, then pauses at an unlocked gap
    around its next stream-lock edge while Stop runs to completion. Exactly one
    complete outcome is allowed: Steer already queued under the stream lock
    before Stop claimed, or Stop claimed first and Steer reports ``stream_dead``
    without ever calling ``agent.steer()`` on the cached worker.
    """
    lock, agent = scene
    config.AGENT_INSTANCES.clear()
    decoy = Mock(session_id="other-session")
    config.SESSION_AGENT_CACHE["other-session"] = (decoy, "sig")
    reached = threading.Event()
    release = threading.Event()
    state = {"steer_ident": None, "selected": None, "paused": False, "enqueue": None}
    order = []

    matches = streaming._cached_agent_matches_session

    def select(candidate, sid):
        result = matches(candidate, sid)
        if threading.get_ident() == state["steer_ident"] and result:
            state["selected"] = candidate
        return result

    def gate(edge):
        # Pause the Steer thread once, at the requested unlocked edge, only
        # after it has selected its cache candidate.
        if (threading.get_ident() != state["steer_ident"]
                or state["selected"] is None or state["paused"] or edge != gap):
            return
        state["paused"] = True
        reached.set()
        assert release.wait(5), "cache-only steer barrier timed out"

    def enqueue(text):
        state["enqueue"] = {
            "held": lock.owner == threading.get_ident(),
            "alive": "run" in config.STREAMS,
            "phase": config.ACTIVE_RUNS["run"]["phase"],
        }
        order.append("steer")
        return True

    def interrupt(reason):
        assert lock.owner != threading.get_ident(), "interrupt must be outside registry lock"
        order.append("cancel")

    def cache_only_steer():
        state["steer_ident"] = threading.get_ident()
        return steer()

    agent.steer.side_effect = enqueue
    agent.interrupt.side_effect = interrupt
    monkeypatch.setattr(streaming, "_cached_agent_matches_session", select)
    lock.before_acquire = lambda: gate("before_acquire")
    lock.after_release = lambda: gate("after_release")
    with ThreadPoolExecutor(max_workers=1) as pool:
        guidance = pool.submit(cache_only_steer)
        try:
            assert reached.wait(5), "steer never reached the requested gap"
            assert lock.owner is None, "steer must not pause while holding the stream lock"
            assert streaming.cancel_stream("run") == {"cancelled": True, "persistence_failed": False, "stream_id": "run"}
        finally:
            release.set()
        result = guidance.result(timeout=5)
    assert state["selected"] is agent, "the selected cache object must be the intended worker"
    decoy.steer.assert_not_called()
    assert config.ACTIVE_RUNS["run"]["phase"] == "cancelling"
    assert "run" not in config.STREAMS
    assert "run" not in config.AGENT_INSTANCES
    agent.interrupt.assert_called_once()
    if order == ["steer", "cancel"]:
        # Steer queued before Stop claimed cancellation, under the stream lock.
        assert result == {"accepted": True, "fallback": None, "stream_id": "run"}
        agent.steer.assert_called_once_with("guidance")
        assert state["enqueue"] == {"held": True, "alive": True, "phase": "running"}
    else:
        # Stop claimed first: the cached worker must not be steered afterwards.
        assert order == ["cancel"]
        assert result == {"accepted": False, "fallback": "stream_dead", "stream_id": None}
        agent.steer.assert_not_called()


def test_cache_only_selection_revalidates_phase_before_enqueue(scene, monkeypatch):
    """Finalization can win after initial resolution but before cache enqueue."""
    lock, agent = scene
    config.AGENT_INSTANCES.clear()
    matches = streaming._cached_agent_matches_session
    selected = []

    def select_then_finalize(candidate, sid):
        matched = matches(candidate, sid)
        if matched:
            selected.append(candidate)
            assert lock.owner != threading.get_ident()
            with config.STREAMS_LOCK:
                streaming.update_active_run("run", phase="finalizing")
        return matched

    monkeypatch.setattr(streaming, "_cached_agent_matches_session", select_then_finalize)
    assert steer() == {"accepted": False, "fallback": "not_running", "stream_id": "run"}
    assert selected == [agent]
    agent.steer.assert_not_called()
