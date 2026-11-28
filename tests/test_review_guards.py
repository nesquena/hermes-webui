"""Additional candidate-only fast-path and ownership-cache checks."""
import json
import os
import threading
from contextlib import contextmanager
import pytest
from tests.test_composer_draft_sidecar_regressions import _build_draft_env, _post_json


def setup(tmp_path, monkeypatch):
    models, routes, directory = _build_draft_env(tmp_path, monkeypatch)
    owner = models.Session(session_id='reviewcacheowner', profile=None,
                           messages=[{'role': 'user', 'content': 'hi'}])
    owner.save(skip_index=True)
    models.SESSIONS[owner.session_id] = owner
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    return models, routes, owner


def test_ordinary_save_completes_while_transcript_lock_is_held(tmp_path, monkeypatch):
    models, routes, owner = setup(tmp_path, monkeypatch)
    models.SESSIONS.clear()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **kw: pytest.fail('full resolver called'))
    monkeypatch.setattr(models.Session, 'load', lambda *a, **kw: pytest.fail('full load called'))
    lock = routes._get_session_agent_lock(owner.session_id)
    lock.acquire()
    outcomes, errors = [], []
    def save():
        try:
            outcomes.append(_post_json(monkeypatch, routes, '/api/session/draft',
                                      {'session_id': owner.session_id, 'text': 'while streaming'}))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=save)
    worker.start()
    try:
        worker.join(2)
        assert not worker.is_alive(), 'ordinary draft blocked on transcript writer'
        assert not errors
        assert outcomes[0][0] == 200
    finally:
        lock.release()
        worker.join(5)


@pytest.mark.parametrize('field,value', [
    ('source_tag', 'subagent'), ('raw_source', 'subagent'),
    ('session_source', 'subagent'), ('profile', 'bravo'),
    ('parent_session_id', 'newparent'),
    ('pre_compression_continuation_session_id', 'newcontinuation'),
    ('pre_compression_snapshot', True),
])
def test_warm_cache_invalidates_in_place_rewrite_with_restored_mtime(tmp_path, monkeypatch, field, value):
    models, routes, owner = setup(tmp_path, monkeypatch)
    models.get_session_for_draft(owner.session_id)
    raw = json.loads(owner.path.read_text())
    before = owner.path.stat()
    raw[field] = value
    owner.path.write_text(json.dumps(raw))
    os.utime(owner.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    resolved = models.get_session_for_draft(owner.session_id)
    assert getattr(resolved, field) == value


@pytest.mark.parametrize('field,value,expected', [
    ('source_tag', 'subagent', 400), ('raw_source', 'subagent', 400),
    ('session_source', 'subagent', 400), ('profile', 'bravo', 409),
    ('pre_compression_snapshot', True, 409),
])
def test_preflight_metadata_change_is_rechecked_under_lifecycle(tmp_path, monkeypatch, field, value, expected):
    models, routes, owner = setup(tmp_path, monkeypatch)
    real_lock = routes._draft_lifecycle_lock_for_session_id
    mutated = []
    @contextmanager
    def change_at_lock(sid):
        with real_lock(sid):
            if not mutated:
                raw = json.loads(owner.path.read_text())
                raw[field] = value
                owner.path.write_text(json.dumps(raw))
                mutated.append(True)
            yield
    monkeypatch.setattr(routes, '_draft_lifecycle_lock_for_session_id', change_at_lock)
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                           {'session_id': owner.session_id, 'text': 'must not persist'})
    assert status == expected
    assert not models._session_draft_path(owner.session_id).exists()
