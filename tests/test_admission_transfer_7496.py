"""Atomic request reservation → concrete worker admission across restart drain."""
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('cancel_before_worker', [False, True])
def test_chat_route_handoff_survives_drain_and_stop(monkeypatch, tmp_path, cancel_before_worker):
    """The real chat ingress transfers its reservation before releasing Thread.start."""
    from api import config, routes, turn_journal

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    session = SimpleNamespace(session_id='7496-route-chat', title='Existing', active_stream_id=None,
                              pending_user_message=None, pending_started_at=None,
                              profile=None, save=lambda: None)
    launched = threading.Event()
    release = threading.Event()
    worker_rows = []
    threads = []
    real_thread = threading.Thread

    def prepare(s, *, stream_id, **kwargs):
        s.active_stream_id = stream_id
        s.pending_user_message = kwargs['msg']
        s.pending_started_at = 1.0
        config.register_session_writeback_owner(s.session_id, stream_id)

    def worker(sid, msg, model, workspace, stream_id, attachments, **kwargs):
        launched.set()
        assert release.wait(5)
        with config.STREAMS_LOCK:
            if stream_id in config.STREAMS:
                config.register_active_run(stream_id, session_id=sid, phase='running')
                worker_rows.append(config.ACTIVE_RUNS[stream_id]['phase'])
            else:
                worker_rows.append('cancelled')

    def thread_factory(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(routes, '_prepare_chat_start_session_for_stream', prepare)
    monkeypatch.setattr(routes, '_run_agent_streaming', worker)
    monkeypatch.setattr(routes, 'set_last_workspace', lambda *a, **k: None)
    monkeypatch.setattr(routes, '_is_hidden_empty_session', lambda s: False)
    monkeypatch.setattr(turn_journal, 'append_turn_journal_event', lambda *a, **k: {})
    monkeypatch.setattr(routes.threading, 'Thread', thread_factory)
    stream_id = None
    try:
        result = routes._start_chat_stream_for_session(
            session, msg='hello', workspace='/tmp', model='test', external_runtime_owned=False)
        stream_id = result['stream_id']
        assert launched.wait(5)
        assert config.ACTIVE_RUNS[stream_id]['phase'] == 'starting'
        assert config.ACTIVE_RUNS[stream_id]['session_id'] == session.session_id
        assert not any(key.startswith('admission:') for key in config.ACTIVE_RUNS)
        config.enter_restart_drain('test')
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run('7496-unreserved-route', session_id=session.session_id)
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run(stream_id, session_id='different-session', phase='running')
        if cancel_before_worker:
            with config.STREAMS_LOCK:
                config.STREAMS.pop(stream_id, None)
                config.update_active_run(stream_id, phase='cancelling')
        release.set()
        threads[0].join(5)
        assert not threads[0].is_alive()
        assert worker_rows == (['cancelled'] if cancel_before_worker else ['running'])
        if cancel_before_worker:
            assert config.ACTIVE_RUNS[stream_id]['phase'] == 'cancelling'
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        config.exit_restart_drain()
        if stream_id:
            config.unregister_active_run(stream_id)
            with config.STREAMS_LOCK:
                config.STREAMS.pop(stream_id, None)
            config.unregister_stream_owner(stream_id)
            config.clear_session_writeback_owner_if_owned(session.session_id, stream_id)


def test_transfer_after_drain_and_worker_upgrade(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-transfer'
    stream_id = '7496-concrete'
    ready = threading.Event()
    proceed = threading.Event()
    outcome = []

    def producer():
        config.register_active_run(reservation, phase='admitting')
        ready.set()
        assert proceed.wait(5)
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = object()
            config.transfer_run_admission(reservation, stream_id, session_id='owner')
        config.unregister_active_run(reservation)
        with config.STREAMS_LOCK:
            config.register_active_run(stream_id, session_id='owner', phase='running')
        outcome.append(config.ACTIVE_RUNS[stream_id]['phase'])

    thread = threading.Thread(target=producer)
    thread.start()
    try:
        assert ready.wait(5)
        config.enter_restart_drain('test')
        proceed.set()
        thread.join(5)
        assert not thread.is_alive()
        assert outcome == ['running']
        assert reservation not in config.ACTIVE_RUNS
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run('7496-unreserved', session_id='owner')
    finally:
        proceed.set()
        thread.join(5)
        config.exit_restart_drain()
        config.unregister_active_run(stream_id)
        config.unregister_active_run(reservation)
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)


def test_transfer_rejects_stolen_or_absent_reservation(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-stolen'
    concrete = '7496-stolen-concrete'
    config.register_active_run(reservation, phase='admitting')
    try:
        config.update_active_run(reservation, phase='running')
        with pytest.raises(config.RunAdmissionDrainingError):
            config.transfer_run_admission(reservation, concrete, session_id='owner')
        assert concrete not in config.ACTIVE_RUNS
        config.unregister_active_run(reservation)
        with pytest.raises(config.RunAdmissionDrainingError):
            config.transfer_run_admission(reservation, concrete, session_id='owner')
    finally:
        config.unregister_active_run(reservation)


def test_drain_rejects_cross_session_worker_upgrade(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-owner'
    concrete = '7496-owner-concrete'
    config.register_active_run(reservation, phase='admitting')
    try:
        config.transfer_run_admission(reservation, concrete, session_id='owner')
        config.enter_restart_drain('test')
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run(concrete, session_id='other', phase='running')
        assert config.ACTIVE_RUNS[concrete]['session_id'] == 'owner'
    finally:
        config.exit_restart_drain()
        config.unregister_active_run(concrete)
        config.unregister_active_run(reservation)


def test_cancelled_concrete_row_cannot_be_resurrected_by_worker(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-cancelled'
    concrete = '7496-cancelled-concrete'
    config.register_active_run(reservation, phase='admitting')
    try:
        config.transfer_run_admission(reservation, concrete, session_id='owner')
        config.update_active_run(concrete, phase='cancelling')
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run(concrete, session_id='owner', phase='running')
        assert config.ACTIVE_RUNS[concrete]['phase'] == 'cancelling'
    finally:
        config.unregister_active_run(concrete)
        config.unregister_active_run(reservation)


def test_gateway_stop_between_lookup_and_registration_releases_prestart_owners(monkeypatch):
    import queue
    from api import config, gateway_chat

    stream_id = '7496-gateway-peek-race'
    events = queue.Queue()
    peeked = threading.Event()
    resume = threading.Event()
    cleanup = []
    original_peek = gateway_chat.peek_stream

    def gated_peek(key):
        result = original_peek(key)
        peeked.set()
        assert resume.wait(5)
        return result

    monkeypatch.setattr(gateway_chat, 'peek_stream', gated_peek)
    monkeypatch.setattr(gateway_chat, '_finish_gateway_run_starting',
                        lambda key, **kw: cleanup.append(('finish', key)))
    monkeypatch.setattr(gateway_chat, '_clear_gateway_run_starting',
                        lambda key: cleanup.append(('clear', key)))
    monkeypatch.setattr(gateway_chat, 'unregister_stream_owner',
                        lambda key: cleanup.append(('owner', key)))
    monkeypatch.setattr(gateway_chat, 'clear_session_writeback_owner_if_owned',
                        lambda sid, key: cleanup.append(('writeback', key)))
    failures = []

    def worker():
        try:
            gateway_chat._run_gateway_chat_streaming('owner', 'hello', 'model',
                                                      '/tmp', stream_id)
        except Exception as exc:
            failures.append(exc)

    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = events
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert peeked.wait(5)
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id)
        resume.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not failures
        assert {kind for kind, key in cleanup if key == stream_id} == {
            'finish', 'clear', 'owner', 'writeback'}
    finally:
        resume.set()
        thread.join(5)
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)
        config.unregister_active_run(stream_id)


@pytest.mark.parametrize('producer', ['chat', 'btw', 'background'])
def test_launch_failure_retires_transferred_admission(monkeypatch, tmp_path, producer):
    from api import config, routes, turn_journal, background

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    session = SimpleNamespace(session_id='7496-launch-' + producer, title='Existing',
                              active_stream_id=None, pending_user_message=None,
                              pending_started_at=None, profile=None, workspace='/tmp',
                              model='test', model_provider=None, messages=[],
                              save=lambda **kw: None)
    monkeypatch.setattr(routes, 'get_session', lambda sid: session)
    monkeypatch.setattr(routes, '_agent_runtime_barrier_response', lambda **kw: None)
    monkeypatch.setattr(routes, 'set_last_workspace', lambda *a, **k: None)
    monkeypatch.setattr(routes, '_is_hidden_empty_session', lambda s: False)
    monkeypatch.setattr(turn_journal, 'append_turn_journal_event', lambda *a, **k: {})
    monkeypatch.setattr(routes, 'j', lambda handler, payload, status=200: payload)
    if producer != 'chat':
        from api import models
        child = SimpleNamespace(**vars(session))
        child.session_id += '-child'
        monkeypatch.setattr(models, 'new_session', lambda **kw: child)
        # Retain the real tracking state to prove a failed launch leaves no phantom task.
        background.cleanup_btw(session.session_id)
        for task in background.get_background_tasks(session.session_id):
            background.discard_background(session.session_id, task['task_id'])
    else:
        def prepare(s, *, stream_id, **kwargs):
            s.active_stream_id = stream_id
            s.pending_user_message = kwargs['msg']
            s.pending_started_at = 1.0
            config.register_session_writeback_owner(s.session_id, stream_id)
        monkeypatch.setattr(routes, '_prepare_chat_start_session_for_stream', prepare)

    class FailedThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            config.enter_restart_drain('test')
            assert any(row.get('phase') == 'starting' for row in config.ACTIVE_RUNS.values())
            raise RuntimeError('thread start failed')

    monkeypatch.setattr(routes.threading, 'Thread', FailedThread)
    try:
        with pytest.raises(RuntimeError, match='thread start failed'):
            if producer == 'chat':
                routes._start_chat_stream_for_session(
                    session, msg='hello', workspace='/tmp', model='test',
                    external_runtime_owned=False)
            elif producer == 'btw':
                routes._handle_btw(object(), {'session_id': session.session_id, 'question': 'hello'})
            else:
                routes._handle_background(object(), {'session_id': session.session_id, 'prompt': 'hello'})
        assert not any(key.startswith('admission:') or row.get('session_id') in
                       {session.session_id, session.session_id + '-child'}
                       for key, row in config.ACTIVE_RUNS.items())
        if producer == 'btw':
            assert background.cleanup_btw(session.session_id) is None
        elif producer == 'background':
            assert background.get_background_tasks(session.session_id) == []
    finally:
        config.exit_restart_drain()
        for key in list(config.ACTIVE_RUNS):
            if key.startswith('admission:') or key.startswith('7496-launch-'):
                config.unregister_active_run(key)


def test_real_worker_stop_route_between_transfer_and_registration(monkeypatch, tmp_path):
    """Stop owns the lock edge even if the actual local worker has only peeked."""
    from urllib.parse import urlsplit
    from api import config, routes, streaming, turn_journal, gateway_chat

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    session = SimpleNamespace(session_id='7496-real-worker', title='Existing',
                              active_stream_id=None, pending_user_message=None,
                              pending_started_at=None, profile=None, save=lambda: None)
    peeked = threading.Event()
    release = threading.Event()
    original_peek = streaming.peek_stream

    def gated_peek(key):
        result = original_peek(key)
        peeked.set()
        assert release.wait(5)
        return result

    def prepare(s, *, stream_id, **kwargs):
        s.active_stream_id = stream_id
        s.pending_user_message = kwargs['msg']
        s.pending_started_at = 1.0
        config.register_session_writeback_owner(s.session_id, stream_id)

    monkeypatch.setattr(streaming, 'peek_stream', gated_peek)
    monkeypatch.setattr(routes, '_prepare_chat_start_session_for_stream', prepare)
    monkeypatch.setattr(routes, 'set_last_workspace', lambda *a, **k: None)
    monkeypatch.setattr(routes, '_is_hidden_empty_session', lambda s: False)
    monkeypatch.setattr(turn_journal, 'append_turn_journal_event', lambda *a, **k: {})
    monkeypatch.setattr(routes, '_stream_id_visible_to_request_profile', lambda *a: True)
    monkeypatch.setattr(gateway_chat, 'wait_for_gateway_run_id', lambda *a: (False, None))
    monkeypatch.setattr(routes, 'get_session', lambda sid: session)
    threads = []
    original_thread = threading.Thread

    def thread_factory(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(routes.threading, 'Thread', thread_factory)
    stream_id = None
    try:
        result = routes._start_chat_stream_for_session(
            session, msg='hello', workspace='/tmp', model='test', external_runtime_owned=False)
        stream_id = result['stream_id']
        assert peeked.wait(5)
        assert config.ACTIVE_RUNS[stream_id]['phase'] == 'starting'
        config.enter_restart_drain('test')
        responses = []
        monkeypatch.setattr(routes, 'j', lambda handler, payload, status=200: responses.append((status, payload)))
        routes.handle_get(object(), urlsplit('/api/chat/cancel?stream_id=' + stream_id))
        assert responses[-1] == (200, {'ok': True, 'cancelled': True, 'stream_id': stream_id})
        assert config.ACTIVE_RUNS[stream_id]['phase'] == 'cancelling'
        release.set()
        threads[0].join(5)
        assert not threads[0].is_alive()
        assert stream_id not in config.ACTIVE_RUNS
        assert stream_id not in config.STREAMS
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        config.exit_restart_drain()
        if stream_id:
            config.unregister_active_run(stream_id)
            with config.STREAMS_LOCK:
                config.STREAMS.pop(stream_id, None)
            config.unregister_stream_owner(stream_id)
            config.clear_session_writeback_owner_if_owned(session.session_id, stream_id)
