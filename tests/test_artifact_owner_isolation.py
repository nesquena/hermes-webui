"""Auth-session ownership regression coverage for published artifacts."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import api.artifacts as artifacts
import api.auth as auth
import api.routes as routes


class _Handler:
    def __init__(self, cookie: str):
        self.headers = {"Cookie": f"{auth.COOKIE_NAME}={cookie}"}
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass


def _capture_routes(monkeypatch):
    captured = []

    def _j(_handler, payload, *_, **kwargs):
        captured.append((payload, kwargs.get("status", 200)))
        return True

    def _bad(_handler, message, status=400, **_kwargs):
        captured.append(({"error": message}, status))
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    return captured


def _request_owner(handler) -> str:
    owner = routes._artifact_owner_for_request(handler)
    assert owner
    return owner


def _publish(monkeypatch, handler, body):
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    assert routes.handle_post(handler, urlparse("/api/artifact/publish")) is True


def test_auth_session_owner_scopes_list_revoke_republish_and_private_get(tmp_path, monkeypatch):
    """A signed server session, never client session_id, owns private artifacts."""
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "artifacts_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    auth._sessions.clear()
    owner_a_cookie = auth.create_session(bound_profile="alpha")
    owner_b_cookie = auth.create_session(bound_profile="bravo")
    owner_a = _Handler(owner_a_cookie)
    owner_b = _Handler(owner_b_cookie)
    assert _request_owner(owner_a) != _request_owner(owner_b)

    source = tmp_path / "report.txt"
    source.write_text("private A", encoding="utf-8")
    captured = _capture_routes(monkeypatch)

    # Owner A can publish while a forged client session_id remains mere metadata.
    _publish(monkeypatch, owner_a, {"path": str(source), "session_id": "forged-client-session"})
    artifact, status = captured.pop()
    assert status == 200
    token = artifact["artifact"]["token"]
    meta = json.loads((artifacts.ARTIFACTS_DIR / token / "meta.json").read_text())
    assert meta["session_id"] == "forged-client-session"
    assert meta["owner"] == _request_owner(owner_a)

    # B cannot enumerate A's token, revoke it, explicit-token re-publish it,
    # or trigger same-source automatic version bump.
    assert routes.handle_get(owner_b, urlparse("/api/artifact/list")) is True
    listed, status = captured.pop()
    assert status == 200 and listed["artifacts"] == []

    monkeypatch.setattr(routes, "read_body", lambda _handler: {"token": token})
    assert routes.handle_post(owner_b, urlparse("/api/artifact/revoke")) is True
    denied, status = captured.pop()
    assert status == 404 and denied["error"] == "unknown artifact token"

    source.write_text("B cannot version A", encoding="utf-8")
    _publish(monkeypatch, owner_b, {"path": str(source), "token": token})
    denied, status = captured.pop()
    assert status == 404 and denied["error"] == "unknown artifact token"

    _publish(monkeypatch, owner_b, {"path": str(source)})
    own_b, status = captured.pop()
    assert status == 200 and own_b["artifact"]["token"] != token

    served = {}
    monkeypatch.setattr(routes, "_serve_file_bytes", lambda *_args, **_kwargs: served.setdefault("served", True))
    assert routes._handle_artifact_get(owner_b, urlparse(f"/artifact/{token}")) is True
    denied, status = captured.pop()
    assert status == 404 and "served" not in served

    # A retains all operations over its own artifact, including private serving.
    assert routes.handle_get(owner_a, urlparse("/api/artifact/list")) is True
    listed, status = captured.pop()
    assert status == 200 and [item["token"] for item in listed["artifacts"]] == [token]

    _publish(monkeypatch, owner_a, {"path": str(source), "token": token})
    republished, status = captured.pop()
    assert status == 200 and republished["artifact"]["version"] == 2

    assert routes._handle_artifact_get(owner_a, urlparse(f"/artifact/{token}")) is True
    assert served["served"] is True

    monkeypatch.setattr(routes, "read_body", lambda _handler: {"token": token})
    assert routes.handle_post(owner_a, urlparse("/api/artifact/revoke")) is True
    revoked, status = captured.pop()
    assert status == 200 and revoked == {"ok": True}


def test_public_pinned_artifact_stays_anonymous_with_auth_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "artifacts_enabled", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    source = tmp_path / "public.txt"
    source.write_text("safe", encoding="utf-8")
    token = artifacts.publish_artifact(str(source), public=True, owner="server-owner")["token"]
    served = {}
    monkeypatch.setattr(routes, "_serve_file_bytes", lambda *_args, **_kwargs: served.setdefault("ok", True))
    anonymous = SimpleNamespace(headers={})
    assert routes._handle_artifact_get(anonymous, urlparse(f"/artifact/{token}?v=1")) is True
    assert served["ok"] is True
