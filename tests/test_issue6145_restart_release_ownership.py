"""Regression coverage for the gateway-restart child ownership contract (#6145).

The restart helper registers the background ``Popen`` so a later reaper tick can
collect the child. That registration is the only handle the process has on a
child whose liveness could not be established, so retiring the entry while the
child may still be running loses the exact case the tracked-ownership design
exists for.

Cases (defects on the reviewed head):

1. all three bounded waits time out and ``poll()`` still reports the child alive
   -> the entry must stay registered, and a later exit must be collected by
   ``reap_stray_restart_processes()``;
2. a guarded ``poll()`` that proves completion retires the entry immediately;
3. ``poll()`` raising is "liveness unknown", not "reaped";
   plus the controls: a normally reaped child is retired at once, and the
   gateway lock is still released on the unknown-liveness path.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from api.gateway_restart import (
    _ACTIVE_RESTART_LOCK,
    _ACTIVE_RESTART_PROCS,
    _GATEWAY_RESTART_LOCK,
    reap_stray_restart_processes,
    restart_active_profile_gateway,
)

_LOCK_WAIT_SECONDS = 5.0


class _FakeProc:
    """Minimal ``Popen`` double with a scripted wait/poll sequence."""

    def __init__(self, waits=("timeout", "timeout", "timeout"), poll_value=None, poll_raises=None):
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self._waits = list(waits)
        self._waits_done = threading.Event()
        self._wait_calls = 0
        self.poll_value = poll_value
        self.poll_raises = poll_raises
        self.terminated = 0
        self.killed = 0

    # --- Popen surface -----------------------------------------------------
    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="hermes gateway restart", timeout=float(timeout or 0.0))

    def wait(self, timeout=None):
        idx = self._wait_calls
        self._wait_calls += 1
        outcome = self._waits[idx] if idx < len(self._waits) else "timeout"
        if outcome == "timeout":
            if idx >= len(self._waits) - 1:
                self._waits_done.set()
            raise subprocess.TimeoutExpired(cmd="hermes gateway restart", timeout=float(timeout or 0.0))
        self.returncode = 0
        self._waits_done.set()
        return 0

    def poll(self):
        if self.poll_raises is not None:
            raise self.poll_raises
        return self.poll_value

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


@pytest.fixture(autouse=True)
def _clean_restart_state():
    with _ACTIVE_RESTART_LOCK:
        _ACTIVE_RESTART_PROCS.clear()
    while _GATEWAY_RESTART_LOCK.locked():
        _GATEWAY_RESTART_LOCK.release()
    yield
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while _GATEWAY_RESTART_LOCK.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    with _ACTIVE_RESTART_LOCK:
        _ACTIVE_RESTART_PROCS.clear()


def _arm(monkeypatch, proc):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("api.gateway_restart._resolve_hermes_command", lambda: "hermes")
    monkeypatch.setattr(
        "api.gateway_restart._gateway_restart_profile_context",
        lambda profile=None: (Path("/tmp/hermes-home"), None),
    )


def _run_restart(proc):
    resp = restart_active_profile_gateway(quick_timeout_seconds=0.01, background_wait_seconds=0.01)
    assert resp["status"] == "in_progress", resp
    # The waiter thread sets its event from inside the final bounded wait, so
    # waiting for the event proves every wait already ran; the lock is released
    # in the same ``finally`` block that decides ownership.
    assert proc._waits_done.wait(_LOCK_WAIT_SECONDS), "waiter thread never reached the last bounded wait"
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while _GATEWAY_RESTART_LOCK.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _GATEWAY_RESTART_LOCK.locked(), "gateway restart lock was not released"
    return resp


def _registered(proc):
    with _ACTIVE_RESTART_LOCK:
        return proc in _ACTIVE_RESTART_PROCS


def test_unknown_liveness_keeps_the_child_registered_until_it_exits(monkeypatch):
    proc = _FakeProc(waits=("timeout", "timeout", "timeout"), poll_value=None)
    _arm(monkeypatch, proc)

    _run_restart(proc)

    assert proc.terminated == 1
    assert proc.killed == 1
    assert _registered(proc), (
        "the restart child was dropped from the registry while poll() still reported it alive, "
        "so no later reaper tick can collect it"
    )
    # A later tick must be able to collect it once it really exits.
    proc.poll_value = 7
    assert reap_stray_restart_processes() == 1
    assert not _registered(proc)


def test_poll_proving_completion_retires_the_entry(monkeypatch):
    proc = _FakeProc(waits=("timeout", "timeout", "timeout"), poll_value=9)
    _arm(monkeypatch, proc)

    _run_restart(proc)

    assert not _registered(proc), "a guarded poll() proved the child exited, so the entry must be retired"


def test_poll_error_is_unknown_liveness_not_reaped(monkeypatch):
    proc = _FakeProc(waits=("timeout", "timeout", "timeout"), poll_raises=OSError("no such child"))
    _arm(monkeypatch, proc)

    _run_restart(proc)

    assert _registered(proc), "a poll() that raised proves nothing about liveness; keep the entry registered"


def test_reaped_child_is_retired_immediately(monkeypatch):
    proc = _FakeProc(waits=("ok",), poll_value=0)
    _arm(monkeypatch, proc)

    _run_restart(proc)

    assert proc.returncode == 0
    assert not _registered(proc), "a child whose return code is known must be retired right away"
