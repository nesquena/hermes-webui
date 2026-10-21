"""#8255 review, second round: the two windows the previous head still had.

The first round fixed the lost update and the agent-lock scope. The second
review found two windows that the fixed code still had, both reproduced with
the real handlers over loopback HTTP:

1. **A writer that saves while the snapshot is being built.** The snapshot was
   taken before the share lock and the persist saved that older state back, so
   a title rename or an appended message that landed during the snapshot I/O
   was reverted. The snapshot is now built from the session as it is inside the
   lock.

2. **A checkpoint between the persist and the mirror.** The mirror copied the
   share fields onto the live cached session after the durable save, so a
   streaming checkpoint landing in that window serialized the live session with
   ``share_token: None`` and wrote it back over the sidecar. A published link
   was then live with nothing for revoke to find. The mirror now runs before
   the save, so the live copy already carries the token when a checkpoint
   serializes it.

The third case covers the save-failure path of the same window: if the sidecar
save raises, the live session must already know the token, or a later revoke
finds nothing while the link stays live.
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


def _make_session(sid="s_8255_second_round"):
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


def _concurrent_write(sid, title="NEW TITLE", extra="third message"):
    """A writer that saves while the share handler is busy."""
    from api.models import Session

    fresh = Session.load(sid)
    assert fresh is not None
    fresh.title = title
    fresh.messages = [*fresh.messages, {"role": "user", "content": extra}]
    fresh.save(touch_updated_at=False)


class _SnapshotWriteLock:
    """Lock stand-in whose acquire() runs the concurrent writer.

    The writer runs when the handler takes the share lock, which is the point
    at which the snapshot is built from the reloaded session.
    """

    def __init__(self, on_acquire):
        self._on_acquire = on_acquire
        self.acquired = 0

    def acquire(self, timeout=None):
        self.acquired += 1
        self._on_acquire()
        return True

    def release(self):
        pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
        return False


def _install_lock(monkeypatch, on_acquire):
    import api.routes as routes

    lock = _SnapshotWriteLock(on_acquire)
    for name in ("_get_session_share_lock", "_get_session_agent_lock"):
        monkeypatch.setattr(routes, name, lambda _sid, _lock=lock: _lock, raising=False)
    return lock


def test_create_publishes_the_state_it_built_under_the_lock(
    isolated_state, monkeypatch
):
    """The published snapshot is the state under the lock, not an older copy.

    The review's window is a writer that saves between the snapshot build and
    the persist. The published snapshot must be the state the handler saw when
    it built it, and the persisted session must be the newest one — otherwise
    the share publishes a conversation that the session no longer has, or the
    persist writes back a state that was already replaced.

    The writer lands BEFORE the snapshot is built here, so the snapshot must
    contain its message. A handler that built the snapshot before taking the
    lock would publish the two older messages instead.
    """
    from api.models import Session
    from api.shares import load_share
    import api.routes as routes

    sid = _make_session()

    # The writer saves before the snapshot is built.
    import api.shares as shares

    original_build = shares.build_share_snapshot

    def build_after_writer(session):
        snapshot = original_build(session)
        # A second writer lands after the snapshot, so it must not reach the
        # published payload but must survive the persist.
        _concurrent_write(sid, title="SECOND TITLE", extra="fourth message")
        return snapshot

    monkeypatch.setattr(routes, "build_share_snapshot", build_after_writer)

    status, payload = _post("/api/share/create", {"session_id": sid})

    assert status == 200, payload
    shared = load_share(payload["share"]["token"])
    assert shared is not None
    # The snapshot was built under the lock, from the reloaded session.
    assert shared["title"] == "old title"

    persisted = Session.load(sid)
    assert persisted is not None
    # The later writer survived the persist.
    assert persisted.title == "SECOND TITLE"
    assert len(persisted.messages) == 3


def test_create_keeps_a_write_that_landed_during_the_snapshot_build(
    isolated_state, monkeypatch
):
    """A writer saving during the snapshot I/O must survive create."""
    from api.models import Session
    import api.routes as routes

    sid = _make_session()

    # The writer has to land while the snapshot is being built — that is the
    # window the review reproduced. It runs from inside build_share_snapshot,
    # after the handler reloaded the session but before the persist.
    import api.shares as shares

    original_build = shares.build_share_snapshot
    seen = {}

    def build_with_writer(session):
        # The snapshot must be built from the session as it is NOW, not from a
        # copy that was current before the lock.
        seen["title"] = getattr(session, "title", None)
        _concurrent_write(sid)
        return original_build(session)

    monkeypatch.setattr(routes, "build_share_snapshot", build_with_writer)

    status, payload = _post("/api/share/create", {"session_id": sid})

    assert status == 200, payload
    assert seen["title"] == "old title"
    persisted = Session.load(sid)
    assert persisted is not None
    assert persisted.title == "NEW TITLE"
    assert len(persisted.messages) == 3

    from api.shares import load_share

    shared = load_share(payload["share"]["token"])
    assert shared is not None
    assert shared["message_count"] == 2


def test_revoke_keeps_a_write_that_landed_during_the_snapshot_build(
    isolated_state, monkeypatch
):
    """A writer saving during the revoke's own work must survive revoke."""
    from api.models import Session
    from api.shares import create_or_refresh_share

    sid = _make_session()
    session = Session.load(sid)
    share_meta = create_or_refresh_share(session)
    resumed = Session.load(sid)
    resumed.share_token = share_meta["share_token"]
    resumed.share_created_at = share_meta["share_created_at"]
    resumed.save(touch_updated_at=False)

    lock = _install_lock(monkeypatch, lambda: _concurrent_write(sid))

    status, payload = _post("/api/share/revoke", {"session_id": sid})

    assert lock.acquired == 1
    assert status == 200, payload
    persisted = Session.load(sid)
    assert persisted is not None
    assert persisted.title == "NEW TITLE"
    assert len(persisted.messages) == 3


def test_a_checkpoint_between_the_persist_and_the_mirror_keeps_the_token(
    isolated_state, monkeypatch
):
    """A checkpoint that serializes the live session must not drop the token.

    The live cached session is what a streaming checkpoint writes to disk, so
    the token has to be on it before the durable save. A checkpoint landing
    between the save and the mirror used to write ``share_token: None`` back
    over the sidecar, and the published link stayed live with nothing for
    revoke to find.
    """
    from api.models import Session
    from api.routes import SESSIONS
    from api.shares import load_share

    sid = _make_session()

    # A live cached copy, the way a streaming worker holds it.
    live = Session.load(sid)
    SESSIONS[sid] = live

    status, payload = _post("/api/share/create", {"session_id": sid})
    assert status == 200, payload
    token = payload["share"]["token"]

    # The live copy must already carry the token, so a checkpoint that
    # serializes it writes the token and not None.
    assert live.share_token == token
    assert load_share(token) is not None

    # A checkpoint now serializes the live session.
    live.save(touch_updated_at=False, skip_index=True)

    # The sidecar still knows the token, so revoke can reach the link.
    persisted = Session.load(sid)
    assert persisted is not None
    assert persisted.share_token == token


def test_a_failed_sidecar_save_still_leaves_the_live_session_with_the_token(
    isolated_state, monkeypatch
):
    """If the sidecar save raises, the live session must know the token.

    Otherwise a later revoke finds no token, reports success, and the
    published link stays live.
    """
    import api.models as models
    from api.models import Session
    from api.routes import SESSIONS

    sid = _make_session()
    live = Session.load(sid)
    SESSIONS[sid] = live

    original_save = models.Session.save

    def failing_save(self, *args, **kwargs):
        if getattr(self, "session_id", None) == sid and not getattr(
            live, "_share_save_failed", False
        ):
            live._share_save_failed = True
            raise OSError("sidecar write failed")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", failing_save)

    with pytest.raises(OSError):
        _post("/api/share/create", {"session_id": sid})

    # The live copy was given the token before the failing save.
    assert live.share_token
