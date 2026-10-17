"""Tests for gateway restart subprocess tracking and targeted zombie reaping."""
from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock

from api.gateway_restart import (
    _ACTIVE_RESTART_LOCK,
    _ACTIVE_RESTART_PROCS,
    reap_stray_restart_processes,
    restart_active_profile_gateway,
)


def test_reap_stray_restart_processes():
    # Clean active set for test
    with _ACTIVE_RESTART_LOCK:
        _ACTIVE_RESTART_PROCS.clear()

    mock_alive = MagicMock()
    mock_alive.poll.return_value = None

    mock_dead = MagicMock()
    mock_dead.poll.return_value = 0

    with _ACTIVE_RESTART_LOCK:
        _ACTIVE_RESTART_PROCS.add(mock_alive)
        _ACTIVE_RESTART_PROCS.add(mock_dead)

    reaped = reap_stray_restart_processes()

    assert reaped == 1
    with _ACTIVE_RESTART_LOCK:
        assert mock_alive in _ACTIVE_RESTART_PROCS
        assert mock_dead not in _ACTIVE_RESTART_PROCS


def test_reaper_does_not_interfere_with_synchronous_failing_process():
    # Verify that running a failing command returns its actual non-zero code
    # and is not corrupted by any reaper.
    result = subprocess.run(
        ["python", "-c", "import sys; sys.exit(42)"],
        capture_output=True,
    )
    assert result.returncode == 42


def test_gateway_restart_background_registration_and_cleanup(monkeypatch):
    with _ACTIVE_RESTART_LOCK:
        _ACTIVE_RESTART_PROCS.clear()

    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_proc.stdout = None
    fake_proc.stderr = None

    def fake_wait(timeout=None):
        fake_proc.returncode = 0
        return 0

    def fake_communicate(timeout=None):
        raise subprocess.TimeoutExpired(cmd=["hermes", "gateway", "restart"], timeout=0.01)

    fake_proc.communicate = fake_communicate
    fake_proc.wait = fake_wait

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_proc)
    monkeypatch.setattr("api.gateway_restart._resolve_hermes_command", lambda: "hermes")
    monkeypatch.setattr("api.gateway_restart._gateway_restart_profile_context", lambda p: ("/tmp/home", None))

    resp = restart_active_profile_gateway(quick_timeout_seconds=0.01, background_wait_seconds=0.5)
    assert resp["status"] == "in_progress"

    # Allow daemon waiter thread to finish
    time.sleep(0.1)

    with _ACTIVE_RESTART_LOCK:
        assert fake_proc not in _ACTIVE_RESTART_PROCS
