"""Admission must close before any pending mutation or restart side effect."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import config, routes, updates, gateway_restart


@pytest.fixture(autouse=True)
def isolated_drain(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    yield
    config.exit_restart_drain()



_run_guard_for_test = routes._run_admission_guard()

def test_admission_handoff_keeps_concrete_run_registered():
    stream_id = "handoff-stream"

    @_run_guard_for_test
    def start():
        config.register_active_run(stream_id, session_id="session-1", phase="starting")
        return {"stream_id": stream_id}

    try:
        result = start()
        assert result["stream_id"] == stream_id
        assert config.ACTIVE_RUNS[stream_id]["phase"] == "starting"
        assert not any(key.startswith("admission:") for key in config.ACTIVE_RUNS)
    finally:
        config.unregister_active_run(stream_id)

def test_pending_start_refused_before_any_session_access(monkeypatch):
    config.enter_restart_drain()
    result = routes._start_chat_stream_for_session(
        SimpleNamespace(), msg='hello', workspace='/tmp', model='', external_runtime_owned=False)
    assert result['_status'] == 503
    assert result['code'] == 'restart_draining'


def test_sync_refused_before_session_lookup(monkeypatch):
    config.enter_restart_drain()
    monkeypatch.setattr(routes, 'get_session', Mock(side_effect=AssertionError('session lookup')))
    monkeypatch.setattr(routes, 'j', lambda h, payload, status=200: (payload, status))
    result, status = routes._handle_chat_sync(None, {'session_id': 'x', 'message': 'hi'})
    assert status == 503
    assert result['code'] == 'restart_draining'


def test_gateway_restart_waits_under_drain_and_aborts_blocked(monkeypatch):
    observed = []
    def wait():
        observed.append(config.restart_drain_active())
        return {'restart_blocked': True, 'wait_timed_out': True}
    monkeypatch.setattr(gateway_restart, '_wait_until_restart_safe', wait)
    spawn = Mock(side_effect=AssertionError('must not restart'))
    monkeypatch.setattr(gateway_restart.subprocess, 'Popen', spawn)
    outcome = gateway_restart.restart_active_profile_gateway()
    assert outcome['status'] == 'failed'
    assert observed == [True]
    spawn.assert_not_called()
    assert not config.restart_drain_active()


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_gateway_restart_interrupted_preflight_releases_admission(monkeypatch, interruption):
    def wait():
        assert config.restart_drain_active()
        raise interruption('preflight interrupted')

    monkeypatch.setattr(gateway_restart, '_wait_until_restart_safe', wait)
    spawn = Mock(side_effect=AssertionError('must not restart'))
    monkeypatch.setattr(gateway_restart.subprocess, 'Popen', spawn)
    try:
        with pytest.raises(interruption):
            gateway_restart.restart_active_profile_gateway()
        assert not config.restart_drain_active()
        assert not gateway_restart._GATEWAY_RESTART_LOCK.locked()
        config.register_active_run('after-interrupted-preflight', session_id='after')
    finally:
        config.unregister_active_run('after-interrupted-preflight')
        config.exit_restart_drain()
        if gateway_restart._GATEWAY_RESTART_LOCK.locked():
            gateway_restart._GATEWAY_RESTART_LOCK.release()
    spawn.assert_not_called()


def test_agent_update_does_not_accept_pending_gateway_restart(monkeypatch):
    monkeypatch.setattr(updates, 'restart_active_profile_gateway',
                        lambda **kwargs: {'status': 'in_progress'})
    monkeypatch.setattr('api.agent_health.get_active_profile_gateway_running_pid', lambda **kwargs: None)
    ok, result = updates._ensure_gateway_restart_for_agent_update()
    assert not ok
    assert result['status'] == 'in_progress'


def test_shutdown_waits_under_drain_and_aborts_blocked(monkeypatch):
    observed = []
    def wait():
        observed.append(config.restart_drain_active())
        return {'restart_blocked': True}
    monkeypatch.setattr(updates, '_wait_until_restart_safe', wait)
    monkeypatch.setattr(routes, 'j', lambda *a, **k: True)
    kill = Mock()
    monkeypatch.setattr(routes.os, 'kill', kill)
    threads = []
    real_thread = routes.threading.Thread
    def thread(*a, **k):
        obj = real_thread(*a, **k)
        threads.append(obj)
        return obj
    monkeypatch.setattr(routes.threading, 'Thread', thread)
    routes._handle_shutdown(SimpleNamespace(headers={}))
    for worker in threads:
        worker.join(3)
        assert not worker.is_alive()
    assert observed == [True]
    kill.assert_not_called()
    assert not config.restart_drain_active()
