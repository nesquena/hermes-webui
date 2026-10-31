"""Continuation projections and controls retain their profile and Gateway authority."""
from collections import OrderedDict
from contextlib import asynccontextmanager
from types import SimpleNamespace
import threading
from urllib.parse import urlparse
from unittest.mock import MagicMock

import pytest

from api import clarify, config, gateway_chat, gateway_session_bridge as bridge
from api import gateway_sessions as sessions, helpers, models, profiles, routes, streaming


@pytest.fixture
def isolated_profiles(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_SESSION_TRANSPORT", raising=False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in {"default", "renamed-root"})
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile",
                        lambda name: tmp_path if name in {None, "default", "renamed-root"} else tmp_path / "profiles" / name)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    root = {"webui_chat_backend": "gateway", "webui_gateway_session_transport": "unix"}
    monkeypatch.setattr(config, "get_config", lambda: root)
    for name in ("work", "family", "lab"):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "webui_chat_backend: gateway\nwebui_gateway_session_transport: unix\n", encoding="utf-8")
    return tmp_path, root


@pytest.mark.parametrize("profile", ["default", "work", "family", "lab"])
def test_config_only_eligibility_uses_session_profile(isolated_profiles, profile):
    _, root = isolated_profiles
    if profile != "default":
        root.clear()  # A worker's ambient root config must not decide for another profile.
    source = {"profile": profile, "source_tag": "telegram", "session_source": "messaging", "read_only": True}
    projected = helpers.public_session_projection(source)
    assert projected.get("chat_via_gateway_session") is True
    assert not projected.get("chat_read_only_reason")
    assert "chat_via_gateway_session" not in source


@pytest.mark.parametrize("backend,transport,blocked", [("legacy", "unix", False), ("gateway", "", True)])
def test_foreign_profile_does_not_inherit_root_eligibility(isolated_profiles, backend, transport, blocked):
    home, _ = isolated_profiles
    (home / "profiles" / "work" / "config.yaml").write_text(
        f"webui_chat_backend: {backend}\nwebui_gateway_session_transport: '{transport}'\n", encoding="utf-8")
    projected = helpers.public_session_projection({"profile": "work", "source_tag": "telegram", "read_only": True})
    assert not projected.get("chat_via_gateway_session")
    assert bool(projected.get("chat_read_only_reason")) is blocked


@pytest.mark.parametrize("profile,wire_profile", [("default", "default"), ("renamed-root", "default"), ("work", "work")])
def test_unix_handshake_and_recovery_bind_canonical_profile(isolated_profiles, monkeypatch, profile, wire_profile):
    home, _ = isolated_profiles
    socket = str(home / "bridge.sock")
    monkeypatch.setattr(sessions, "config_for_session", lambda _: {
        "webui_gateway_session_transport": "unix", "webui_gateway_session_socket": socket})
    opened, records = [], []

    @asynccontextmanager
    async def connect(path, name):
        opened.append((path, name, bridge.profile_home(home, name, {"default", "work"})))
        yield object()

    async def run(relay):
        async with relay.connector():
            relay.record(relay.run_id, relay.request)
        return "reply", {}

    monkeypatch.setattr(bridge, "connect_bridge", connect)
    monkeypatch.setattr(sessions.SessionRelay, "run", run)
    session = SimpleNamespace(profile=profile, session_id="same-session")
    args = dict(emit=lambda *a: None, cancel=threading.Event(), record=lambda *a, **kw: records.append(kw))
    sessions.run_session(session, "stable", "text", **args)
    assert opened[0][1] == wire_profile
    assert records[0]["session_connection"]["profile"] == wire_profile
    # The display name can change; the same root home and wire binding recover.
    if wire_profile == "default":
        session.profile = "default" if profile == "renamed-root" else "renamed-root"
    sessions.run_session(session, "stable", "text", saved=records[0], **args)
    assert records[0]["session_request"] == records[1]["session_request"]
    assert opened[0] == opened[1]


@pytest.mark.parametrize("settlement", ["before_ack", "after_ack", "refused", "expired"])
def test_clarification_http_result_follows_gateway_ack(monkeypatch, settlement):
    sid, cid = "review-clarify", "gw-session:stable::question"
    owner = SimpleNamespace(session_id=sid, run_id="session:stable", clarifications={cid: ("clarify.respond", {})})
    monkeypatch.setattr(sessions, "_CONNECTIONS", {owner.run_id: owner})
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy")
    calls = []

    def control(*args, **kwargs):
        calls.append((args, kwargs))
        if settlement == "refused":
            raise sessions.SessionTransportError("not acknowledged")
        if settlement == "before_ack":
            assert clarify.resolve_clarify_by_id(sid, cid, "yes")
            owner.clarifications.clear()
        return {"status": "expired" if settlement == "expired" else "ok"}

    monkeypatch.setattr(sessions, "control", control)
    replies = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: replies.append((status, payload)))
    body = {"session_id": sid, "clarify_id": cid, "answer": "yes"}
    monkeypatch.setattr(routes, "_check_csrf", lambda *a: True)
    monkeypatch.setattr(routes, "read_body", lambda *a: body)
    try:
        pending = clarify.submit_pending(sid, {"clarify_id": cid, "question": "Continue?"})
        routes.handle_post(MagicMock(), urlparse("/api/clarify/respond"))
        accepted = settlement not in {"refused", "expired"}
        assert replies[0][0] == (200 if accepted else 409)
        assert replies[0][1]["ok"] is accepted
        assert pending.event.is_set() is accepted
        assert len(calls) == 1
    finally:
        clarify.clear_pending(sid)


@pytest.mark.parametrize("terminal", ["error", "stop"])
@pytest.mark.parametrize("transport", ["unix", ""])
def test_terminal_payload_keeps_native_eligibility_and_redaction(isolated_profiles, monkeypatch, terminal, transport):
    home, root = isolated_profiles
    root["webui_gateway_session_transport"] = transport
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    state = home / "sessions"
    state.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", state)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": True})
    session = models.new_session(profile="default", workspace=str(home))
    session.source_tag, session.session_source, session.read_only = "telegram", "messaging", True
    session.messages = [{"role": "user", "content": "Authorization: Bearer fixture-private-token"}]
    session.active_stream_id = "terminal-stream"
    if terminal == "error":
        monkeypatch.setattr(gateway_chat, "get_session", lambda _: session)
        payload = gateway_chat._settle_gateway_terminal_error(session.session_id, "terminal-stream", str(home),
            "model", "provider", {"type": "gateway_http_error", "message": "refused"})["session"]
    else:
        payload = streaming._redacted_session_payload_with_full_messages(session)
    assert payload is not None
    assert payload.get("chat_via_gateway_session", False) is bool(transport)
    assert bool(payload.get("chat_read_only_reason")) is (not transport)
    assert payload["read_only"] is True
    assert "fixture-private-token" not in repr(payload)
    assert "fixture-private-token" in session.messages[0]["content"]
