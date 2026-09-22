"""Contract tests for the native-client Hermes front door."""

from __future__ import annotations

import io
import json
from urllib.parse import urlparse

import api.frontdoor as frontdoor


class _FakeHandler:
    def __init__(self, headers=None, body=b"{}"):
        self.headers = headers or {}
        self.client_address = ("127.0.0.1", 12345)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass


def _json_body(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _handler(headers=None, body=None):
    raw = json.dumps(body or {}).encode("utf-8")
    return _FakeHandler(headers=headers, body=raw)


def test_enrollment_binds_credential_to_origin_and_authenticates(monkeypatch, tmp_path):
    monkeypatch.setattr(frontdoor, "_STORE_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(frontdoor, "_IDEMPOTENCY_PATH", tmp_path / "idempotency.json")
    monkeypatch.setenv("HERMES_FRONTDOOR_PAIRING_CODE", "482-916")
    monkeypatch.setattr(frontdoor, "_request_origin", lambda handler: "https://hermes.example")

    enrollment = _handler(body={
        "code": "482916",
        "device_id": "mac-a",
        "display_name": "Test Mac",
        "kind": "mac",
    })
    frontdoor.handle_post(enrollment, urlparse("/v1/devices/enroll"), {
        "code": "482916",
        "device_id": "mac-a",
        "display_name": "Test Mac",
        "kind": "mac",
    })
    assert enrollment.status == 200
    reply = _json_body(enrollment)
    assert reply["device_id"] == "mac-a"
    assert reply["server_origin"] == "https://hermes.example"
    assert reply["token"]
    assert reply["token"] not in (tmp_path / "devices.json").read_text()

    authenticated = _FakeHandler({
        "Authorization": f"Bearer {reply['token']}",
    })
    device, response = frontdoor._authenticate(authenticated)
    assert response is None
    assert device["device_id"] == "mac-a"

    wrong_origin = _FakeHandler({
        "Authorization": f"Bearer {reply['token']}",
    })
    monkeypatch.setattr(frontdoor, "_request_origin", lambda handler: "https://other.example")
    device, response = frontdoor._authenticate(wrong_origin)
    assert device is None
    assert wrong_origin.status == 401


def test_roster_and_bot_chat_are_dynamic(monkeypatch):
    rows = [
        {"name": "clarence", "visible": True},
        {"name": "franklin", "visible": False},
    ]
    monkeypatch.setattr(frontdoor, "_profile_rows", lambda: rows)
    monkeypatch.setattr(frontdoor, "_request_origin", lambda handler: "https://hermes.example")
    monkeypatch.setattr(frontdoor, "_authenticate", lambda handler: ({"device_id": "mac-a"}, None))
    monkeypatch.setattr(frontdoor, "_ensure_chat_session", lambda profile: None)

    handler = _FakeHandler({"Authorization": "Bearer fixture"})
    frontdoor.handle_get(handler, urlparse("/v1/profiles"))
    assert handler.status == 200
    roster = _json_body(handler)
    assert [item["id"] for item in roster["profiles"]] == ["clarence", "franklin"]
    assert roster["profiles"][1]["available"] is False

    chat_handler = _FakeHandler({"Authorization": "Bearer fixture"})
    frontdoor.handle_get(chat_handler, urlparse("/v1/profiles/clarence/bot-chat"))
    assert _json_body(chat_handler)["chat_id"] == "sgbot.bot-chat.clarence"


def test_run_creation_is_idempotent_and_uses_canonical_session(monkeypatch, tmp_path):
    monkeypatch.setattr(frontdoor, "_STORE_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(frontdoor, "_IDEMPOTENCY_PATH", tmp_path / "idempotency.json")
    monkeypatch.setattr(frontdoor, "_profile_rows", lambda: [{"name": "clarence", "visible": True}])
    monkeypatch.setattr(frontdoor, "_ensure_chat_session", lambda profile: None)
    monkeypatch.setattr(frontdoor, "_profile_context", lambda profile: __import__("contextlib").nullcontext())
    calls = []

    def fake_start(session_id, content, source):
        calls.append((session_id, content, source))
        return {"_status": 200, "stream_id": "run-clarence-1"}

    monkeypatch.setattr("api.routes.start_session_turn", fake_start)
    monkeypatch.setattr(frontdoor, "_authenticate", lambda handler: ({"device_id": "mac-a"}, None))

    first = _handler({"Authorization": "Bearer fixture", "Idempotency-Key": "turn-1"}, {
        "session_id": "sgbot.bot-chat.clarence",
        "content": "Hello",
    })
    frontdoor.handle_post(first, urlparse("/v1/bot-chats/sgbot.bot-chat.clarence/runs"), {
        "session_id": "sgbot.bot-chat.clarence",
        "content": "Hello",
    })
    assert first.status == 202
    assert _json_body(first)["replayed"] is False

    second = _handler({"Authorization": "Bearer fixture", "Idempotency-Key": "turn-1"}, {
        "session_id": "sgbot.bot-chat.clarence",
        "content": "Hello",
    })
    frontdoor.handle_post(second, urlparse("/v1/bot-chats/sgbot.bot-chat.clarence/runs"), {
        "session_id": "sgbot.bot-chat.clarence",
        "content": "Hello",
    })
    assert second.status == 200
    assert _json_body(second)["replayed"] is True
    assert calls == [("sgbot_bot_chat_clarence", "Hello", "frontdoor")]


def test_revocation_invalidates_only_one_device(monkeypatch, tmp_path):
    monkeypatch.setattr(frontdoor, "_STORE_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(frontdoor, "_request_origin", lambda handler: "https://hermes.example")
    monkeypatch.setenv("HERMES_FRONTDOOR_PAIRING_CODES", "482916,739204")
    enrolled = []
    for device_id, code in (("mac-a", "482916"), ("phone-b", "739204")):
        handler = _handler(body={"code": code, "device_id": device_id})
        frontdoor.handle_post(handler, urlparse("/v1/devices/enroll"), {"code": code, "device_id": device_id})
        enrolled.append(_json_body(handler)["token"])

    first = _FakeHandler({"Authorization": f"Bearer {enrolled[0]}"})
    frontdoor.handle_delete(first, urlparse("/v1/devices/self"))
    assert first.status == 200
    stale = _FakeHandler({"Authorization": f"Bearer {enrolled[0]}"})
    device, response = frontdoor._authenticate(stale)
    assert device is None
    assert stale.status == 401
    still_valid = _FakeHandler({"Authorization": f"Bearer {enrolled[1]}"})
    device, response = frontdoor._authenticate(still_valid)
    assert response is None
    assert device["device_id"] == "phone-b"
