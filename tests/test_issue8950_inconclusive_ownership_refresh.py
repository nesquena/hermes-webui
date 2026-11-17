"""#8950 round 10: an inconclusive share ancestry is not foreign ownership.

The maintainer's 2026-11-16 comment on PR #8950 describes two regressions that
the three-way ownership logic introduced in `api/shares.py`:

1. a compression child that has not been saved for the first time yet;
2. an ancestry walk that runs out of its hop budget, which a long but legitimate
   chain of compressions can reach.

Both are answered "no ancestry" by `_compression_continuation_state`, which the
refresh path then reads as "owned by somebody else": it mints a replacement token
and leaves the original public, and "Stop sharing" closes only the replacement.

Contract under test:

* an undecidable ancestry keeps the original token authoritative and publishes
  nothing;
* "Stop sharing" still closes the token the session holds, including the inherited
  one;
* a chain at the hop budget still authorizes, and a proven foreign claim still
  mints its own token and is still refused on revoke.

Everything here drives the real `routes.handle_post` handlers, the real
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

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None

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


def _write_sidecar(sid, payload):
    (models.SESSION_DIR / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


def _snapshot(token):
    return json.loads((shares.SHARES_DIR / f"{token}.json").read_text(encoding="utf-8"))


def _tokens():
    return sorted(p.stem for p in (shares.SHARES_DIR).glob("*.json"))


def _build_chain(root_sid: str, hops: int) -> str:
    """Create root -> c001 -> ... -> c{hops} with reciprocal compression lineage.

    Returns the tip session id. ``hops`` is the number of ancestry edges between
    the tip and the root, which is exactly what the walk spends from its budget.
    """
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


def _publish_from_root_and_inherit(root_sid: str, tip_sid: str) -> str:
    """Publish the root's share, then hand the token to the tip as a continuation."""
    status, body = _post("/api/share/create", {"session_id": root_sid})
    assert status == 200, body
    token = body["share"]["token"]
    tip_payload = _sidecar(tip_sid)
    tip_payload["share_token"] = token
    tip_payload["share_created_at"] = 1000.0
    _write_sidecar(tip_sid, tip_payload)
    return token


def test_refresh_before_first_save_defers_and_stop_sharing_closes_the_token(isolated_state):
    """A child that was never saved is undecidable: defer, then close the link."""
    sid_a = "s_8950r10_parent_a"
    sid_b = "s_8950r10_child_b"
    _make_session(sid_a)

    status, body = _post("/api/share/create", {"session_id": sid_a})
    assert status == 200, body
    token = body["share"]["token"]

    resident = models.SESSIONS.get(sid_a)
    assert resident is not None
    assert streaming._migrate_compression_session_ownership(
        resident, sid_a, sid_b, threading.Lock()
    ) is True
    # The child owns the conversation now but has no sidecar yet: absence of its
    # own lineage is "not saved yet", never proof that the token is foreign.
    assert not (models.SESSION_DIR / f"{sid_b}.json").exists()
    assert shares._compression_continuation_state(sid_b, sid_a) == "unknown"
    assert shares._is_compression_continuation(sid_b, sid_a) is False

    status, body = _post("/api/share/create", {"session_id": sid_b})

    assert status != 200, f"refresh published a replacement under an unknown ancestry: {body}"
    assert not (body or {}).get("share"), body
    assert _tokens() == [token], _tokens()
    assert _snapshot(token)["source_session_id"] == sid_a
    assert _snapshot(token)["revoked_at"] is None
    assert resident.share_token == token
    assert shares.load_share(token) is not None

    status, body = _post("/api/share/revoke", {"session_id": sid_b})

    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None, "the inherited share stayed public"
    assert shares.load_share(token) is None
    assert models.SESSIONS.get(sid_b).share_token is None


def test_refresh_past_hop_budget_defers_and_stop_sharing_closes_the_token(isolated_state):
    """A chain longer than the ancestry budget is undecidable, not foreign."""
    root = "s_8950r10_chain_root"
    tip = _build_chain(root, shares._MAX_COMPRESSION_CHAIN_HOPS + 6)
    token = _publish_from_root_and_inherit(root, tip)

    assert shares._compression_continuation_state(tip, root) == "unknown"

    status, body = _post("/api/share/create", {"session_id": tip})

    assert status != 200, f"refresh published a replacement past the budget: {body}"
    assert not (body or {}).get("share"), body
    assert _tokens() == [token], _tokens()
    assert _snapshot(token)["source_session_id"] == root
    assert _snapshot(token)["revoked_at"] is None
    assert _sidecar(tip)["share_token"] == token
    assert shares.load_share(token) is not None

    status, body = _post("/api/share/revoke", {"session_id": tip})

    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None, "the inherited share stayed public"
    assert shares.load_share(token) is None
    assert _sidecar(tip)["share_token"] is None


def test_control_chain_at_the_budget_still_reuses_the_token(isolated_state):
    """Control: a chain that fits the budget is a verified continuation."""
    root = "s_8950r10_budget_root"
    tip = _build_chain(root, shares._MAX_COMPRESSION_CHAIN_HOPS)
    token = _publish_from_root_and_inherit(root, tip)

    assert shares._compression_continuation_state(tip, root) == "yes"

    status, body = _post("/api/share/create", {"session_id": tip})

    assert status == 200, body
    assert body["share"]["token"] == token, "a verified continuation must reuse its token"
    assert body["share"]["token_reassigned"] is False
    assert _tokens() == [token], _tokens()
    assert _snapshot(token)["source_session_id"] == tip
    assert shares.load_share(token) is not None

    status, body = _post("/api/share/revoke", {"session_id": tip})

    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None
    assert shares.load_share(token) is None


def test_control_proven_foreign_claim_still_mints_and_is_refused_on_revoke(isolated_state):
    """Control: proven foreign ownership keeps its link and the claim is refused."""
    sid_owner = "s_8950r10_owner"
    sid_claimer = "s_8950r10_claimer"
    _make_session(sid_owner)
    _make_session(sid_claimer)

    status, body = _post("/api/share/create", {"session_id": sid_owner})
    assert status == 200, body
    owner_token = body["share"]["token"]

    claimer_payload = _sidecar(sid_claimer)
    claimer_payload["share_token"] = owner_token
    claimer_payload["share_created_at"] = 1000.0
    _write_sidecar(sid_claimer, claimer_payload)

    assert shares._compression_continuation_state(sid_claimer, sid_owner) == "no"

    status, body = _post("/api/share/create", {"session_id": sid_claimer})

    assert status == 200, body
    replacement = body["share"]["token"]
    assert replacement != owner_token, "a proven foreign token must not be overwritten"
    assert body["share"]["token_reassigned"] is True
    assert sorted(_tokens()) == sorted([owner_token, replacement])
    assert _snapshot(owner_token)["source_session_id"] == sid_owner
    assert _snapshot(owner_token)["revoked_at"] is None

    status, body = _post("/api/share/revoke", {"session_id": sid_claimer})
    assert status == 200, body
    assert shares.load_share(owner_token) is not None, "the foreign link must survive"

    # A session that still names somebody else's token is refused on revoke.
    foreign_payload = _sidecar(sid_claimer)
    foreign_payload["share_token"] = owner_token
    foreign_payload["share_created_at"] = 1000.0
    _write_sidecar(sid_claimer, foreign_payload)

    status, body = _post("/api/share/revoke", {"session_id": sid_claimer})

    assert status == 409, body
    assert sid_owner in body["error"] and sid_claimer in body["error"], body
    assert _snapshot(owner_token)["revoked_at"] is None
    assert shares.load_share(owner_token) is not None
