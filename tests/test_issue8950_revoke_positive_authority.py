"""#8950 round 11: revocation authority has to be positive ownership evidence.

The maintainer's 2026-11-17 review on PR #8950 credits the two lifecycle repairs
(an undecidable refresh defers and "Stop sharing" closes the inherited link) but
reports that the revoke path now treats *any* inconclusive ancestry as
authorization. A session that never proved any relation to the snapshot owner can
therefore take down another owner's public link, which the pre-round
implementation refused.

Contract under test:

* the two lifecycle successes keep working -- an unsaved compression child and a
  chain longer than the ancestry budget can still close the inherited link,
  because the owner's own durable succession record authorizes them;
* a stranger -- an unsaved session, an unrelated saved session, or a session
  whose owner no longer has a readable sidecar -- is refused with 409 and the
  owner's snapshot stays public;
* the succession walk is bounded: at the bound a proven chain authorizes, one hop
  past it refuses instead of authorizing on an inconclusive answer.

Everything here drives the real ``routes.handle_post`` handlers, the real
compression migration, real sidecars on an isolated store and the real public
loader.
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

    def json_body(self):
        try:
            return json.loads(bytes(self.body).decode("utf-8"))
        except Exception:
            return {}


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


def _make_session(sid, title="share owner"):
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


def _write_sidecar(sid, payload):
    (models.SESSION_DIR / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


def _snapshot(token):
    return json.loads((shares.SHARES_DIR / f"{token}.json").read_text(encoding="utf-8"))


def _tokens():
    return sorted(p.stem for p in shares.SHARES_DIR.glob("*.json"))


def _publish(sid):
    status, body = _post("/api/share/create", {"session_id": sid})
    assert status == 200, body
    return body["share"]["token"]


def _inherit(token, sid):
    """Give *sid* the token the way a compression continuation inherits it."""
    payload = _sidecar(sid)
    payload["share_token"] = token
    payload["share_created_at"] = 1000.0
    _write_sidecar(sid, payload)


def _build_chain(root_sid: str, hops: int) -> str:
    """Create root -> c001 -> ... -> c{hops} with reciprocal compression lineage."""
    _make_session(root_sid)
    prev = root_sid
    for idx in range(1, hops + 1):
        sid = f"{root_sid}_c{idx:03d}"
        _write_sidecar(
            sid,
            {
                "session_id": sid,
                "title": f"chain {idx}",
                "messages": [{"role": "user", "content": f"hop {idx}"}],
                "parent_session_id": prev,
                "pre_compression_snapshot": False,
                "pre_compression_continuation_session_id": "",
            },
        )
        parent = _sidecar(prev)
        parent["pre_compression_snapshot"] = True
        parent["pre_compression_continuation_session_id"] = sid
        _write_sidecar(prev, parent)
        prev = sid
    return prev


# ── lifecycle successes the maintainer credited ──────────────────────────────


def test_unsaved_child_succession_still_closes_the_inherited_link(isolated_state):
    """A child that was never saved is authorized by the owner's own record."""
    sid_a = "s_8950r11_parent_a"
    sid_b = "s_8950r11_child_b"
    _make_session(sid_a)
    token = _publish(sid_a)

    resident = models.SESSIONS.get(sid_a)
    assert resident is not None
    assert streaming._migrate_compression_session_ownership(
        resident, sid_a, sid_b, threading.Lock()
    ) is True
    assert not (models.SESSION_DIR / f"{sid_b}.json").exists()
    assert shares._compression_continuation_state(sid_b, sid_a) == "unknown"

    status, body = _post("/api/share/create", {"session_id": sid_b})
    assert status != 200, f"refresh published a replacement under an unknown ancestry: {body}"
    assert _tokens() == [token], _tokens()
    assert _snapshot(token)["revoked_at"] is None
    assert shares.load_share(token) is not None

    status, body = _post("/api/share/revoke", {"session_id": sid_b})

    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None, "the inherited link stayed public"
    assert shares.load_share(token) is None
    assert models.SESSIONS.get(sid_b).share_token is None


def test_long_chain_succession_still_closes_the_inherited_link(isolated_state):
    """A chain past the ancestry budget is still owned per the owner's record."""
    root = "s_8950r11_chain_root"
    tip = _build_chain(root, shares._MAX_COMPRESSION_CHAIN_HOPS + 6)
    token = _publish(root)
    _inherit(token, tip)

    assert shares._compression_continuation_state(tip, root) == "unknown"

    status, body = _post("/api/share/revoke", {"session_id": tip})

    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None, "the inherited link stayed public"
    assert shares.load_share(token) is None
    assert _sidecar(tip)["share_token"] is None


# ── the protection the last round walked back ────────────────────────────────


def test_unsaved_session_cannot_revoke_another_owners_link(isolated_state):
    """REGRESSION: an undecidable ancestry is not permission to revoke."""
    owner = "s_8950r11_owner"
    stranger = "s_8950r11_unsaved_stranger"
    _make_session(owner)
    token = _publish(owner)

    intruder = models.Session(session_id=stranger, title="intruder")
    intruder.messages = [{"role": "user", "content": "not mine"}]
    intruder.share_token = token
    intruder.share_created_at = 1000.0
    models.SESSIONS[stranger] = intruder
    assert not (models.SESSION_DIR / f"{stranger}.json").exists()
    assert shares._compression_continuation_state(stranger, owner) == "unknown"

    status, body = _post("/api/share/revoke", {"session_id": stranger})

    assert status == 409, body
    assert owner in body["error"] and stranger in body["error"], body
    assert _snapshot(token)["revoked_at"] is None, "a stranger took the owner's link down"
    assert shares.load_share(token) is not None

    # The owner still revokes its own link afterwards.
    status, body = _post("/api/share/revoke", {"session_id": owner})
    assert status == 200, body
    assert shares.load_share(token) is None


def test_unrelated_saved_session_cannot_revoke_another_owners_link(isolated_state):
    """Control: a saved session with no lineage relation is refused."""
    owner = "s_8950r11_owner_b"
    stranger = "s_8950r11_saved_stranger"
    _make_session(owner)
    _make_session(stranger)
    token = _publish(owner)
    _inherit(token, stranger)

    status, body = _post("/api/share/revoke", {"session_id": stranger})

    assert status == 409, body
    assert _snapshot(token)["revoked_at"] is None
    assert shares.load_share(token) is not None


def test_stranger_cannot_revoke_when_the_owner_sidecar_is_unreadable(isolated_state):
    """No readable owner record means no authority -- refuse, keep the link up."""
    owner = "s_8950r11_unreadable_owner"
    stranger = "s_8950r11_claimant"
    _make_session(owner)
    token = _publish(owner)

    claimant = models.Session(session_id=stranger, title="claimant")
    claimant.messages = [{"role": "user", "content": "not mine"}]
    claimant.share_token = token
    claimant.share_created_at = 1000.0
    models.SESSIONS[stranger] = claimant
    assert not (models.SESSION_DIR / f"{stranger}.json").exists()

    # The owner's record exists (so the public link keeps serving) but its lineage
    # cannot be read, which is exactly the inconclusive shape that must not authorize.
    (models.SESSION_DIR / f"{owner}.json").write_text("{not json", encoding="utf-8")
    status, body = _post("/api/share/revoke", {"session_id": stranger})

    assert status == 409, body
    assert _snapshot(token)["revoked_at"] is None, "the snapshot was taken down without authority"
    assert shares.load_share(token) is not None, "the owner's public link stopped serving"


# ── the forward walk completes any real chain without a hop cap ──────────────


def test_succession_walk_completes_a_long_chain_and_keeps_strangers_refused(isolated_state):
    root = "s_8950r11_long_chain"
    tip = _build_chain(root, 257)
    root_other = "s_8950r11_other_chain"
    tip_other = _build_chain(root_other, 4)

    assert shares._share_succession_state(root, tip) == "yes"

    # An unrelated actor is never authorized, not even between two valid chains.
    assert shares._share_succession_state(root, tip_other) == "no"
