"""#8255 review, third round: the two blockers the second-round head still had.

1. **Create and revoke reverted a finished conversation write.** Both handlers
   saved the Session object they resolved (or reloaded) earlier in the request,
   so a title rename or an appended message that landed after that read was
   written back to the older state. The handlers now patch only
   ``share_token`` and ``share_created_at`` in the bytes on disk
   (``update_session_share_fields``), under the same lock every whole-session
   writer uses for its replace, so a share call never writes conversation
   fields.

2. **Refreshing a share orphaned an already-issued link.** Create consulted only
   the reloaded sidecar's token. A checkpoint that committed after serializing
   the live session without a token left the sidecar empty while the published
   snapshot was still live, so create minted a second token and the original
   link stayed reachable. Create now falls back to the identity-checked
   resident session's token.
"""

import io
import json
import threading
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


def _make_session(sid="s_8255_third_round"):
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


def _sidecar(sid):
    import api.models as models

    return json.loads((models.SESSION_DIR / f"{sid}.json").read_text(encoding="utf-8"))


def test_update_session_share_fields_patches_only_the_share_keys(isolated_state):
    import api.models as models

    sid = _make_session()
    before = _sidecar(sid)

    assert models.update_session_share_fields(
        sid, share_token="tok-1", share_created_at=1234.5
    ) is True

    after = _sidecar(sid)
    assert after["share_token"] == "tok-1"
    assert after["share_created_at"] == 1234.5
    before.pop("share_token", None)
    before.pop("share_created_at", None)
    after.pop("share_token", None)
    after.pop("share_created_at", None)
    assert before == after, "the patch changed fields other than the share fields"


def test_update_session_share_fields_reports_a_missing_sidecar(isolated_state):
    import api.models as models

    assert models.update_session_share_fields(
        "no_such_session", share_token="tok-1", share_created_at=None
    ) is False


def test_update_session_share_fields_holds_the_persistence_lock(isolated_state):
    """The patch runs under the same per-session lock ``Session.save()`` takes,
    so a concurrent save cannot slip between its read and its replace and be
    silently reverted (#8399 review)."""
    import api.models as models

    sid = _make_session()
    lock = models._session_persist_lock(sid)
    done = threading.Event()

    def _patch():
        models.update_session_share_fields(
            sid, share_token="tok-3", share_created_at=7.0
        )
        done.set()

    with lock:
        writer = threading.Thread(target=_patch)
        writer.start()
        assert not done.wait(0.3), (
            "the share patch ran while another holder owned the persistence lock"
        )
    writer.join(10)
    assert done.is_set()

    stored = _sidecar(sid)
    assert stored["share_token"] == "tok-3"
    assert stored["share_created_at"] == 7.0


def test_create_keeps_a_write_that_lands_after_the_final_reload(
    isolated_state, monkeypatch
):
    """The reviewer's scenario 1: a writer saves during the snapshot write."""
    import api.routes as routes

    sid = _make_session()
    real_write = routes.write_share_snapshot

    def write_wrapper(snapshot, *, token="", source_session_id=""):
        # The handler has already re-read the sidecar; this write lands after it.
        _concurrent_write(sid)
        return real_write(snapshot, token=token, source_session_id=source_session_id)

    monkeypatch.setattr(routes, "write_share_snapshot", write_wrapper)

    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body

    stored = _sidecar(sid)
    assert stored["title"] == "NEW TITLE"
    assert len(stored["messages"]) == 3
    assert stored["share_token"] == body["share"]["token"]


def test_revoke_keeps_a_write_that_lands_after_the_final_reload(
    isolated_state, monkeypatch
):
    """The reviewer's scenario 1 on the revoke path."""
    import api.routes as routes

    sid = _make_session()
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body

    real_mirror = routes._mirror_share_fields_to_cached_session
    state = {"fired": False}

    def mirror_wrapper(session_id, source):
        if not state["fired"]:
            state["fired"] = True
            # Lands after the handler's final re-read, before its persist.
            _concurrent_write(sid)
        return real_mirror(session_id, source)

    monkeypatch.setattr(routes, "_mirror_share_fields_to_cached_session", mirror_wrapper)

    status, body = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, body

    stored = _sidecar(sid)
    assert stored["title"] == "NEW TITLE"
    assert len(stored["messages"]) == 3
    assert stored["share_token"] is None


def test_create_reuses_the_resident_token_when_the_sidecar_lost_it(
    isolated_state, monkeypatch
):
    """The reviewer's scenario 2: a checkpoint committed without the token while
    the link is still live, so the resident token is reused rather than
    orphaned. The link has to be live: a revoked one is never reused (#8399)."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares
    from api.models import Session

    sid = _make_session()
    # A published snapshot whose token only the live session still carries.
    shares.write_share_snapshot(
        {"title": "t", "messages": [], "message_count": 0},
        token="tok-resident",
        source_session_id=sid,
    )
    assert shares.load_share("tok-resident") is not None
    resident = Session.load(sid)
    assert resident is not None
    resident.share_token = "tok-resident"
    resident.share_created_at = 11.0
    models.SESSIONS[sid] = resident
    assert _sidecar(sid)["share_token"] is None

    captured = {}
    real_write = routes.write_share_snapshot

    def write_wrapper(snapshot, *, token="", source_session_id=""):
        captured["token"] = token
        return real_write(snapshot, token=token, source_session_id=source_session_id)

    monkeypatch.setattr(routes, "write_share_snapshot", write_wrapper)

    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    assert captured["token"] == "tok-resident"
    assert body["share"]["token"] == "tok-resident"
    assert _sidecar(sid)["share_token"] == "tok-resident"
