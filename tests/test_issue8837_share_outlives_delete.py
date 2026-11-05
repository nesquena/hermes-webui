"""#8837: a deleted conversation's public share link must stop working.

Three parts, each driven through the real share, delete and public-share handlers:

1. ``load_share`` serves a snapshot only while the session that published it still
   exists and still names that token. This also closes links that deletes made
   before the fix already orphaned.
2. ``/api/session/delete`` revokes the session's share before it deletes anything.
3. Share create/revoke never bring a deleted session back: they persist under the
   session lock delete holds, refuse a session deleted since the request began,
   and only fall back to writing a new sidecar for an external session that never
   had one.
"""

import io
import json
import secrets
import threading
import urllib.parse
from collections import OrderedDict

import pytest

SID = "s_8837_shared_conversation"
EXTERNAL_SID = "s_8837_external_cli"
WAIT = 10


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for ``handle_post``/``handle_get``."""

    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Sessions, the sidebar index, share snapshots and delete records inside tmp_path."""
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes
    import api.shares as shares

    sessions = OrderedDict()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for module in (models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir)
        monkeypatch.setattr(module, "SESSIONS", sessions)
    index = session_dir / "_index.json"
    for module in (config, models, routes):
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid, **kw: {})
    monkeypatch.setattr(routes, "_DELETED_SESSION_IDS", set())
    monkeypatch.setattr(routes, "_SESSION_DELETE_GENERATIONS", {}, raising=False)
    # These sessions have no state.db rows; the delete's state.db step succeeds.
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)
    routes._session_list_cache_clear()
    yield tmp_path
    routes._session_list_cache_clear()


def _save_session(sid=SID):
    from api import models

    session = models.Session(session_id=sid, title="Shared history")
    session.messages = [
        {"role": "user", "content": "hello", "timestamp": 100},
        {"role": "assistant", "content": "world", "timestamp": 101},
    ]
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    return session.path


def _post(path, body):
    from api.routes import handle_post

    handler = _FakeHandler(json.dumps(body).encode())
    handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _public(token):
    """What an anonymous visitor of the link gets: status and exact body bytes."""
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(f"/api/share/{token}"))
    return handler.status, bytes(handler.body)


def _unknown_token_reply():
    return _public(secrets.token_urlsafe(18))


def _share(sid=SID):
    status, reply = _post("/api/share/create", {"session_id": sid})
    assert status == 200, reply
    return reply["share"]["token"]


def _snapshot_file(token):
    from api import shares

    return json.loads((shares.SHARES_DIR / f"{token}.json").read_text(encoding="utf-8"))


def _index_ids():
    from api import models

    if not models.SESSION_INDEX_FILE.exists():
        return set()
    return {row.get("session_id") for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))}


def _assert_not_served(token):
    from api import shares

    assert shares.load_share(token) is None
    assert _public(token) == _unknown_token_reply()
    assert _public(token)[0] == 404


# ── Part 1: the public route serves only a link its session still holds ──────


def test_share_then_delete_stops_serving_the_link(state):
    from api import shares

    path = _save_session()
    token = _share()
    assert shares.load_share(token) is not None
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 200, reply
    assert not path.exists()
    assert SID not in _index_ids()
    _assert_not_served(token)


def test_link_orphaned_by_a_delete_before_the_fix_is_not_served(state):
    """A delete before the fix removed the sidecar and left the snapshot unrevoked."""
    path = _save_session()
    token = _share()
    path.unlink()
    path.with_suffix(".json.bak").unlink(missing_ok=True)
    assert _snapshot_file(token).get("revoked_at") is None  # still published on disk
    _assert_not_served(token)


def test_snapshot_its_session_no_longer_names_is_not_served(state):
    """A sidecar that dropped the token (a stale write) no longer owns the link."""
    from api import models

    _save_session()
    token = _share()
    assert models.update_session_share_fields(SID, share_token=None, share_created_at=None)
    assert _snapshot_file(token).get("revoked_at") is None
    _assert_not_served(token)


# ── Controls: unchanged behaviour (green before and after the fix) ───────────


def test_control_a_shared_conversation_is_served(state):
    from api import shares

    _save_session()
    token = _share()
    status, body = _public(token)
    assert status == 200
    share = json.loads(body)["share"]
    assert [m["content"] for m in share["messages"]] == ["hello", "world"]
    assert shares.load_share(token)["title"] == "Shared history"


def test_control_a_revoked_link_is_not_served(state):
    _save_session()
    token = _share()
    status, reply = _post("/api/share/revoke", {"session_id": SID})
    assert status == 200, reply
    _assert_not_served(token)


def test_control_sharing_again_after_a_revoke_publishes_a_new_link(state):
    _save_session()
    first = _share()
    assert _post("/api/share/revoke", {"session_id": SID})[0] == 200
    second = _share()
    assert second != first
    assert _public(second)[0] == 200
    _assert_not_served(first)


def _external_session(monkeypatch):
    """A CLI session that lives only in state.db: no WebUI sidecar at all."""
    from api import models, routes

    meta = {"session_id": EXTERNAL_SID, "title": "CLI chat", "source_tag": "cli", "raw_source": "cli"}
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda sid, **kw: dict(meta) if sid == EXTERNAL_SID else {},
    )

    def synthesize(sid, cli_meta=None):
        if sid != EXTERNAL_SID:
            return None, "no_foreign_state"
        session = models.Session(
            session_id=sid,
            title="CLI chat",
            messages=[
                {"role": "user", "content": "from the terminal", "timestamp": 200},
                {"role": "assistant", "content": "answered there", "timestamp": 201},
            ],
            is_cli_session=True,
            source_tag="cli",
            raw_source="cli",
        )
        return session, "materialized"

    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", synthesize)
    return models.SESSION_DIR / f"{EXTERNAL_SID}.json"


def test_control_an_external_session_share_is_served(state, monkeypatch):
    """The token authority for an external session is the metadata sidecar the share writes."""
    path = _external_session(monkeypatch)
    assert not path.exists()
    token = _share(EXTERNAL_SID)
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert sidecar["share_token"] == token
    assert sidecar["messages"] == []  # metadata only; the transcript stays in state.db
    status, body = _public(token)
    assert status == 200
    assert [m["content"] for m in json.loads(body)["share"]["messages"]] == [
        "from the terminal",
        "answered there",
    ]
    assert _post("/api/share/revoke", {"session_id": EXTERNAL_SID})[0] == 200
    _assert_not_served(token)


# ── Part 2: deleting a conversation revokes its share ────────────────────────


def test_delete_revokes_the_share(state):
    _save_session()
    token = _share()
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 200, reply
    assert _snapshot_file(token).get("revoked_at")  # the store itself no longer publishes it
    _assert_not_served(token)


def test_delete_of_an_external_session_revokes_its_share(state, monkeypatch):
    path = _external_session(monkeypatch)
    token = _share(EXTERNAL_SID)
    status, reply = _post("/api/session/delete", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    assert not path.exists()
    assert _snapshot_file(token).get("revoked_at")
    _assert_not_served(token)


def test_a_refused_delete_keeps_the_session_but_not_its_link(state, monkeypatch):
    """Revoke comes first; a delete refused after it leaves a whole session that no longer names the dead link."""
    from api import models

    path = _save_session()
    token = _share()

    def refuse(sid):
        raise models.SessionDeleteRefused("session_busy")

    monkeypatch.setattr(models, "delete_cli_session", refuse)
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 409, reply
    assert path.exists() and SID in _index_ids()
    assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == 2
    assert _snapshot_file(token).get("revoked_at")
    assert json.loads(path.read_text(encoding="utf-8"))["share_token"] is None
    _assert_not_served(token)


def test_a_revoke_that_does_not_land_refuses_the_delete_before_removing_anything(state, monkeypatch):
    from api import routes

    path = _save_session()
    token = _share()
    monkeypatch.setattr(routes, "revoke_share_token", lambda token: False, raising=False)
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 500, reply
    assert reply.get("error") == "share_revoke_failed"
    # Not half-deleted: sidecar, index row and link are all still there.
    assert path.exists() and SID in _index_ids()
    assert _public(token)[0] == 200


# ── Part 3: share create/revoke racing a delete never bring it back ──────────


def _published_tokens():
    """Tokens the snapshot store still publishes (unrevoked snapshot files)."""
    from api import shares

    if not shares.SHARES_DIR.exists():
        return set()
    return {
        path.stem
        for path in shares.SHARES_DIR.glob("*.json")
        if not json.loads(path.read_text(encoding="utf-8")).get("revoked_at")
    }


def _run(name, results, path, body):
    def target():
        try:
            results[name] = _post(path, body)
        except BaseException as exc:  # surfaced by the assertions below
            results[name] = ("raised", repr(exc))

    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread


def _pause_after_resolve(monkeypatch, thread_name):
    """Pause ``thread_name``'s share request right after it resolved the session,
    before it takes any lock (it asks for its share lock next)."""
    from api import routes

    entered, go = threading.Event(), threading.Event()
    real = routes._get_session_share_lock

    def paused(sid):
        if threading.current_thread().name == thread_name:
            entered.set()
            assert go.wait(WAIT)
        return real(sid)

    monkeypatch.setattr(routes, "_get_session_share_lock", paused)
    return entered, go


def _assert_deleted_for_good(path, results):
    assert results["delete"][0] == 200, results
    assert not path.exists(), "the deleted conversation's sidecar came back"
    assert SID not in _index_ids(), "the deleted conversation's index row came back"
    assert _published_tokens() == set(), "a link of the deleted conversation is still published"


def test_share_create_that_resolved_before_a_delete_does_not_bring_it_back(state, monkeypatch):
    path = _save_session()
    entered, go = _pause_after_resolve(monkeypatch, "share")
    results = {}
    share = _run("share", results, "/api/share/create", {"session_id": SID})
    assert entered.wait(WAIT)
    results["delete"] = _post("/api/session/delete", {"session_id": SID})
    assert not path.exists()
    go.set()
    share.join(WAIT)
    assert not share.is_alive()
    _assert_deleted_for_good(path, results)
    assert results["share"][0] == 404, results


def test_share_create_waiting_on_a_running_delete_does_not_bring_it_back(state, monkeypatch):
    """The other order: the delete holds the session first, the share arrives mid-delete."""
    from api import models, routes

    path = _save_session()
    delete_entered, delete_go = threading.Event(), threading.Event()
    share_progressed = threading.Event()

    def paused_state_db_delete(sid):
        delete_entered.set()
        assert delete_go.wait(WAIT)
        return True

    monkeypatch.setattr(models, "delete_cli_session", paused_state_db_delete)
    real_lock = routes._get_session_agent_lock

    class _Spy:
        """The real session lock; notes when the share request starts waiting on it."""

        def __init__(self, lock):
            self._lock = lock

        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "share":
                share_progressed.set()
            return self._lock.acquire(*args, **kwargs)

        def release(self):
            return self._lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: _Spy(real_lock(sid)))
    results = {}
    delete = _run("delete", results, "/api/session/delete", {"session_id": SID})
    assert delete_entered.wait(WAIT)

    def share_target():
        try:
            results["share"] = _post("/api/share/create", {"session_id": SID})
        finally:
            share_progressed.set()

    share = threading.Thread(target=share_target, name="share")
    share.start()
    # Either the share finished while the delete was paused (it never waited on the
    # session lock), or it is now waiting on the lock the delete holds.
    assert share_progressed.wait(WAIT)
    delete_go.set()
    delete.join(WAIT)
    share.join(WAIT)
    assert not delete.is_alive() and not share.is_alive()
    _assert_deleted_for_good(path, results)
    assert results["share"][0] == 404, results


def test_share_revoke_that_resolved_before_a_delete_does_not_bring_it_back(state, monkeypatch):
    path = _save_session()
    token = _share()
    entered, go = _pause_after_resolve(monkeypatch, "revoke")
    results = {}
    revoke = _run("revoke", results, "/api/share/revoke", {"session_id": SID})
    assert entered.wait(WAIT)
    results["delete"] = _post("/api/session/delete", {"session_id": SID})
    assert not path.exists()
    go.set()
    revoke.join(WAIT)
    assert not revoke.is_alive()
    _assert_deleted_for_good(path, results)
    _assert_not_served(token)
    assert results["revoke"][0] == 404, results


def test_external_share_that_resolved_before_a_delete_writes_no_sidecar(state, monkeypatch):
    """An external session has no sidecar to go missing; the delete record refuses it."""
    path = _external_session(monkeypatch)
    entered, go = _pause_after_resolve(monkeypatch, "share")
    results = {}
    share = _run("share", results, "/api/share/create", {"session_id": EXTERNAL_SID})
    assert entered.wait(WAIT)
    results["delete"] = _post("/api/session/delete", {"session_id": EXTERNAL_SID})
    assert results["delete"][0] == 200, results
    go.set()
    share.join(WAIT)
    assert not share.is_alive()
    assert not path.exists(), "the share wrote a sidecar for the deleted session"
    assert EXTERNAL_SID not in _index_ids()
    assert _published_tokens() == set()
    assert results["share"][0] == 404, results

def test_control_a_session_still_resolvable_after_an_earlier_delete_shares_again(state, monkeypatch):
    """Only a delete during the request refuses it: a session still resolvable after an
    earlier delete (a messaging session keeps its state.db rows) shares as before."""
    path = _external_session(monkeypatch)
    first = _share(EXTERNAL_SID)
    assert _post("/api/session/delete", {"session_id": EXTERNAL_SID})[0] == 200
    assert not path.exists()
    second = _share(EXTERNAL_SID)
    assert second != first
    assert _public(second)[0] == 200
    assert json.loads(path.read_text(encoding="utf-8"))["share_token"] == second
