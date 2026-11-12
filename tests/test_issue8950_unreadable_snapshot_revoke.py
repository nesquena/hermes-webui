"""#8950 B1 regression tests: unreadable share snapshot must not be acknowledged as revoked.

Validates that when a share snapshot exists on disk but is unreadable (e.g. due to
an OSError such as a directory replacing the file or permission denial):
- _read_share_snapshot distinguishes unreadable from absent snapshots by raising
  ShareSnapshotUnreadable instead of returning None.
- load_share catches ShareSnapshotUnreadable and returns None (anonymous 404).
- revoke_share catches ShareSnapshotUnreadable and returns False without writing
  a tombstone or acknowledging revocation.
- /api/share/revoke honours revoke_share's boolean, returning 500 without clearing
  the session's durable share_token / share_created_at pair.
- Once readability is restored, retrying revocation succeeds, writes the tombstone,
  and clears the durable pair.
- Missing snapshots (absent) and unservable bytes (corrupt JSON) retain their
  idempotent 200 semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from api import models, shares, streaming
from api.models import Session
from api.shares import SHARES_DIR, create_or_refresh_share, load_share, revoke_share
from tests._pytest_port import BASE


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolate the session store and the public snapshot dir for library-level tests."""
    session_dir = tmp_path / "sessions"
    share_dir = tmp_path / "shares"
    session_dir.mkdir()
    share_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(shares, "SHARES_DIR", share_dir)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    return session_dir, share_dir


def _make_session(sid: str, title: str, text: str, workspace: Path, **kwargs) -> models.Session:
    session = models.Session(
        session_id=sid,
        title=title,
        workspace=str(workspace),
        messages=[{"role": "user", "content": text}],
        **kwargs,
    )
    session.save()
    return models.Session.load(sid)


def _seed_share_token(session_dir: Path, sid: str, token: str, created_at: float = 1000.0) -> models.Session:
    path = session_dir / f"{sid}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["share_token"] = token
    payload["share_created_at"] = created_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    return models.Session.load(sid)


def get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            data = r.read()
            content_type = r.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(data), r.status, dict(r.headers)
            return data.decode("utf-8"), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        data = e.read()
        content_type = e.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(data), e.code, dict(e.headers)
        return data.decode("utf-8"), e.code, dict(e.headers)


def post(path: str, body: dict | None = None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _make_session_with_messages() -> str:
    created, _ = post("/api/session/new", {})
    sid = created["session"]["session_id"]
    session = Session.load(sid) or Session(session_id=sid)
    session.title = "Unreadable Snapshot Revoke Test"
    session.messages = [
        {"role": "user", "content": "Confidential discussion."},
        {"role": "assistant", "content": "Acknowledged."},
    ]
    session.save()
    return sid


def test_library_unreadable_snapshot_revoke_fails_closed_and_leaves_snapshot_untouched(store):
    """Case 1: library-level: while denied, revoke_share returns False; after restoration

    the snapshot bytes are byte-identical (no tombstone written) and load_share serves it again.
    """
    session_dir, share_dir = store
    sid = "sess-unreadable-lib-1"
    session = _make_session(sid, "Unreadable Test", "Private content", session_dir)
    created = create_or_refresh_share(session)
    token = created["share_token"]
    session = _seed_share_token(session_dir, sid, token)

    snapshot_path = share_dir / f"{token}.json"
    assert snapshot_path.is_file()
    original_bytes = snapshot_path.read_bytes()

    # Deny: unlink snapshot and replace with a directory
    snapshot_path.unlink()
    snapshot_path.mkdir()
    assert snapshot_path.exists()
    assert snapshot_path.is_dir()

    # While denied, revoke_share must fail closed
    assert revoke_share(session) is False

    # Restoration: remove the directory and restore the original snapshot bytes
    snapshot_path.rmdir()
    snapshot_path.write_bytes(original_bytes)

    # After restoration, snapshot is byte-identical (no tombstone written)
    assert snapshot_path.read_bytes() == original_bytes

    # load_share serves it again
    assert load_share(token) is not None


def test_library_unreadable_snapshot_revoke_retry_after_restoration_succeeds(store):
    """Case 2: library-level: the retry after restoration returns True, the snapshot

    carries revoked_at, and load_share answers None.
    """
    session_dir, share_dir = store
    sid = "sess-unreadable-lib-2"
    session = _make_session(sid, "Retry Test", "Private content", session_dir)
    created = create_or_refresh_share(session)
    token = created["share_token"]
    session = _seed_share_token(session_dir, sid, token)

    snapshot_path = share_dir / f"{token}.json"
    assert snapshot_path.is_file()
    original_bytes = snapshot_path.read_bytes()

    # Deny
    snapshot_path.unlink()
    snapshot_path.mkdir()

    # While denied, revoke must fail closed
    assert revoke_share(session) is False

    # Restoration
    snapshot_path.rmdir()
    snapshot_path.write_bytes(original_bytes)

    # The retry after restoration returns True
    assert revoke_share(session) is True

    # Snapshot carries revoked_at
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload.get("revoked_at") is not None
    assert isinstance(payload["revoked_at"], (int, float))

    # load_share answers None
    assert load_share(token) is None


def test_library_missing_snapshot_revoke_stays_idempotent(store):
    """Case 3: library-level control: a token with no snapshot file returns True (idempotent, unchanged)."""
    session_dir, share_dir = store
    sid = "sess-missing-lib-3"
    session = _make_session(sid, "Missing Control", "Private content", session_dir)
    token = "absent_token_12345"
    session = _seed_share_token(session_dir, sid, token)

    snapshot_path = share_dir / f"{token}.json"
    assert not snapshot_path.exists()

    assert revoke_share(session) is True


def test_http_unreadable_snapshot_revoke_returns_500_and_preserves_durable_token():
    """Case 4: HTTP-level: POST /api/share/revoke while denied answers 500 and the sidecar

    still carries the token (Session.load(sid).share_token == token), i.e. the durable
    pair was not cleared.
    """
    sid = _make_session_with_messages()
    snapshot_path = None
    original_bytes = None
    try:
        session = Session.load(sid)
        assert session is not None
        share_meta = create_or_refresh_share(session)
        token = share_meta["share_token"]
        session.share_token = token
        session.share_created_at = share_meta["share_created_at"]
        session.save(touch_updated_at=False)

        snapshot_path = SHARES_DIR / f"{token}.json"
        assert snapshot_path.is_file()
        original_bytes = snapshot_path.read_bytes()

        # Deny
        snapshot_path.unlink()
        snapshot_path.mkdir()

        res, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 500
        assert res.get("error") == "Share link could not be revoked"

        # Sidecar still carries the token: durable pair was not cleared
        reloaded = Session.load(sid)
        assert reloaded is not None
        assert reloaded.share_token == token
    finally:
        if snapshot_path is not None and snapshot_path.is_dir():
            snapshot_path.rmdir()
        if snapshot_path is not None and original_bytes is not None and not snapshot_path.exists():
            snapshot_path.write_bytes(original_bytes)
        post("/api/session/delete", {"session_id": sid})


def test_http_unreadable_snapshot_revoke_retry_after_restoration_clears_token():
    """Case 5: HTTP-level: after restoration the same POST answers 200, the sidecar token

    is None and the public link no longer resolves.
    """
    sid = _make_session_with_messages()
    snapshot_path = None
    original_bytes = None
    try:
        session = Session.load(sid)
        assert session is not None
        share_meta = create_or_refresh_share(session)
        token = share_meta["share_token"]
        session.share_token = token
        session.share_created_at = share_meta["share_created_at"]
        session.save(touch_updated_at=False)

        snapshot_path = SHARES_DIR / f"{token}.json"
        assert snapshot_path.is_file()
        original_bytes = snapshot_path.read_bytes()

        # Deny
        snapshot_path.unlink()
        snapshot_path.mkdir()

        res, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 500
        assert Session.load(sid).share_token == token

        # Restore
        snapshot_path.rmdir()
        snapshot_path.write_bytes(original_bytes)

        # Retry after restoration answers 200
        res2, status2 = post("/api/share/revoke", {"session_id": sid})
        assert status2 == 200
        assert res2.get("ok") is True

        # Sidecar token is cleared
        reloaded = Session.load(sid)
        assert reloaded is not None
        assert reloaded.share_token is None

        # Public link no longer resolves
        assert load_share(token) is None
        missing, get_status, _ = get(f"/api/share/{token}")
        assert get_status == 404
        assert missing.get("error") == "Shared conversation not found"
    finally:
        if snapshot_path is not None and snapshot_path.is_dir():
            snapshot_path.rmdir()
        post("/api/session/delete", {"session_id": sid})


def test_http_corrupt_json_snapshot_revoke_answers_200():
    """Case 6: HTTP-level control: a snapshot whose bytes are corrupt JSON still answers 200

    (unservable content, acknowledged) -- the pre-existing semantics must stay green.
    """
    sid = _make_session_with_messages()
    snapshot_path = None
    try:
        session = Session.load(sid)
        assert session is not None
        share_meta = create_or_refresh_share(session)
        token = share_meta["share_token"]
        session.share_token = token
        session.share_created_at = share_meta["share_created_at"]
        session.save(touch_updated_at=False)

        snapshot_path = SHARES_DIR / f"{token}.json"
        assert snapshot_path.is_file()
        snapshot_path.write_text("{corrupt json", encoding="utf-8")

        res, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert res.get("ok") is True

        reloaded = Session.load(sid)
        assert reloaded is not None
        assert reloaded.share_token is None
    finally:
        post("/api/session/delete", {"session_id": sid})
