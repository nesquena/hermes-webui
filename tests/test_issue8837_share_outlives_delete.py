"""#8837: a deleted conversation's public share link must stop working.

Driven through the real share, delete and public-share handlers:

1. ``load_share`` serves a snapshot only while the conversation that published it
   (``source_session_id``) still exists: one stat of its sidecar path, nothing
   read. This also closes links that deletes made before the fix left behind.
2. ``/api/session/delete`` revokes the snapshot the session owns before it removes
   anything, and refuses the delete when that revoke does not land.

The bounded check never compares tokens and never reads the sidecar, so a backup
recovery that rewrote the sidecar, or a compression continuation that took the
link over, keeps a legitimate link serving.
"""

import builtins
import io
import json
import os
import pathlib
import secrets
import urllib.parse
from collections import OrderedDict

import pytest

SID = "s_8837_shared_conversation"
CHILD_SID = "s_8837_compression_continuation"
EXTERNAL_SID = "s_8837_external_cli"


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
    """What an anonymous visitor of the link gets: status, headers and body bytes."""
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(f"/api/share/{token}"))
    return handler.status, list(handler.sent_headers), bytes(handler.body)


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


def _assert_served(token):
    status, _headers, body = _public(token)
    assert status == 200, body
    return json.loads(body)["share"]


def _assert_not_served(token):
    """Refused exactly like a token that never existed: same status, headers and body."""
    from api import shares

    assert shares.load_share(token) is None
    reply = _public(token)
    assert reply[0] == 404
    assert reply == _unknown_token_reply()


# ── Part 1: the public route serves a link only while its conversation exists ──


def test_share_then_delete_stops_serving_the_link(state):
    path = _save_session()
    token = _share()
    _assert_served(token)
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


@pytest.mark.parametrize("owner", ["", None, "../sessions/s_8837_shared_conversation", 7])
def test_snapshot_without_a_valid_owner_is_not_served(state, owner):
    """Every snapshot a share create writes names its session; one that does not
    (or names something that is not a session id) has no conversation to exist."""
    from api import shares

    _save_session()
    token = _share()
    payload = _snapshot_file(token)
    payload["source_session_id"] = owner
    shares._write_json_atomic(shares.SHARES_DIR / f"{token}.json", payload)
    _assert_not_served(token)


# ── Part 2: deleting a conversation revokes the share it owns ─────────────────


def test_delete_revokes_the_share(state):
    _save_session()
    token = _share()
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 200, reply
    assert _snapshot_file(token).get("revoked_at")  # the store itself no longer publishes it
    _assert_not_served(token)


def test_a_revoked_link_stays_dead_when_a_sidecar_with_that_id_comes_back(state):
    """The stat alone would revive the link if the id reappeared (a messaging
    session keeps its state.db rows); the revoke written by delete keeps it dead."""
    path = _save_session()
    token = _share()
    assert _post("/api/session/delete", {"session_id": SID})[0] == 200
    _save_session()
    assert path.exists()
    _assert_not_served(token)


def test_a_revoke_that_does_not_land_refuses_the_delete_before_removing_anything(state, monkeypatch):
    from api import models, shares

    path = _save_session()
    token = _share()
    real_write = shares._write_json_atomic

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    state_db_deletes = []
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: state_db_deletes.append(sid) or True)
    monkeypatch.setattr(shares, "_write_json_atomic", disk_full)
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 500, reply
    assert reply == {"ok": False, "error": "share_revoke_failed", "action": "session_delete"}
    # Nothing was removed: state.db, sidecar, index row and the link are intact.
    assert state_db_deletes == []
    assert path.exists() and SID in _index_ids()
    assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == 2
    assert _snapshot_file(token).get("revoked_at") is None
    _assert_served(token)
    # The user can retry once the snapshot store is writable again.
    monkeypatch.setattr(shares, "_write_json_atomic", real_write)
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 200, reply
    assert state_db_deletes == [SID]
    assert not path.exists()
    _assert_not_served(token)


def test_delete_revokes_the_resident_token_the_sidecar_lost(state):
    """A checkpoint can write the sidecar without the token while the link is live;
    the resident session still names it, so delete revokes that one too."""
    from api import models

    path = _save_session()
    token = _share()
    assert models.update_session_share_fields(SID, share_token=None, share_created_at=None)
    resident = models.Session.load(SID)
    resident.share_token = token
    models.SESSIONS[SID] = resident
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 200, reply
    assert not path.exists()
    assert _snapshot_file(token).get("revoked_at")
    _assert_not_served(token)


def test_a_delete_refused_after_the_revoke_keeps_the_session_and_a_new_share_works(state, monkeypatch):
    """The revoke comes first, so a delete the state.db step then refuses leaves
    the conversation whole with its old link dead; sharing again issues a new one."""
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
    _assert_not_served(token)
    again = _share()
    assert again != token
    _assert_served(again)


# ── Codex's findings on the first attempt: legitimate links keep serving ─────


def test_backup_recovery_keeps_a_live_link_serving(state):
    """A ``.bak`` restore rewrites the sidecar, possibly without the token; the link
    is still the conversation's and must keep serving."""
    from api.session_recovery import recover_session

    path = _save_session()
    token = _share()
    live = json.loads(path.read_text(encoding="utf-8"))
    backup = dict(live)
    backup["messages"] = live["messages"] + [
        {"role": "user", "content": "a turn only the backup kept", "timestamp": 102},
        {"role": "assistant", "content": "restored", "timestamp": 103},
    ]
    backup.pop("message_count", None)
    backup["share_token"] = None
    backup["share_created_at"] = None
    path.with_suffix(".json.bak").write_text(json.dumps(backup), encoding="utf-8")
    result = recover_session(path)
    assert result.get("restored") is True, result
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert len(restored["messages"]) == 4
    assert restored.get("share_token") != token  # the restored sidecar no longer names it
    share = _assert_served(token)
    assert [m["content"] for m in share["messages"]] == ["hello", "world"]


def test_continuation_that_refreshed_the_share_keeps_it_when_the_parent_is_deleted(state):
    """Compression rotates the session to a continuation id that inherits the
    token; refreshing its share makes it the snapshot's owner, so deleting the
    pre-compression parent neither revokes nor stops serving the link."""
    from api import models

    parent_path = _save_session()
    token = _share()
    child = models.Session.load(SID)
    child.session_id = CHILD_SID
    child.parent_session_id = SID
    child.save(touch_updated_at=False)
    parent = models.Session.load(SID)
    parent.pre_compression_snapshot = True
    parent.pre_compression_continuation_session_id = CHILD_SID
    parent.save(touch_updated_at=False)
    models.SESSIONS.clear()
    assert json.loads(child.path.read_text(encoding="utf-8"))["share_token"] == token
    assert _share(CHILD_SID) == token
    assert _snapshot_file(token)["source_session_id"] == CHILD_SID
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 200, reply
    assert not parent_path.exists()
    assert _snapshot_file(token).get("revoked_at") is None
    _assert_served(token)


def test_public_share_request_reads_no_sidecar_bytes(state, monkeypatch):
    """The owner check is a stat: any read of the owner's sidecar fails the request."""
    from api import models

    path = _save_session()
    token = _share()
    sidecar = os.path.realpath(path)
    reads = []

    def is_sidecar(target):
        try:
            return os.path.realpath(os.fspath(target)) == sidecar
        except TypeError:
            return False  # a file descriptor

    def guard(real, name):
        def wrapper(target, *args, **kwargs):
            if is_sidecar(target):
                reads.append(name)
                raise PermissionError(f"public share request read the sidecar via {name}")
            return real(target, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", guard(builtins.open, "open"))
    monkeypatch.setattr(io, "open", guard(io.open, "io.open"))
    monkeypatch.setattr(os, "open", guard(os.open, "os.open"))
    for name in ("open", "read_text", "read_bytes"):
        monkeypatch.setattr(pathlib.Path, name, guard(getattr(pathlib.Path, name), f"Path.{name}"))
    monkeypatch.setattr(
        models, "_read_metadata_json_prefix", guard(models._read_metadata_json_prefix, "prefix")
    )
    monkeypatch.setattr(models, "_persisted_share_fields", guard(models._persisted_share_fields, "share-fields"))
    share = _assert_served(token)
    assert [m["content"] for m in share["messages"]] == ["hello", "world"]
    assert reads == []
    # The guard is live: reading the sidecar through any of these would have failed.
    with pytest.raises(PermissionError):
        path.read_text(encoding="utf-8")


# ── Controls: unchanged behaviour (green before and after the fix) ───────────


def test_control_a_shared_conversation_is_served(state):
    from api import shares

    _save_session()
    token = _share()
    share = _assert_served(token)
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
    _assert_served(second)
    _assert_not_served(first)


def test_control_deleting_an_unshared_conversation_still_works(state):
    path = _save_session()
    status, reply = _post("/api/session/delete", {"session_id": SID})
    assert status == 200, reply
    assert not path.exists()


def _external_session(monkeypatch, *, source="cli", title="External chat"):
    """An external session that lives only in state.db: no WebUI sidecar at all."""
    from api import models, routes

    meta = {"session_id": EXTERNAL_SID, "title": title, "source_tag": source, "raw_source": source}
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
            title=title,
            messages=[
                {"role": "user", "content": "from elsewhere", "timestamp": 200},
                {"role": "assistant", "content": "answered there", "timestamp": 201},
            ],
            is_cli_session=True,
            source_tag=source,
            raw_source=source,
        )
        return session, "materialized"

    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", synthesize)
    return models.SESSION_DIR / f"{EXTERNAL_SID}.json"


def test_control_an_external_session_share_is_served(state, monkeypatch):
    """Sharing an external session writes its metadata-only sidecar, which is the
    owner the public route checks for."""
    path = _external_session(monkeypatch)
    assert not path.exists()
    token = _share(EXTERNAL_SID)
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert sidecar["share_token"] == token
    assert sidecar["messages"] == []  # metadata only; the transcript stays in state.db
    share = _assert_served(token)
    assert [m["content"] for m in share["messages"]] == ["from elsewhere", "answered there"]
    assert _post("/api/share/revoke", {"session_id": EXTERNAL_SID})[0] == 200
    _assert_not_served(token)


def test_delete_of_an_external_session_revokes_its_share(state, monkeypatch):
    path = _external_session(monkeypatch)
    token = _share(EXTERNAL_SID)
    status, reply = _post("/api/session/delete", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    assert not path.exists()
    assert _snapshot_file(token).get("revoked_at")
    _assert_not_served(token)


def test_messaging_session_delete_ends_its_link_and_sharing_again_works(state, monkeypatch):
    """A messaging session keeps its state.db rows after a WebUI delete: its old
    link is dead, and sharing it again publishes a new one that serves."""
    from api import routes

    path = _external_session(monkeypatch, source="telegram")
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: sid == EXTERNAL_SID)
    first = _share(EXTERNAL_SID)
    assert _post("/api/session/delete", {"session_id": EXTERNAL_SID})[0] == 200
    assert not path.exists()
    _assert_not_served(first)
    second = _share(EXTERNAL_SID)
    assert second != first
    _assert_served(second)
    _assert_not_served(first)


# ── Codex round 1, F1: cleanup of empty sessions keeps an external share link ──

CLEANUP_ROUTES = ["/api/sessions/cleanup", "/api/sessions/cleanup_zero_message"]


@pytest.mark.parametrize("route", CLEANUP_ROUTES)
def test_cleanup_keeps_a_shared_external_conversation_serving_and_revocable(state, monkeypatch, route):
    """Sharing an external conversation writes a metadata-only sidecar: no
    messages, an untitled CLI session keeps "Untitled", and the transcript stays
    in state.db. Cleaning up empty sessions must not remove it, or the link of a
    conversation that still exists stops serving (and on master, where it kept
    serving, revoke lost the token)."""
    path = _external_session(monkeypatch, title="Untitled")
    token = _share(EXTERNAL_SID)
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert sidecar["messages"] == [] and sidecar["title"] == "Untitled"
    assert EXTERNAL_SID in _index_ids()
    status, reply = _post(route, {})
    assert status == 200, reply
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["share_token"] == token
    assert EXTERNAL_SID in _index_ids()  # the index sweep keeps it too
    share = _assert_served(token)
    assert [m["content"] for m in share["messages"]] == ["from elsewhere", "answered there"]
    assert _post("/api/share/revoke", {"session_id": EXTERNAL_SID})[0] == 200
    _assert_not_served(token)


@pytest.mark.parametrize("route", CLEANUP_ROUTES)
def test_control_cleanup_still_removes_a_never_shared_empty_native_session(state, route):
    """Unchanged from master: an empty native session cannot be shared, so it
    carries no token, and cleanup removes it."""
    from api import models

    sid = "s_8837_empty_native"
    empty = models.Session(session_id=sid, title="Untitled")
    empty.save(touch_updated_at=False)
    models.SESSIONS.clear()
    status, reply = _post("/api/share/create", {"session_id": sid})
    assert status == 400, reply
    assert json.loads(empty.path.read_text(encoding="utf-8")).get("share_token") is None
    status, reply = _post(route, {})
    assert status == 200, reply
    assert reply["cleaned"] >= 1
    assert not empty.path.exists()


@pytest.mark.parametrize("route", CLEANUP_ROUTES)
def test_control_cleanup_removes_an_unshared_external_metadata_sidecar(state, monkeypatch, route):
    """Unchanged from master: an external conversation's metadata-only sidecar
    with no link behind it is still cleaned up (no link is lost)."""
    path = _external_session(monkeypatch, title="Untitled")
    _share(EXTERNAL_SID)
    assert _post("/api/share/revoke", {"session_id": EXTERNAL_SID})[0] == 200
    assert json.loads(path.read_text(encoding="utf-8")).get("share_token") is None
    status, reply = _post(route, {})
    assert status == 200, reply
    assert not path.exists()


# ── Codex round 3: cleanup racing a share create keeps the new link ──


@pytest.mark.parametrize("route", CLEANUP_ROUTES)
def test_cleanup_racing_a_share_create_keeps_the_new_link(state, monkeypatch, route):
    """Cleanup loads an empty, unshared external sidecar; a share of that
    conversation then completes before cleanup decides. Judging the stale
    tokenless copy removed the sidecar the share had just written, so a link
    published for a conversation that still exists stopped serving. Cleanup now
    holds the share lock from the load through the unlink: the share waits for
    it and lands afterwards."""
    import threading

    from api import models

    path = _external_session(monkeypatch, title="Untitled")
    _share(EXTERNAL_SID)
    assert _post("/api/share/revoke", {"session_id": EXTERNAL_SID})[0] == 200
    assert json.loads(path.read_text(encoding="utf-8")).get("share_token") is None
    models.SESSIONS.clear()

    # With a metadata sidecar on disk, a share reads the transcript from
    # state.db; serve the same two rows the fixture synthesizes.
    from api import routes

    original_snapshot = routes._share_snapshot_messages_for_session

    def snapshot_messages(session, *args, **kwargs):
        if getattr(session, "session_id", None) == EXTERNAL_SID:
            return [
                {"role": "user", "content": "from elsewhere", "timestamp": 200},
                {"role": "assistant", "content": "answered there", "timestamp": 201},
            ]
        return original_snapshot(session, *args, **kwargs)

    monkeypatch.setattr(routes, "_share_snapshot_messages_for_session", snapshot_messages)

    cleanup_thread = threading.current_thread()
    original_load = models.Session.load.__func__
    racer = {}

    def share_now():
        racer["reply"] = _post("/api/share/create", {"session_id": EXTERNAL_SID})

    def load(cls, sid):
        loaded = original_load(cls, sid)
        if sid == EXTERNAL_SID and threading.current_thread() is cleanup_thread and "thread" not in racer:
            racer["thread"] = threading.Thread(target=share_now, daemon=True)
            racer["thread"].start()
            # Without the share lock the create finishes inside this wait.
            racer["thread"].join(timeout=2.0)
        return loaded

    monkeypatch.setattr(models.Session, "load", classmethod(load))
    status, reply = _post(route, {})
    assert status == 200, reply
    assert "thread" in racer, "cleanup never loaded the external sidecar"
    racer["thread"].join(timeout=15)
    assert not racer["thread"].is_alive()
    share_status, share_reply = racer["reply"]
    assert share_status == 200, share_reply
    token = share_reply["share"]["token"]
    assert path.exists(), "cleanup removed the sidecar of a link it raced"
    assert json.loads(path.read_text(encoding="utf-8"))["share_token"] == token
    _assert_served(token)
