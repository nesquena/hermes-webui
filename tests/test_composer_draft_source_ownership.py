"""Explicit foreign session sources must not borrow the active store's drafts."""
import json

import pytest


@pytest.fixture
def stores(tmp_path, monkeypatch):
    import api.models as models

    active = tmp_path / 'active'
    foreign = tmp_path / 'foreign'
    for directory in (active, foreign):
        (directory / '.drafts').mkdir(parents=True)
    monkeypatch.setattr(models, 'SESSION_DIR', active)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', active / '_index.json')
    monkeypatch.setattr(models, 'SESSIONS', {})
    sid = 'same-source-sid'
    embedded = {'text': 'foreign embedded', 'files': ['foreign-file']}
    for directory, draft in ((active, {'text': 'active embedded'}), (foreign, embedded)):
        (directory / f'{sid}.json').write_text(json.dumps({
            'session_id': sid, 'title': 'Synthetic session',
            'created_at': 1, 'updated_at': 2, 'composer_draft': draft,
            'messages': [{'role': 'user', 'content': 'synthetic message'}],
        }), encoding='utf-8')
    (active / '.drafts' / f'{sid}.json').write_text(
        json.dumps({'text': 'active live', 'files': ['active-file']}), encoding='utf-8')
    (foreign / '.drafts' / f'{sid}.json').write_text(
        json.dumps({'text': 'foreign live must not overlay explicit foreign read'}), encoding='utf-8')
    return models, active, foreign, sid, embedded


@pytest.mark.parametrize('projection', ['compact', 'rebuilt-index'])
def test_explicit_foreign_source_keeps_embedded_draft(stores, projection):
    models, active, foreign, sid, embedded = stores
    before = (foreign / f'{sid}.json').read_bytes()
    if projection == 'compact':
        session = models._load_session_from_path(foreign / f'{sid}.json')
        actual = session.compact()['composer_draft']
    else:
        index = foreign / '_index.json'
        models._write_session_index(session_dir=foreign, session_index_file=index)
        rows = json.loads(index.read_text(encoding='utf-8'))
        assert [row['session_id'] for row in rows] == [sid]
        actual = rows[0]['composer_draft']
    assert actual == embedded
    assert (foreign / f'{sid}.json').read_bytes() == before


@pytest.mark.parametrize('loader', ['explicit-path', 'load', 'load_metadata_only'])
def test_active_source_keeps_live_overlay_and_embedded_fallback(stores, loader):
    models, active, _, sid, _ = stores
    path = active / f'{sid}.json'
    embedded = json.loads(path.read_text(encoding='utf-8'))['composer_draft']
    session = (models._load_session_from_path(path) if loader == 'explicit-path'
               else getattr(models.Session, loader)(sid))
    latest = {'text': 'latest active live', 'files': ['latest-file']}
    draft_path = active / '.drafts' / f'{sid}.json'
    draft_path.write_text(json.dumps(latest), encoding='utf-8')
    assert session.compact()['composer_draft'] == latest
    draft_path.write_text('{damaged', encoding='utf-8')
    assert session.composer_draft == embedded


def test_foreign_source_never_consults_active_draft_store(stores, monkeypatch):
    models, _, foreign, sid, embedded = stores
    session = models._load_session_from_path(foreign / f'{sid}.json')
    monkeypatch.setattr(models, '_load_session_draft',
                        lambda *args: pytest.fail('foreign source consulted active drafts'))
    assert session.composer_draft == embedded
    assert session.compact()['composer_draft'] == embedded
