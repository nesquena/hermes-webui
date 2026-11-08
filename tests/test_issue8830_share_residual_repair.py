"""#8830: a full load repairs a sidecar an exp-v0.52.621..630 build left with its share keys after ``messages``.

#8815's fix (#8818) stops new damage and repairs a damaged sidecar on its next share
write, but a legacy conversation those builds already shared kept its token after the
message body, past the metadata prefix, until something wrote it again: metadata reloads
and the sidebar showed it unshared (no Stop sharing) while its public link stayed live.
``Session.load`` only notes it (a load runs before or without any ownership decision, and a
refused request must never write, #8094); the detail route moves those keys into the prefix,
under the persistence lock, once the request owns the session.

(The #8815 tests' notes follow.)

Sidecars last saved before shares existed (exp-v0.52.31, 2026-07-11) have no
``share_token``/``share_created_at`` keys. ``update_session_share_fields``
assigned them, which appends a missing key after ``messages``: past the prefix
``Session.load_metadata_only`` reads. Every metadata reload (``/api/session``
with ``messages=0``, the sidebar index) then returned no token, so the UI showed
the session as unshared and its Stop sharing control did nothing, while the
public link stayed live. v0.52.544 kept the token; exp-v0.52.621 (092ec8979)
introduced the loss.

These tests use the real share and session handlers.
"""

import io
import json
import urllib.parse
from collections import OrderedDict

import pytest

SID = "s_8815_legacy_share"


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
def isolated_state(tmp_path, monkeypatch):
    """Keep the handler's sessions and share snapshots inside tmp_path."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares

    sessions = OrderedDict()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    yield tmp_path


def _save_session(*, legacy, profile=None):
    """Save a session; ``legacy`` strips the share keys, as a pre-share sidecar has none."""
    from api import models

    session = models.Session(session_id=SID, title="Legacy history", profile=profile)
    session.messages = [
        {"role": "user", "content": "hello", "timestamp": 100},
        {"role": "assistant", "content": "world", "timestamp": 101},
    ]
    session.save(touch_updated_at=False)
    path = session.path
    if legacy:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for key in ("share_token", "share_created_at"):
            raw.pop(key, None)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    models.SESSIONS.clear()
    return path


def _post(path, body):
    from api.routes import handle_post

    handler = _FakeHandler(json.dumps(body).encode())
    handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _metadata_reload():
    """What a page reload sees: a cold metadata load, then ``/api/session`` without messages."""
    from api import models
    from api.routes import handle_get

    models.SESSIONS.clear()
    metadata_token = models.Session.load_metadata_only(SID).share_token
    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(f"/api/session?session_id={SID}&messages=0&resolve_model=0"))
    assert handler.status == 200
    return metadata_token, handler.json_body()["session"].get("share_token")


def _key_order(path):
    return list(json.loads(path.read_text(encoding="utf-8")))




def _damage_like_621(path, keys=("share_token", "share_created_at")):
    """Move share keys after ``messages``, as exp-v0.52.621..630 left a shared legacy sidecar."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    moved = {key: raw.pop(key) for key in keys}
    raw.update(moved)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    order = list(raw)
    for key in keys:
        assert order.index(key) > order.index("messages")


def _full_load():
    from api import models
    models.SESSIONS.clear()
    return models.Session.load(SID)


def _open(messages=1):
    """Open the conversation the way the page does: ``GET /api/session`` (cold cache)."""
    from api import models
    from api.routes import handle_get
    models.SESSIONS.clear()
    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(f"/api/session?session_id={SID}&messages={messages}"))
    return handler


def _index_rows():
    from api import models
    if not models.SESSION_INDEX_FILE.exists():
        return []
    return json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))


def _index_tokens():
    """The share token the sidebar index row carries (``/api/sessions/search`` reads it)."""
    return [row.get("share_token") for row in _index_rows() if row.get("session_id") == SID]


def _shared_then_damaged(profile=None, **kwargs):
    from api import models
    path = _save_session(legacy=True, profile=profile)
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200, reply
    _damage_like_621(path, **kwargs)
    models.SESSIONS.clear()
    return path, reply["share"]["token"]


def _before_body(path, key):
    order = _key_order(path)
    return order.index(key) < order.index("messages") and order.count(key) == 1


@pytest.fixture(autouse=True)
def _own_index(isolated_state, monkeypatch):
    """Keep the sidebar index inside this test's session dir, and the list cache cold."""
    import api.config as config
    import api.models as models
    import api.routes as routes
    index = isolated_state / "sessions" / "_index.json"
    for module in (config, models, routes):
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", index)
    routes._session_list_cache_clear()
    yield
    routes._session_list_cache_clear()


@pytest.fixture
def active_profile(monkeypatch):
    """Pin the request's active profile (detail/guard and sidebar lookups); returns a setter."""
    import api.profiles as profiles
    import api.routes as routes
    current = {"name": "default"}
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: current["name"])
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: current["name"])

    def set_active(name):
        current["name"] = name
    return set_active


def _snapshot(path):
    from api import models
    index = models.SESSION_INDEX_FILE
    return {
        "bytes": path.read_bytes(),
        "mtime": path.stat().st_mtime_ns,
        "bak": path.with_suffix(".json.bak").exists(),
        "index": index.read_bytes() if index.exists() else None,
    }


# --- the owner opening the conversation repairs it ---------------------------------------


def test_opening_the_conversation_repairs_a_sidecar_an_earlier_build_left_damaged(isolated_state, active_profile):
    from api import shares
    path, token = _shared_then_damaged()
    before = json.loads(path.read_text(encoding="utf-8"))
    assert _metadata_reload() == (None, None)  # the residual #8830 describes

    handler = _open()
    assert handler.status == 200
    assert handler.json_body()["session"]["share_token"] == token
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert json.loads(path.read_text(encoding="utf-8")) == before  # only the key order changed
    assert not path.with_suffix(".json.bak").exists()
    assert _metadata_reload() == (token, token)
    assert _index_tokens() == [token]
    assert shares.load_share(token) is not None


UI_MESSAGE_FETCHES = {
    # messages.js:7759/7959 and sessions.js:4373 (the first page of a conversation)
    "msg_limit=30": "messages=1&resolve_model=0&msg_limit=30&expand_renderable=1",
    # sessions.js:4373 without a limit
    "reload": "messages=1&resolve_model=0",
    # sessions.js:5316/5382 (older pages)
    "msg_before": "messages=1&resolve_model=0&msg_before=2&msg_limit=30",
    # sessions.js:5524 ("load all")
    "msg_limit=all": "messages=1&resolve_model=0&msg_limit=all",
}


@pytest.mark.parametrize("fetch", list(UI_MESSAGE_FETCHES))
def test_the_ui_open_sequence_repairs_it(isolated_state, active_profile, fetch):
    """The client opens a conversation with a metadata GET (sessions.js:244/2989), then fetches
    messages. The server cache is NOT cleared between the two, as on a live server, so the
    metadata-only object from the first GET is what the second one finds."""
    from api import models
    from api.routes import handle_get
    path, token = _shared_then_damaged()
    models.SESSIONS.clear()

    def get(query):
        handler = _FakeHandler()
        handle_get(handler, urllib.parse.urlparse(f"/api/session?session_id={SID}&{query}"))
        assert handler.status == 200, (query, handler.status)
        return handler.json_body()["session"]

    opened = get("messages=0&resolve_model=0")
    assert opened.get("share_token") is None  # the damage: the prefix has no token
    fetched = get(UI_MESSAGE_FETCHES[fetch])
    assert fetched.get("share_token") == token
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    # the next reload, warm and cold, now shows the token
    assert get("messages=0&resolve_model=0").get("share_token") == token
    models.SESSIONS.clear()
    assert get("messages=0&resolve_model=0").get("share_token") == token
    assert _metadata_reload() == (token, token)


def test_a_load_alone_never_writes(isolated_state):
    """``Session.load`` runs before (or without) an ownership decision, so it only notes the damage."""
    path, token = _shared_then_damaged()
    before = _snapshot(path)
    session = _full_load()
    assert session.share_token == token
    assert session._share_fields_misplaced is True
    assert _snapshot(path) == before


def test_stop_sharing_works_after_the_repair(isolated_state, active_profile):
    from api import shares
    _path, token = _shared_then_damaged()
    assert _open().status == 200
    status, _payload = _post("/api/share/revoke", {"session_id": SID})
    assert status == 200
    assert shares.load_share(token) is None
    assert _metadata_reload() == (None, None)


def test_opening_again_does_not_rewrite_a_repaired_sidecar(isolated_state, active_profile):
    path, _token = _shared_then_damaged()
    assert _open().status == 200
    before = _snapshot(path)
    assert _open().status == 200
    assert _open(messages=0).status == 200
    assert _snapshot(path) == before


@pytest.mark.parametrize("legacy", [True, False])
def test_opening_leaves_an_undamaged_sidecar_untouched(isolated_state, active_profile, legacy):
    path = _save_session(legacy=legacy)
    if not legacy:
        status, _reply = _post("/api/share/create", {"session_id": SID})
        assert status == 200
    before = _snapshot(path)
    assert _open().status == 200
    assert _snapshot(path) == before


def test_a_partly_damaged_sidecar_is_repaired(isolated_state, active_profile):
    path, token = _shared_then_damaged(keys=("share_created_at",))
    assert _before_body(path, "share_token")
    assert _open().status == 200
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert _metadata_reload() == (token, token)


def test_a_revoked_share_left_after_the_body_is_moved_too(isolated_state, active_profile):
    """A .621..630 revoke on a legacy sidecar wrote ``share_token: null`` after ``messages``."""
    from api import models
    path = _save_session(legacy=True)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update({"share_token": None, "share_created_at": None})
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    models.SESSIONS.clear()
    handler = _open()
    assert handler.status == 200
    assert handler.json_body()["session"].get("share_token") is None
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert _metadata_reload() == (None, None)


def test_a_failed_repair_does_not_break_opening(isolated_state, active_profile, monkeypatch, caplog):
    from api import models
    path, token = _shared_then_damaged()
    damaged = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(models, "_patch_session_sidecar", boom)
    handler = _open()
    assert handler.status == 200
    assert handler.json_body()["session"]["share_token"] == token
    assert path.read_bytes() == damaged
    assert "could not move its share fields" in caplog.text


def test_the_repair_rechecks_under_the_lock(isolated_state, active_profile):
    """A writer that fixed the sidecar between the parse and the repair wins: nothing is rewritten."""
    from api import models
    path, _token = _shared_then_damaged()
    session = _full_load()  # notes the damage
    assert _open().status == 200  # repairs it
    repaired = path.read_bytes()
    assert models.repair_session_share_field_order(session, SID) is False  # stale note, file already fixed
    assert path.read_bytes() == repaired


def test_a_revoke_during_the_repair_waits_and_wins(isolated_state, active_profile, monkeypatch):
    """The repair writes under the session persistence lock. A revoke that arrives while the
    repair is mid-write waits for it and then wins: the repair can never write a revoked token
    back into the sidecar."""
    import sys
    import threading

    from api import models, shares

    path, token = _shared_then_damaged()
    paused = threading.Event()
    resume = threading.Event()
    real_fsync = models.os.fsync

    def _pause_repair_fsync(fd):
        if (
            threading.current_thread().name == "repair"
            and sys._getframe(1).f_code.co_name == "_patch_session_sidecar"
            and not paused.is_set()
        ):
            paused.set()  # repaired bytes written, sidecar not yet replaced
            assert resume.wait(10), "test never resumed the repair"
        return real_fsync(fd)

    monkeypatch.setattr(models.os, "fsync", _pause_repair_fsync)
    results = {}
    repair = threading.Thread(target=lambda: results.update(open=_open()), name="repair")
    repair.start()
    assert paused.wait(10), "opening the conversation never reached the repair write"
    revoke = threading.Thread(
        target=lambda: results.update(revoke=_post("/api/share/revoke", {"session_id": SID})),
        name="revoke",
    )
    revoke.start()
    revoke.join(0.5)
    revoke_waited = revoke.is_alive()
    resume.set()
    repair.join(10)
    revoke.join(10)
    assert not repair.is_alive() and not revoke.is_alive(), "a writer deadlocked"
    assert results["open"].status == 200
    assert results["revoke"][0] == 200
    assert revoke_waited, "the revoke ran while the repair held the sidecar mid-write"
    assert shares.load_share(token) is None
    assert json.loads(path.read_text(encoding="utf-8")).get("share_token") is None
    assert _metadata_reload() == (None, None)


def test_a_delete_during_the_repair_waits_and_wins(isolated_state, active_profile, monkeypatch):
    """``/api/session/delete`` removes the sidecar under the session lock. A delete that arrives
    while the repair is mid-write waits for it and then wins: the repair can never put a deleted
    sidecar, or its index row, back."""
    import sys
    import threading

    from api import models, routes

    monkeypatch.setattr(routes, "SESSION_DIR", models.SESSION_DIR)
    path, _token = _shared_then_damaged()
    paused = threading.Event()
    resume = threading.Event()
    real_fsync = models.os.fsync

    def _pause_repair_fsync(fd):
        if (
            threading.current_thread().name == "repair"
            and sys._getframe(1).f_code.co_name == "_patch_session_sidecar"
            and not paused.is_set()
        ):
            paused.set()  # repaired bytes written, sidecar not yet replaced
            assert resume.wait(10), "test never resumed the repair"
        return real_fsync(fd)

    monkeypatch.setattr(models.os, "fsync", _pause_repair_fsync)
    results = {}
    repair = threading.Thread(target=lambda: results.update(open=_open()), name="repair")
    repair.start()
    assert paused.wait(10), "opening the conversation never reached the repair write"
    delete = threading.Thread(
        target=lambda: results.update(delete=_post("/api/session/delete", {"session_id": SID})),
        name="delete",
    )
    delete.start()
    delete.join(0.5)
    delete_waited = delete.is_alive()
    resume.set()
    repair.join(10)
    delete.join(10)
    assert not repair.is_alive() and not delete.is_alive(), "a writer deadlocked"
    assert results["open"].status == 200
    assert results["delete"][0] == 200, results["delete"]
    assert delete_waited, "the delete ran while the repair held the sidecar mid-write"
    assert not path.exists(), "the repair put the deleted sidecar back"
    assert SID not in {row.get("session_id") for row in _index_rows()}


def test_a_session_caught_mid_save_keeps_its_note_and_is_repaired_on_the_next_open(isolated_state, active_profile):
    """The repair never waits for the session's agent lock. While another thread holds it (a
    turn's save), opening the conversation succeeds and writes nothing, and the cached object
    keeps its note; the next open, warm cache, repairs it. A direct call honours the same lock,
    so no caller can repair without it."""
    from api import models
    from api.config import _get_session_agent_lock
    from api.routes import handle_get

    path, token = _shared_then_damaged()
    before = path.read_bytes()
    lock = _get_session_agent_lock(SID)
    assert lock.acquire(timeout=5)
    try:
        assert _open().status == 200
        assert path.read_bytes() == before, "the repair wrote while the session lock was held"
        assert not path.with_suffix(".json.bak").exists()
        cached = models.SESSIONS.get(SID)
        assert cached is not None and cached._share_fields_misplaced is True, "the note was lost"
        assert models.repair_session_share_field_order(cached, SID) is False
        assert path.read_bytes() == before and cached._share_fields_misplaced is True
    finally:
        lock.release()
    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(f"/api/session?session_id={SID}&messages=1"))
    assert handler.status == 200
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert _metadata_reload() == (token, token)


def _recoverable_damaged_sidecar():
    """A share-damaged sidecar that ALSO has a real shrink backup with more messages.

    ``Session.save`` writes the backup itself (no hand-written fixture), so
    ``/api/session/recovery/repair-safe`` restores four messages over the live two.
    """
    from api import models

    path, token = _shared_then_damaged()
    session = models.Session.load(SID)
    assert session is not None
    session.messages.extend([
        {"role": "user", "content": "recover this question", "timestamp": 200},
        {"role": "assistant", "content": "recover this answer", "timestamp": 201},
    ])
    session.save(touch_updated_at=False)
    session.messages = session.messages[:2]
    session.save(touch_updated_at=False)
    assert len(json.loads(path.with_suffix(".json.bak").read_text())["messages"]) == 4
    _damage_like_621(path)
    models.SESSIONS.clear()
    return path, token


def _pause_fsync(monkeypatch, thread_name, caller):
    """Pause the first fsync ``thread_name`` makes from ``caller`` until ``resume`` is set."""
    import sys
    import threading

    from api import models

    paused, resume = threading.Event(), threading.Event()
    real_fsync = models.os.fsync

    def _fsync(fd):
        if (
            threading.current_thread().name == thread_name
            and sys._getframe(1).f_code.co_name == caller
            and not paused.is_set()
        ):
            paused.set()
            assert resume.wait(10), "test never resumed the paused writer"
        return real_fsync(fd)

    monkeypatch.setattr(models.os, "fsync", _fsync)
    return paused, resume


def test_a_backup_recovery_during_the_repair_waits_and_is_not_undone(isolated_state, active_profile, monkeypatch):
    """Backup recovery (``/api/session/recovery/repair-safe``) holds the same persist lock as
    the repair, from its inspection through its replace. A recovery that arrives while the
    repair is mid-write waits; it then restores over the repaired copy, so the repair can
    never overwrite restored messages with the copy it read earlier (gate finding on #8833)."""
    import threading

    from api import models, routes

    monkeypatch.setattr(routes, "SESSION_DIR", models.SESSION_DIR)
    path, token = _recoverable_damaged_sidecar()
    paused, resume = _pause_fsync(monkeypatch, "repair", "_patch_session_sidecar")
    results = {}
    repair = threading.Thread(target=lambda: results.update(open=_open()), name="repair")
    repair.start()
    assert paused.wait(10), "opening the conversation never reached the repair write"
    recovery = threading.Thread(
        target=lambda: results.update(recovery=_post("/api/session/recovery/repair-safe", {})),
        name="recovery",
    )
    recovery.start()
    recovery.join(0.5)
    recovery_waited = recovery.is_alive()
    resume.set()
    repair.join(10)
    recovery.join(10)
    assert not repair.is_alive() and not recovery.is_alive(), "a writer deadlocked"
    assert results["open"].status == 200
    status, result = results["recovery"]
    assert status == 200 and result["backup_repair"]["restored"] == 1, result
    assert recovery_waited, "the recovery ran while the repair held the sidecar mid-write"
    assert len(json.loads(path.read_text())["messages"]) == 4, "the repair undid the recovery"
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert _metadata_reload() == (token, token)


def test_a_repair_during_a_backup_recovery_waits_for_it(isolated_state, active_profile, monkeypatch):
    """The other order: the repair waits for a recovery already mid-write, then re-checks the
    restored file under the lock and finds nothing to move."""
    import threading

    from api import models, routes

    monkeypatch.setattr(routes, "SESSION_DIR", models.SESSION_DIR)
    path, token = _recoverable_damaged_sidecar()
    paused, resume = _pause_fsync(monkeypatch, "recovery", "_recover_session_locked")
    results = {}
    recovery = threading.Thread(
        target=lambda: results.update(recovery=_post("/api/session/recovery/repair-safe", {})),
        name="recovery",
    )
    recovery.start()
    assert paused.wait(10), "the recovery never reached its write"
    repair = threading.Thread(target=lambda: results.update(open=_open()), name="repair")
    repair.start()
    repair.join(0.5)
    repair_waited = repair.is_alive()
    resume.set()
    recovery.join(10)
    repair.join(10)
    assert not repair.is_alive() and not recovery.is_alive(), "a writer deadlocked"
    status, result = results["recovery"]
    assert status == 200 and result["backup_repair"]["restored"] == 1, result
    assert results["open"].status == 200
    assert repair_waited, "the repair ran while the recovery held the sidecar mid-write"
    assert len(json.loads(path.read_text())["messages"]) == 4
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert _metadata_reload() == (token, token)


# --- another profile's damaged sidecar: a refused request never writes (#8094) ----------


def _foreign_damaged(active_profile):
    """A share-damaged sidecar owned by ``work``, requested while ``default`` is active."""
    active_profile("work")
    path, token = _shared_then_damaged(profile="work")
    active_profile("default")
    return path, token


def test_the_visibility_guards_do_not_repair_a_foreign_damaged_sidecar(isolated_state, active_profile):
    from api import models, routes
    path, _token = _foreign_damaged(active_profile)
    before = _snapshot(path)
    assert not routes._session_id_visible_to_request_profile(object(), SID, emit_error=False)
    for method, url, body in [
        ("GET", f"/api/session/status?session_id={SID}", None),
        ("POST", "/api/share/create", {"session_id": SID}),
        ("PATCH", "/api/session", {"session_id": SID}),
        ("DELETE", "/api/session", {"session_id": SID}),
        ("PUT", "/api/session", {"session_id": SID}),
    ]:
        handler = _FakeHandler()
        assert not routes._guard_request_session_visibility(
            handler, urllib.parse.urlparse(url), body=body, method=method
        ), (method, url)
        assert handler.status == 409, (method, url, handler.status)
    assert _snapshot(path) == before
    assert SID not in models.SESSIONS


REFUSED_ROUTES = [
    # exempt from the generic guard: the handler owns its profile boundary
    ("GET", f"/api/session?session_id={SID}&messages=1", None),
    ("GET", f"/api/session?session_id={SID}&messages=0", None),
    ("POST", "/api/session/archive", {"session_id": SID}),
    ("POST", "/api/chat/start", {"session_id": SID, "message": "hello"}),
    ("POST", "/api/session/import_cli", {"session_id": SID}),
    ("POST", "/api/session/import_cli", {"session_id": SID, "all_profiles": True, "profile": "default"}),
    # refused by the generic request guard
    ("GET", f"/api/session/status?session_id={SID}", None),
    ("POST", "/api/share/create", {"session_id": SID}),
    ("POST", "/api/share/revoke", {"session_id": SID}),
    ("POST", "/api/session/rename", {"session_id": SID, "title": "renamed"}),
]


@pytest.mark.parametrize("method,url,body", REFUSED_ROUTES, ids=[f"{m} {u.split('?')[0]}{'?messages=' + u[-1] if 'messages=' in u else ''}{' all_profiles' if (b or {}).get('all_profiles') else ''}" for m, u, b in REFUSED_ROUTES])
def test_a_refused_route_never_writes_a_foreign_damaged_sidecar(isolated_state, active_profile, method, url, body):
    from api import models
    from api.routes import handle_get, handle_post
    path, _token = _foreign_damaged(active_profile)
    before = _snapshot(path)
    models.SESSIONS.clear()
    handler = _FakeHandler(json.dumps(body).encode() if body is not None else b"")
    (handle_get if method == "GET" else handle_post)(handler, urllib.parse.urlparse(url))
    assert handler.status in (404, 409), (url, handler.status, bytes(handler.body)[:200])
    assert _snapshot(path) == before
    cached = models.SESSIONS.get(SID)
    # A refused detail/chat load may still cache the foreign session (pre-existing, outside
    # #8830); it must not have consumed the note, i.e. no repair was attempted.
    assert cached is None or getattr(cached, "_share_fields_misplaced", True) is True


def test_the_owner_opening_it_later_still_repairs(isolated_state, active_profile):
    path, token = _foreign_damaged(active_profile)
    assert _open().status == 409  # refused under ``default``: nothing written
    active_profile("work")
    handler = _open()
    assert handler.status == 200
    assert _before_body(path, "share_token")
    assert _metadata_reload() == (token, token)


def test_the_repair_writes_only_the_sidecar_the_request_was_allowed(isolated_state, active_profile):
    """A sidecar whose body carries another session's id (a copied file) must not make the
    owner's open rewrite that other session's sidecar: the repair uses the request's id and
    refuses a mismatch."""
    from api import models
    from api.routes import handle_get
    active_profile("work")
    other, _token = _shared_then_damaged(profile="work")
    active_profile("default")
    raw = json.loads(other.read_text(encoding="utf-8"))
    raw["profile"] = "default"
    raw["title"] = "COPY"
    (other.parent / "s_8830_copy.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    models.SESSIONS.clear()
    before = (other.read_bytes(), other.stat().st_mtime_ns)
    row_before = [(r.get("profile"), r.get("title")) for r in _index_rows() if r.get("session_id") == SID]
    assert row_before == [("work", "Legacy history")]
    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse("/api/session?session_id=s_8830_copy&messages=1"))
    # #8834: the route now refuses a sidecar whose body names another session before it loads
    # it (ownership can't be verified, the #8094 rule), so it never reaches the repair.
    assert handler.status == 404
    assert (other.read_bytes(), other.stat().st_mtime_ns) == before
    assert not other.with_suffix(".json.bak").exists()
    # The repair's own refusal still guards any other caller: load the copy and repair it
    # directly under the id the request named.
    session = models.Session.load("s_8830_copy")
    assert session is not None and session._share_fields_misplaced is True
    assert models.repair_session_share_field_order(session, "s_8830_copy") is False
    assert (other.read_bytes(), other.stat().st_mtime_ns) == before
    assert not other.with_suffix(".json.bak").exists()
    # The index row is rebuilt from the body, keyed by the body's id: without the refusal the
    # copy would move the work session's row into this profile, retitled.
    row_after = [(r.get("profile"), r.get("title")) for r in _index_rows() if r.get("session_id") == SID]
    assert row_after == row_before


def test_a_file_swapped_in_after_the_load_is_not_repaired(isolated_state, active_profile):
    """The repair re-checks, on the bytes read under the lock, that they still name the file
    it writes. A file whose body names another session, swapped in after the load, is left
    alone, and that session's index row is untouched."""
    from api import models

    active_profile("work")
    _shared_then_damaged(profile="work")
    active_profile("default")
    target = "s_8830_swap"
    raw = json.loads((models.SESSION_DIR / f"{SID}.json").read_text(encoding="utf-8"))
    raw["session_id"] = target
    raw["profile"] = "default"
    swap_path = models.SESSION_DIR / f"{target}.json"
    swap_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    models.SESSIONS.clear()
    session = models.Session.load(target)
    assert session is not None and session._share_fields_misplaced is True
    raw["session_id"] = SID
    raw["title"] = "SWAPPED"
    swap_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    before = swap_path.read_bytes()
    row_before = [(r.get("profile"), r.get("title")) for r in _index_rows() if r.get("session_id") == SID]
    assert row_before == [("work", "Legacy history")]
    assert models.repair_session_share_field_order(session, target) is False
    assert swap_path.read_bytes() == before
    assert [(r.get("profile"), r.get("title")) for r in _index_rows() if r.get("session_id") == SID] == row_before


def test_the_repair_never_writes_outside_the_session_dir(isolated_state, active_profile):
    """A body ``session_id`` is data, not a path: the repair writes only the validated id's file."""
    from api import models
    from api.routes import handle_get
    path, _token = _shared_then_damaged()
    raw = json.loads(path.read_text(encoding="utf-8"))
    evil = models.SESSION_DIR.parent / "evil.json"
    evil.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    raw["session_id"] = "../evil"
    (models.SESSION_DIR / "s_8830_traversal.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    models.SESSIONS.clear()
    before = evil.read_bytes()
    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse("/api/session?session_id=s_8830_traversal&messages=1"))
    # #8834: refused before the load (the body doesn't name this file), so nothing is written.
    assert handler.status == 404
    assert evil.read_bytes() == before
    # The repair itself, called directly with the request's id, still writes nothing outside.
    session = models.Session.load("s_8830_traversal")
    assert session is not None and session._share_fields_misplaced is True
    assert models.repair_session_share_field_order(session, "s_8830_traversal") is False
    assert evil.read_bytes() == before
    assert sorted(p.name for p in models.SESSION_DIR.parent.iterdir() if p.name.startswith("evil")) == ["evil.json"]


# --- the index refresh stays in the session's own profile -----------------------------


def test_a_repair_under_another_active_profile_keeps_the_session_in_its_own_profile(isolated_state, active_profile):
    """Even if a repair ran while profile B is active, the index row is built from the sidecar
    itself: it keeps the owner's profile, and B's sidebar never lists the session."""
    from api import models
    from api.routes import handle_get
    path, token = _foreign_damaged(active_profile)  # owner ``work``, active ``default``
    rows_before = _index_rows()
    assert models._repair_misplaced_share_fields(SID) is True
    assert _before_body(path, "share_token")
    rows_after = _index_rows()
    mine = [row for row in rows_after if row.get("session_id") == SID]
    assert len(mine) == 1 and mine[0]["profile"] == "work" and mine[0]["share_token"] == token
    assert sorted(r["session_id"] for r in rows_after) == sorted(r["session_id"] for r in rows_before)
    assert not any(r.get("profile") == "default" for r in rows_after if r.get("session_id") == SID)

    def sidebar_ids(profile):
        from api import routes
        active_profile(profile)
        models.SESSIONS.clear()
        routes._session_list_cache_clear()
        handler = _FakeHandler()
        handle_get(handler, urllib.parse.urlparse("/api/sessions"))
        assert handler.status == 200
        return {s.get("session_id") for s in handler.json_body().get("sessions", [])}

    assert SID not in sidebar_ids("default")
    assert SID in sidebar_ids("work")
