"""Independent review probes; run on candidate and upstream in isolated state."""
import json
from pathlib import Path
import pytest
from tests.test_composer_draft_sidecar_regressions import _build_draft_env, _post_json


def seed(tmp_path, monkeypatch):
    models, routes, directory = _build_draft_env(tmp_path, monkeypatch)
    import api.streaming as streaming
    monkeypatch.setattr(streaming, 'SESSION_DIR', directory)
    owner = models.Session(session_id='reviewdraftowner', title='Review',
                           messages=[{'role': 'user', 'content': 'hi'}],
                           composer_draft={'text': 'legacy', 'files': ['legacy-file']})
    owner.save(skip_index=True)
    models.SESSIONS[owner.session_id] = owner
    return models, routes, streaming, directory, owner


@pytest.mark.parametrize('draft', [
    {'text': 'acknowledged draft', 'files': ['new-file']},
    {'text': '', 'files': []},
], ids=['latest-draft', 'explicit-clear'])
def test_rotation_preserves_acknowledged_draft(tmp_path, monkeypatch, draft):
    models, routes, streaming, directory, owner = seed(tmp_path, monkeypatch)
    old_sid, new_sid = owner.session_id, 'reviewdraftcontinuation'
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft',
                                {'session_id': old_sid, **draft})
    assert status == 200 and payload['draft'] == draft
    assert owner.composer_draft == draft
    lock = routes._get_session_agent_lock(old_sid)
    with lock:
        assert streaming._migrate_compression_session_ownership(owner, old_sid, new_sid, lock)
        owner.save(skip_index=True)
    observed = models.Session.load(new_sid).composer_draft
    print('rotation', {'expected': draft, 'observed': observed})
    assert observed == draft


def test_warm_corruption_uses_current_embedded_snapshot(tmp_path, monkeypatch):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    latest = {'text': 'new durable embedded snapshot', 'files': []}
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                           {'session_id': owner.session_id, **latest})
    assert status == 200
    owner.save(skip_index=True)
    embedded = json.loads(owner.path.read_text())['composer_draft']
    assert embedded == latest
    models._session_draft_path(owner.session_id).write_text('{damaged')
    cold = models.Session.load(owner.session_id).composer_draft
    warm = owner.composer_draft
    print('corruption', {'embedded': embedded, 'cold': cold, 'warm': warm})
    assert warm == cold == embedded


def test_export_does_not_include_cleared_private_fallback(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    from urllib.parse import urlparse
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                           {'session_id': owner.session_id, 'text': '', 'files': []})
    assert status == 200
    handler = SimpleNamespace(wfile=io.BytesIO(), send_response=lambda status: None,
                              send_header=lambda *a: None, end_headers=lambda: None)
    monkeypatch.setattr(routes, 'get_session', lambda sid: owner)
    monkeypatch.setattr(routes, 'get_active_profile_name', lambda: owner.profile)
    routes._handle_session_export(handler, urlparse('/api/session/export?session_id=' + owner.session_id))
    payload = json.loads(handler.wfile.getvalue())
    assert payload['composer_draft'] == {'text': '', 'files': []}
    private = {key: value for key, value in payload.items() if key.startswith('_') and 'draft' in key}
    print('export_private_draft_fields', private)
    assert not private
    assert 'legacy' not in handler.wfile.getvalue().decode()


def test_modern_large_metadata_draft_avoids_full_transcript(tmp_path, monkeypatch):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    # Valid modern Session.save output with a large pending prompt before messages.
    owner.pending_user_message = 'p' * (models._METADATA_PREFIX_MAX_BYTES + 1)
    owner.messages[0]['content'] = 'transcript ' * 200_000
    owner.save(skip_index=True)
    models.SESSIONS.clear()
    if hasattr(models, '_draft_owner_metadata'):
        models._draft_owner_metadata.cache_clear()
    real_read = Path.read_text
    whole_reads = []
    def read_text(path, *args, **kwargs):
        if path == owner.path:
            whole_reads.append(path.stat().st_size)
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                           {'session_id': owner.session_id, 'text': 'new draft'})
    assert status == 200
    print('whole_transcript_reads', whole_reads)
    assert not whole_reads
