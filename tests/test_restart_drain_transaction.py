"""Behavioral regressions for scheduler publication and rollback."""
import threading
from unittest.mock import Mock

import pytest


def test_scheduler_closes_admission_before_thread_starts(monkeypatch, tmp_path):
    from api import config, updates
    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    try:
        updates._schedule_restart(delay=0)
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run('not-admitted')
    finally:
        config.exit_restart_drain()
        config.unregister_active_run('not-admitted')


def test_marker_publication_failure_aborts_scheduler(monkeypatch, tmp_path):
    from api import config, updates
    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    start = Mock()
    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(config.os, 'replace', Mock(side_effect=OSError('disk unavailable')))
    with pytest.raises(OSError):
        updates._schedule_restart(delay=0)
    start.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_blocked_timeout_never_replaces_process(monkeypatch, tmp_path):
    from api import config, updates
    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    monkeypatch.setattr(updates, '_wait_until_restart_safe', lambda: {'restart_blocked': True, 'wait_timed_out': True})
    purge, execute, exit_process = Mock(), Mock(), Mock()
    monkeypatch.setattr(updates, '_purge_agent_pycache', purge)
    monkeypatch.setattr(updates.os, 'execv', execute)
    monkeypatch.setattr(updates, '_windows_restart_exit', exit_process)
    thread = updates._schedule_restart(delay=0)
    thread.join(5)
    assert not thread.is_alive()
    execute.assert_not_called()
    exit_process.assert_not_called()
    purge.assert_not_called()
    assert not config.restart_drain_active()


def test_second_scheduler_cannot_release_first_drain(monkeypatch, tmp_path):
    from api import config, updates
    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    try:
        updates._schedule_restart(delay=0)
        with pytest.raises(config.RunAdmissionDrainingError):
            updates._schedule_restart(delay=0)
        assert config.restart_drain_active()
    finally:
        config.exit_restart_drain()


def test_thread_construction_failure_releases_drain(monkeypatch, tmp_path):
    from api import config, updates
    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    monkeypatch.setattr(threading, 'Thread', Mock(side_effect=RuntimeError('no threads')))
    with pytest.raises(RuntimeError, match='no threads'):
        updates._schedule_restart(delay=0)
    assert not config.restart_drain_active()
