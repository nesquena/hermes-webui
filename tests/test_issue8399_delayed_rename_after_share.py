"""#8399 release fold: a writer's delayed save must not orphan a newer share.

A rename reloads the session between the share handler's pre-save mirror and
the sidecar patch, then pauses. If the pair were then moved onto that cached
copy WITHOUT its stale-save baseline, the copy would carry the new pair as its
own change: after an eviction and a newer share, its delayed save wrote the old
token back over the new one, and a later revoke returned 200 while the newer
public URL stayed live. Found by the Codex review of the first fold.

Real in-process route handlers, real Session persistence, the real LRU eviction
check and disposable session/share files.
"""
import pytest
from urllib.parse import urlparse
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


@pytest.mark.parametrize('trigger', ['create', 'revoke'])
def test_delayed_rename_does_not_orphan_a_newer_share(isolated_state, monkeypatch, trigger):
    import threading
    import api.models as models
    import api.routes as routes
    import api.session_ops as session_ops
    sid = _make_session('fold_real_rename')
    models.get_session(sid)
    original_token = None
    if trigger == 'revoke':
        status, initial = _post('/api/share/create', {'session_id': sid})
        assert status == 200
        original_token = initial['share']['token']
    filler_sid = _make_session('fold_cache_pressure')
    models.get_session(filler_sid)
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
        paused.set()
        assert resume.wait(10)
    monkeypatch.setattr(session_ops, 'apply_session_title_rename', rename_and_pause)
    def writer():
        try:
            results['rename'] = _post('/api/session/rename', {'session_id': sid, 'title': 'delayed rename'})
        except BaseException as e:
            results['error'] = repr(e)
    rename = threading.Thread(target=writer, name='rename')
    real_persist = routes._persist_share_fields
    injected = []
    def persist(*args, **kw):
        if not injected:
            injected.append(True)
            evict_target()
            rename.start()
            assert paused.wait(10), results
            cached = models.SESSIONS[sid]
            assert cached.share_token == original_token and cached._share_token_saved == original_token
        return real_persist(*args, **kw)
    monkeypatch.setattr(routes, '_persist_share_fields', persist)
    try:
        status, first = _post('/api/share/' + trigger, {'session_id': sid})
        assert status == 200
        first_token = original_token if trigger == 'revoke' else first['share']['token']
        # Eviction is performed by the actual LRU safety check, not by a direct
        # replacement. An in-progress title rename does not pin this cache row.
        evict_target()
        if trigger == 'create':
            status, revoked = _post('/api/share/revoke', {'session_id': sid})
            assert status == 200, revoked
        status, second = _post('/api/share/create', {'session_id': sid})
        assert status == 200, second
        second_token = second['share']['token']
        assert first_token != second_token
        assert _get('/api/share/' + first_token)[0] == 404
    finally:
        resume.set()
        rename.join(10)
    assert not rename.is_alive() and 'error' not in results, results
    assert results['rename'][0] == 200, results
    assert _sidecar(sid)['title'] == 'delayed rename'
    recorded = _sidecar(sid)['share_token']
    # The request now resolves disk state. A successful revoke must invalidate U.
    models.SESSIONS.clear()
    status, body = _post('/api/share/revoke', {'session_id': sid})
    assert status == 200, body
    public_status, _ = _get('/api/share/' + second_token)
    assert public_status == 404, (recorded, first_token, second_token, public_status)
