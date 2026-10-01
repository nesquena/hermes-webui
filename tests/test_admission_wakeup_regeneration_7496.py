"""Request admission transfer for server wakeup and retained-row regeneration."""
import copy
import threading

import pytest

from api.models import Session
from api.session_ops import plan_regeneration


class _Registered(BaseException):
    """Stop the real worker after registration, before provider I/O."""


def _session():
    rows = [
        {'role': 'user', 'content': 'prompt', 'id': 'u1', '_source': 'webui'},
        {'role': 'assistant', 'content': 'answer'},
    ]
    return Session(session_id='admission-7496-regeneration', messages=copy.deepcopy(rows),
                   context_messages=copy.deepcopy(rows), workspace='/tmp')


def _regenerate(routes, session, reservation, gateway):
    return routes._start_regeneration_stream_locked(
        session, turn=plan_regeneration(session).turn, workspace='/tmp', model='test',
        model_provider=None, normalized_model=False, diag=None, goal_related=False,
        source='webui', moa_config=None, backend_is_gateway=gateway,
        reservation=reservation,
    )


@pytest.mark.parametrize('gateway', [False, True])
def test_wakeup_reservation_handoff_to_concrete_worker(monkeypatch, tmp_path, gateway):
    from api import config, gateway_chat, routes, streaming, turn_journal

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    session = _session()
    monkeypatch.setattr(Session, 'save', lambda self, **kw: None)
    monkeypatch.setattr(routes, 'set_last_workspace', lambda *a, **kw: None)
    monkeypatch.setattr(routes, '_agent_runtime_barrier_response', lambda **kw: None)
    monkeypatch.setattr(turn_journal, 'append_turn_journal_event', lambda *a, **kw: {'turn_id': '7496'})
    # Keep the public wakeup reservation and real chat-stream launch, replacing
    # only model/workspace/credential resolution in its admitted helper.
    monkeypatch.setattr(routes, '_start_session_turn_admitted',
                        lambda sid, msg, *, source, reservation:
                        routes._start_chat_stream_for_session(
                            session, msg=msg, workspace='/tmp', model='test',
                            source=source, external_runtime_owned=gateway,
                            reservation=reservation))
    module = gateway_chat if gateway else streaming
    real_peek = module.peek_stream
    real_register = module.register_active_run
    reached = threading.Event()
    proceed = threading.Event()
    registered = []
    failures = []
    threads = []
    real_thread = threading.Thread

    def peek(stream_id):
        channel = real_peek(stream_id)
        reached.set()
        assert proceed.wait(5)
        return channel

    def register(stream_id, **kwargs):
        real_register(stream_id, **kwargs)
        registered.append((stream_id, config.ACTIVE_RUNS[stream_id]['phase']))
        raise _Registered()

    def worker(*args, **kwargs):
        try:
            (gateway_chat._run_gateway_chat_streaming if gateway else streaming._run_agent_streaming)(*args, **kwargs)
        except _Registered:
            pass
        except BaseException as exc:
            failures.append(exc)

    def thread_factory(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(module, 'peek_stream', peek)
    monkeypatch.setattr(module, 'register_active_run', register)
    monkeypatch.setattr(routes, '_run_gateway_chat_streaming' if gateway else '_run_agent_streaming', worker)
    monkeypatch.setattr(routes.threading, 'Thread', thread_factory)
    result = None
    try:
        result = routes.start_session_turn(session.session_id, 'wake up')
        stream_id = result['stream_id']
        assert reached.wait(5)
        assert config.ACTIVE_RUNS[stream_id]['phase'] == 'starting'
        config.enter_restart_drain('test')
        proceed.set()
        threads[0].join(5)
        assert not threads[0].is_alive()
        assert not failures
        assert registered == [(stream_id, 'gateway-starting' if gateway else 'starting')]
        assert not any(k.startswith('admission:') for k in config.ACTIVE_RUNS)
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
            config.clear_session_writeback_owner_if_owned(session.session_id, stream_id)


@pytest.mark.parametrize('gateway', [False, True])
def test_regeneration_concrete_worker_survives_restart_drain(monkeypatch, tmp_path, gateway):
    from api import config, gateway_chat, routes, streaming, turn_journal

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    session = _session()
    monkeypatch.setattr(Session, 'save', lambda self, **kw: None)
    monkeypatch.setattr(routes, 'set_last_workspace', lambda *a, **kw: None)
    monkeypatch.setattr(turn_journal, 'append_turn_journal_event', lambda *a, **kw: {'turn_id': '7496'})
    module = gateway_chat if gateway else streaming
    real_peek = module.peek_stream
    real_register = module.register_active_run
    reached = threading.Event()
    proceed = threading.Event()
    registered = []
    failures = []
    threads = []
    real_thread = threading.Thread

    def peek(stream_id):
        channel = real_peek(stream_id)
        reached.set()
        assert proceed.wait(5)
        return channel

    def register(stream_id, **kwargs):
        real_register(stream_id, **kwargs)
        registered.append((stream_id, config.ACTIVE_RUNS[stream_id]['phase']))
        raise _Registered()

    def worker(*args, **kwargs):
        try:
            (gateway_chat._run_gateway_chat_streaming if gateway else streaming._run_agent_streaming)(*args, **kwargs)
        except _Registered:
            pass
        except BaseException as exc:
            failures.append(exc)

    def thread_factory(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(module, 'peek_stream', peek)
    monkeypatch.setattr(module, 'register_active_run', register)
    monkeypatch.setattr(routes, '_run_gateway_chat_streaming' if gateway else '_run_agent_streaming', worker)
    monkeypatch.setattr(routes.threading, 'Thread', thread_factory)
    reservation = 'admission:7496-regeneration'
    result = None
    try:
        config.register_active_run(reservation, phase='admitting')
        result = _regenerate(routes, session, reservation, gateway)
        stream_id = result['stream_id']
        config.unregister_active_run(reservation)
        assert reached.wait(5)
        assert config.ACTIVE_RUNS[stream_id]['phase'] == 'starting'
        config.enter_restart_drain('test')
        proceed.set()
        threads[0].join(5)
        assert not threads[0].is_alive()
        assert not failures
        assert registered == [(stream_id, 'gateway-starting' if gateway else 'starting')]
        assert config.ACTIVE_RUNS[stream_id]['backend'] == ('gateway' if gateway else 'legacy')
        assert not any(k.startswith('admission:') for k in config.ACTIVE_RUNS)
    finally:
        proceed.set()
        for thread in threads:
            thread.join(5)
        config.exit_restart_drain()
        config.unregister_active_run(reservation)
        if result and 'stream_id' in result:
            stream_id = result['stream_id']
            config.unregister_active_run(stream_id)
            with config.STREAMS_LOCK:
                config.STREAMS.pop(stream_id, None)
                config.CANCEL_FLAGS.pop(stream_id, None)
            config.unregister_stream_owner(stream_id)
            config.clear_session_writeback_owner_if_owned(session.session_id, stream_id)


@pytest.mark.parametrize('gateway', [False, True])
@pytest.mark.parametrize('failure', ['thread', 'save'])
def test_regeneration_launch_failure_compensates_every_owner(monkeypatch, tmp_path, gateway, failure):
    from api import config, gateway_chat, routes, turn_journal

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    session = _session()
    before = copy.deepcopy(session.__dict__)
    saves = []
    events = []
    def save(self, **kw):
        saves.append((copy.deepcopy(self.__dict__), kw))
        if failure == 'save' and len(saves) == 1:
            raise RuntimeError('save failed')

    monkeypatch.setattr(Session, 'save', save)
    monkeypatch.setattr(turn_journal, 'append_turn_journal_event',
                        lambda sid, event: events.append(event) or {'turn_id': '7496'})
    stream_ids = []
    real_transfer = config.transfer_run_admission

    def transfer(reservation, stream_id, **kwargs):
        stream_ids.append(stream_id)
        return real_transfer(reservation, stream_id, **kwargs)

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError('thread start failed')

    monkeypatch.setattr(config, 'transfer_run_admission', transfer)
    if failure == 'thread':
        monkeypatch.setattr(routes.threading, 'Thread', FailingThread)
    else:
        # The real gated thread must be released and joined on rollback.
        monkeypatch.setattr(routes, '_run_agent_streaming', lambda *a, **kw: pytest.fail('aborted worker ran'))
        monkeypatch.setattr(routes, '_run_gateway_chat_streaming', lambda *a, **kw: pytest.fail('aborted worker ran'))
    reservation = 'admission:7496-regen-failure'
    config.register_active_run(reservation, phase='admitting')
    try:
        with pytest.raises(RuntimeError, match='thread start failed' if failure == 'thread' else 'save failed'):
            _regenerate(routes, session, reservation, gateway)
        config.unregister_active_run(reservation)
        assert session.__dict__ == before
        assert len(stream_ids) == 1
        stream_id = stream_ids[0]
        assert stream_id not in config.ACTIVE_RUNS
        assert stream_id not in config.STREAMS
        assert stream_id not in config.STREAM_SESSION_OWNERS
        assert config.session_writeback_owner(session.session_id) != stream_id
        if gateway:
            assert stream_id not in gateway_chat._STREAM_RUN_LIFECYCLE
        assert [e['event'] for e in events] == ['submitted', 'interrupted']
        assert events[-1]['reason'] == 'start_compensated'
        if failure == 'save':
            assert saves[-1] == (before, {'touch_updated_at': False})
    finally:
        config.unregister_active_run(reservation)
        for stream_id in stream_ids:
            config.unregister_active_run(stream_id)
            with config.STREAMS_LOCK:
                config.STREAMS.pop(stream_id, None)
            config.unregister_stream_owner(stream_id)
            config.clear_session_writeback_owner_if_owned(session.session_id, stream_id)
