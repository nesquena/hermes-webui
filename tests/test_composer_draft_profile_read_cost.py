"""Profile-only ownership reads do not require absent legacy draft provenance."""
import builtins
import io
import json
import os
from pathlib import Path

import pytest
from tests.test_review_probes import seed


@pytest.fixture
def profile_store(tmp_path, monkeypatch):
    models, _, _, _, owner = seed(tmp_path, monkeypatch)
    models.SESSIONS.clear()
    models._draft_owner_metadata.cache_clear()
    return models, owner


def track_reads(monkeypatch, path):
    reads = []
    real_open, real_io_open = builtins.open, io.open

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size=-1):
            result = self.stream.read(size)
            reads.append(len(result.encode('utf-8')) if isinstance(result, str) else len(result))
            return result

    def instrument(opener):
        def open_file(filename, *args, **kwargs):
            stream = opener(filename, *args, **kwargs)
            return Reader(stream) if isinstance(filename, (str, Path)) and Path(filename) == path else stream
        return open_file

    monkeypatch.setattr(builtins, 'open', instrument(real_open))
    monkeypatch.setattr(io, 'open', instrument(real_io_open))
    return reads


@pytest.mark.parametrize('layout', ['header', 'after_messages'])
def test_legacy_profile_read_returned_bytes(profile_store, monkeypatch, layout):
    models, owner = profile_store
    identity = {'session_id': owner.session_id, 'profile': 'bravo'}
    transcript = {'messages': [{'role': 'user', 'content': 'x' * 4_194_000}]}
    document = {**identity, **transcript} if layout == 'header' else {**transcript, **identity}
    text = json.dumps(document)
    # Match the reported ASCII fixture size without depending on SID length.
    document['messages'][0]['content'] += 'x' * (4_194_413 - len(text))
    text = json.dumps(document)
    owner.path.write_text(text, encoding='utf-8')
    assert owner.path.stat().st_size == 4_194_413
    before = owner.path.stat()
    reads = track_reads(monkeypatch, owner.path)
    assert models.get_session_profile_readonly(owner.session_id) == 'bravo'
    returned = sum(reads)
    print(json.dumps({'layout': layout, 'file_bytes': before.st_size,
                      'read_calls': len(reads), 'returned_bytes': returned}))
    if layout == 'header':
        assert 0 < returned <= 65_536
    else:
        assert returned == before.st_size
    count = len(reads)
    assert models.get_session_profile_readonly(owner.session_id) == 'bravo'
    assert len(reads) == count
    assert owner.session_id not in models.SESSIONS
    assert owner.path.stat() == before


@pytest.mark.parametrize('replacement', [False, True])
def test_profile_cache_rechecks_file_revision(profile_store, monkeypatch, replacement):
    models, owner = profile_store
    original = json.dumps({'session_id': owner.session_id, 'profile': 'alpha', 'messages': []})
    owner.path.write_text(original)
    before = owner.path.stat()
    reads = track_reads(monkeypatch, owner.path)
    assert models.get_session_profile_readonly(owner.session_id) == 'alpha'
    count = len(reads)
    assert models.get_session_profile_readonly(owner.session_id) == 'alpha'
    assert len(reads) == count
    changed = original.replace('alpha', 'bravo')
    if replacement:
        temporary = owner.path.with_suffix('.replacement')
        temporary.write_text(changed)
        temporary.replace(owner.path)
    else:
        owner.path.write_text(changed)
    # Same size and restored mtime still require ctime/inode invalidation.
    os.utime(owner.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert models.get_session_profile_readonly(owner.session_id) == 'bravo'
    assert len(reads) > count
    assert owner.session_id not in models.SESSIONS
    owner.path.unlink()
    with pytest.raises(KeyError):
        models.get_session_profile_readonly(owner.session_id)


@pytest.mark.parametrize('suffix,expected', [
    (', "profile": "bravo", "messages": []}', 'bravo'),
    (', "profile": 3, "messages": []}', ValueError),
    (', "session_id": "different", "messages": []}', ValueError),
    (', "broken": tru, "messages": []}', ValueError),
    (', "broken": "\\q", "messages": []}', ValueError),
    (', "broken": [1,], "messages": []}', ValueError),
    (', "messages" []}', ValueError),
    (', "messages":', ValueError),
    ('', ValueError),
    ('} trailing', ValueError),
    (', "messages": null, "profile": "bravo"}', 'bravo'),
    (', "messages": {}, "profile": "bravo"}', 'bravo'),
    (', "messages": [], "profile": "bravo"}', 'alpha'),
])
def test_profile_header_validation_and_duplicate_semantics(profile_store, suffix, expected):
    models, owner = profile_store
    header = json.dumps({'session_id': owner.session_id, 'profile': 'alpha'})[:-1]
    owner.path.write_text(header + suffix)
    if expected is ValueError:
        with pytest.raises(ValueError):
            models.get_session_profile_readonly(owner.session_id)
    else:
        assert models.get_session_profile_readonly(owner.session_id) == expected


@pytest.mark.parametrize('late', [False, True])
def test_profile_missing_header_field_validates_complete_document(profile_store, late):
    models, owner = profile_store
    document = {'session_id': owner.session_id, 'messages': [{'text': 'x' * 70_000}]}
    if late:
        document['profile'] = 'bravo'
    owner.path.write_text(json.dumps(document))
    assert models.get_session_profile_readonly(owner.session_id) == ('bravo' if late else None)
    owner.path.write_text(json.dumps(document)[:-1])
    with pytest.raises(ValueError):
        models.get_session_profile_readonly(owner.session_id)


@pytest.mark.parametrize('field', ['source_tag', 'raw_source', 'session_source'])
def test_profile_only_cache_cannot_establish_missing_draft_provenance(profile_store, monkeypatch, field):
    from tests.test_composer_draft_sidecar_regressions import _post_json
    import api.routes as routes
    models, owner = profile_store
    document = {'session_id': owner.session_id, 'profile': None,
                'messages': [{'text': 'x' * 70_000}], field: 'subagent'}
    owner.path.write_text(json.dumps(document))
    reads = track_reads(monkeypatch, owner.path)
    assert models.get_session_profile_readonly(owner.session_id) is None
    assert sum(reads) <= 65_536
    assert getattr(models.get_session_for_draft(owner.session_id), field) == 'subagent'
    assert sum(reads) > owner.path.stat().st_size
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    status, _ = _post_json(monkeypatch, routes, '/api/session/draft',
                           {'session_id': owner.session_id, 'text': 'forbidden'})
    assert status == 400
    assert not models._session_draft_path(owner.session_id).exists()


def test_late_profile_duplicates_keep_last_value(profile_store):
    models, owner = profile_store
    document = json.dumps({'messages': [{'text': 'x' * 70_000}],
                           'session_id': owner.session_id, 'profile': 'alpha'})
    owner.path.write_text(document[:-1] + ', "profile": "bravo"}')
    assert models.get_session_profile_readonly(owner.session_id) == 'bravo'


def test_profile_cache_does_not_validate_corrupt_legacy_transcript(profile_store):
    models, owner = profile_store
    header = json.dumps({'session_id': owner.session_id, 'profile': 'alpha'})[:-1]
    owner.path.write_text(header + ', "messages": [invalid')
    assert models.get_session_profile_readonly(owner.session_id) == 'alpha'
    with pytest.raises(ValueError):
        models.get_session_for_draft(owner.session_id)
