"""#8950 round 12: the succession walk must complete a legitimate chain.

The maintainer's 2026-11-17T19:05 review on PR #8950 credits the positive
ownership repair (generic unknown ancestry no longer authorizes a revoke) and
reports the remaining boundary defect at the *other* end of the same seam:

* a real compression chain with **257** durable migrations is a legitimate
  inherited link, but ``_share_succession_state()`` stops at
  ``_MAX_SHARE_REVOCATION_CHAIN_HOPS`` (256) and answers ``'unknown'``;
* ``/api/share/revoke`` therefore answers **409** and does not write the
  tombstone, so the original snapshot is still served by the real public
  loader -- a truthful refusal with a link that stays public;
* simply raising the constant moves the same failure one hop further, and
  restoring generic-``unknown`` authorization would reopen the protection that
  the previous round repaired.

Contract under test:

* a production-migration chain longer than the previous bound revokes with
  **200** and the original token stops being served;
* a short production chain keeps working (control);
* unrelated owners are still refused with **409** and their snapshot stays
  public, however long the chain next door is (protection preserved);
* an inconclusive refresh on the tip of such a chain still refuses to publish a
  replacement (uncertain-refresh preservation).

Everything here drives the real ``routes.handle_post`` handlers, the real
compression migration helper, real sidecars on an isolated store and the real
public loader.
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


def _migrate_chain(root_sid, hops):
    """Publish the root, then perform *hops* real compression migrations."""
    _make_session(root_sid)
    token = _publish(root_sid)
    sid = root_sid
    # The agent lock migrates with the session, so the whole chain shares one.
    agent_lock = threading.Lock()
    for idx in range(1, hops + 1):
        new_sid = f"{root_sid}_m{idx:03d}"
        resident = models.SESSIONS.get(sid)
        assert resident is not None, f"missing resident for {sid}"
        migrated = streaming._migrate_compression_session_ownership(
            resident, sid, new_sid, agent_lock
        )
        assert migrated is True, f"migration {idx} failed"
        # A real compression continues the turn and saves the new session; the
        # next migration needs that durable sidecar as its preimage.
        models.SESSIONS[new_sid].save(touch_updated_at=False, skip_index=True)
        sid = new_sid
    tip = sid
    if _sidecar(tip)["share_token"] != token:
        payload = _sidecar(tip)
        payload["share_token"] = token
        payload["share_created_at"] = 1000.0
        _write_sidecar(tip, payload)
    return tip, token


LONG_HOPS = 257


def test_production_chain_past_the_previous_bound_closes_the_original_link(isolated_state):
    """A 257-hop production chain still owns the inherited link at its tip."""
    tip, token = _migrate_chain("s_8950r12_long_root", LONG_HOPS)

    assert shares.load_share(token) is not None

    status, body = _post("/api/share/revoke", {"session_id": tip})

    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None, "the inherited link stayed public"
    assert shares.load_share(token) is None, "the original token is still served"
    assert _sidecar(tip)["share_token"] is None


def test_control_short_production_chain_closes_the_inherited_link(isolated_state):
    """Control: an ordinary three-hop production chain closes its link."""
    tip, token = _migrate_chain("s_8950r12_short_root", 3)

    status, body = _post("/api/share/revoke", {"session_id": tip})

    assert status == 200, body
    assert _snapshot(token)["revoked_at"] is not None
    assert shares.load_share(token) is None


def test_unrelated_owner_is_still_refused_next_to_a_long_chain(isolated_state):
    """Protection: a stranger cannot use a long chain as cover for a revoke."""
    owner = "s_8950r12_owner"
    stranger = "s_8950r12_stranger"
    _make_session(owner)
    _make_session(stranger)
    owner_token = _publish(owner)

    tip, chain_token = _migrate_chain("s_8950r12_long_root_b", LONG_HOPS)
    assert chain_token != owner_token

    payload = _sidecar(stranger)
    payload["share_token"] = owner_token
    payload["share_created_at"] = 1000.0
    _write_sidecar(stranger, payload)

    status, body = _post("/api/share/revoke", {"session_id": stranger})

    assert status == 409, body
    assert owner in body["error"] and stranger in body["error"], body
    assert _snapshot(owner_token)["revoked_at"] is None, "a stranger took the link down"
    assert shares.load_share(owner_token) is not None

    # The chain's own tip still revokes after the refusal.
    status, body = _post("/api/share/revoke", {"session_id": tip})
    assert status == 200, body
    assert shares.load_share(chain_token) is None


def test_uncertain_refresh_still_publishes_nothing(isolated_state):
    """Preservation: an inconclusive ancestry still defers instead of minting."""
    tip, token = _migrate_chain("s_8950r12_long_root_c", 3)
    unsaved = f"{tip}_unsaved"
    intruder = models.Session(session_id=unsaved, title="unsaved tip")
    intruder.messages = [{"role": "user", "content": "not saved yet"}]
    intruder.share_token = token
    intruder.share_created_at = 1000.0
    models.SESSIONS[unsaved] = intruder
    assert not (models.SESSION_DIR / f"{unsaved}.json").exists()

    status, body = _post("/api/share/create", {"session_id": unsaved})

    assert status != 200, f"refresh published a replacement on an unknown ancestry: {body}"
    assert not (body or {}).get("share"), body
    assert _tokens() == [token], _tokens()
    assert shares.load_share(token) is not None
