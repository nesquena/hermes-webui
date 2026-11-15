"""#8950 round 7: an explicit pending share clear must survive a metadata save.

When a share revocation writes its tombstone but the sidecar replacement fails
with an OSError, the recovery helper hands the resident session a pending
(None, None) clear against its old durable baseline. An unrelated metadata save
(touch_updated_at=False) must not adopt the old durable pair from disk: an empty
pending token is an explicit clear and must not reach the snapshot classifier.
"""

from __future__ import annotations

import io
import json
import urllib.parse
from collections import OrderedDict

import pytest

import api.models as models
import api.routes as routes
import api.shares as shares


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
    sessions = OrderedDict()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    yield tmp_path


def _make_session(sid="s_8950_revoke_recovery"):
    session = models.Session(session_id=sid, title="initial title")
    session.messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    session.save(touch_updated_at=False)
    return sid


def _post(path, body):
    raw = json.dumps(body).encode()
    handler = _FakeHandler(raw)
    routes.handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _sidecar(sid):
    return json.loads((models.SESSION_DIR / f"{sid}.json").read_text(encoding="utf-8"))


def test_composed_revoke_recovery_clear(isolated_state, monkeypatch):
    """1. Composed regression: revoke tombstone + failed sidecar patch + metadata save preserves clear."""
    sid = _make_session("s_8950_composed")
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    token = body["share"]["token"]
    assert token

    assert _sidecar(sid)["share_token"] == token
    resident = models.get_session(sid)
    assert resident.share_token == token
    assert shares.load_share(token) is not None

    real_replace = models._replace_session_sidecar

    def _failing_replace(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr(models, "_replace_session_sidecar", _failing_replace)
    with pytest.raises(OSError, match="injected disk failure"):
        _post("/api/share/revoke", {"session_id": sid})

    assert shares.load_share(token) is None
    assert _sidecar(sid)["share_token"] == token

    monkeypatch.setattr(models, "_replace_session_sidecar", real_replace)

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token is None
    assert resident.compact()["share_token"] is None
    assert _sidecar(sid)["share_token"] is None
    assert shares.load_share(token) is None

    # Prove the fix is not a one-shot: a second metadata save keeps them clear
    resident.save(touch_updated_at=False, skip_index=True)
    assert resident.share_token is None
    assert resident.compact()["share_token"] is None
    assert _sidecar(sid)["share_token"] is None
    assert shares.load_share(token) is None


def test_normal_save_control(isolated_state, monkeypatch):
    """2. Normal-save control: failed patch followed by normal save keeps clear."""
    sid = _make_session("s_8950_normal_ctrl")
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    token = body["share"]["token"]
    assert _sidecar(sid)["share_token"] == token

    resident = models.get_session(sid)
    real_replace = models._replace_session_sidecar

    def _failing_replace(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr(models, "_replace_session_sidecar", _failing_replace)
    with pytest.raises(OSError, match="injected disk failure"):
        _post("/api/share/revoke", {"session_id": sid})

    assert shares.load_share(token) is None
    assert _sidecar(sid)["share_token"] == token

    monkeypatch.setattr(models, "_replace_session_sidecar", real_replace)

    resident.save(touch_updated_at=True, skip_index=True)

    assert resident.share_token is None
    assert resident.compact()["share_token"] is None
    assert _sidecar(sid)["share_token"] is None
    assert shares.load_share(token) is None


def test_newer_durable_pair_control(isolated_state, monkeypatch):
    """3. Newer-durable-pair control: newer durable pair on disk wins precedence over pending clear."""
    sid = _make_session("s_8950_newer_durable")
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    token = body["share"]["token"]
    assert _sidecar(sid)["share_token"] == token

    resident = models.get_session(sid)
    real_replace = models._replace_session_sidecar

    def _failing_replace(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr(models, "_replace_session_sidecar", _failing_replace)
    with pytest.raises(OSError, match="injected disk failure"):
        _post("/api/share/revoke", {"session_id": sid})

    assert shares.load_share(token) is None
    assert _sidecar(sid)["share_token"] == token

    monkeypatch.setattr(models, "_replace_session_sidecar", real_replace)

    newer_token = "tok_newer_durable_8950"
    newer_created_at = 99999.0
    updated = models.update_session_share_fields(
        sid, share_token=newer_token, share_created_at=newer_created_at
    )
    assert updated is True
    assert _sidecar(sid)["share_token"] == newer_token

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token == newer_token
    assert resident.share_created_at == newer_created_at
    assert resident.compact()["share_token"] == newer_token
    assert _sidecar(sid)["share_token"] == newer_token
    assert _sidecar(sid)["share_created_at"] == newer_created_at


def test_nonempty_pending_token_control(isolated_state):
    """4. Nonempty pending token control: absent or foreign snapshot still lets metadata save adopt durable pair."""
    # Sub-case A: no snapshot exists
    sid_absent = _make_session("s_8950_nonempty_absent")
    resident_absent = models.get_session(sid_absent)
    assert resident_absent.share_token is None

    detached_absent = models.Session.load(sid_absent)
    pending_token = "tok_nonexistent_8950"
    pending_created_at = 12345.0

    routes._return_mirrored_share_pair_to_resident(
        sid_absent,
        resident_absent,
        detached_absent,
        share_token=pending_token,
        share_created_at=pending_created_at,
    )
    assert resident_absent.share_token == pending_token
    assert _sidecar(sid_absent)["share_token"] is None

    resident_absent.save(touch_updated_at=False, skip_index=True)

    assert resident_absent.share_token is None
    assert resident_absent.share_created_at is None
    assert resident_absent.compact()["share_token"] is None
    assert _sidecar(sid_absent)["share_token"] is None

    # Sub-case B: foreign snapshot exists
    sid_foreign = _make_session("s_8950_nonempty_foreign")
    foreign_sid = _make_session("s_8950_foreign_target")
    resident_foreign = models.get_session(sid_foreign)
    detached_foreign = models.Session.load(sid_foreign)

    foreign_detached = models.Session.load(foreign_sid)
    snapshot = shares.build_share_snapshot(foreign_detached)
    published = shares.write_share_snapshot(snapshot, source_session_id=foreign_sid)
    foreign_token = published["share_token"]
    foreign_created_at = published["share_created_at"]

    routes._return_mirrored_share_pair_to_resident(
        sid_foreign,
        resident_foreign,
        detached_foreign,
        share_token=foreign_token,
        share_created_at=foreign_created_at,
    )
    assert resident_foreign.share_token == foreign_token
    assert _sidecar(sid_foreign)["share_token"] is None

    resident_foreign.save(touch_updated_at=False, skip_index=True)

    assert resident_foreign.share_token is None
    assert resident_foreign.share_created_at is None
    assert resident_foreign.compact()["share_token"] is None
    assert _sidecar(sid_foreign)["share_token"] is None
