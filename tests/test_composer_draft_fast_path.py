"""Fast draft ownership, rotation, export and bounded removal regressions."""
import io
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from tests.test_composer_draft_sidecar_regressions import (
    _build_draft_env, _post_json, _cleanup_route,
)


def seed(tmp_path, monkeypatch, *, empty=False):
    models, routes, directory = _build_draft_env(tmp_path, monkeypatch)
    s = models.Session(session_id='fastdraftowner', title='Untitled',
                       messages=[] if empty else [{'role': 'user', 'content': 'existing'}],
                       composer_draft={'text': 'legacy', 'files': ['original-file']})
    s.save(touch_updated_at=False, skip_index=True)
    models.SESSIONS[s.session_id] = s
    return models, routes, directory, s


@pytest.mark.parametrize('permanent', [False, True], ids=['snapshot-rebind', 'rotation'])
def test_inflight_response_and_cached_property_keep_their_own_identity(tmp_path, monkeypatch, permanent):
    models, routes, directory, owner = seed(tmp_path, monkeypatch)
    sid = owner.session_id
    target = models.Session(session_id='fastdrafttarget')
    target.save(skip_index=True)
    original = {'text': 'original draft', 'files': ['original-file']}
    other = {'text': 'other draft', 'files': ['other-file']}
    models._save_session_draft(sid, original)
    models._save_session_draft(target.session_id, other)
    before = models._session_draft_path(target.session_id).read_bytes()
    real_save = routes._save_session_draft

    def rotate_during_write(write_sid, draft, **kw):
        assert write_sid == sid
        owner.session_id = target.session_id
        if permanent:
            owner._composer_draft_session_id = target.session_id
        return real_save(write_sid, draft, **kw)

    monkeypatch.setattr(routes, '_save_session_draft', rotate_during_write)
    try:
        status, payload = _post_json(monkeypatch, routes, '/api/session/draft',
                                     {'session_id': sid, 'text': 'for original'})
        expected = {'text': 'for original', 'files': ['original-file']}
        assert status == 200 and payload['draft'] == expected
        assert models._load_session_draft(sid) == expected
        assert models._session_draft_path(target.session_id).read_bytes() == before
        assert owner.composer_draft == (other if permanent else expected)
    finally:
        owner.session_id = sid


def test_property_and_compact_ignore_temporary_snapshot_rebind(tmp_path, monkeypatch):
    models, _, _, owner = seed(tmp_path, monkeypatch)
    own = {'text': 'own draft'}
    models._save_session_draft(owner.session_id, own)
    archived = models.Session(session_id='fastdraftarchive')
    archived.save(skip_index=True)
    models._save_session_draft(archived.session_id, {'text': 'archive draft'})
    sid = owner.session_id
    try:
        owner.session_id = archived.session_id
        assert owner.composer_draft == own
        assert owner.compact()['composer_draft'] == own
    finally:
        owner.session_id = sid


@pytest.mark.parametrize('zero_only', [False, True])
def test_cleanup_bounds_busy_transcript_wait_and_removes_available_owner(tmp_path, monkeypatch, zero_only):
    models, routes, directory, busy = seed(tmp_path, monkeypatch, empty=True)
    other = models.Session(session_id='fastdraftavailable', title='Untitled')
    other.save(skip_index=True)
    for s in (busy, other):
        models._save_session_draft(s.session_id, {'text': s.session_id})
    before = busy.path.read_bytes(), models._session_draft_path(busy.session_id).read_bytes()
    lock = routes._get_session_agent_lock(busy.session_id)
    lock.acquire()
    done = threading.Event()
    result = {}
    loads = []
    real_load = models.Session.load
    monkeypatch.setattr(models.Session, 'load', lambda sid: (loads.append(sid), real_load(sid))[1])

    def clean():
        try:
            result['response'] = _cleanup_route(monkeypatch, routes, zero_only=zero_only)
        finally:
            done.set()

    thread = threading.Thread(target=clean)
    thread.start()
    try:
        assert done.wait(1), 'cleanup waited for busy transcript writer'
        status, payload = result['response']
        assert status == 200 and payload == {'ok': True, 'cleaned': 1, 'skipped': 1}
        assert busy.session_id not in loads
        assert models.SESSIONS[busy.session_id] is busy
        assert (busy.path.read_bytes(), models._session_draft_path(busy.session_id).read_bytes()) == before
        assert not other.path.exists() and not models._session_draft_path(other.session_id).exists()
    finally:
        lock.release()
        thread.join(6)
    assert not thread.is_alive()


def test_cleanup_waits_for_draft_publication_before_removing_owner(tmp_path, monkeypatch):
    models, routes, _, owner = seed(tmp_path, monkeypatch, empty=True)
    publishing, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    errors = []
    real_save = routes._save_session_draft
    real_load = models.Session.load

    def paused_save(sid, value, **kw):
        publishing.set()
        assert resume.wait(5)
        return real_save(sid, value, **kw)

    def observe_load(sid):
        attempted.set()
        return real_load(sid)

    monkeypatch.setattr(routes, '_save_session_draft', paused_save)
    monkeypatch.setattr(models.Session, 'load', observe_load)
    monkeypatch.setattr(routes, '_check_csrf', lambda h: True)
    monkeypatch.setattr(routes, 'read_body', lambda h: {'session_id': owner.session_id, 'text': 'publishing'})
    monkeypatch.setattr(routes, 'j', lambda *a, **kw: True)
    handler = SimpleNamespace(command='POST')

    def run(fn):
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    saver = threading.Thread(target=lambda: run(lambda: routes.handle_post(handler, urlparse('/api/session/draft'))))
    remover = threading.Thread(target=lambda: run(lambda: routes._handle_sessions_cleanup(handler, {}, zero_only=True)))
    saver.start()
    assert publishing.wait(5)
    remover.start()
    try:
        attempted.wait(.2)
        assert owner.path.exists(), 'cleanup removed owner during draft publication'
    finally:
        resume.set()
        saver.join(6)
        remover.join(6)
    assert not errors and not saver.is_alive() and not remover.is_alive()
    assert not owner.path.exists() and not models._session_draft_path(owner.session_id).exists()


def test_export_reads_authoritative_draft_without_requiring_property_on_session_double(tmp_path, monkeypatch):
    models, routes, _, owner = seed(tmp_path, monkeypatch)
    latest = {'text': 'current exported draft', 'files': []}
    models._save_session_draft(owner.session_id, latest)
    handler = SimpleNamespace(wfile=io.BytesIO(), send_response=lambda status: None,
                              send_header=lambda *a: None, end_headers=lambda: None)
    monkeypatch.setattr(routes, 'get_session', lambda sid: owner)
    monkeypatch.setattr(routes, 'get_active_profile_name', lambda: owner.profile)
    routes._handle_session_export(handler, urlparse('/api/session/export?session_id=' + owner.session_id))
    assert json.loads(handler.wfile.getvalue())['composer_draft'] == latest
    double = SimpleNamespace(session_id=owner.session_id, profile=owner.profile, messages=[])
    monkeypatch.setattr(routes, 'get_session', lambda sid: double)
    handler.wfile = io.BytesIO()
    routes._handle_session_export(handler, urlparse('/api/session/export?session_id=' + owner.session_id))
    assert json.loads(handler.wfile.getvalue()).get('composer_draft', {}) == {}


@pytest.mark.parametrize('removal', ['ephemeral-cancel', 'admission-rollback'])
def test_secondary_removal_deletes_draft(tmp_path, monkeypatch, removal):
    models, routes, _, owner = seed(tmp_path, monkeypatch)
    import api.streaming as streaming
    models._save_session_draft(owner.session_id, {'text': 'private draft'})
    with routes._get_session_agent_lock(owner.session_id):
        if removal == 'ephemeral-cancel':
            streaming._cleanup_ephemeral_cancelled_turn(owner)
        else:
            routes._restore_chat_start_entry_sidecar((owner.path, None, owner.path.with_suffix('.json.bak'), False, None, False))
    assert not owner.path.exists() and not models._session_draft_path(owner.session_id).exists()


def test_compression_marker_waits_for_inflight_draft_lifecycle(tmp_path, monkeypatch):
    models, routes, directory, owner = seed(tmp_path, monkeypatch)
    import api.streaming as streaming
    monkeypatch.setattr(streaming, 'SESSION_DIR', directory)
    old_sid = owner.session_id
    new_sid = 'fastdraftcontinuation'
    draft_lock = models._draft_lifecycle_lock_for_session_id(old_sid)
    agent_lock = routes._get_session_agent_lock(old_sid)
    attempted, marker_entered = threading.Event(), threading.Event()
    result = {}
    original_preserve = streaming._preserve_pre_compression_snapshot

    def observe_marker(*args):
        marker_entered.set()
        return original_preserve(*args)

    monkeypatch.setattr(streaming, '_preserve_pre_compression_snapshot', observe_marker)

    def rotate():
        with agent_lock:
            attempted.set()
            result['ok'] = streaming._migrate_compression_session_ownership(owner, old_sid, new_sid, agent_lock)

    draft_lock.acquire()
    thread = threading.Thread(target=rotate)
    thread.start()
    try:
        assert attempted.wait(2)
        assert not marker_entered.wait(.1), 'compression marker bypassed draft lifecycle ownership'
        assert not json.loads((directory/f'{old_sid}.json').read_text())['pre_compression_snapshot']
    finally:
        draft_lock.release()
        thread.join(5)
    assert not thread.is_alive() and result['ok'] is True
    assert json.loads((directory/f'{old_sid}.json').read_text())['pre_compression_snapshot'] is True


def test_snapshot_published_after_preflight_escalates_without_changing_target(tmp_path, monkeypatch):
    from tests.test_composer_draft_sidecar_regressions import _build_pre_compression_migration_state
    models, routes, directory, old_sid, next_sid, _ = _build_pre_compression_migration_state(tmp_path, monkeypatch)
    original = models.Session.load(old_sid)
    original.pre_compression_snapshot = False
    original.save(touch_updated_at=False, skip_index=True)
    real_lock = routes._draft_lifecycle_lock_for_session_id
    published = []
    agent_locks = []
    real_agent_lock = routes._get_session_agent_lock

    @contextmanager
    def publish_snapshot(sid):
        with real_lock(sid):
            if sid == old_sid and not published:
                original.pre_compression_snapshot = True
                original.save(touch_updated_at=False, skip_index=True)
                published.append(True)
            yield

    monkeypatch.setattr(routes, '_draft_lifecycle_lock_for_session_id', publish_snapshot)
    monkeypatch.setattr(routes, '_get_session_agent_lock', lambda sid: (agent_locks.append(sid), real_agent_lock(sid))[1])
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': old_sid, 'text': 'late autosave'})
    assert status == 409 and payload['code'] == 'session_moved' and payload['session_id'] == next_sid
    assert old_sid in agent_locks and next_sid in agent_locks
    assert not models._session_draft_path(old_sid).exists() and not models._session_draft_path(next_sid).exists()


def test_delete_wins_before_ordinary_draft_lifecycle_and_prevents_recreation(tmp_path, monkeypatch):
    models, routes, _, owner = seed(tmp_path, monkeypatch)
    models._save_session_draft(owner.session_id, {'text': 'old draft'})
    waiting, resume = threading.Event(), threading.Event()
    real_lock = routes._draft_lifecycle_lock_for_session_id
    responses, errors = {}, []

    @contextmanager
    def delayed_lock(sid):
        if threading.current_thread().name == 'late-draft':
            waiting.set()
            assert resume.wait(5)
        with real_lock(sid):
            yield

    monkeypatch.setattr(routes, '_draft_lifecycle_lock_for_session_id', delayed_lock)
    monkeypatch.setattr(routes, '_check_csrf', lambda h: True)
    monkeypatch.setattr(routes, 'read_body', lambda h: {'session_id': owner.session_id, 'text': 'late'})
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda sid: {})
    monkeypatch.setattr(routes, '_worktree_retained_payload_for_session_id', lambda sid: {})
    monkeypatch.setattr(routes, '_is_messaging_session_id', lambda sid: False)
    monkeypatch.setattr(routes, '_session_is_subagent_view_only', lambda sid: False)
    def respond(h, payload, status=200, **kw):
        responses[threading.current_thread().name] = (status, payload)
        return True
    monkeypatch.setattr(routes, 'j', respond)
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: respond(h, {'error': message}, status))
    handler = SimpleNamespace(command='POST')
    def save():
        try:
            routes.handle_post(handler, urlparse('/api/session/draft'))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=save, name='late-draft')
    thread.start()
    assert waiting.wait(3)
    try:
        assert routes.handle_post(handler, urlparse('/api/session/delete')) is True
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert responses['late-draft'][0] == 409
    assert not owner.path.exists() and not models._session_draft_path(owner.session_id).exists()
