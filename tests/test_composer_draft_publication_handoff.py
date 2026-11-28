"""Publication snapshots and streaming ownership extraction boundaries."""
import builtins
import copy
import io
import json
from pathlib import Path

import pytest
from tests.test_review_probes import seed
from tests.test_composer_draft_sidecar_regressions import _post_json


@pytest.mark.parametrize('draft', [
    {'text': 'acknowledged draft', 'files': ['new-file']},
    {'text': '', 'files': []},
])
@pytest.mark.parametrize('failure', ['preserve', 'publish'])
def test_rotation_failure_restores_draft_handoff_state(tmp_path, monkeypatch, draft, failure):
    models, routes, streaming, _, owner = seed(tmp_path, monkeypatch)
    old_sid, new_sid = owner.session_id, 'handoffrollbackchild'
    assert _post_json(monkeypatch, routes, '/api/session/draft',
                      {'session_id': old_sid, **draft})[0] == 200
    fallback = copy.deepcopy(owner._composer_draft)
    before = owner.path.read_bytes()
    if failure == 'preserve':
        monkeypatch.setattr(streaming, '_preserve_pre_compression_snapshot', lambda *a: False)
    else:
        def fail_publish(*args):
            assert owner._composer_draft == draft
            assert owner._composer_draft_session_id == new_sid
            raise RuntimeError('publication failed')
        monkeypatch.setattr(streaming, '_evict_sessions_over_cap', fail_publish)
    lock = routes._get_session_agent_lock(old_sid)
    with lock:
        assert not streaming._migrate_compression_session_ownership(owner, old_sid, new_sid, lock)
    assert owner.session_id == owner._composer_draft_session_id == old_sid
    assert owner._composer_draft == fallback
    assert owner.composer_draft == draft
    assert owner.path.read_bytes() == before
    assert models.SESSIONS.get(old_sid) is owner
    assert new_sid not in models.SESSIONS
    assert not models._session_draft_path(new_sid).exists()
    models._session_draft_path(old_sid).unlink()
    assert owner.composer_draft == fallback


@pytest.mark.parametrize('skip_index', [False, True])
@pytest.mark.parametrize('failure', ['none', 'replace', 'index'])
def test_embedded_fallback_advances_at_owner_publication(tmp_path, monkeypatch, skip_index, failure):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    old = copy.deepcopy(owner._composer_draft)
    latest = {'text': 'latest durable snapshot', 'files': []}
    assert _post_json(monkeypatch, routes, '/api/session/draft',
                      {'session_id': owner.session_id, **latest})[0] == 200
    assert owner.composer_draft == latest
    assert owner._composer_draft == old  # A read is not an embedded publication.
    def fail(*args, **kwargs):
        raise OSError('publication failed')
    if failure == 'replace':
        monkeypatch.setattr(models, '_replace_session_sidecar', fail)
    elif failure == 'index':
        monkeypatch.setattr(models, '_write_session_index', fail)
    raises = failure == 'replace' or (failure == 'index' and not skip_index)
    if raises:
        with pytest.raises(OSError):
            owner.save(skip_index=skip_index)
    else:
        owner.save(skip_index=skip_index)
    expected = old if failure == 'replace' else latest
    assert json.loads(owner.path.read_text())['composer_draft'] == expected
    models._session_draft_path(owner.session_id).write_text('{damaged')
    assert owner.composer_draft == models.Session.load(owner.session_id).composer_draft == expected


def test_private_draft_state_is_absent_from_public_projections(tmp_path, monkeypatch):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    from api.helpers import public_session_projection, redact_session_data
    from api.shares import build_share_snapshot
    assert _post_json(monkeypatch, routes, '/api/session/draft',
                      {'session_id': owner.session_id, 'text': '', 'files': []})[0] == 200
    raw = {**owner.__dict__, 'composer_draft': owner.composer_draft}
    for payload in [redact_session_data(raw), public_session_projection(raw),
                    owner.compact(), build_share_snapshot(owner)]:
        assert not set(payload) & {'_composer_draft', '_composer_draft_session_id', '_draft_identity_persisted'}
        assert 'legacy' not in json.dumps(payload)
    assert public_session_projection(raw)['composer_draft'] == {'text': '', 'files': []}


@pytest.mark.parametrize('transcript_size', [2_000_000, 6_000_000])
def test_oversized_modern_owner_read_budget_is_independent_of_transcript(tmp_path, monkeypatch, transcript_size):
    models, routes, _, _, owner = seed(tmp_path, monkeypatch)
    owner.pending_user_message = ('p' * 65500 + '\\"☃\n') * 18
    owner.messages[0]['content'] = 'm' * transcript_size
    owner.save(skip_index=True)
    models.SESSIONS.clear()
    models._draft_owner_metadata.cache_clear()
    sizes = []
    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            data = self.stream.read(size)
            sizes.append(len(data.encode('utf-8')) if isinstance(data, str) else len(data))
            return data
        def __getattr__(self, name):
            assert name not in {'readline', 'readlines', '__iter__'}
            return getattr(self.stream, name)
    def wrap(original):
        def open_file(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return BoundedReader(stream) if Path(path) == owner.path else stream
        return open_file
    monkeypatch.setattr(builtins, 'open', wrap(builtins.open))
    monkeypatch.setattr(io, 'open', wrap(io.open))
    # Exercise the route's two ownership resolutions, which share a stat cache.
    assert _post_json(monkeypatch, routes, '/api/session/draft',
                      {'session_id': owner.session_id, 'text': 'small draft'})[0] == 200
    assert sizes
    assert sum(sizes) < len(json.dumps(owner.pending_user_message, ensure_ascii=False).encode()) + 128 * 1024


@pytest.mark.parametrize('padding', [0, 65523, 65530, 65535, 65536, 131071])
def test_streaming_owner_handles_escaped_chunk_boundaries_and_late_provenance(tmp_path, monkeypatch, padding):
    models, _, _, _, owner = seed(tmp_path, monkeypatch)
    document = {'session_id': owner.session_id,
                'messages': [{'content': 'x' * padding + '\\"☃\n',
                              'source_tag': 'forged nested source',
                              'nested': [None, True, False, -12.5e10, {}, []]}],
                'source_tag': 'subagent', 'profile': None}
    owner.path.write_text(json.dumps(document, ensure_ascii=False))
    observed = models._read_draft_owner_fields(owner.path, ('session_id', 'source_tag', 'profile', 'raw_source'))
    assert observed == {k: document[k] for k in ('session_id', 'source_tag', 'profile')}
    assert models.get_session_for_draft(owner.session_id).source_tag == 'subagent'


@pytest.mark.parametrize('broken', [
    '{"session_id":"owner", "messages": ["unterminated]',
    '{"session_id":"owner", "messages": ["\\q"]}',
    '{"session_id":"owner", "messages": ["\\u12x4"]}',
    '{"session_id":"owner", "messages": [true false]}',
    '{"session_id":"owner", "messages": [{"x": 1,}]}',
    '{"session_id":"owner", "messages": [1,]}',
    '{"session_id":"owner", "messages": []} trailing',
])
def test_streaming_owner_rejects_malformed_skipped_values(tmp_path, monkeypatch, broken):
    models, _, _, _, owner = seed(tmp_path, monkeypatch)
    owner.path.write_text(broken)
    with pytest.raises(ValueError):
        models._read_draft_owner_fields(owner.path, ('session_id', 'source_tag'))
