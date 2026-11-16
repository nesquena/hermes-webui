"""#8950 REFRESH-LINEAGE-UNCERTAINTY: a refresh must consume the tri-state ancestry.

A share T is published by the parent A and inherited by a verified compression
continuation B (real session, real migration, real share handlers). A later
refresh of B resolves the token's owner by walking B's ancestry. When reading
A's sidecar is denied (a real I/O denial, not a proven absence), the walk cannot
decide, so the tri-state answer is "unknown".

Today the refresh path feeds the boolean wrapper into the foreign-owner
decision: "unknown" is mapped to False, which reads as proof that the token
belongs to somebody else. The refresh then mints a replacement U on B while the
original T stays public, and "Stop sharing" on B closes only U -- the real
public loader keeps serving T.

Contract under test:
* verified continuation ("yes") may reuse its token;
* proven foreign ownership ("no") may mint a separate token;
* unknown ancestry must refuse, publishing no replacement and leaving the
  resident/durable pair untouched.
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


def _tokens():
    return sorted(p.stem for p in (shares.SHARES_DIR).glob("*.json"))


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


def _verified_continuation():
    """Share from A, then carry the resident into a verified continuation B.

    Returns ``(parent_sid, child_sid, token)``. Both real handlers run, the real
    migration publishes B, and B's sidecar durably records T.
    """
    sid_a = "s_8950_refresh_parent_a"
    sid_b = "s_8950_refresh_child_b"
    _make_session(sid_a)

    status, body = _post("/api/share/create", {"session_id": sid_a})
    assert status == 200, body
    token = body["share"]["token"]
    assert _snapshot(token)["source_session_id"] == sid_a
    assert _sidecar(sid_a)["share_token"] == token

    resident = models.SESSIONS.get(sid_a)
    assert resident is not None
    assert streaming._migrate_compression_session_ownership(
        resident, sid_a, sid_b, threading.Lock()
    ) is True
    resident.save(touch_updated_at=False, skip_index=True)

    assert resident.session_id == sid_b
    assert resident.parent_session_id == sid_a
    assert _sidecar(sid_a)["pre_compression_snapshot"] is True
    assert _sidecar(sid_a)["pre_compression_continuation_session_id"] == sid_b
    assert _sidecar(sid_b)["share_token"] == token
    assert _sidecar(sid_b)["share_token"] == resident.share_token
    assert shares.load_share(token) is not None
    return sid_a, sid_b, token


def test_denied_ancestor_read_refuses_refresh_and_preserves_the_original_share(
    isolated_state,
):
    """Blocker: an undecidable ancestry must refuse, not abandon T for a new U."""
    sid_a, sid_b, token = _verified_continuation()
    resident = models.SESSIONS.get(sid_b)
    ancestor = models.SESSION_DIR / f"{sid_a}.json"

    with _deny_read(ancestor):
        # Authorization stays fail-closed while the walk cannot decide.
        assert shares._is_compression_continuation(sid_b, sid_a) is False
        assert shares._compression_continuation_state(sid_b, sid_a) == "unknown"
        status, body = _post("/api/share/create", {"session_id": sid_b})

    assert status != 200, f"refresh published a replacement under an unknown ancestry: {body}"
    assert "share" not in body or not body.get("share"), body
    # No replacement snapshot: the original T is the only one on disk.
    assert _tokens() == [token], _tokens()
    assert _snapshot(token)["source_session_id"] == sid_a
    assert _snapshot(token)["revoked_at"] is None
    # Resident and durable pair untouched.
    assert resident.share_token == token
    assert _sidecar(sid_b)["share_token"] == token
    assert shares.load_share(token) is not None

    # Access returns: "Stop sharing" must close the link that is actually public.
    status, body = _post("/api/share/revoke", {"session_id": sid_b})
    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None, "the original share stayed public"
    assert shares.load_share(token) is None
    assert _sidecar(sid_b)["share_token"] is None


def test_readable_ancestor_control_reuses_the_token(isolated_state):
    """Control: a readable ancestry reuses T and "Stop sharing" closes it."""
    sid_a, sid_b, token = _verified_continuation()

    status, body = _post("/api/share/create", {"session_id": sid_b})

    assert status == 200, body
    assert body["share"]["token"] == token, "a verified continuation must reuse its token"
    assert _tokens() == [token], _tokens()
    assert _snapshot(token)["source_session_id"] == sid_b
    assert shares.load_share(token) is not None

    status, body = _post("/api/share/revoke", {"session_id": sid_b})
    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None
    assert shares.load_share(token) is None


def test_genuinely_foreign_token_control_mints_a_separate_token(isolated_state):
    """Control: a proven foreign owner keeps its link; the claimer gets its own token."""
    sid_owner = "s_8950_refresh_owner_c"
    sid_claimer = "s_8950_refresh_claimer_d"
    _make_session(sid_owner)
    _make_session(sid_claimer)

    status, body = _post("/api/share/create", {"session_id": sid_owner})
    assert status == 200, body
    owner_token = body["share"]["token"]

    claimer = models.Session.load(sid_claimer)
    assert claimer is not None
    models.SESSIONS[sid_claimer] = claimer
    claimer.share_token = owner_token
    claimer.save(touch_updated_at=False, skip_index=True)

    status, body = _post("/api/share/create", {"session_id": sid_claimer})
    assert status == 200, body
    replacement = body["share"]["token"]
    assert replacement != owner_token, "a proven foreign token must not be overwritten"
    assert sorted(_tokens()) == sorted([owner_token, replacement])
    assert _snapshot(owner_token)["source_session_id"] == sid_owner
    assert _snapshot(owner_token)["revoked_at"] is None
    assert shares.load_share(owner_token) is not None
    assert _sidecar(sid_claimer)["share_token"] == replacement

    status, body = _post("/api/share/revoke", {"session_id": sid_claimer})
    assert status == 200, body
    assert shares.load_share(owner_token) is not None, "the foreign link must survive"
