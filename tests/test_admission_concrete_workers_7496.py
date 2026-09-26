"""Concrete worker registration retains transferred admission during drain."""
import threading
from types import SimpleNamespace

import pytest


class _RegistrationObserved(BaseException):
    """Stop a worker just after its actual registration, before provider I/O."""


@pytest.mark.parametrize('producer', ['btw', 'background'])
def test_hidden_producer_real_worker_upgrades_during_drain(monkeypatch, tmp_path, producer):
    from api import background, config, models, routes, streaming, turn_journal

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    parent = SimpleNamespace(session_id='7496-concrete-parent-' + producer,
                             workspace='/tmp', model='test', model_provider=None,
                             profile=None, messages=[], active_stream_id=None)
    child = SimpleNamespace(session_id=parent.session_id + '-child',
                            active_stream_id=None, messages=[], save=lambda: None)
    monkeypatch.setattr(routes, 'get_session', lambda sid: parent)
    monkeypatch.setattr(models, 'new_session', lambda **kw: child)
    monkeypatch.setattr(routes, '_agent_runtime_barrier_response', lambda **kw: None)
    monkeypatch.setattr(routes, 'j', lambda handler, payload, status=200: payload)
    monkeypatch.setattr(turn_journal, 'append_turn_journal_event', lambda *a, **kw: {})
    background.cleanup_btw(parent.session_id)
    seen = threading.Event()
    proceed = threading.Event()
    registered = []
    failures = []
    threads = []
    real_peek = streaming.peek_stream
    real_register = streaming.register_active_run
    real_thread = threading.Thread

    def peek(stream_id):
        channel = real_peek(stream_id)
        seen.set()
        assert proceed.wait(5)
        return channel

    def register(stream_id, **kwargs):
        real_register(stream_id, **kwargs)
        registered.append((stream_id, config.ACTIVE_RUNS[stream_id]['phase']))
        raise _RegistrationObserved()

    def run(*args, **kwargs):
        try:
            streaming._run_agent_streaming(*args, **kwargs)
        except _RegistrationObserved:
            pass
        except BaseException as exc:
            failures.append(exc)

    def thread_factory(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(streaming, 'peek_stream', peek)
    monkeypatch.setattr(streaming, 'register_active_run', register)
    monkeypatch.setattr(routes, '_run_agent_streaming', run)
    monkeypatch.setattr(routes.threading, 'Thread', thread_factory)
    result = None
    try:
        result = (routes._handle_btw(object(), {'session_id': parent.session_id, 'question': 'why'})
                  if producer == 'btw' else
                  routes._handle_background(object(), {'session_id': parent.session_id, 'prompt': 'why'}))
        assert seen.wait(5)
        stream_id = result['stream_id']
        assert config.ACTIVE_RUNS[stream_id]['phase'] == 'starting'
        config.enter_restart_drain('test')
        proceed.set()
        threads[0].join(5)
        assert not threads[0].is_alive()
        assert not failures
        assert registered == [(stream_id, 'starting')]
        assert not any(k.startswith('admission:') for k in config.ACTIVE_RUNS)
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run('7496-unreserved-' + producer, session_id=child.session_id)
    finally:
        proceed.set()
        for thread in threads:
            thread.join(5)
        config.exit_restart_drain()
        if result and 'stream_id' in result:
            stream_id = result['stream_id']
            config.unregister_active_run(stream_id)
            with config.STREAMS_LOCK:
                config.STREAMS.pop(stream_id, None)
                config.CANCEL_FLAGS.pop(stream_id, None)
            config.unregister_stream_owner(stream_id)
            config.clear_session_writeback_owner_if_owned(child.session_id, stream_id)
            if producer == 'btw':
                background.cleanup_btw(parent.session_id, stream_id=stream_id)
            else:
                background.discard_background(parent.session_id, result['task_id'])


def test_gateway_real_worker_upgrades_transferred_row_after_drain(monkeypatch, tmp_path):
    from api import config, gateway_chat

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-gateway-concrete'
    stream_id = '7496-gateway-concrete'
    session_id = '7496-gateway-session'
    seen = threading.Event()
    proceed = threading.Event()
    registered = []
    failures = []
    real_peek = gateway_chat.peek_stream
    real_register = gateway_chat.register_active_run

    def peek(key):
        channel = real_peek(key)
        seen.set()
        assert proceed.wait(5)
        return channel

    def register(key, **kwargs):
        real_register(key, **kwargs)
        registered.append((key, config.ACTIVE_RUNS[key]['phase']))
        raise _RegistrationObserved()

    monkeypatch.setattr(gateway_chat, 'peek_stream', peek)
    monkeypatch.setattr(gateway_chat, 'register_active_run', register)
    config.register_active_run(reservation, phase='admitting')
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = config.create_stream_channel()
        config.transfer_run_admission(reservation, stream_id, session_id=session_id)
    config.unregister_active_run(reservation)

    def run():
        try:
            gateway_chat._run_gateway_chat_streaming(session_id, 'hello', 'test', '/tmp', stream_id)
        except _RegistrationObserved:
            pass
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert seen.wait(5)
        config.enter_restart_drain('test')
        proceed.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not failures
        assert registered == [(stream_id, 'gateway-starting')]
        assert config.ACTIVE_RUNS[stream_id]['backend'] == 'gateway'
    finally:
        proceed.set()
        thread.join(5)
        config.exit_restart_drain()
        config.unregister_active_run(stream_id)
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)
        config.unregister_stream_owner(stream_id)
