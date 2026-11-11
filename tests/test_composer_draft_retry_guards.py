"""Bounded retries retain the request's authority and current draft semantics."""
import json
import threading

import pytest

from tests.test_composer_draft_profile_races import env as env


def test_same_profile_revision_churn_is_bounded(env, monkeypatch):
    e = env
    owner = e.owner(composer_draft={'text': 'durable'})
    real_fsync = e.m.os.fsync
    attempts = []
    publishing = False

    def fsync(fd):
        nonlocal publishing
        real_fsync(fd)
        if not publishing:
            publishing = True
            try:
                attempts.append(True)
                owner.save(touch_updated_at=False, skip_index=True)
            finally:
                publishing = False

    monkeypatch.setattr(e.m.os, 'fsync', fsync)
    status, payload = e.call(text='not committed')
    assert status == 409 and payload['code'] == 'draft_authority_unconfirmed'
    assert len(attempts) == 4
    assert 'draft' not in payload and 'session_id' not in payload
    assert e.m.get_session_for_draft(owner.session_id).composer_draft['text'] == 'durable'
    assert not list(e.folder.rglob('*.tmp.*'))


@pytest.mark.parametrize('mode', ['ordinary', 'compare-clear', 'unchanged'])
def test_retry_reprojects_current_draft_without_adopting_process_profile(env, monkeypatch, mode):
    e = env
    if mode == 'compare-clear' and not hasattr(e.r, '_project_session_draft_write'):
        pytest.skip('This branch predates the upstream compare-clear projection')
    owner = e.owner(composer_draft={'text': 'durable', 'files': []})
    e.p.clear_request_profile()
    real_current = e.r._draft_owner_is_current
    changed = []

    def current(session):
        if not changed:
            changed.append(True)
            # Both owner revision and stored draft change after resolution.
            e.m._save_session_draft(owner.session_id, {'text': 'newer', 'files': [], 'opaque': 'retain'})
            owner.save(touch_updated_at=False, skip_index=True)
            e.p._active_profile = 'bravo'
        return real_current(session)

    monkeypatch.setattr(e.r, '_draft_owner_is_current', current)
    body = {'text': 'typed'}
    if mode == 'compare-clear':
        body = {'text': '', 'files': [], 'if_text': 'durable'}
    elif mode == 'unchanged':
        body = {}
    status, payload = e.call(**body)
    assert status == 200
    expected = {'text': 'typed' if mode == 'ordinary' else 'newer', 'files': [], 'opaque': 'retain'}
    assert payload['draft'] == expected
    assert e.m.get_session_for_draft(owner.session_id).composer_draft == expected
    if mode == 'compare-clear':
        # The PR has upstream compare/mismatch projection; the port predates it.
        assert payload.get('mismatch') is True


@pytest.mark.parametrize('publication', ['draft', 'first-shell'])
def test_windows_retry_revalidates_authority_after_backoff(env, monkeypatch, publication):
    e = env
    if publication == 'first-shell':
        owner = e.m.Session(session_id='probe-owner', profile='default')
        e.m.SESSIONS[owner.session_id] = owner
    else:
        owner = e.owner(composer_draft={'text': 'durable'})
    foreign = e.m.Session(session_id=owner.session_id, profile='bravo', composer_draft={'text': 'private'})
    real_os, real_time = e.m.os, e.m.time
    caller = threading.current_thread()
    target = owner.path if publication == 'first-shell' else e.m._session_draft_path(owner.session_id)
    ready, done = threading.Event(), threading.Event()
    errors, attempts, progress = [], [], []

    class OSProxy:
        name = 'nt'

        def __getattr__(self, name):
            return getattr(real_os, name)

        def replace(self, src, dst):
            if threading.current_thread() is caller and dst == target:
                attempts.append(True)
                if len(attempts) == 1:
                    raise PermissionError('synthetic Windows sharing violation')
            return real_os.replace(src, dst)

    class TimeProxy:
        def __getattr__(self, name):
            return getattr(real_time, name)

        def sleep(self, delay):
            ready.set()
            progress.append(done.wait(1))

    def publish():
        try:
            assert ready.wait(5)
            foreign.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(e.m, 'os', OSProxy())
    monkeypatch.setattr(e.m, 'time', TimeProxy())
    thread = threading.Thread(target=publish)
    thread.start()
    try:
        status, payload = e.call(text='must not publish')
    finally:
        ready.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    assert progress == [True], 'owner must publish during backoff'
    assert len(attempts) == 1, 'second attempt must reject before replacing'
    assert status == 409 and payload['code'] == 'draft_authority_unconfirmed'
    assert json.loads(owner.path.read_text())['profile'] == 'bravo'
    assert not e.m._session_draft_path(owner.session_id).exists()
    assert not list(e.folder.rglob('*.tmp.*'))


def test_first_shell_collision_reprojects_same_profile_owner(env, monkeypatch):
    e = env
    shell = e.m.Session(session_id='probe-owner', profile='default')
    e.m.SESSIONS[shell.session_id] = shell
    real_fsync = e.m.os.fsync
    published = []

    def fsync(fd):
        real_fsync(fd)
        if not published:
            published.append(True)
            e.owner(composer_draft={'text': 'existing', 'files': ['kept'], 'opaque': 'retained'})

    monkeypatch.setattr(e.m.os, 'fsync', fsync)
    status, payload = e.call(text='typed')
    assert status == 200
    assert payload['draft'] == {'text': 'typed', 'files': ['kept'], 'opaque': 'retained'}
    assert json.loads(shell.path.read_text())['composer_draft']['text'] == 'existing'
    assert not list(e.folder.rglob('*.tmp.*'))
