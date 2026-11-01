"""#8399 review: a revoked share link must stay dead.

The create handler reused whatever token the sidecar (or the resident copy)
still named, and ``write_share_snapshot`` rewrote that token's file with
``revoked_at: None``. A session that still recorded a revoked token therefore
republished it: the old URL became readable again for anyone who kept it, and no
new link was ever issued.

Covers both layers the review asked for, the stale whole-object save that feeds
the same bug, and the create/revoke x ordinary/checkpoint schedules the earlier
round requested.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.parse
from collections import OrderedDict
from types import SimpleNamespace

import pytest


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for ``routes.handle_post``."""

    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Keep sessions, the sidebar index and share snapshots inside tmp_path."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares

    sessions = OrderedDict()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    yield tmp_path


def _make_session(sid="s_8399_revocation"):
    from api.models import Session

    session = Session(session_id=sid, title="old title")
    session.messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    session.save(touch_updated_at=False)
    return sid


def _post(path, body):
    from api.routes import handle_post

    raw = json.dumps(body).encode()
    handler = _FakeHandler(raw)
    handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _sidecar(sid):
    import api.models as models

    return json.loads((models.SESSION_DIR / f"{sid}.json").read_text(encoding="utf-8"))


def _index_entries():
    import api.models as models

    path = models.SESSION_INDEX_FILE
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_revoked_token_is_never_republished_by_create(isolated_state):
    """Create T, keep a detached copy, revoke T, save the detached copy, create
    again: the old URL stays 404 and the second create issues a new token."""
    import api.shares as shares
    from api.models import Session

    sid = _make_session()
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    first = body["share"]["token"]
    assert shares.load_share(first) is not None

    detached = Session.load(sid)  # resolved before the revoke
    assert detached is not None

    status, body = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, body
    assert shares.load_share(first) is None

    # A writer holding the pre-revoke copy saves after the revoke.
    detached.title = "detached writer"
    detached.save(touch_updated_at=False)

    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    second = body["share"]["token"]
    assert second != first, "the revoked link was republished under its old token"
    assert shares.load_share(first) is None, "the old URL became readable again"
    assert shares.load_share(second) is not None
    assert _sidecar(sid)["share_token"] == second


def test_create_checks_the_token_is_live_before_reusing_it(isolated_state, monkeypatch):
    """The handler layer on its own. A revoke whose sidecar write was lost leaves
    the session naming T while T's snapshot is a tombstone; with the snapshot
    writer stubbed to accept whatever it is handed, T must still never reach it
    (#8399 review: fix at both layers)."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares
    from api.models import Session

    sid = _make_session()
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    first = body["share"]["token"]
    status, _ = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200
    assert shares.load_share(first) is None

    # Put the revoked token back on disk and in the resident copy: the state a
    # lost revoke-sidecar write leaves behind.
    sidecar_path = models.SESSION_DIR / f"{sid}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["share_token"] = first
    payload["share_created_at"] = 1.0
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
    resident = Session.load(sid)
    assert resident is not None
    resident.share_token = first
    resident._share_token_saved = first
    with models.LOCK:
        models.SESSIONS[sid] = resident
    assert _sidecar(sid)["share_token"] == first

    seen = {}

    def _stub(snapshot, *, token="", source_session_id=""):
        seen["token"] = token
        return {
            "share_token": token or "stub-minted",
            "share_created_at": 1.0,
            "share_title": "t",
            "share_message_count": 0,
            "share_updated_at": 1.0,
        }

    monkeypatch.setattr(routes, "write_share_snapshot", _stub)
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    assert seen["token"] == "", "the handler handed a revoked token to the snapshot writer"
    assert body["share"]["token"] == "stub-minted"


def test_write_share_snapshot_mints_a_fresh_token_over_a_revoked_one(isolated_state):
    """The second layer: the snapshot writer itself refuses a token whose file
    already carries a tombstone, leaving that tombstone untouched."""
    import api.shares as shares

    snapshot = {"title": "t", "messages": [], "message_count": 0}
    shares.write_share_snapshot(snapshot, token="tok-dead", source_session_id="s1")
    assert shares.revoke_share(SimpleNamespace(share_token="tok-dead")) is True
    assert shares.load_share("tok-dead") is None

    meta = shares.write_share_snapshot(snapshot, token="tok-dead", source_session_id="s1")
    assert meta["share_token"] != "tok-dead"
    assert shares.load_share("tok-dead") is None, "the tombstone was overwritten"
    assert shares.load_share(meta["share_token"]) is not None


def test_a_stale_save_does_not_erase_the_share_pair(isolated_state):
    """A writer that loaded before the share write and saves after it adopts the
    on-disk pair instead of restoring its own."""
    import api.shares as shares
    from api.models import Session

    sid = _make_session()
    stale = Session.load(sid)  # resolved before the create
    assert stale is not None

    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    token = body["share"]["token"]

    stale.title = "stale writer"
    stale.save(touch_updated_at=False)
    stored = _sidecar(sid)
    assert stored["share_token"] == token, "the stale save erased the live token"
    assert stored["title"] == "stale writer", "the writer's own change was dropped"
    assert stale.share_token == token

    stale_after_revoke = Session.load(sid)
    status, body = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, body
    assert shares.load_share(token) is None

    stale_after_revoke.title = "stale writer 2"
    stale_after_revoke.save(touch_updated_at=False)
    assert _sidecar(sid)["share_token"] is None, (
        "the stale save restored a revoked token"
    )
    assert _sidecar(sid)["title"] == "stale writer 2"


def test_index_carries_the_token_after_create_and_none_after_revoke(isolated_state):
    """The sidebar index is what a cold search reads, so it has to move with the
    sidecar on both transitions (review: cold /api/sessions/search checks)."""
    import api.models as models

    sid = _make_session()
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    token = body["share"]["token"]

    models.SESSIONS.clear()  # cold: the next search reads the index only
    rows = [r for r in _index_entries() if r.get("session_id") == sid]
    assert rows and rows[0].get("share_token") == token

    status, body = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, body
    models.SESSIONS.clear()
    rows = [r for r in _index_entries() if r.get("session_id") == sid]
    assert rows and rows[0].get("share_token") is None


@pytest.mark.parametrize("path", ["/api/share/create", "/api/share/revoke"])
@pytest.mark.parametrize("skip_index", [False, True])
def test_share_write_keeps_a_concurrent_save(isolated_state, path, skip_index):
    """The schedules from the previous round: a save that lands while the share
    write holds the persistence lock must not be reverted, in the ordinary
    (``skip_index=False``) and checkpoint (``skip_index=True``) shapes."""
    import api.models as models
    from api.models import Session

    sid = _make_session()
    if path.endswith("revoke"):
        status, body = _post("/api/share/create", {"session_id": sid})
        assert status == 200, body

    paused = threading.Event()
    resume = threading.Event()
    real_replace = models._replace_session_sidecar

    def _pause_share_replace(tmp, target, sid_arg, *a, **kw):
        if threading.current_thread().name == "share" and not paused.is_set():
            paused.set()  # the share write holds the persistence lock here
            assert resume.wait(10), "test never resumed the share replace"
        return real_replace(tmp, target, sid_arg, *a, **kw)

    models._replace_session_sidecar = _pause_share_replace
    try:
        share = threading.Thread(
            target=_post, args=(path, {"session_id": sid}), name="share",
        )
        share.start()
        assert paused.wait(10), "the share write never reached its sidecar replace"

        writer = Session.load(sid)
        writer.title = "concurrent writer"
        writer.messages = [*writer.messages, {"role": "user", "content": "later"}]
        done = threading.Event()

        def _save():
            writer.save(touch_updated_at=False, skip_index=skip_index)
            done.set()

        save_thread = threading.Thread(target=_save, name="writer")
        save_thread.start()
        assert not done.wait(0.3), "the save ran while the share write held the lock"

        resume.set()
        share.join(10)
        save_thread.join(10)
        assert not share.is_alive() and not save_thread.is_alive(), "a writer deadlocked"
        assert done.is_set()
    finally:
        models._replace_session_sidecar = real_replace

    stored = _sidecar(sid)
    assert stored["title"] == "concurrent writer", "the share write reverted the save"
    assert len(stored["messages"]) == 3
    if path.endswith("create"):
        assert stored["share_token"] is not None
    else:
        assert stored["share_token"] is None
