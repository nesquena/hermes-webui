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

    def __enter__(self):
        if self.owner is not None and self.owner != threading.get_ident():
            self.contender.set()
        assert self.lock.acquire(timeout=5), "stream lock acquisition timed out"
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *args):
        self.owner = None
        self.lock.release()


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
        assert cancel.result(timeout=5) is True
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


def test_steer_claimed_first_enqueues_before_cancel(scene):
    lock, agent = scene
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
        assert cancel.result(timeout=5) is True
    assert order == ["steer", "cancel"]
