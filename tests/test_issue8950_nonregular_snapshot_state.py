"""#8950: a present but non-regular snapshot path is inconclusive, not definitive absence.

A metadata save (touch_updated_at=False) adopts the empty durable pair only when
the snapshot is definitively absent. A present but non-regular path (directory)
is inconclusive ('unknown'), keeping the pending share pair intact and revokable.
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


def _make_session(sid="s_8950_nonregular"):
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


def test_readable_control(isolated_state):
    """1. Readable control: snapshot is readable, metadata save keeps pair."""
    sid = _make_session("s_8950_control")
    resident = models.get_session(sid)
    assert resident.share_token is None

    detached = models.Session.load(sid)
    snapshot = shares.build_share_snapshot(detached)
    published = shares.write_share_snapshot(snapshot, source_session_id=sid)
    token = published["share_token"]
    created_at = published["share_created_at"]

    routes._return_mirrored_share_pair_to_resident(
        sid, resident, detached, share_token=token, share_created_at=created_at
    )
    assert resident.share_token == token
    assert _sidecar(sid)["share_token"] is None
    assert models._share_snapshot_state_for_session(token, sid) == "present"

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token == token
    assert resident.share_created_at == created_at
    assert _sidecar(sid)["share_token"] == token
    assert _sidecar(sid)["share_created_at"] == created_at
    assert shares.load_share(token) is not None


def test_directory_substitution(isolated_state):
    """2. Directory substitution: present non-regular path is unknown, keeps pair, revoke works."""
    sid = _make_session("s_8950_dir")
    resident = models.get_session(sid)
    assert resident.share_token is None

    detached = models.Session.load(sid)
    snapshot = shares.build_share_snapshot(detached)
    published = shares.write_share_snapshot(snapshot, source_session_id=sid)
    token = published["share_token"]
    created_at = published["share_created_at"]

    routes._return_mirrored_share_pair_to_resident(
        sid, resident, detached, share_token=token, share_created_at=created_at
    )
    assert resident.share_token == token
    assert _sidecar(sid)["share_token"] is None

    snapshot_path = shares._share_path(token)
    orig_bytes = snapshot_path.read_bytes()

    snapshot_path.unlink()
    snapshot_path.mkdir()

    with pytest.raises(OSError):
        snapshot_path.read_text(encoding="utf-8")

    assert models._share_snapshot_state_for_session(token, sid) == "unknown"

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token == token
    assert resident.share_created_at == created_at
    assert _sidecar(sid)["share_token"] == token
    assert _sidecar(sid)["share_created_at"] == created_at

    snapshot_path.rmdir()
    snapshot_path.write_bytes(orig_bytes)

    status, body = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, body
    assert shares.load_share(token) is None
    assert _sidecar(sid)["share_token"] is None
    assert resident.share_token is None


def test_definitive_absence_control(isolated_state):
    """3. Definitive absence: deleted snapshot path drops pending pair on metadata save."""
    sid = _make_session("s_8950_absence")
    resident = models.get_session(sid)
    assert resident.share_token is None

    detached = models.Session.load(sid)
    snapshot = shares.build_share_snapshot(detached)
    published = shares.write_share_snapshot(snapshot, source_session_id=sid)
    token = published["share_token"]
    created_at = published["share_created_at"]

    routes._return_mirrored_share_pair_to_resident(
        sid, resident, detached, share_token=token, share_created_at=created_at
    )
    assert resident.share_token == token
    assert _sidecar(sid)["share_token"] is None

    snapshot_path = shares._share_path(token)
    snapshot_path.unlink()
    assert not snapshot_path.exists()
    assert models._share_snapshot_state_for_session(token, sid) == "absent"

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token is None
    assert resident.share_created_at is None
    assert _sidecar(sid)["share_token"] is None
    assert _sidecar(sid)["share_created_at"] is None


def test_foreign_owner_control(isolated_state):
    """4. Foreign owner: snapshot belongs to another session, metadata save drops pair."""
    sid = _make_session("s_8950_this_session")
    foreign_sid = _make_session("s_8950_foreign_session")
    resident = models.get_session(sid)
    detached = models.Session.load(sid)

    foreign_detached = models.Session.load(foreign_sid)
    snapshot = shares.build_share_snapshot(foreign_detached)
    published = shares.write_share_snapshot(snapshot, source_session_id=foreign_sid)
    foreign_token = published["share_token"]
    foreign_created_at = published["share_created_at"]

    routes._return_mirrored_share_pair_to_resident(
        sid, resident, detached, share_token=foreign_token, share_created_at=foreign_created_at
    )
    assert resident.share_token == foreign_token
    assert _sidecar(sid)["share_token"] is None
    assert models._share_snapshot_state_for_session(foreign_token, sid) == "absent"

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token is None
    assert resident.share_created_at is None
    assert _sidecar(sid)["share_token"] is None
    assert _sidecar(sid)["share_created_at"] is None


def test_unparseable_payload_control(isolated_state):
    """5. Unparseable payload: malformed snapshot payload drops pending pair on metadata save."""
    sid = _make_session("s_8950_garbage")
    resident = models.get_session(sid)
    detached = models.Session.load(sid)

    token = "tok_unparseable_8950"
    shares._ensure_share_dir()
    snapshot_path = shares._share_path(token)
    snapshot_path.write_bytes(b"invalid json bytes {{{")

    routes._return_mirrored_share_pair_to_resident(
        sid, resident, detached, share_token=token, share_created_at=12345.0
    )
    assert resident.share_token == token
    assert _sidecar(sid)["share_token"] is None
    assert models._share_snapshot_state_for_session(token, sid) == "absent"

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token is None
    assert resident.share_created_at is None
    assert _sidecar(sid)["share_token"] is None
    assert _sidecar(sid)["share_created_at"] is None
