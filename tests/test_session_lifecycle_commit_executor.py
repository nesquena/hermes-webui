"""Tests for the background memory-commit submit helper."""
import threading
import time
from unittest.mock import patch

import pytest

import api.session_lifecycle as lifecycle


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    """Clear sessions, captured errors, and background-commit registry between tests."""
    lifecycle._reset_for_tests()
    with lifecycle._MEMORY_COMMIT_ERRORS_LOCK:
        lifecycle._MEMORY_COMMIT_ERRORS.clear()
    with lifecycle._background_commit_threads_lock:
        lifecycle._background_commit_threads.clear()
        lifecycle._draining = False
    yield
    lifecycle._reset_for_tests()
    with lifecycle._MEMORY_COMMIT_ERRORS_LOCK:
        lifecycle._MEMORY_COMMIT_ERRORS.clear()
    with lifecycle._background_commit_threads_lock:
        lifecycle._background_commit_threads.clear()
        lifecycle._draining = False


class _BlockingAgent:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def commit_memory_session(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        if self.should_fail:
            raise RuntimeError("FailingAgent forced failure")


class _FakeAgent:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = 0

    def commit_memory_session(self):
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("FailingAgent forced failure")


def test_submit_commit_session_memory_runs_in_background():
    """submit_commit_session_memory() should not block and should invoke commit."""
    sid = "test_sid"
    agent = _BlockingAgent()
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid)

    lifecycle.submit_commit_session_memory(sid)

    assert agent.entered.wait(timeout=2), "background commit did not start"
    agent.release.set()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with lifecycle._background_commit_threads_lock:
            if not any(t.is_alive() for t in lifecycle._background_commit_threads):
                break
        time.sleep(0.05)

    errors = lifecycle.get_recent_memory_commit_errors()
    assert len(errors) == 0, f"unexpected commit errors: {errors}"
    assert agent.calls == 1
    entry = lifecycle._sessions.get(sid)
    assert entry is not None
    assert entry["committed_generation"] >= entry["generation"]


def test_submit_commit_session_memory_captures_errors():
    """A failing commit must be recorded in the error ring buffer."""
    sid = "test_sid_err"
    agent = _BlockingAgent(should_fail=True)
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid)

    lifecycle.submit_commit_session_memory(sid)

    assert agent.entered.wait(timeout=2), "background commit did not start"
    agent.release.set()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        errors = lifecycle.get_recent_memory_commit_errors()
        if errors:
            break
        time.sleep(0.05)

    errors = lifecycle.get_recent_memory_commit_errors()
    assert len(errors) == 1
    assert errors[0]["session_id"] == sid
    assert "commit_session_memory returned False" in errors[0]["error"]


def test_get_recent_memory_commit_errors_bounded():
    """The error ring must not grow without bound."""
    with lifecycle._MEMORY_COMMIT_ERRORS_LOCK:
        for i in range(20):
            lifecycle._MEMORY_COMMIT_ERRORS.append({"session_id": f"s{i}"})
    recent = lifecycle.get_recent_memory_commit_errors(limit=8)
    assert len(recent) == 8
    assert recent[-1]["session_id"] == "s19"


def test_submit_commit_session_memory_registers_thread():
    """The helper must register the background thread so shutdown can drain it."""
    sid = "test_register"
    agent = _BlockingAgent()
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid)

    lifecycle.submit_commit_session_memory(sid)

    assert agent.entered.wait(timeout=2), "background commit did not start"

    # The thread should be in the registry while the commit is in flight.
    with lifecycle._background_commit_threads_lock:
        assert len(lifecycle._background_commit_threads) >= 1

    agent.release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with lifecycle._background_commit_threads_lock:
            if not any(t.is_alive() for t in lifecycle._background_commit_threads):
                break
        time.sleep(0.05)

    with lifecycle._background_commit_threads_lock:
        assert len(lifecycle._background_commit_threads) == 0
