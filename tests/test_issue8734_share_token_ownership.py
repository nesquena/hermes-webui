"""#8734: a share token must be proven to belong to the session acting on it.

Every snapshot records ``source_session_id``, but neither the create nor the
revoke path compared it with the session the request is about.  A sidecar that
names another session's live token (hand-edited, restored from a backup, or
copied by a future wholesale session clone) therefore used to:

* **create** rewrite that token's public snapshot with the acting session's
  conversation (the other session's public URL starts serving someone else's
  transcript), and
* **revoke** take the other session's link down while its owner still believes
  it is shared.

These tests drive the same module entry points the ``/api/share/*`` handlers
call (``api.shares``) against real sidecar + snapshot files in an isolated
state dir, so the assertions are about the bytes a visitor of the public URL
actually receives.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from api import models, shares
from tests._pytest_port import BASE


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolate the session store and the public snapshot dir for one test."""
    session_dir = tmp_path / "sessions"
    share_dir = tmp_path / "shares"
    session_dir.mkdir()
    share_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(shares, "SHARES_DIR", share_dir)
    return session_dir, share_dir


def _make_session(sid, title, text, workspace):
    session = models.Session(
        session_id=sid,
        title=title,
        workspace=str(workspace),
        messages=[{"role": "user", "content": text}],
    )
    session.save()
    return models.Session.load(sid)


def _seed_share_token(session_dir: Path, sid: str, token, created_at=1000.0):
    """Write the share pair into the sidecar exactly as a restore/hand-edit would.

    This is the corruption the issue names: the session file names a token that
    may belong to a different session.  Writing the file directly keeps the
    fixture honest about the defect's origin (the object never agreed to it).
    """
    path = session_dir / f"{sid}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["share_token"] = token
    payload["share_created_at"] = created_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    return models.Session.load(sid)


def _snapshot_bytes(share_dir: Path, token: str) -> bytes:
    return (share_dir / f"{token}.json").read_bytes()


def test_share_create_never_overwrites_another_sessions_snapshot(store):
    session_dir, share_dir = store
    owner = _make_session("sess-owner-a", "Session A", "A private words", session_dir)
    thief = _make_session("sess-thief-b", "Session B", "B private words", session_dir)

    created = shares.create_or_refresh_share(owner)
    token_a = created["share_token"]
    owner = _seed_share_token(session_dir, "sess-owner-a", token_a)
    before = _snapshot_bytes(share_dir, token_a)

    # B's sidecar names A's live token — the state the issue describes.
    thief = _seed_share_token(session_dir, "sess-thief-b", token_a)
    assert thief.share_token == token_a

    refreshed = shares.create_or_refresh_share(thief)

    after = _snapshot_bytes(share_dir, token_a)
    assert after == before, "create on B rewrote A's public snapshot"
    public_a = shares.load_share(token_a)
    assert public_a is not None, "A's link stopped resolving after B's create"
    assert public_a["title"] == "Session A"
    assert [m["content"] for m in public_a["messages"]] == ["A private words"]

    # B still gets a working share — of its own conversation, under a new URL.
    assert refreshed["share_token"] != token_a
    public_b = shares.load_share(refreshed["share_token"])
    assert public_b is not None
    assert public_b["title"] == "Session B"
    assert refreshed.get("token_reassigned") is True


def test_share_revoke_refuses_a_token_owned_by_another_session(store):
    session_dir, share_dir = store
    owner = _make_session("sess-owner-a", "Session A", "A private words", session_dir)
    thief = _make_session("sess-thief-b", "Session B", "B private words", session_dir)

    created = shares.create_or_refresh_share(owner)
    token_a = created["share_token"]
    owner = _seed_share_token(session_dir, "sess-owner-a", token_a)
    before = _snapshot_bytes(share_dir, token_a)
    thief = _seed_share_token(session_dir, "sess-thief-b", token_a)

    refused = None
    try:
        result = shares.revoke_share(thief)
    except Exception as exc:  # the named refusal is asserted below
        refused = exc

    assert _snapshot_bytes(share_dir, token_a) == before, (
        "revoke on B wrote to A's snapshot"
    )
    assert shares.load_share(token_a) is not None, "revoke on B took A's link down"

    ownership_error = getattr(shares, "ShareTokenOwnershipError", None)
    assert ownership_error is not None, (
        "api.shares exposes no named ownership error (revoke returned "
        f"{result!r} silently)"
    )
    assert isinstance(refused, ownership_error), (
        f"revoke on B returned {result!r} instead of refusing the foreign token"
    )
    assert "sess-thief-b" in str(refused)
    assert "sess-owner-a" in str(refused)


def test_owner_scoped_read_refuses_a_foreign_token(store):
    """The read side of the same invariant: a session cannot claim a token."""
    session_dir, share_dir = store
    owner = _make_session("sess-owner-a", "Session A", "A private words", session_dir)
    created = shares.create_or_refresh_share(owner)
    token_a = created["share_token"]

    ownership_error = getattr(shares, "ShareTokenOwnershipError", None)
    assert ownership_error is not None, "api.shares exposes no named ownership error"
    with pytest.raises(ownership_error):
        shares.load_share(token_a, expected_session_id="sess-thief-b")

    # The owner and the anonymous public reader both keep working.
    assert shares.load_share(token_a, expected_session_id="sess-owner-a") is not None
    assert shares.load_share(token_a) is not None


def test_legacy_snapshot_without_source_session_id_keeps_working(store):
    """A snapshot written before ``source_session_id`` existed is not foreign."""
    session_dir, share_dir = store
    session = _make_session("sess-legacy", "Legacy", "legacy words", session_dir)
    created = shares.create_or_refresh_share(session)
    token = created["share_token"]

    path = share_dir / f"{token}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("source_session_id", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    session = _seed_share_token(session_dir, "sess-legacy", token)
    again = shares.create_or_refresh_share(session)
    assert again["share_token"] == token
    assert shares.load_share(token) is not None
    assert shares.revoke_share(session) is True
    assert shares.load_share(token) is None


# ── HTTP surface: the refusal must be a named 409, never a 500 or a mute 200 ──
# These two run against the shared test server (autouse ``test_server``); the
# sessions are written straight to the store so the server's first read of them
# is the corrupted sidecar itself.


def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def test_revoke_endpoint_answers_named_409_and_keeps_the_other_link_up():
    session_dir, share_dir = models.SESSION_DIR, shares.SHARES_DIR
    owner = _make_session("http-8734-owner", "HTTP A", "HTTP A words", session_dir)
    token_a = shares.create_or_refresh_share(owner)["share_token"]
    _seed_share_token(session_dir, "http-8734-owner", token_a)
    _make_session("http-8734-thief", "HTTP B", "HTTP B words", session_dir)
    _seed_share_token(session_dir, "http-8734-thief", token_a)
    before = _snapshot_bytes(share_dir, token_a)

    payload, status = _post("/api/share/revoke", {"session_id": "http-8734-thief"})

    assert status == 409, payload
    assert "http-8734-owner" in payload["error"], payload
    assert "http-8734-thief" in payload["error"], payload
    assert _snapshot_bytes(share_dir, token_a) == before
    public, status = _get(f"/api/share/{token_a}")
    assert status == 200, public
    assert public["share"]["title"] == "HTTP A"


def test_create_endpoint_mints_a_new_token_and_leaves_the_other_link_alone():
    session_dir, share_dir = models.SESSION_DIR, shares.SHARES_DIR
    owner = _make_session("http-8734-owner2", "HTTP A2", "HTTP A2 words", session_dir)
    token_a = shares.create_or_refresh_share(owner)["share_token"]
    _seed_share_token(session_dir, "http-8734-owner2", token_a)
    _make_session("http-8734-thief2", "HTTP B2", "HTTP B2 words", session_dir)
    _seed_share_token(session_dir, "http-8734-thief2", token_a)
    before = _snapshot_bytes(share_dir, token_a)

    payload, status = _post("/api/share/create", {"session_id": "http-8734-thief2"})

    assert status == 200, payload
    assert payload["share"]["token"] != token_a
    assert payload["share"]["token_reassigned"] is True
    assert payload["share"]["replaced_token"] == token_a
    assert payload["session"]["share_token"] == payload["share"]["token"]
    assert _snapshot_bytes(share_dir, token_a) == before
    public, status = _get(f"/api/share/{token_a}")
    assert status == 200
    assert public["share"]["title"] == "HTTP A2"
    stolen, status = _get(f"/api/share/{payload['share']['token']}")
    assert status == 200
    assert stolen["share"]["title"] == "HTTP B2"

