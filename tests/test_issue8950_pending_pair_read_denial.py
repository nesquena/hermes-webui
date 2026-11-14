"""#8950: a metadata save must not drop a pending share pair when snapshot read is inconclusive.

A metadata save (touch_updated_at=False) checks if the snapshot exists for this session
before adopting an empty durable pair. When reading the snapshot encounters an I/O
denial (PermissionError/OSError), the state is unknown and must not drop the pending
pair handed back by _return_mirrored_share_pair_to_resident.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import urllib.parse
from collections import OrderedDict
from pathlib import Path

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


def _make_session(sid="s_8950_read_denial"):
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


@contextlib.contextmanager
def _deny_read(path: Path):
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

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token == token
    assert resident.share_created_at == created_at
    assert _sidecar(sid)["share_token"] == token
    assert _sidecar(sid)["share_created_at"] == created_at
    assert shares.load_share(token) is not None


def test_read_denial(isolated_state):
    """2. Read denial: snapshot read fails, metadata save keeps pair, revoke works."""
    sid = _make_session("s_8950_denial")
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
    with _deny_read(snapshot_path):
        resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token == token
    assert resident.share_created_at == created_at
    assert _sidecar(sid)["share_token"] == token
    assert _sidecar(sid)["share_created_at"] == created_at

    # Restore access and prove the revocation authority is functional
    status, body = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, body
    assert shares.load_share(token) is None
    assert _sidecar(sid)["share_token"] is None
    assert resident.share_token is None


def test_foreign_owner(isolated_state):
    """3. Foreign owner: snapshot belongs to another session, metadata save drops pair."""
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

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token is None
    assert resident.share_created_at is None
    assert _sidecar(sid)["share_token"] is None
    assert _sidecar(sid)["share_created_at"] is None


def test_unreadable_bytes(isolated_state):
    """4. Unreadable bytes: malformed snapshot payload, metadata save drops pair."""
    sid = _make_session("s_8950_unreadable_bytes")
    resident = models.get_session(sid)
    detached = models.Session.load(sid)

    token = "tok_unreadable_8950"
    shares._ensure_share_dir()
    snapshot_path = shares._share_path(token)
    snapshot_path.write_bytes(b"invalid json bytes {{{")

    routes._return_mirrored_share_pair_to_resident(
        sid, resident, detached, share_token=token, share_created_at=12345.0
    )
    assert resident.share_token == token
    assert _sidecar(sid)["share_token"] is None

    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.share_token is None
    assert resident.share_created_at is None
    assert _sidecar(sid)["share_token"] is None
    assert _sidecar(sid)["share_created_at"] is None
