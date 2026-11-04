"""#8815: sharing a conversation whose sidecar predates shares keeps its token after a reload.

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


def test_a_share_on_a_legacy_sidecar_survives_a_metadata_reload(isolated_state):
    from api import shares

    path = _save_session(legacy=True)
    assert "share_token" not in _key_order(path)

    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200
    token = reply["share"]["token"]
    assert token and shares.load_share(token) is not None

    keys = _key_order(path)
    assert keys.index("share_token") < keys.index("messages"), keys
    assert keys.index("share_created_at") < keys.index("messages"), keys
    assert _metadata_reload() == (token, token)


def test_stop_sharing_works_after_a_reload_of_a_legacy_sidecar(isolated_state):
    """The token the reloaded page holds is the live one, and revoking it kills the link."""
    from api import shares

    _save_session(legacy=True)
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200
    token = reply["share"]["token"]

    reloaded_token, _wire = _metadata_reload()
    assert reloaded_token == token

    status, _payload = _post("/api/share/revoke", {"session_id": SID})
    assert status == 200
    assert shares.load_share(token) is None
    assert _metadata_reload() == (None, None)


def test_a_sidecar_that_has_the_share_keys_keeps_its_key_order(isolated_state):
    """Only a missing key is placed; an existing one is updated where it is (no reordering)."""
    path = _save_session(legacy=False)
    before = _key_order(path)
    assert "share_token" in before

    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200
    assert _key_order(path) == before
    assert _metadata_reload() == (reply["share"]["token"],) * 2


def _damage_like_621(path):
    """Move the share keys after ``messages``, as exp-v0.52.621..630 left a shared legacy sidecar."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    moved = {key: raw.pop(key) for key in ("share_token", "share_created_at")}
    raw.update(moved)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    keys = list(raw)
    assert keys.index("share_token") > keys.index("messages")


def test_a_reshare_repairs_a_sidecar_an_earlier_build_left_damaged(isolated_state):
    """A sidecar shared on .621..630 already has the token after ``messages``; updating it in
    place would leave the reloaded page without its Stop sharing control."""
    from api import models, shares

    path = _save_session(legacy=True)
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200
    token = reply["share"]["token"]
    _damage_like_621(path)
    models.SESSIONS.clear()
    assert _metadata_reload() == (None, None)  # the damaged state the issue describes

    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200
    assert reply["share"]["token"] == token  # the live link is reused, not replaced
    keys = _key_order(path)
    assert keys.index("share_token") < keys.index("messages"), keys
    assert keys.count("share_token") == 1
    assert _metadata_reload() == (token, token)
    assert shares.load_share(token) is not None


def test_a_revoke_repairs_a_sidecar_an_earlier_build_left_damaged(isolated_state):
    from api import models, shares

    path = _save_session(legacy=True)
    status, reply = _post("/api/share/create", {"session_id": SID})
    assert status == 200
    token = reply["share"]["token"]
    _damage_like_621(path)
    models.SESSIONS.clear()

    status, _payload = _post("/api/share/revoke", {"session_id": SID})
    assert status == 200
    assert shares.load_share(token) is None
    keys = _key_order(path)
    assert keys.index("share_token") < keys.index("messages"), keys
    assert _metadata_reload() == (None, None)
