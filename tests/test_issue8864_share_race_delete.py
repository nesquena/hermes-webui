"""#8864: a share create or revoke racing a session delete must not bring it back.

``_persist_share_fields`` used to fall back to ``session.save()`` whenever the
sidecar was missing. That fallback exists for an external session that never had
a WebUI sidecar, but it also fired right after ``/api/session/delete`` removed
the sidecar: the save rewrote the whole conversation and its index row, so the
deleted conversation came back in the sidebar, and the link the racing create
published served again. Revoke took the same fallback with a null token.

The races are driven deterministically through the real handlers: a hook runs
the delete, on its own thread, at the exact point inside the share request.
"""

import io
import json
import logging
import threading
import urllib.parse
from collections import OrderedDict

import pytest

SID = "s_8864_shared_conversation"
EXTERNAL_SID = "s_8864_external_cli"


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
    monkeypatch.setattr(routes, "_DELETED_SESSION_GENERATION", {}, raising=False)
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


def _external_session(monkeypatch, *, source="cli"):
    """An external session that lives only in state.db: no WebUI sidecar at all."""
    from api import models, routes

    meta = {"session_id": EXTERNAL_SID, "title": "External chat", "source_tag": source, "raw_source": source}
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
            title="External chat",
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


def _post(path, body):
    from api.routes import handle_post

    handler = _FakeHandler(json.dumps(body).encode())
    handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _public(token):
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(f"/api/share/{token}"))
    return handler.status, bytes(handler.body)


def _index_ids():
    from api import models

    if not models.SESSION_INDEX_FILE.exists():
        return set()
    return {row.get("session_id") for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))}


def _snapshot_tokens():
    from api import shares

    if not shares.SHARES_DIR.exists():
        return set()
    return {p.stem for p in shares.SHARES_DIR.glob("*.json")}


def _snapshot_file(token):
    from api import shares

    return json.loads((shares.SHARES_DIR / f"{token}.json").read_text(encoding="utf-8"))


def _assert_not_served(token):
    from api import shares

    assert shares.load_share(token) is None
    assert _public(token)[0] == 404


def _assert_served(token):
    status, body = _public(token)
    assert status == 200, body


def _delete_on_its_own_thread(sid):
    """Run /api/session/delete on another thread, the way a second request would."""
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("reply", _post("/api/session/delete", {"session_id": sid})),
        daemon=True,
    )
    thread.start()
    return thread, result


# Steps of the share handlers between their last re-read and _persist_share_fields.
# After the snapshot write the resident copy does not carry the new token yet,
# so the delete has no way to find the link the create just published.
PUBLISHED = "write_share_snapshot"
MIRRORED = "_mirror_share_fields_to_cached_session"


def _delete_right_after(monkeypatch, step, sid):
    """Run a complete delete of ``sid`` right after the share handler's ``step``."""
    from api import routes

    original = getattr(routes, step)
    race = {}

    def step_then_delete(*args, **kwargs):
        value = original(*args, **kwargs)
        if "reply" not in race:
            thread, result = _delete_on_its_own_thread(sid)
            thread.join(timeout=15)
            assert not thread.is_alive(), "the racing delete did not finish"
            race["reply"] = result["reply"]
        return value

    monkeypatch.setattr(routes, step, step_then_delete)
    return race


def _assert_stays_deleted(path, sid, race):
    assert race["reply"][0] == 200, race["reply"]
    assert not path.exists(), "the share request saved the deleted conversation back"
    assert sid not in _index_ids(), "the deleted conversation is back in the sidebar index"


# ── The sidecar is gone by the time the share request persists ──────────────


@pytest.mark.parametrize("step", [PUBLISHED, MIRRORED])
def test_share_create_racing_a_delete_does_not_bring_the_conversation_back(state, monkeypatch, step):
    path = _save_session()
    race = _delete_right_after(monkeypatch, step, SID)
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert "reply" in race, "the delete never ran inside the share request"
    assert (status, reply.get("error")) == (404, "Session not found"), reply
    _assert_stays_deleted(path, SID, race)
    (token,) = _snapshot_tokens()
    # The snapshot it published is revoked, so it stays dead if a sidecar with
    # this id ever comes back.
    assert _snapshot_file(token).get("revoked_at")
    _assert_not_served(token)


def test_share_revoke_racing_a_delete_does_not_bring_the_conversation_back(state, monkeypatch):
    path = _save_session()
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200, reply
    token = reply["share"]["token"]
    race = _delete_right_after(monkeypatch, MIRRORED, SID)
    status, reply = _post("/api/share/revoke", {"session_id": SID})
    assert "reply" in race, "the delete never ran inside the revoke request"
    assert (status, reply.get("error")) == (404, "Session not found"), reply
    _assert_stays_deleted(path, SID, race)
    _assert_not_served(token)


@pytest.mark.parametrize("source", ["cli", "telegram"])
def test_share_create_of_an_external_session_deleted_mid_request_fails_closed(state, monkeypatch, source):
    """An external session never had a sidecar, so the share would write the first
    one; it must not do that once a delete of the session landed during the request.
    The link it published is revoked: a messaging session keeps its state.db rows,
    so a later share writes a sidecar for this id again."""
    from api import routes

    path = _external_session(monkeypatch, source=source)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: source == "telegram" and sid == EXTERNAL_SID)
    race = _delete_right_after(monkeypatch, PUBLISHED, EXTERNAL_SID)
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert "reply" in race, "the delete never ran inside the share request"
    assert (status, reply.get("error")) == (404, "Session not found"), reply
    _assert_stays_deleted(path, EXTERNAL_SID, race)
    (raced,) = _snapshot_tokens()
    _assert_not_served(raced)
    # Sharing it again afterwards works and does not revive the raced link.
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    assert reply["share"]["token"] != raced
    _assert_served(reply["share"]["token"])
    _assert_not_served(raced)


# ── A messaging session deleted a second time in the same process ────────────


def _messaging_session(monkeypatch):
    """A messaging session: its state.db rows survive a delete, so it can be
    shared again afterwards. Returns its sidecar path (absent at first)."""
    from api import routes

    path = _external_session(monkeypatch, source="telegram")
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: sid == EXTERNAL_SID)
    # The transcript stays in state.db; the sidecar a share writes holds metadata only.
    transcript = [
        {"role": "user", "content": "from elsewhere", "timestamp": 200},
        {"role": "assistant", "content": "answered there", "timestamp": 201},
    ]
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda sid, **kw: [dict(m) for m in transcript] if sid == EXTERNAL_SID else [],
    )
    return path


def _messaging_session_deleted_and_shared_again(monkeypatch):
    """A messaging session keeps its state.db rows, so it can be shared, deleted
    and shared again. Returns its sidecar path and the token of the second share."""
    path = _messaging_session(monkeypatch)
    assert _post("/api/share/create", {"session_id": EXTERNAL_SID})[0] == 200
    assert _post("/api/session/delete", {"session_id": EXTERNAL_SID})[0] == 200
    assert not path.exists()
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    assert path.exists()
    token = reply["share"]["token"]
    _assert_served(token)
    return path, token


@pytest.mark.parametrize("step", [PUBLISHED, MIRRORED])
def test_share_create_racing_a_second_delete_of_a_messaging_session(state, monkeypatch, step):
    """The id is already in the delete record from the first delete, so whether
    it is in that record cannot tell this request that a delete landed during it."""
    path, earlier = _messaging_session_deleted_and_shared_again(monkeypatch)
    # Revoke the second share first, so the racing create mints a link the
    # delete cannot find (it reuses a live token otherwise).
    assert _post("/api/share/revoke", {"session_id": EXTERNAL_SID})[0] == 200
    assert path.exists()
    before = _snapshot_tokens()
    race = _delete_right_after(monkeypatch, step, EXTERNAL_SID)
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert "reply" in race, "the delete never ran inside the share request"
    assert (status, reply.get("error")) == (404, "Session not found"), reply
    _assert_stays_deleted(path, EXTERNAL_SID, race)
    (raced,) = _snapshot_tokens() - before  # the link this create published
    # Revoked in the snapshot itself: this session keeps its state.db rows, so a
    # later share writes a sidecar for this id again.
    assert _snapshot_file(raced).get("revoked_at")
    _assert_not_served(raced)
    _assert_not_served(earlier)


def test_share_revoke_racing_a_second_delete_of_a_messaging_session(state, monkeypatch):
    path, token = _messaging_session_deleted_and_shared_again(monkeypatch)
    race = _delete_right_after(monkeypatch, MIRRORED, EXTERNAL_SID)
    status, reply = _post("/api/share/revoke", {"session_id": EXTERNAL_SID})
    assert "reply" in race, "the delete never ran inside the revoke request"
    assert (status, reply.get("error")) == (404, "Session not found"), reply
    _assert_stays_deleted(path, EXTERNAL_SID, race)
    _assert_not_served(token)


# ── The share persists between the delete's revoke step and its unlink ───────


def _delete_held_after_its_revoke_step(monkeypatch, sid):
    """Start a delete of ``sid`` right after the share handler publishes its
    snapshot and hold it just after its revoke-before-delete step. That step
    finds no token: the new one is in neither the sidecar nor the resident copy
    yet. The share request then persists its token into the sidecar, which still
    exists. Returns ``finish()``, which lets the delete take the persist lock
    and unlink, and returns its reply."""
    from api import routes

    revoked, resume = threading.Event(), threading.Event()
    race = {}
    original_revoke = routes._revoke_owned_shares_before_delete
    original_publish = routes.write_share_snapshot

    def revoke_then_hold(*args, **kwargs):
        value = original_revoke(*args, **kwargs)
        revoked.set()
        assert resume.wait(15), "the test never resumed the delete"
        return value

    def publish_then_start_delete(*args, **kwargs):
        meta = original_publish(*args, **kwargs)
        if "thread" not in race:
            race["thread"], race["result"] = _delete_on_its_own_thread(sid)
            assert revoked.wait(15), "the delete never reached its revoke step"
        return meta

    monkeypatch.setattr(routes, "_revoke_owned_shares_before_delete", revoke_then_hold)
    monkeypatch.setattr(routes, "write_share_snapshot", publish_then_start_delete)

    def finish():
        assert "thread" in race, "the delete never ran inside the share request"
        resume.set()
        race["thread"].join(timeout=15)
        assert not race["thread"].is_alive(), "the delete never finished"
        # Later requests run without the race hooks.
        monkeypatch.setattr(routes, "write_share_snapshot", original_publish)
        monkeypatch.setattr(routes, "_revoke_owned_shares_before_delete", original_revoke)
        return race["result"]["reply"]

    return finish


def _share_persisted_before_the_unlink(monkeypatch, path, sid):
    finish_delete = _delete_held_after_its_revoke_step(monkeypatch, sid)
    status, reply = _post("/api/share/create", {"session_id": sid})
    on_disk = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    delete_reply = finish_delete()  # before any assert, so the held delete never outlives the test
    # The share landed first: its token was in the sidecar the delete had not removed yet.
    assert status == 200, reply
    token = reply["share"]["token"]
    assert on_disk.get("share_token") == token, on_disk
    assert delete_reply[0] == 200, delete_reply
    assert not path.exists()
    assert sid not in _index_ids()
    return token


def test_share_persisted_after_the_delete_revoke_step_is_revoked(state, monkeypatch):
    path = _save_session()
    raced = _share_persisted_before_the_unlink(monkeypatch, path, SID)
    assert _snapshot_file(raced).get("revoked_at"), "the delete left the raced link live in its snapshot"
    _assert_not_served(raced)


def test_share_persisted_after_the_delete_revoke_step_stays_dead_after_a_reshare(state, monkeypatch):
    """The unlink removed the only record of the raced token, and the snapshot
    itself was never revoked. A messaging session keeps its state.db rows, so
    sharing it again writes a sidecar for this id, and the raced link served again."""
    path = _messaging_session(monkeypatch)
    raced = _share_persisted_before_the_unlink(monkeypatch, path, EXTERNAL_SID)
    assert _snapshot_file(raced).get("revoked_at"), "the delete left the raced link live in its snapshot"
    _assert_not_served(raced)
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    assert path.exists()
    assert reply["share"]["token"] != raced
    _assert_served(reply["share"]["token"])
    _assert_not_served(raced)


@pytest.mark.parametrize("failure", ["raises", "returns_false"])
def test_share_create_racing_a_delete_still_answers_404_when_revoking_its_token_fails(
    state, monkeypatch, caplog, failure
):
    """The 404 path revokes the token the request minted. If that revoke fails (a
    write error in the share store), the request must still answer 404 rather
    than escape as a 500, and the failure must be logged, as the delete's own
    late-share revoke logs it; the token is never returned to the caller."""
    from api import routes

    path = _messaging_session(monkeypatch)
    race = _delete_right_after(monkeypatch, PUBLISHED, EXTERNAL_SID)
    share_thread = threading.current_thread()
    real_revoke = routes.revoke_share_owned_by

    def failing_revoke(token, sid):
        if threading.current_thread() is share_thread:
            if failure == "raises":
                raise OSError("share store write failed")
            return False
        return real_revoke(token, sid)

    monkeypatch.setattr(routes, "revoke_share_owned_by", failing_revoke)
    with caplog.at_level(logging.WARNING):
        status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert "reply" in race, "the delete never ran inside the share request"
    assert (status, reply.get("error")) == (404, "Session not found"), reply
    _assert_stays_deleted(path, EXTERNAL_SID, race)
    assert any(
        record.levelno >= logging.WARNING and EXTERNAL_SID in record.getMessage() and "revoke" in record.getMessage()
        for record in caplog.records
    ), "a failed revoke of the minted token was not logged"


# ── The delete lands while the share request is patching the sidecar ─────────


def test_delete_during_the_share_patch_does_not_bring_the_sidecar_back(state, monkeypatch):
    """The share patch reads the sidecar and atomically replaces it under the
    persist lock. A delete that unlinked between the read and the replace had
    the replace write the whole conversation back. Delete now removes the
    sidecar under that lock, so it lands after the patch instead."""
    from api import models

    path = _save_session()
    original = models._set_sidecar_metadata_fields
    share_thread = threading.current_thread()
    race = {}

    def patch_fields(data, fields):
        if "share_token" in fields and threading.current_thread() is share_thread and "thread" not in race:
            race["thread"], race["result"] = _delete_on_its_own_thread(SID)
            # Without the lock the delete finishes inside this wait.
            race["thread"].join(timeout=2.0)
        return original(data, fields)

    monkeypatch.setattr(models, "_set_sidecar_metadata_fields", patch_fields)
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert "thread" in race, "the share never patched the sidecar"
    race["thread"].join(timeout=15)
    assert not race["thread"].is_alive(), "the delete never finished"
    assert status == 200, reply  # the share landed first; the delete wins afterwards
    _assert_stays_deleted(path, SID, race["result"])
    _assert_not_served(reply["share"]["token"])


# ── Controls: what must keep working ─────────────────────────────────────────


def test_control_an_external_session_share_still_writes_its_first_sidecar(state, monkeypatch):
    path = _external_session(monkeypatch)
    assert not path.exists()
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    token = reply["share"]["token"]
    assert json.loads(path.read_text(encoding="utf-8"))["share_token"] == token
    _assert_served(token)
    assert _post("/api/share/revoke", {"session_id": EXTERNAL_SID})[0] == 200
    assert json.loads(path.read_text(encoding="utf-8"))["share_token"] is None
    _assert_not_served(token)


def test_control_a_messaging_session_deleted_earlier_can_be_shared_again(state, monkeypatch):
    """A delete that finished before the request started is not a race: a
    messaging session keeps its state.db rows, so sharing it again still writes
    a new sidecar."""
    from api import routes

    path = _external_session(monkeypatch, source="telegram")
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: sid == EXTERNAL_SID)
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    assert _post("/api/session/delete", {"session_id": EXTERNAL_SID})[0] == 200
    assert not path.exists()
    status, reply = _post("/api/share/create", {"session_id": EXTERNAL_SID})
    assert status == 200, reply
    assert path.exists()
    _assert_served(reply["share"]["token"])


def test_control_share_and_revoke_without_a_race(state):
    path = _save_session()
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200, reply
    token = reply["share"]["token"]
    _assert_served(token)
    assert json.loads(path.read_text(encoding="utf-8"))["share_token"] == token
    assert _post("/api/share/revoke", {"session_id": SID})[0] == 200
    _assert_not_served(token)
    assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == 2
