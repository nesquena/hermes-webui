"""#8950 ANCESTOR-READ-F1: an inconclusive ancestor read must not erase a pending token.

A share is published under token T with the parent A as ``source_session_id`` and
mirrored into the resident; before the pair reaches disk a streaming checkpoint
saves, the real compression migration republishes the resident as continuation B,
and the share-field persist fails with an ``OSError``. Recovery hands T back to
the resident object as a pending pair, so B owes the token to its next save.

The blocker: the ownership classifier resolves B's ancestry by reading A's
sidecar. When that read is denied (transient I/O failure, not a proven absence),
``_read_session_lineage`` collapses the failure into ``None``, the continuation
check answers False, and the classifier answers "absent". The continuation's
metadata save then adopts the empty durable pair and T becomes unreachable: after
access returns, "Stop sharing" answers 200 while the public snapshot is still
served.

The fix must keep three states apart -- verified ancestry, proven absence, and an
inconclusive read -- while ownership authorization stays fail-closed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import urllib.parse
from collections import OrderedDict
from pathlib import Path

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


@contextlib.contextmanager
def _deny_read(path: Path):
    """Deny reads of *path* for the duration of the block (real I/O denial)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,  # dwShareMode = 0 (exclusive)
            None,
            3,  # OPEN_EXISTING
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle or handle == -1:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            try:
                path.read_text(encoding="utf-8")
                denied = False
            except OSError:
                denied = True
            if not denied:
                pytest.skip("Read denial did not take effect")
            yield
        finally:
            kernel32.CloseHandle(handle)
    else:
        old_mode = path.stat().st_mode
        os.chmod(path, 0o000)
        try:
            try:
                path.read_text(encoding="utf-8")
                denied = False
            except OSError:
                denied = True
            if not denied:
                pytest.skip("Read denial did not take effect (running as root?)")
            yield
        finally:
            os.chmod(path, old_mode)


def _pending_continuation(isolated_state, monkeypatch):
    """Drive a real share create into a continuation with T pending on B.

    Returns ``(parent_sid, child_sid, token)``. B is a real compression
    continuation of A (reciprocal markers on both sidecars) and holds T only on
    the resident object: B's sidecar still carries the empty pair.
    """
    sid_a = "s_8950_anc_parent_a"
    sid_b = "s_8950_anc_child_b"
    _make_session(sid_a)
    agent_lock = threading.Lock()

    def _checkpoint_migrates_then_persist_fails(sid, *, share_token, share_created_at):
        resident = models.SESSIONS.get(sid_a)
        assert resident is not None, "the parent must be the resident during create"
        resident.save(touch_updated_at=False, skip_index=True)
        assert streaming._migrate_compression_session_ownership(
            resident, sid_a, sid_b, agent_lock
        ) is True
        resident.save(touch_updated_at=False, skip_index=True)
        raise OSError("injected disk failure")

    real_update = routes._update_session_share_fields
    monkeypatch.setattr(
        routes, "_update_session_share_fields", _checkpoint_migrates_then_persist_fails
    )
    with pytest.raises(OSError, match="injected disk failure"):
        _post("/api/share/create", {"session_id": sid_a})
    monkeypatch.setattr(routes, "_update_session_share_fields", real_update)

    tokens = [p.stem for p in (shares.SHARES_DIR).glob("*.json")]
    assert len(tokens) == 1, tokens
    token = tokens[0]
    assert _snapshot(token)["source_session_id"] == sid_a
    assert _snapshot(token)["revoked_at"] is None

    resident = models.SESSIONS.get(sid_b)
    assert resident is not None and resident.session_id == sid_b
    assert resident.parent_session_id == sid_a
    assert resident.share_token == token
    # Pending: on the object, not yet in B's sidecar.
    assert _sidecar(sid_b)["share_token"] is None
    return sid_a, sid_b, token


def test_denied_ancestor_read_keeps_pending_token(isolated_state, monkeypatch):
    """Blocker: denying the ANCESTOR sidecar read must not erase the pending token."""
    sid_a, sid_b, token = _pending_continuation(isolated_state, monkeypatch)
    resident = models.SESSIONS.get(sid_b)
    ancestor = models.SESSION_DIR / f"{sid_a}.json"

    with _deny_read(ancestor):
        resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token == token, "denied ancestor read: resident lost the token"
    assert _sidecar(sid_b)["share_token"] == token, "denied ancestor read: sidecar lost the token"

    # Access returns: the token must still be revocable and the public link must die.
    status, body = _post("/api/share/revoke", {"session_id": sid_b})
    assert status == 200, body
    assert _sidecar(sid_b)["share_token"] is None
    assert _snapshot(token)["revoked_at"] is not None, "snapshot stayed public after revoke"
    assert shares.load_share(token) is None, "public loader still returns the snapshot"


def test_readable_ancestor_control(isolated_state, monkeypatch):
    """Control: a readable ancestor keeps the pending token and the revoke still works."""
    sid_a, sid_b, token = _pending_continuation(isolated_state, monkeypatch)
    resident = models.SESSIONS.get(sid_b)

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token == token
    assert _sidecar(sid_b)["share_token"] == token
    status, body = _post("/api/share/revoke", {"session_id": sid_b})
    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None
    assert shares.load_share(token) is None


def test_denied_ancestor_read_is_inconclusive_not_absent(isolated_state, monkeypatch):
    """The classifier must answer "unknown" for a denied ancestry read, never "absent"."""
    sid_a, sid_b, token = _pending_continuation(isolated_state, monkeypatch)
    ancestor = models.SESSION_DIR / f"{sid_a}.json"

    assert models._share_snapshot_state_for_session(token, sid_b) == "present"

    with _deny_read(ancestor):
        state = models._share_snapshot_state_for_session(token, sid_b)
        # Authorization stays fail-closed: an unproven chain is not ownership.
        assert shares._is_compression_continuation(sid_b, sid_a) is False
        with pytest.raises(shares.ShareTokenOwnershipError):
            shares._read_share_snapshot(
                token, expected_session_id=sid_b, allow_continuation=True
            )

    assert state == "unknown", f"denied ancestry read classified as {state!r}"
    # With access restored the chain is provable again.
    assert models._share_snapshot_state_for_session(token, sid_b) == "present"


def test_proven_absent_ancestor_still_drops_pending_pair(isolated_state, monkeypatch):
    """Control: a proven absence is not an inconclusive read; the empty pair is adopted."""
    sid_a, sid_b, token = _pending_continuation(isolated_state, monkeypatch)
    resident = models.SESSIONS.get(sid_b)

    (models.SESSION_DIR / f"{sid_a}.json").unlink()

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token is None
    assert _sidecar(sid_b)["share_token"] is None
