"""Draft authorization follows the caller, not stale cached ownership."""
from collections import OrderedDict
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


@pytest.fixture
def draft_env(tmp_path, monkeypatch):
    import api.models as models
    import api.routes as routes

    directory = tmp_path / 'sessions'
    directory.mkdir()
    cache = OrderedDict()
    for module in (models, routes):
        monkeypatch.setattr(module, 'SESSION_DIR', directory)
        monkeypatch.setattr(module, 'SESSION_INDEX_FILE', directory / '_index.json')
        monkeypatch.setattr(module, 'SESSIONS', cache)
    active = ['default']
    monkeypatch.setattr(routes, '_get_active_profile_name', lambda: active[0])
    monkeypatch.setattr(routes, 'get_active_profile_name', lambda: active[0])
    monkeypatch.setattr(routes, '_check_csrf', lambda h: True)
    monkeypatch.setattr(routes, 'bad', lambda h, message, status=400: routes.j(h, {'error': message}, status=status))
    return models, routes, active


def request(monkeypatch, routes, sid, method='POST'):
    result = {}
    monkeypatch.setattr(routes, 'read_body', lambda h: {'session_id': sid, 'text': 'caller text'})
    monkeypatch.setattr(routes, 'j', lambda h, payload, status=200, **kw: result.update(status=status, payload=payload) or True)
    parsed = urlparse('/api/session/draft?session_id=' + sid)
    assert routes.handle_post(SimpleNamespace(command=method), parsed) is True
    return result['status'], result['payload']


def save_owner(models, sid, profile, **kwargs):
    owner = models.Session(session_id=sid, profile=profile, **kwargs)
    owner.save(touch_updated_at=False, skip_index=True)
    return owner


PRIVATE_DRAFT = {
    'text': 'bravo private text',
    'files': [{'name': 'bravo-private.txt', 'path': '/synthetic/bravo-private.txt'}],
    'opaque': {'private': 'bravo opaque field'},
}


@pytest.mark.parametrize('method', ['POST', 'GET'])
@pytest.mark.parametrize('storage', ['embedded', 'sidecar'])
@pytest.mark.parametrize('cached', [True, False], ids=['stale-cache', 'cold-cache'])
def test_current_durable_owner_overrules_cached_default(draft_env, monkeypatch, method, storage, cached):
    models, routes, _ = draft_env
    sid = 'draft-profile-owner'
    old = save_owner(models, sid, 'default', composer_draft={'text': 'former default draft'})
    models.SESSIONS[sid] = old
    foreign = save_owner(models, sid, 'bravo', composer_draft=PRIVATE_DRAFT)
    if storage == 'sidecar':
        models._save_session_draft(sid, PRIVATE_DRAFT)
    if not cached:
        models.SESSIONS.clear()
    owner_before = foreign.path.read_bytes()
    draft_path = models._session_draft_path(sid)
    draft_before = draft_path.read_bytes() if draft_path.exists() else None

    status, payload = request(monkeypatch, routes, sid, method)

    # Report non-disclosure and non-mutation independently of the HTTP result.
    outcome = {
        'draft_disclosed': 'draft' in payload,
        'owner_changed': foreign.path.read_bytes() != owner_before,
        'draft_changed': (draft_path.read_bytes() if draft_path.exists() else None) != draft_before,
    }
    assert outcome == {'draft_disclosed': False, 'owner_changed': False, 'draft_changed': False}
    assert status in (404, 409)


@pytest.mark.parametrize('switch_active', [False, True], ids=['same-caller', 'active-profile-changed'])
def test_owner_is_revalidated_under_lifecycle_against_original_caller(draft_env, monkeypatch, switch_active):
    models, routes, active = draft_env
    sid = 'draft-profile-lifecycle'
    old = save_owner(models, sid, 'default', composer_draft={'text': 'default draft'})
    models.SESSIONS[sid] = old
    real_lock = routes._draft_lifecycle_lock_for_session_id
    before = []

    @contextmanager
    def publish_foreign_owner(lock_sid):
        with real_lock(lock_sid):
            if not before:
                foreign = save_owner(models, sid, 'bravo', composer_draft=PRIVATE_DRAFT)
                before.append(foreign.path.read_bytes())
                if switch_active:
                    active[0] = 'bravo'
            yield

    monkeypatch.setattr(routes, '_draft_lifecycle_lock_for_session_id', publish_foreign_owner)
    status, payload = request(monkeypatch, routes, sid)
    assert status in (404, 409) and 'draft' not in payload
    assert old.path.read_bytes() == before[0]
    assert not models._session_draft_path(sid).exists()


@pytest.mark.parametrize('foreign_at', ['parent', 'child', 'grandchild', 'child-lock'])
def test_each_compression_successor_uses_original_request_profile(draft_env, monkeypatch, foreign_at):
    models, routes, active = draft_env
    parent_sid, child_sid, final_sid = 'draft-profile-parent', 'draft-profile-child', 'draft-profile-final'
    parent = save_owner(models, parent_sid, 'default', pre_compression_snapshot=True,
                        pre_compression_continuation_session_id=child_sid)
    child = save_owner(models, child_sid, 'default', parent_session_id=parent_sid,
                       pre_compression_snapshot=True, pre_compression_continuation_session_id=final_sid)
    final = save_owner(models, final_sid, 'default', parent_session_id=child_sid, composer_draft=PRIVATE_DRAFT)
    owners = {'parent': parent, 'child': child, 'grandchild': final}
    models.SESSIONS.update({s.session_id: s for s in (parent, child, final)})
    if foreign_at != 'child-lock':
        victim = owners[foreign_at]
        # Leave an older default Session cached, as in the ordinary POST case.
        foreign = save_owner(models, victim.session_id, 'bravo',
                             parent_session_id=victim.parent_session_id,
                             pre_compression_snapshot=victim.pre_compression_snapshot,
                             pre_compression_continuation_session_id=victim.pre_compression_continuation_session_id,
                             composer_draft=PRIVATE_DRAFT)
        assert foreign.profile == 'bravo'
    else:
        real_lock = routes._draft_lifecycle_lock_for_session_id

        @contextmanager
        def change_child(lock_sid):
            with real_lock(lock_sid):
                if lock_sid == child_sid:
                    save_owner(models, child_sid, 'bravo', parent_session_id=parent_sid,
                               pre_compression_snapshot=True,
                               pre_compression_continuation_session_id=final_sid, composer_draft=PRIVATE_DRAFT)
                    active[0] = 'bravo'
                yield

        monkeypatch.setattr(routes, '_draft_lifecycle_lock_for_session_id', change_child)
    status, payload = request(monkeypatch, routes, parent_sid)
    assert status in (404, 409) and 'draft' not in payload
    assert payload.get('code') != 'session_moved'
    assert not any(models._session_draft_path(sid).exists() for sid in (parent_sid, child_sid, final_sid))
