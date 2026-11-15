"""A stale share token must not be restored by a concurrent save.

Found by the stable-lane Codex audit of exp-v0.52.620..621 (pre-existing since
at least v0.52.544). A rename resolves the resident session and pauses between
its read and its save. A share create then mirrors token T onto that resident
object (fields only, no stale-save baseline) and the object is evicted before
the create commits the baseline. The paused rename's object now carries T as if
it were its OWN change. After revoke T (+ optionally create U), resuming the
rename wrote T back over the durable pair: U was orphaned (still publicly
readable, while a later revoke returned 200 against the dead T), or the revoked
T came back into the sidecar.

The share pair is owned by the share lifecycle: a whole-session save may never
write a pair over a durable pair that moved since the object last loaded or
saved it, and a cache-coherence mirror never turns into an own change.

Real in-process route handlers, real Session persistence, the real LRU eviction
check and disposable session/share files. Deterministic: threading.Event
barriers, no sleeps.
"""
import threading
from urllib.parse import urlparse

import pytest

from tests.test_issue8399_share_revocation import (  # noqa: F401  (isolated_state is a fixture)
    _FakeHandler,
    _make_session,
    _post,
    _sidecar,
    isolated_state,
)


def _get(path):
    import api.routes as routes
    handler = _FakeHandler()
    handler.command = 'GET'
    routes.handle_get(handler, urlparse(path))
    return handler.status, handler.json_body()


def _public_status(token):
    return _get('/api/share/' + token)[0]


@pytest.mark.usefixtures('isolated_state')
@pytest.mark.parametrize('reshare', [True, False], ids=['revoke-then-reshare', 'revoke-only'])
def test_paused_rename_does_not_restore_a_revoked_share_token(monkeypatch, reshare):
    import api.models as models
    import api.routes as routes
    import api.session_ops as session_ops

    sid = _make_session('stale_restore_rename')
    models.get_session(sid)  # resident
    filler_sid = _make_session('stale_restore_filler')

    def evict_target():
        models.get_session(filler_sid)
        with models.LOCK:
            assert models._evict_sessions_over_cap(cap=1) == 1
            assert sid not in models.SESSIONS

    paused, resume = threading.Event(), threading.Event()
    results = {}
    real_rename = session_ops.apply_session_title_rename

    def rename_and_pause(session, title):
        real_rename(session, title)
        results['rename_object'] = session
        paused.set()
        assert resume.wait(10)

    monkeypatch.setattr(session_ops, 'apply_session_title_rename', rename_and_pause)

    def writer():
        try:
            results['rename'] = _post('/api/session/rename', {'session_id': sid, 'title': 'paused rename'})
        except BaseException as exc:  # surfaced by the final assertion
            results['error'] = repr(exc)

    rename = threading.Thread(target=writer, name='paused-rename')
    rename.start()
    assert paused.wait(10), results
    rename_object = results['rename_object']
    with models.LOCK:
        assert models.SESSIONS.get(sid) is rename_object, 'the rename must hold the resident object'

    # Evict the mirrored resident between the create's pre-save mirror and its
    # baseline commit (the commit runs inside _persist_share_fields).
    real_persist = routes._persist_share_fields
    injected = []

    def persist(*args, **kw):
        if not injected:
            injected.append(True)
            assert rename_object.share_token, 'the create mirrors its token onto the resident'
            evict_target()
        return real_persist(*args, **kw)

    monkeypatch.setattr(routes, '_persist_share_fields', persist)
    try:
        status, first = _post('/api/share/create', {'session_id': sid})
        assert status == 200, first
        token_t = first['share']['token']
        assert injected, 'the eviction window was not exercised'
        assert _public_status(token_t) == 200

        status, body = _post('/api/share/revoke', {'session_id': sid})
        assert status == 200, body
        assert _public_status(token_t) == 404
        token_u = None
        if reshare:
            status, second = _post('/api/share/create', {'session_id': sid})
            assert status == 200, second
            token_u = second['share']['token']
            assert token_u != token_t, 'a reshare after revoke must mint a fresh token'
            assert _public_status(token_u) == 200
    finally:
        resume.set()
        rename.join(10)
    assert not rename.is_alive() and 'error' not in results, results
    assert results['rename'][0] == 200, results

    stored = _sidecar(sid)
    with models.LOCK:
        resident = models.SESSIONS.get(sid)
    resident_token = getattr(resident, 'share_token', token_u)
    assert _public_status(token_t) == 404, 'the revoked link is readable again'

    if reshare:
        # The user-visible failure first: revoking the current link must take
        # it down. On the unfixed tree the sidecar names the dead T, so this
        # revoke returns 200 while U stays public.
        status, body = _post('/api/share/revoke', {'session_id': sid})
        assert status == 200, body
        assert _public_status(token_u) == 404, (
            'revoke returned 200 but the current link is still public', stored['share_token'], token_t, token_u,
        )
        assert _sidecar(sid)['share_token'] is None
    assert stored['title'] == 'paused rename', "the rename's own change was dropped"
    assert stored['share_token'] == token_u, (
        'a paused save restored the revoked token over the durable share pair',
        stored['share_token'], token_t, token_u,
    )
    assert resident_token == token_u, 'the resident copy serves a stale token'

    if not reshare:
        # The dead token stays dead: the next share is a fresh link.
        status, again = _post('/api/share/create', {'session_id': sid})
        assert status == 200, again
        assert again['share']['token'] != token_t
        assert _public_status(token_t) == 404


@pytest.mark.usefixtures('isolated_state')
@pytest.mark.parametrize('durable_next', ['U', None], ids=['reshared', 'revoked'])
def test_mirrored_then_evicted_object_never_owns_the_share_pair(durable_next):
    """Model layer: the pre-save mirror is cache coherence, never an own change.

    Any save path of an object that received the mirror and was evicted before
    the baseline commit must adopt the durable pair, including the ABA case
    where the sidecar is back at the value the object loaded (revoke).
    """
    import api.models as models
    import api.routes as routes

    sid = _make_session('stale_restore_mirror')
    resident = models.get_session(sid)
    source = models.Session.load(sid)
    source.share_token, source.share_created_at = 'tok-T', 1.0
    routes._mirror_share_fields_to_cached_session(sid, source)
    assert resident.share_token == 'tok-T'
    with models.LOCK:
        models.SESSIONS.pop(sid, None)  # evicted before the baseline commit
    models.update_session_share_fields(sid, share_token='tok-T', share_created_at=1.0)
    nxt = None if durable_next is None else 'tok-U'
    models.update_session_share_fields(
        sid, share_token=nxt, share_created_at=None if nxt is None else 2.0,
    )

    resident.title = 'late writer'
    resident.save()

    stored = _sidecar(sid)
    assert stored['title'] == 'late writer'
    assert stored['share_token'] == nxt
    assert resident.share_token == nxt


@pytest.mark.usefixtures('isolated_state')
def test_a_save_never_overwrites_a_durable_pair_that_moved_since_its_baseline():
    """Save layer, every writer: a share pair this object set on its own is only
    written as a transition from the durable pair it last saw. When the share
    lifecycle moved the durable pair since then, the lifecycle wins."""
    import api.models as models

    sid = _make_session('stale_restore_cas')
    stale = models.Session.load(sid)
    stale.share_token, stale.share_created_at = 'tok-stale', 1.0
    models.update_session_share_fields(sid, share_token='tok-U', share_created_at=2.0)

    stale.title = 'stale writer'
    stale.save()

    stored = _sidecar(sid)
    assert stored['title'] == 'stale writer'
    assert (stored['share_token'], stored['share_created_at']) == ('tok-U', 2.0)
    assert (stale.share_token, stale.share_created_at) == ('tok-U', 2.0)


@pytest.mark.usefixtures('isolated_state')
def test_an_own_share_change_from_the_durable_baseline_is_still_written():
    """The legitimate whole-object path (no concurrent share write) is unchanged."""
    import api.models as models

    sid = _make_session('stale_restore_own_change')
    owner = models.Session.load(sid)
    owner.share_token, owner.share_created_at = 'tok-own', 3.0
    owner.save()
    stored = _sidecar(sid)
    assert (stored['share_token'], stored['share_created_at']) == ('tok-own', 3.0)
    owner.share_token, owner.share_created_at = None, None
    owner.save()
    assert _sidecar(sid)['share_token'] is None


@pytest.mark.usefixtures('isolated_state')
@pytest.mark.parametrize('operation', ['create', 'revoke'])
def test_a_failed_share_persist_leaves_the_resident_owning_the_new_pair(monkeypatch, operation):
    """Failure path of the mirror's baseline move: when the share-field patch
    raises after the snapshot was published (create) or tombstoned (revoke),
    the resident still owns the new pair, so its next save writes it and the
    link stays reachable by revoke (create) or the dead token stays out of the
    sidecar (revoke)."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares

    sid = _make_session('stale_restore_failed_persist')
    resident = models.get_session(sid)
    token = None
    if operation == 'revoke':
        status, created = _post('/api/share/create', {'session_id': sid})
        assert status == 200, created
        token = created['share']['token']
        assert resident.share_token == token

    real_patch = routes._update_session_share_fields

    def failing_patch(*args, **kw):
        raise OSError('transient disk error')

    monkeypatch.setattr(routes, '_update_session_share_fields', failing_patch)
    try:
        status, _ = _post('/api/share/' + operation, {'session_id': sid})
    except OSError:
        status = 500
    assert status != 200
    monkeypatch.setattr(routes, '_update_session_share_fields', real_patch)

    if operation == 'create':
        token = resident.share_token
        assert token and shares.load_share(token) is not None, 'the create published its snapshot'
        assert _sidecar(sid)['share_token'] is None
    else:
        assert shares.load_share(token) is None, 'the revoke tombstoned the snapshot'
        assert _sidecar(sid)['share_token'] == token

    resident.save()
    expected = token if operation == 'create' else None
    assert _sidecar(sid)['share_token'] == expected
    if operation == 'create':
        status, body = _post('/api/share/revoke', {'session_id': sid})
        assert status == 200, body
        assert shares.load_share(token) is None, 'the published link must stay revocable'


@pytest.mark.usefixtures('isolated_state')
@pytest.mark.parametrize('operation', ['create', 'revoke'])
@pytest.mark.parametrize('window', ['checkpoint', 'evicted', 'replaced'])
def test_a_failed_share_persist_returns_the_pair_to_the_mirrored_object(monkeypatch, window, operation):
    """Codex stage-2 CORE. The share-field patch raises after the handler mirrored
    the new pair onto the resident. Between the mirror and the failure, that
    exact object is checkpointed (the save adopts the old durable pair), evicted
    by the LRU, or replaced by a reload, while a writer still holds it. When the
    writer resumes and saves, the new pair must reach disk: after a create the
    published link stays revocable (a revoke that returns 200 really takes it
    down), after a revoke the tombstoned token stays out of the sidecar."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares
    import api.streaming as streaming

    sid = _make_session('failed_persist_' + window)
    filler_sid = _make_session('failed_persist_filler')
    held = models.get_session(sid)  # the resident; a paused writer holds this object
    old_token = None
    if operation == 'revoke':
        status, created = _post('/api/share/create', {'session_id': sid})
        assert status == 200, created
        old_token = created['share']['token']
        assert held.share_token == old_token

    real_persist = routes._persist_share_fields
    real_patch = routes._update_session_share_fields
    seen = {}

    def window_then_failing_persist(*args, **kw):
        seen['token'] = kw['share_token']
        assert held.share_token == kw['share_token'], 'the mirror reached the held resident'
        if window == 'checkpoint':
            streaming._save_streaming_checkpoint(held)
        elif window == 'evicted':
            models.get_session(filler_sid)
            with models.LOCK:
                assert models._evict_sessions_over_cap(cap=1) == 1
                assert sid not in models.SESSIONS
        else:
            replacement = models.Session.load(sid)
            with models.LOCK:
                models.SESSIONS[sid] = replacement
        return real_persist(*args, **kw)

    def failing_patch(*args, **kw):
        raise OSError('transient disk error')

    monkeypatch.setattr(routes, '_persist_share_fields', window_then_failing_persist)
    monkeypatch.setattr(routes, '_update_session_share_fields', failing_patch)
    try:
        status, _ = _post('/api/share/' + operation, {'session_id': sid})
    except OSError:
        status = 500
    assert status != 200
    monkeypatch.setattr(routes, '_persist_share_fields', real_persist)
    monkeypatch.setattr(routes, '_update_session_share_fields', real_patch)

    # The paused writer resumes and saves the object it holds.
    held.title = 'writer resumed after the failed share persist'
    held.save()
    stored = _sidecar(sid)
    assert stored['title'] == 'writer resumed after the failed share persist'

    if operation == 'create':
        token = seen['token']
        assert token and shares.load_share(token) is not None, 'the create published its snapshot'
        status, body = _post('/api/share/revoke', {'session_id': sid})
        assert status == 200, body
        assert _public_status(token) == 404, 'revoke returned 200 but the published link is still public'
    else:
        assert stored['share_token'] is None, 'the revoked token came back into the sidecar'
        assert _public_status(old_token) == 404
