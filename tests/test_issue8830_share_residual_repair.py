"""#8830: a full load repairs a sidecar an exp-v0.52.621..630 build left with its share keys after ``messages``.

#8815's fix (#8818) stops new damage and repairs a damaged sidecar on its next share
write, but a legacy conversation those builds already shared kept its token after the
message body, past the metadata prefix, until something wrote it again: metadata reloads
and the sidebar showed it unshared (no Stop sharing) while its public link stayed live.
``Session.load`` now moves only those keys into the prefix, under the persistence lock.

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


def _save_session(*, legacy):
    """Save a session; ``legacy`` strips the share keys, as a pre-share sidecar has none."""
    from api import models

    session = models.Session(session_id=SID, title="Legacy history")
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


def _index_tokens():
    """The share token the sidebar index row carries (``/api/sessions/search`` reads it)."""
    from api import models
    rows = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    return [row.get("share_token") for row in rows if row.get("session_id") == SID]


def _shared_then_damaged(**kwargs):
    from api import models
    path = _save_session(legacy=True)
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200
    _damage_like_621(path, **kwargs)
    models.SESSIONS.clear()
    return path, reply["share"]["token"]


def _before_body(path, key):
    order = _key_order(path)
    return order.index(key) < order.index("messages") and order.count(key) == 1


def test_a_full_load_repairs_a_sidecar_an_earlier_build_left_damaged(isolated_state):
    from api import shares
    path, token = _shared_then_damaged()
    before = json.loads(path.read_text(encoding="utf-8"))
    assert _metadata_reload() == (None, None)  # the residual #8830 describes

    session = _full_load()
    assert session.share_token == token
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert json.loads(path.read_text(encoding="utf-8")) == before  # only the key order changed
    assert _metadata_reload() == (token, token)
    assert _index_tokens() == [token]
    assert shares.load_share(token) is not None


def test_stop_sharing_works_after_the_repair(isolated_state):
    from api import shares
    _path, token = _shared_then_damaged()
    _full_load()
    status, _payload = _post("/api/share/revoke", {"session_id": SID})
    assert status == 200
    assert shares.load_share(token) is None
    assert _metadata_reload() == (None, None)


def test_a_full_load_does_not_rewrite_a_repaired_sidecar(isolated_state):
    path, _token = _shared_then_damaged()
    _full_load()
    repaired = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    _full_load()
    assert path.read_bytes() == repaired
    assert path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("legacy", [True, False])
def test_a_full_load_leaves_an_undamaged_sidecar_untouched(isolated_state, legacy):
    path = _save_session(legacy=legacy)
    if not legacy:
        status, _reply = _post("/api/share/create", {"session_id": SID})
        assert status == 200
    original = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    _full_load()
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == mtime


def test_a_partly_damaged_sidecar_is_repaired(isolated_state):
    path, token = _shared_then_damaged(keys=("share_created_at",))
    assert _before_body(path, "share_token")
    _full_load()
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert _metadata_reload() == (token, token)


def test_a_revoked_share_left_after_the_body_is_moved_too(isolated_state):
    """A .621..630 revoke on a legacy sidecar wrote ``share_token: null`` after ``messages``."""
    from api import models
    path = _save_session(legacy=True)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update({"share_token": None, "share_created_at": None})
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    models.SESSIONS.clear()
    session = _full_load()
    assert session.share_token is None
    assert _before_body(path, "share_token") and _before_body(path, "share_created_at")
    assert _metadata_reload() == (None, None)


def test_a_failed_repair_does_not_break_the_load(isolated_state, monkeypatch, caplog):
    from api import models
    path, token = _shared_then_damaged()
    damaged = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(models, "_patch_session_sidecar", boom)
    session = _full_load()
    assert session is not None and session.share_token == token
    assert path.read_bytes() == damaged
    assert "could not move its share fields" in caplog.text


def test_the_repair_rechecks_under_the_lock(isolated_state):
    """A writer that fixed the sidecar between the parse and the repair wins: nothing is rewritten."""
    from api import models
    path, _token = _shared_then_damaged()
    _full_load()
    repaired = path.read_bytes()
    assert models._patch_session_sidecar is not None
    models._repair_misplaced_share_fields(SID)
    assert path.read_bytes() == repaired


def test_a_revoke_during_the_repair_waits_and_wins(isolated_state, monkeypatch):
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
    repair = threading.Thread(target=lambda: results.update(load=_full_load()), name="repair")
    repair.start()
    assert paused.wait(10), "the full load never reached the repair write"
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
    assert results["revoke"][0] == 200
    assert revoke_waited, "the revoke ran while the repair held the sidecar mid-write"
    assert shares.load_share(token) is None
    assert json.loads(path.read_text(encoding="utf-8")).get("share_token") is None
    assert _metadata_reload() == (None, None)
