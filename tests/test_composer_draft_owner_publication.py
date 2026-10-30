"""Validated ownership headers and concurrent embedded-draft publication."""
import json
import threading
import pytest
from tests.test_review_probes import seed
from tests.test_composer_draft_sidecar_regressions import _post_json

FIELDS = ('session_id', 'title', 'workspace', 'profile', 'composer_draft',
          'source_tag', 'raw_source', 'session_source', 'pre_compression_snapshot',
          'pre_compression_continuation_session_id', 'parent_session_id')


def fields_from(owner):
    return {k: getattr(owner, k, None) for k in FIELDS}


@pytest.mark.parametrize('field,value', [('source_tag', 'subagent'),
                                        ('raw_source', 'subagent'),
                                        ('session_source', 'subagent'),
                                        ('profile', 'bravo'),
                                        ('pre_compression_snapshot', True)])
def test_metadata_duplicate_after_last_wanted_field_does_not_authorize_draft(tmp_path, monkeypatch, field, value):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    document = json.dumps(fields_from(owner))[:-1] + ', ' + json.dumps(field) + ': ' + json.dumps(value) + ', "messages": []}'
    assert json.loads(document)[field] == value
    owner.path.write_text(document)
    models.SESSIONS.clear()
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, 'text': 'must not persist'})
    print('late_header_authority', field, status, payload)
    assert status == (400 if field in {'source_tag', 'raw_source', 'session_source'} else 409)
    assert not models._session_draft_path(owner.session_id).exists()


@pytest.mark.parametrize('suffix', ['', ' trailing', ', "source_tag":'])
def test_truncated_complete_field_set_is_not_a_writable_owner(tmp_path, monkeypatch, suffix):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    owner.path.write_text(json.dumps(fields_from(owner))[:-1] + suffix)
    models.SESSIONS.clear()
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, 'text': 'must not persist'})
    print('partial_owner', repr(suffix), status, payload)
    assert status == 404
    assert not models._session_draft_path(owner.session_id).exists()


@pytest.mark.parametrize('tail', [
    '} trailing', ',}', ', "messages" []}',
])
def test_complete_field_set_requires_validated_header_boundary(tmp_path, monkeypatch, tail):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    owner.path.write_text(json.dumps(fields_from(owner))[:-1] + tail)
    models.SESSIONS.clear()
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                           {'session_id': owner.session_id, 'text': 'must not persist'})
    assert status == 404
    assert not models._session_draft_path(owner.session_id).exists()


@pytest.mark.parametrize('messages', [None, {}])
@pytest.mark.parametrize('source', [None, 'subagent'])
def test_non_array_transcript_requires_complete_document(tmp_path, monkeypatch, messages, source):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    document = json.dumps(fields_from(owner))[:-1] + ', "messages": ' + json.dumps(messages)
    document += ', "source_tag": ' + json.dumps(source) + '}'
    owner.path.write_text(document)
    models.SESSIONS.clear()
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                           {'session_id': owner.session_id, 'text': 'validated document'})
    assert status == (400 if source else 200)
    assert models._session_draft_path(owner.session_id).exists() == (source is None)


@pytest.mark.parametrize('older_skip_index,newer_skip_index', [(True, True), (True, False), (False, True)])
def test_overlapping_skip_index_saves_keep_fallback_in_publication_order(tmp_path, monkeypatch, older_skip_index, newer_skip_index):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    published, resume = threading.Event(), threading.Event()
    real_replace = models._replace_session_sidecar if hasattr(models, '_replace_session_sidecar') else models._safe_replace
    seam = '_replace_session_sidecar' if hasattr(models, '_replace_session_sidecar') else '_safe_replace'
    errors = []
    def replace(*args, **kwargs):
        result = real_replace(*args, **kwargs)
        if threading.current_thread().name == 'older-owner-save':
            published.set()
            assert resume.wait(5)
        return result
    monkeypatch.setattr(models, seam, replace)
    def save():
        try:
            owner.save(skip_index=older_skip_index)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=save, name='older-owner-save')
    worker.start()
    latest = {'text': 'new durable publication', 'files': []}
    try:
        assert published.wait(5)
        assert _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, **latest})[0] == 200
        owner.save(skip_index=newer_skip_index)
        assert json.loads(owner.path.read_text())['composer_draft'] == latest
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    models._session_draft_path(owner.session_id).write_text('{damaged')
    cold = models.Session.load(owner.session_id).composer_draft
    warm = owner.composer_draft
    print('overlapping_publication', {'warm': warm, 'cold': cold})
    assert warm == cold == latest


@pytest.mark.parametrize('draft', [{'text': 'new draft', 'files': ['new']}, {'text': '', 'files': []}])
def test_real_handoff_rejects_late_old_sid_without_overwriting_continuation(tmp_path, monkeypatch, draft):
    models, routes, streaming, _, owner = seed(tmp_path, monkeypatch)
    old_sid, new_sid = owner.session_id, 'reviewround2child'
    assert _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': old_sid, **draft})[0] == 200
    lock = routes._get_session_agent_lock(old_sid)
    with lock:
        assert streaming._migrate_compression_session_ownership(owner, old_sid, new_sid, lock)
        owner.save(skip_index=True)
    old_file = models._session_draft_path(old_sid).read_bytes()
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': old_sid, 'text': 'late old autosave'})
    print('late_old_sid', status, payload)
    assert status == 409 and payload['code'] == 'session_moved'
    assert models._session_draft_path(old_sid).read_bytes() == old_file
    assert not models._session_draft_path(new_sid).exists()
    assert owner.composer_draft == models.Session.load(new_sid).composer_draft == draft
