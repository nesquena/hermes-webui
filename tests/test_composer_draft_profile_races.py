"""Draft requests retain their entry authority across concurrent changes.

Tests use real Session.save and draft storage with synthetic data. Request plumbing
is patched, but the visibility guard, profile matcher and draft route remain real.
No sockets, agent/provider calls or existing session data are used.
"""
from collections import OrderedDict
import json
import threading
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


PRIVATE = {'text': 'foreign text', 'files': [{'name': 'synthetic-private.txt'}],
           'opaque': {'marker': 'foreign-only'}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    import api.models as m
    import api.routes as r
    import api.profiles as p

    folder = tmp_path / 'owners'
    folder.mkdir()
    cache = OrderedDict()
    for mod in (m, r):
        monkeypatch.setattr(mod, 'SESSION_DIR', folder)
        monkeypatch.setattr(mod, 'SESSION_INDEX_FILE', folder / '_index.json')
        monkeypatch.setattr(mod, 'SESSIONS', cache)
    monkeypatch.setattr(p, '_root_profile_name_cache_loaded', True)
    monkeypatch.setattr(p, '_root_profile_name_cache', {'default'})
    monkeypatch.setattr(p, '_is_isolated_profile_mode', lambda: False)
    monkeypatch.setattr(p, '_active_profile', 'default')
    p.set_request_profile('default')
    monkeypatch.setattr(r, '_check_csrf', lambda h: True)
    monkeypatch.setattr(r, 'bad', lambda h, message, status=400: r.j(h, {'error': message}, status=status))

    def owner(sid='probe-owner', profile='default', **kw):
        s = m.Session(session_id=sid, profile=profile, **kw)
        s.save(touch_updated_at=False, skip_index=True)
        return s

    def call(sid='probe-owner', method='POST', **body):
        out = {}
        monkeypatch.setattr(r, 'read_body', lambda h: {'session_id': sid, **body})
        monkeypatch.setattr(r, 'j', lambda h, payload, status=200, **kw: out.update(status=status, payload=payload) or True)
        path = '/api/session/draft' + ('?session_id=' + sid if method == 'GET' else '')
        assert r.handle_post(SimpleNamespace(command=method), urlparse(path)) is True
        return out['status'], out['payload']

    yield SimpleNamespace(m=m, r=r, p=p, folder=folder, owner=owner, call=call)
    p.clear_request_profile()


def disk_state(env):
    return {str(p.relative_to(env.folder)): p.read_bytes()
            for p in env.folder.rglob('*') if p.is_file()}


@pytest.mark.parametrize('method', ['POST', 'GET'])
@pytest.mark.parametrize('cookie_context', [False, True], ids=['no-cookie', 'request-tls'])
def test_profile_switch_after_initial_guard_cannot_change_caller(env, monkeypatch, method, cookie_context):
    e = env
    old = e.owner(composer_draft={'text': 'old default'})
    e.m.SESSIONS[old.session_id] = old
    e.owner(profile='bravo', composer_draft=PRIVATE)
    before = disk_state(e)
    if not cookie_context:
        e.p.clear_request_profile()
    assert e.p.get_active_profile_name() == 'default'
    real_guard = e.r._guard_request_session_visibility
    admitted_as = []

    def switch_after_guard(*args, **kw):
        result = real_guard(*args, **kw)
        assert result is True
        admitted_as.append(e.p.get_active_profile_name())
        # The process-level setter used by a concurrent profile switch updates
        # this global; cookie-less requests have no TLS snapshot in server.py.
        e.p._active_profile = 'bravo'
        return result

    monkeypatch.setattr(e.r, '_guard_request_session_visibility', switch_after_guard)
    status, payload = e.call(method=method, text='default request edit')
    actual = {'initial_authority': admitted_as, 'status': status, 'payload': payload,
              'persisted_draft': e.m._load_session_draft(old.session_id)}
    print('PROFILE_SWITCH_RESULT', json.dumps(actual, sort_keys=True))
    assert admitted_as == ['default']
    assert status in (404, 409) and 'draft' not in payload, actual
    assert disk_state(e) == before


@pytest.mark.parametrize('skip_index', [False, True], ids=['normal-save', 'skip-index-save'])
def test_foreign_owner_publication_after_locked_read_cannot_receive_write(env, monkeypatch, skip_index):
    """A concurrent Session.save completes without waiting for the draft lock.

    Pause immediately after the under-lifecycle owner read returns. The publisher
    uses normal Session.save with and without skip_index, not raw filesystem writes
    or fake loader values. The route resumes with its already-read default owner.
    """
    e = env
    original = e.owner(composer_draft={'text': 'default draft', 'files': ['default-file']})
    e.m.SESSIONS[original.session_id] = original
    foreign = e.m.Session(session_id=original.session_id, profile='bravo', composer_draft=PRIVATE)
    ready, published = threading.Event(), threading.Event()
    errors = []

    def publisher():
        try:
            assert ready.wait(5)
            foreign.save(touch_updated_at=False, skip_index=skip_index)
            published.set()
        except BaseException as exc:
            errors.append(exc)
            published.set()

    thread = threading.Thread(target=publisher)
    real_get = e.r.get_session_for_draft
    reads = []

    def pause_after_read(sid, **kw):
        result = real_get(sid, **kw)
        reads.append(sid)
        if len(reads) == 2:
            ready.set()
            assert published.wait(5), 'publisher blocked while draft lifecycle lock held'
            assert not errors
            assert json.loads(foreign.path.read_text())['profile'] == 'bravo'
        return result

    monkeypatch.setattr(e.r, 'get_session_for_draft', pause_after_read)
    thread.start()
    try:
        status, payload = e.call(text='default caller writes after bravo owns the SID')
    finally:
        ready.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    actual = {'status': status, 'payload': payload,
              'durable_owner': json.loads(foreign.path.read_text())['profile'],
              'persisted_draft': e.m._load_session_draft(original.session_id)}
    print('CONCURRENT_OWNER_RESULT', json.dumps(actual, sort_keys=True))
    assert actual['persisted_draft'] is None, actual
    assert status == 409 and 'draft' not in payload


@pytest.mark.parametrize('skip_index', [False, True], ids=['normal-save', 'skip-index-save'])
@pytest.mark.parametrize('boundary', ['fsync', 'GET', 'unchanged', 'mismatch'])
def test_owner_publication_during_draft_io_fails_closed(env, monkeypatch, skip_index, boundary):
    """Slow draft I/O must neither block a publisher nor commit its stale owner."""
    e = env
    original = e.owner(composer_draft={'text': 'default draft', 'files': []})
    e.m.SESSIONS[original.session_id] = original
    foreign = e.m.Session(session_id=original.session_id, profile='bravo', composer_draft=PRIVATE)
    ready, published = threading.Event(), threading.Event()
    errors = []
    caller = threading.current_thread()

    def publisher():
        try:
            assert ready.wait(5)
            foreign.save(touch_updated_at=False, skip_index=skip_index)
        except BaseException as exc:
            errors.append(exc)
        finally:
            published.set()

    def pause():
        if threading.current_thread() is caller:
            ready.set()
            assert published.wait(5), 'Session.save waited for draft I/O'
            assert not errors

    if boundary == 'fsync':
        real_sync = e.m.os.fsync

        def slow_sync(fd):
            real_sync(fd)
            pause()

        monkeypatch.setattr(e.m.os, 'fsync', slow_sync)
    else:
        real_load = e.m._load_session_draft

        def slow_load(sid):
            result = real_load(sid)
            pause()
            return result

        monkeypatch.setattr(e.m, '_load_session_draft', slow_load)

    thread = threading.Thread(target=publisher)
    thread.start()
    body = {'text': 'new draft'}
    if boundary == 'unchanged':
        body = {'text': 'default draft'}
    elif boundary == 'mismatch':
        body = {'text': '', 'if_text': 'stale'}
    try:
        status, payload = e.call(method='GET' if boundary == 'GET' else 'POST', **body)
    finally:
        ready.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    assert status == 409 and 'draft' not in payload
    assert json.loads(foreign.path.read_text())['profile'] == 'bravo'
    assert not e.m._session_draft_path(original.session_id).exists()
    assert not list(e.m._session_draft_dir().glob('*.tmp.*'))


def test_entry_profile_is_used_by_initial_guard_even_if_body_read_switches_global(env, monkeypatch):
    e = env
    e.owner(composer_draft={'text': 'default draft'})
    e.p.clear_request_profile()
    out = {}

    def read_body(handler):
        e.p._active_profile = 'bravo'
        return {'session_id': 'probe-owner', 'text': 'default edit'}

    monkeypatch.setattr(e.r, 'read_body', read_body)
    monkeypatch.setattr(e.r, 'j', lambda h, payload, status=200, **kw: out.update(status=status, payload=payload) or True)
    assert e.r.handle_post(SimpleNamespace(command='POST'), urlparse('/api/session/draft')) is True
    assert out['status'] == 200
    assert out['payload']['draft']['text'] == 'default edit'


@pytest.mark.parametrize('older_skip_index,newer_skip_index', [(True, True), (True, False), (False, True), (False, False)])
def test_owner_saves_prepare_independent_temp_files(env, monkeypatch, older_skip_index, newer_skip_index):
    e = env
    older = e.owner(composer_draft={'text': 'older'})
    newer = e.m.Session(session_id=older.session_id, title='newer', composer_draft={'text': 'newer'})
    prepared, resume = threading.Event(), threading.Event()
    errors = []
    real_sync = e.m.os.fsync

    def pause_older(fd):
        real_sync(fd)
        if threading.current_thread().name == 'older-owner':
            prepared.set()
            assert resume.wait(5)

    monkeypatch.setattr(e.m.os, 'fsync', pause_older)

    def save_older():
        try:
            older.save(touch_updated_at=False, skip_index=older_skip_index)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=save_older, name='older-owner')
    thread.start()
    try:
        assert prepared.wait(5)
        newer.save(touch_updated_at=False, skip_index=newer_skip_index)
        assert json.loads(newer.path.read_text())['title'] == 'newer'
    finally:
        resume.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    assert not list(e.folder.glob('*.tmp.*'))


@pytest.mark.parametrize('skip_index', [False, True], ids=['normal-save', 'skip-index-save'])
def test_owner_compare_and_draft_replace_exclude_owner_publication(env, monkeypatch, skip_index):
    e = env
    original = e.owner(composer_draft={'text': 'default draft'})
    foreign = e.m.Session(session_id=original.session_id, profile='bravo', composer_draft=PRIVATE)
    ready, attempted, published = threading.Event(), threading.Event(), threading.Event()
    errors, committed = [], []
    caller = threading.current_thread()
    real_revision = e.m._draft_owner_revision
    real_publish = e.m._replace_session_sidecar
    real_replace = e.m.os.replace

    def publisher():
        try:
            assert ready.wait(5)
            foreign.save(touch_updated_at=False, skip_index=skip_index)
        except BaseException as exc:
            errors.append(exc)
        finally:
            published.set()

    def publish(*args, **kw):
        attempted.set()
        return real_publish(*args, **kw)

    def compare(sid):
        revision = real_revision(sid)
        if threading.current_thread() is caller and list(e.m._session_draft_dir().glob('*.tmp.*')):
            ready.set()
            assert attempted.wait(5)
            assert not published.wait(.05), 'owner replaced between draft compare and publish'
        return revision

    def replace(src, dst):
        if dst == e.m._session_draft_path(original.session_id):
            assert json.loads(original.path.read_text())['profile'] == 'default'
            committed.append(True)
        return real_replace(src, dst)

    monkeypatch.setattr(e.m, '_draft_owner_revision', compare)
    monkeypatch.setattr(e.m, '_replace_session_sidecar', publish)
    monkeypatch.setattr(e.m.os, 'replace', replace)
    thread = threading.Thread(target=publisher)
    thread.start()
    try:
        status, payload = e.call(text='draft ordered before owner replacement')
    finally:
        ready.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    assert committed == [True]
    assert status == 200 and payload['draft']['text'] == 'draft ordered before owner replacement'
    assert json.loads(foreign.path.read_text())['profile'] == 'bravo'
