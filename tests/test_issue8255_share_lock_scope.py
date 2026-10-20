"""#8255 review regressions: the share lock's scope and the create-side token.

The first fix coordinated `/api/share/create` and `/api/share/revoke` on the
per-session *agent* lock and saved the session object it had resolved *before*
taking that lock. Review found two consequences, both reproduced here:

1. A writer that finishes while a share call waits for the lock (a title rename,
   an appended message) is reverted when the share handler saves its pre-wait
   copy.
2. Coordinating on the agent lock makes a share call wait for a whole agent turn.

These tests drive `handle_post` in-process with a lock stand-in whose `acquire()`
performs the concurrent write before returning, which is exactly the interleaving
the reviewers reproduced over real HTTP. They fail on the previous head (which
used the agent lock and saved the pre-lock object) and on `master`.

The third test covers the create side of the same bug: under the #8255 cache
condition (cached copy without `share_token`, sidecar with one) create used to
mint a *new* token, leaving the previous snapshot live and unreachable.
"""

import io
import json
import urllib.parse
from collections import OrderedDict

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


def _make_session(sid="s_8255_lock_scope"):
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


class _WaitingLock:
    """Lock stand-in: the concurrent writer finishes while the handler waits."""

    def __init__(self, on_acquire):
        self._on_acquire = on_acquire
        self.acquired = 0

    def acquire(self, timeout=None):
        self.acquired += 1
        self._on_acquire()
        return True

    def release(self):
        pass


def _wait_with(monkeypatch, on_acquire):
    import api.routes as routes

    lock = _WaitingLock(on_acquire)
    # The previous head coordinated on the agent lock; this head uses the share
    # lock. Patch whichever one the handler reaches for, so the same test proves
    # the old code wrong and the new code right.
    for name in ("_get_session_share_lock", "_get_session_agent_lock"):
        monkeypatch.setattr(routes, name, lambda _sid, _lock=lock: _lock, raising=False)
    return lock


def _concurrent_write(sid, title, extra_message):
    from api.models import Session

    fresh = Session.load(sid)
    assert fresh is not None
    fresh.title = title
    fresh.messages = [*fresh.messages, {"role": "user", "content": extra_message}]
    fresh.save(touch_updated_at=False)


def test_create_keeps_a_write_that_landed_while_it_waited(isolated_state, monkeypatch):
    from api.models import Session
    from api.shares import load_share

    sid = _make_session()
    lock = _wait_with(
        monkeypatch, lambda: _concurrent_write(sid, "NEW TITLE", "third message")
    )

    status, payload = _post("/api/share/create", {"session_id": sid})

    assert lock.acquired == 1
    assert status == 200, payload
    assert payload["ok"] is True
    # The published snapshot still comes from the session the handler resolved.
    assert load_share(payload["share"]["token"]) is not None
    # The concurrent writer's save survives the share handler's own save.
    persisted = Session.load(sid)
    assert persisted is not None
    assert persisted.title == "NEW TITLE"
    assert len(persisted.messages) == 3
    # And the token it minted is the one the sidecar records.
    assert persisted.share_token == payload["share"]["token"]


def test_revoke_keeps_a_write_that_landed_while_it_waited(isolated_state, monkeypatch):
    from api.models import Session
    from api.shares import create_or_refresh_share, load_share

    sid = _make_session()

    # Publish a share and persist its token the way the create handler does.
    session = Session.load(sid)
    share_meta = create_or_refresh_share(session)
    token = share_meta["share_token"]
    resumed = Session.load(sid)
    resumed.share_token = token
    resumed.share_created_at = share_meta["share_created_at"]
    resumed.save(touch_updated_at=False)

    lock = _wait_with(
        monkeypatch, lambda: _concurrent_write(sid, "NEW TITLE", "third message")
    )

    status, payload = _post("/api/share/revoke", {"session_id": sid})

    assert lock.acquired == 1
    assert status == 200, payload
    assert payload["ok"] is True
    assert load_share(token) is None  # the security fix still holds
    persisted = Session.load(sid)
    assert persisted is not None
    assert persisted.title == "NEW TITLE"
    assert len(persisted.messages) == 3
    assert persisted.share_token is None


def test_create_reuses_the_durable_token_when_the_cached_copy_lacks_one(
    isolated_state,
):
    """Create side of #8255: refresh the recorded snapshot, never orphan it."""
    from api.models import Session
    from api.routes import SESSIONS
    from api.shares import create_or_refresh_share, load_share

    sid = _make_session()

    # 1. A share snapshot exists and its token is on disk.
    session = Session.load(sid)
    first = create_or_refresh_share(session)
    token = first["share_token"]
    assert load_share(token) is not None

    # 2. The #8255 cache condition: the resident copy has no share_token.
    stale = Session.load(sid)
    assert stale.share_token is None
    SESSIONS[sid] = stale

    # 3. The durable sidecar records the token (what the create handler persists).
    resumed = Session.load(sid)
    resumed.share_token = token
    resumed.share_created_at = first["share_created_at"]
    resumed.save(touch_updated_at=False)

    status, payload = _post("/api/share/create", {"session_id": sid})

    assert status == 200, payload
    # The recorded token is refreshed, not replaced: no second live snapshot.
    assert payload["share"]["token"] == token
    assert load_share(token) is not None
    snapshots = sorted((isolated_state / "shares").glob("*.json"))
    assert len(snapshots) == 1, [p.name for p in snapshots]
    assert Session.load(sid).share_token == token
