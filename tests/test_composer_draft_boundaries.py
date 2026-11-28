"""Retained boundary and atomic-persistence contracts for the upstream store."""
import json
from types import SimpleNamespace

import pytest
from tests.test_composer_draft_fast_path import seed
from tests.test_composer_draft_sidecar_regressions import _post_json, _build_draft_env


@pytest.fixture(autouse=True)
def capture_bad_responses(monkeypatch):
    import api.routes as routes
    monkeypatch.setattr(routes, '_check_csrf', lambda h: True)
    monkeypatch.setattr(routes, 'read_body', lambda h: {})
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_unsafe_draft_identity_returns_400_without_path_io(tmp_path, monkeypatch, method):
    models, routes, _ = _build_draft_env(tmp_path, monkeypatch)
    if method == 'POST':
        status, _ = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': '../unsafe', 'text': 'no'})
    else:
        result = {}
        monkeypatch.setattr(routes, 'j', lambda h, payload, status=200: result.update(status=status) or True)
        routes.handle_post(SimpleNamespace(command='GET'), SimpleNamespace(path='/api/session/draft', query='session_id=..%2Funsafe'))
        status = result['status']
    assert status == 400
    assert not list(models._session_draft_dir().glob('*'))


def test_cold_draft_get_uses_read_only_ownership_not_full_session_load(tmp_path, monkeypatch):
    models, routes, _, owner = seed(tmp_path, monkeypatch)
    models.SESSIONS.clear()
    monkeypatch.setattr(models.Session, 'load', lambda *a, **kw: pytest.fail('draft GET loaded transcript'))
    result = {}
    monkeypatch.setattr(routes, 'j', lambda h, payload, status=200: result.update(payload=payload, status=status) or True)
    routes.handle_post(SimpleNamespace(command='GET'), SimpleNamespace(path='/api/session/draft', query='session_id='+owner.session_id))
    assert result['status'] == 200 and result['payload']['draft'] == owner.composer_draft


def test_atomic_writer_preserves_lone_surrogates_and_ordinary_unicode(tmp_path, monkeypatch):
    models, routes, _, owner = seed(tmp_path, monkeypatch)
    text = 'snowman \u2603 and lone surrogate \ud83d'
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, 'text': text})
    assert status == 200 and payload['draft']['text'] == text
    assert models._load_session_draft(owner.session_id)['text'] == text


@pytest.mark.parametrize('failure', ['fsync', '_replace_with_retry'])
def test_atomic_failure_keeps_previous_draft_and_removes_temporary_file(tmp_path, monkeypatch, failure):
    models, routes, _, owner = seed(tmp_path, monkeypatch)
    models._save_session_draft(owner.session_id, {'text': 'durable'})
    before = models._session_draft_path(owner.session_id).read_bytes()
    def fail(*args, **kwargs):
        raise OSError('injected draft write failure')
    monkeypatch.setattr(models.os if failure == 'fsync' else models, failure, fail)
    with pytest.raises(OSError, match='injected draft write failure'):
        _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, 'text': 'not durable'})
    assert models._session_draft_path(owner.session_id).read_bytes() == before
    assert not list(models._session_draft_dir().glob('*.tmp.*'))


def test_published_owner_is_not_a_new_shell_if_index_update_failed(tmp_path, monkeypatch):
    models, routes, _ = _build_draft_env(tmp_path, monkeypatch)
    owner = models.Session(session_id='publishedbeforeindex', composer_draft={'text': 'legacy'})
    models.SESSIONS[owner.session_id] = owner
    def fail(*args, **kwargs):
        raise OSError('injected index failure')
    monkeypatch.setattr(models, '_write_session_index', fail)
    with pytest.raises(OSError, match='injected index failure'):
        owner.save()
    assert owner.path.exists()
    owner.path.unlink()
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, 'text': 'must not recreate'})
    assert status == 409
    assert not owner.path.exists() and not models._session_draft_path(owner.session_id).exists()


def test_transcript_serialization_cannot_revert_concurrent_draft(tmp_path, monkeypatch):
    import threading
    models, routes, _, owner = seed(tmp_path, monkeypatch)
    original_dumps = models.json.dumps
    entered, resume = threading.Event(), threading.Event()
    errors = []
    def paused_dumps(value, *args, **kwargs):
        if isinstance(value, dict) and 'messages' in value:
            entered.set()
            assert resume.wait(5), 'transcript serialization wait expired'
        return original_dumps(value, *args, **kwargs)
    monkeypatch.setattr(models.json, 'dumps', paused_dumps)
    def save_transcript():
        try:
            with routes._get_session_agent_lock(owner.session_id):
                owner.save(skip_index=True)
        except BaseException as exc:
            errors.append(exc)
    writer = threading.Thread(target=save_transcript)
    writer.start()
    assert entered.wait(2)
    try:
        status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, 'text': 'new draft'})
        assert status == 200
    finally:
        resume.set()
        writer.join(6)
    assert not errors and not writer.is_alive()
    assert json.loads(owner.path.read_text())['composer_draft']['text'] == 'legacy'
    assert models.Session.load(owner.session_id).composer_draft == payload['draft']
