"""Additional streaming ownership safety and legacy cost checks."""
import json
import os
import statistics
import time
import pytest
from tests.test_review_probes import seed
from tests.test_composer_draft_sidecar_regressions import _post_json


@pytest.mark.parametrize('field', ['source_tag', 'raw_source', 'session_source'])
@pytest.mark.parametrize('broken', [False, True])
def test_late_provenance_is_not_missing_authority(tmp_path, monkeypatch, field, broken):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    doc = {'session_id': owner.session_id, 'profile': None,
           'messages': [{'content': 'm' * 1_100_000}], field: 'subagent'}
    text = json.dumps(doc)
    if broken:
        text = text[:text.rfind(':') + 1]
    owner.path.write_text(text)
    models.SESSIONS.clear()
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    try:
        status, _ = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': owner.session_id, 'text': 'rejected'})
    except (ValueError, KeyError):
        status = 400
    assert status != 200
    assert not models._session_draft_path(owner.session_id).exists()


def test_streaming_decoder_chunk_boundary_equivalence(tmp_path, monkeypatch):
    models, _, _, _, owner = seed(tmp_path, monkeypatch)
    for offset in range(32755, 32785):
        for tail in ['\\', '"', '\n', '\u2603', '\u0000', '\ud800']:
            doc = {'session_id': owner.session_id,
                   'messages': [{'text': 'x' * offset + tail,
                                 'objects': {'bool': True, 'null': None, 'numbers': [-12e5, 0.001], 'arr': []}}],
                   'source_tag': 'subagent', 'profile': None}
            owner.path.write_text(json.dumps(doc))
            got = models._read_draft_owner_fields(owner.path, ('session_id', 'source_tag', 'profile', 'raw_source'))
            assert got == {k: doc[k] for k in ('session_id', 'source_tag', 'profile')}


@pytest.mark.parametrize('layout', ['legacy', 'modern'])
def test_legacy_cost_observation(tmp_path, monkeypatch, layout):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    owner.messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'word ' * 190,
                       'timestamp': i, 'metadata': {'ok': True, 'tags': ['a', 'b']}} for i in range(6000)]
    owner.save(skip_index=True)
    if layout == 'legacy':
        raw = json.loads(owner.path.read_text())
        raw.pop('session_source', None)
        raw.pop('raw_source', None)
        messages = raw.pop('messages')
        # Missing provenance can only be established at EOF.
        raw = {'session_id': owner.session_id, 'messages': messages, **raw}
        owner.path.write_text(json.dumps(raw, ensure_ascii=False))
    size = owner.path.stat().st_size
    models.SESSIONS.clear()
    results = {'cold': [], 'warm': [], 'revision': []}
    scans = {'cold': [], 'warm': [], 'revision': []}
    active_scans = []
    if hasattr(models, '_read_draft_owner_fields'):
        real_scan = models._read_draft_owner_fields
        def scan(path, fields):
            active_scans.append(list(fields))
            return real_scan(path, fields)
        monkeypatch.setattr(models, '_read_draft_owner_fields', scan)
    for index in range(3):
        if hasattr(models, '_draft_owner_metadata'):
            models._draft_owner_metadata.cache_clear()
        for state in ['cold', 'warm', 'revision']:
            if state == 'revision':
                stat = owner.path.stat()
                os.utime(owner.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
            active_scans.clear()
            started = time.perf_counter()
            status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                                   {'session_id': owner.session_id, 'text': f'{index}-{state}'})
            results[state].append((time.perf_counter() - started) * 1000)
            scans[state].append(list(active_scans))
            assert status == 200
            assert owner.session_id not in models.SESSIONS
            if hasattr(models, '_read_draft_owner_fields'):
                assert len(active_scans) == (0 if state == 'warm' else 1)
    record = {'layout': layout, 'bytes': size, 'samples_ms': results,
              'ownership_scan_calls': scans,
              'median_ms': {k: statistics.median(v) for k, v in results.items()}}
    print('REVIEW_BENCH ' + json.dumps(record))


@pytest.mark.parametrize('profile', [None, 'bravo', False, 3, [], {}])
def test_readonly_profile_shares_validated_cache_and_rechecks_revision(tmp_path, monkeypatch, profile):
    models, _, _, _, owner = seed(tmp_path, monkeypatch)
    models.SESSIONS.clear()
    models._draft_owner_metadata.cache_clear()
    calls = []
    real_scan = models._read_draft_owner_fields
    def scan(path, fields):
        calls.append(path)
        return real_scan(path, fields)
    monkeypatch.setattr(models, '_read_draft_owner_fields', scan)
    models.get_session_for_draft(owner.session_id)
    assert len(calls) == 1
    assert models.get_session_profile_readonly(owner.session_id) is None
    assert len(calls) == 1
    raw = json.loads(owner.path.read_text())
    raw['profile'] = profile
    owner.path.write_text(json.dumps(raw))
    if profile is None or isinstance(profile, str):
        assert models.get_session_profile_readonly(owner.session_id) == profile
    else:
        with pytest.raises(ValueError, match='Invalid session profile'):
            models.get_session_profile_readonly(owner.session_id)
    assert len(calls) == 2
    assert owner.session_id not in models.SESSIONS
    owner.path.unlink()
    with pytest.raises(KeyError):
        models.get_session_profile_readonly(owner.session_id)
    with pytest.raises(KeyError):
        models.get_session_profile_readonly('../unsafe')
    assert len(calls) == 2


def test_readonly_profile_rejects_changed_owner_identity(tmp_path, monkeypatch):
    models, _, _, _, owner = seed(tmp_path, monkeypatch)
    models.SESSIONS.clear()
    models.get_session_for_draft(owner.session_id)
    raw = json.loads(owner.path.read_text())
    raw['session_id'] = 'other-owner'
    owner.path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='Invalid session ownership metadata'):
        models.get_session_profile_readonly(owner.session_id)
