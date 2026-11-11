"""Bounded cold reads and fresh persisted ownership on draft requests."""
import builtins
import io
import json
from pathlib import Path

import pytest
from tests.test_composer_draft_fast_path import seed
from tests.test_composer_draft_sidecar_regressions import _build_draft_env, _post_json


@pytest.fixture(autouse=True)
def capture_bad_responses(monkeypatch):
    import api.routes as routes
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))


def test_cold_modern_draft_does_not_read_full_transcript(tmp_path, monkeypatch):
    models, routes, _, s = seed(tmp_path, monkeypatch)
    s.messages[0]['content'] = 'large transcript body ' * 100_000
    s.save(skip_index=True)
    transcript = s.path
    models.SESSIONS.clear()
    if hasattr(models, '_draft_owner_metadata'):
        models._draft_owner_metadata.cache_clear()
    real_open, real_io_open = builtins.open, io.open
    real_read_text, real_read_bytes = Path.read_text, Path.read_bytes
    reads = []

    class BoundedTranscript:
        def __init__(self, stream):
            self.stream = stream
            self.total = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size=-1):
            if size < 0:
                pytest.fail('draft save read full transcript directly')
            self.total += size
            assert self.total <= models._METADATA_PREFIX_FIRST_STAGE_BYTES, 'modern metadata budget exceeded'
            reads.append(size)
            return self.stream.read(size)
        def __iter__(self):
            pytest.fail('draft save iterated full transcript')
        def readline(self, *args):
            pytest.fail('draft save scanned transcript lines')
        def readlines(self, *args):
            pytest.fail('draft save read all transcript lines')

    def guard_open(opener):
        def guarded(path, *a, **kw):
            stream = opener(path, *a, **kw)
            return BoundedTranscript(stream) if isinstance(path, (str, Path)) and Path(path) == transcript else stream
        return guarded

    def guard_full_read(reader):
        def guarded(path, *a, **kw):
            if path == transcript:
                pytest.fail('draft save read full transcript directly')
            return reader(path, *a, **kw)
        return guarded

    monkeypatch.setattr(Path, 'read_text', guard_full_read(real_read_text))
    monkeypatch.setattr(Path, 'read_bytes', guard_full_read(real_read_bytes))
    monkeypatch.setattr(builtins, 'open', guard_open(real_open))
    monkeypatch.setattr(io, 'open', guard_open(real_io_open))
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': s.session_id, 'text': 'cold'})
    assert status == 200 and reads


@pytest.mark.parametrize('source_signal', [None, 'source_tag', 'raw_source', 'session_source'])
def test_cached_owner_uses_fresh_replaced_metadata(tmp_path, monkeypatch, source_signal):
    models, routes, directory, s = seed(tmp_path, monkeypatch)
    raw = json.loads(s.path.read_text())
    raw['composer_draft'] = {'text': 'new', 'files': ['new-file']}
    if source_signal:
        raw[source_signal] = 'subagent'
    replacement = directory/'replacement.tmp'
    replacement.write_text(json.dumps(raw))
    replacement.replace(s.path)
    before = s.path.read_bytes(), s.path.stat().st_mtime_ns
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': s.session_id, 'text': 'edit'})
    if source_signal:
        assert status == 400 and 'view-only' in payload['error']
        assert not models._session_draft_path(s.session_id).exists()
    else:
        assert status == 200 and payload['draft'] == {'text': 'edit', 'files': ['new-file']}
    assert (s.path.read_bytes(), s.path.stat().st_mtime_ns) == before


@pytest.mark.parametrize('source_signal', ['source_tag', 'raw_source', 'session_source'])
def test_legacy_provenance_after_messages_is_still_view_only(tmp_path, monkeypatch, source_signal):
    models, routes, directory = _build_draft_env(tmp_path, monkeypatch)
    sid = 'legacydraftsource'
    raw = {'session_id': sid, 'messages': [], source_signal: 'subagent'}
    (directory/f'{sid}.json').write_text(json.dumps(raw))
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': sid, 'text': 'not allowed'})
    assert status == 400 and 'view-only' in payload['error']
    assert not models._session_draft_path(sid).exists()


def test_legacy_draft_read_has_no_transcript_self_heal(tmp_path, monkeypatch):
    models, routes, directory = _build_draft_env(tmp_path, monkeypatch)
    sid = 'legacydraftlayout'
    raw = {'session_id': sid, 'messages': [{'role': 'assistant', 'content': 'partial', 'partial': True}]*2,
           'composer_draft': {'text': 'legacy', 'files': ['keep']}}
    path = directory/f'{sid}.json'
    path.write_text(json.dumps(raw))
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(models.Session, 'save', lambda *a, **kw: pytest.fail('legacy transcript rewritten'))
    status, payload = _post_json(monkeypatch, routes, '/api/session/draft', {'session_id': sid, 'text': 'new'})
    assert status == 200 and payload['draft'] == {'text': 'new', 'files': ['keep']}
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sid not in models.SESSIONS


@pytest.mark.parametrize('loader', ['load', 'load_metadata_only'])
def test_damaged_record_after_load_warns_and_restores_embedded_snapshot(tmp_path, monkeypatch, caplog, loader):
    models, _, _, original = seed(tmp_path, monkeypatch)
    legacy = original.composer_draft
    models._save_session_draft(original.session_id, {'text': 'newer dedicated draft'})
    cached = getattr(models.Session, loader)(original.session_id)
    assert cached.composer_draft == {'text': 'newer dedicated draft'}
    models._session_draft_path(original.session_id).write_text('{damaged', encoding='utf-8')
    with caplog.at_level('WARNING'):
        assert cached.composer_draft == legacy
    assert any('Ignoring malformed draft sidecar' in record.getMessage() for record in caplog.records)
