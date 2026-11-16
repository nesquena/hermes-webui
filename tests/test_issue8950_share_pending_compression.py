"""#8950 SHARE-COMPRESSION-F1: a metadata save must not drop a pending share token.

Composed regression for the finding certified on this branch's head. A share is
published under token T with the parent A as ``source_session_id`` and mirrored
into the resident, then -- before the pair reaches disk -- a streaming checkpoint
saves, the real compression migration moves the resident to continuation B, and
the share-field persist fails with an OSError. Recovery hands T back to the
resident object as a pending pair, so B owes the token to its next save.

A metadata save (``touch_updated_at=False``) on B must keep that pending token.
When the snapshot classifier only accepts an exact ``source_session_id`` it
answers "absent" for a token owned by the verified parent, the save adopts the
empty durable pair, and T disappears from B. "Stop sharing" then resolves no
token at all, answers 200, and the public snapshot of T stays readable.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.parse
from collections import OrderedDict

import pytest

import api.models as models
import api.routes as routes
import api.shares as shares
import api.streaming as streaming


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
    # streaming imported SESSIONS/SESSION_DIR by value at import time, and the
    # real migration reads both of them: point them at the same isolated objects.
    monkeypatch.setattr(streaming, "SESSIONS", sessions)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    yield tmp_path


def _make_session(sid, title="compression parent"):
    session = models.Session(session_id=sid, title=title)
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


def _snapshot(token):
    return json.loads((shares.SHARES_DIR / f"{token}.json").read_text(encoding="utf-8"))


def test_pending_token_survives_continuation_metadata_save(isolated_state, monkeypatch):
    """T must survive the continuation's metadata save, and revoke must kill the link."""
    sid_a = "s_8950_pending_parent_a"
    sid_b = "s_8950_pending_child_b"
    _make_session(sid_a)

    agent_lock = threading.Lock()

    def _checkpoint_migrates_then_persist_fails(sid, *, share_token, share_created_at):
        # The share route mirrored T into the resident and is now persisting it.
        resident = models.SESSIONS.get(sid_a)
        assert resident is not None, "the parent must be the resident during create"
        assert models.SESSIONS.get(sid) is resident
        # A streaming checkpoint saves the live object first: the mirror moved the
        # stale-save baseline with the pair, so this save adopts the durable
        # (empty) pair the sidecar still holds.
        resident.save(touch_updated_at=False, skip_index=True)
        # The compression migration then republishes the resident as continuation
        # B before the share pair reaches disk, and the checkpoint persists it.
        assert streaming._migrate_compression_session_ownership(
            resident, sid_a, sid_b, agent_lock
        ) is True
        resident.save(touch_updated_at=False, skip_index=True)
        raise OSError("injected disk failure")

    real_update = routes._update_session_share_fields
    monkeypatch.setattr(routes, "_update_session_share_fields", _checkpoint_migrates_then_persist_fails)

    with pytest.raises(OSError, match="injected disk failure"):
        _post("/api/share/create", {"session_id": sid_a})

    monkeypatch.setattr(routes, "_update_session_share_fields", real_update)

    # The published snapshot exists under A's ownership; the migration left the
    # resident on B, and recovery handed it T as a pending pair.
    tokens = [p.stem for p in (shares.SHARES_DIR).glob("*.json")]
    assert len(tokens) == 1, tokens
    token = tokens[0]
    snapshot = _snapshot(token)
    assert snapshot["source_session_id"] == sid_a
    assert snapshot["revoked_at"] is None

    resident = models.SESSIONS.get(sid_b)
    assert resident is not None
    assert resident.session_id == sid_b
    assert resident.parent_session_id == sid_a
    assert models.SESSIONS.get(sid_a) is None
    assert resident.share_token == token
    # Pending: the pair is on the object, not yet in B's sidecar.
    assert _sidecar(sid_b)["share_token"] is None
    assert shares.load_share(token) is not None

    # The finding: this metadata save used to adopt the empty durable pair.
    resident.save(touch_updated_at=False, skip_index=True)

    assert _sidecar(sid_b)["share_token"] == token
    assert _sidecar(sid_b)["share_created_at"] == resident.share_created_at
    assert _sidecar(sid_a)["pre_compression_snapshot"] is True
    assert _sidecar(sid_a)["pre_compression_continuation_session_id"] == sid_b

    # "Stop sharing" on the continuation must reach and revoke A's snapshot.
    status, body = _post("/api/share/revoke", {"session_id": sid_b})
    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None
    assert shares.load_share(token) is None
    assert models.SESSIONS[sid_b].share_token is None
