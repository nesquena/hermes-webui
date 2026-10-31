"""Maintainer re-review: native profile ownership and canonical clarification ACKs."""
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse
import threading

import pytest

from api import clarify, config, gateway_sessions as sessions, models, profiles, routes


@pytest.fixture
def native_home(tmp_path, monkeypatch):
    for name in ("default", "work", "josue", "tomo", "tristan"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text("webui_chat_backend: gateway\nwebui_gateway_session_transport: unix\n")
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: tmp_path / (name or "default"))
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in {None, "default"})
    monkeypatch.setattr(config, "get_config", lambda: {"webui_chat_backend": "gateway"})
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_SESSION_TRANSPORT", raising=False)
    state = tmp_path / "webui"
    state.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", state)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state / "_index.json")
    cache = OrderedDict()
    monkeypatch.setattr(models, "SESSIONS", cache)
    monkeypatch.setattr(routes, "SESSIONS", cache)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    return tmp_path


@pytest.mark.parametrize("owner,active", [("default", "work"), ("josue", "tomo"), ("tomo", "tristan"), ("tristan", "default")])
@pytest.mark.parametrize("source", ["telegram", "discord", "desktop"])
@pytest.mark.parametrize("message", ["", "hello"])
def test_foreign_native_chat_preserves_owner(native_home, monkeypatch, owner, active, source, message):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    db = SessionDB(native_home / owner / "state.db")
    sid = "foreign-current"
    db.create_session(sid, source, session_key=f"{source}:fixture", profile_name=owner)
    db.append_message(sid, "user", "Existing Agent history")
    session = models.Session(session_id=sid, profile=owner, workspace=str(native_home),
                             source_tag=source, session_source="other" if source == "desktop" else "messaging",
                             read_only=True)
    session.save()
    models.SESSIONS[sid] = session
    original = session.path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active)
    monkeypatch.setattr(routes, "_check_csrf", lambda *a: True)
    monkeypatch.setattr(routes, "read_body", lambda *a: {"session_id": sid, "profile": active, "message": message})
    monkeypatch.setattr(routes, "_start_run", MagicMock(side_effect=AssertionError("must not admit foreign session")))
    replies = []
    monkeypatch.setattr(routes, "j", lambda h, body, status=200, **kw: replies.append((status, body)) or True)
    monkeypatch.setattr(routes, "bad", lambda h, text, status=400: replies.append((status, text)) or True)
    try:
        routes.handle_post(MagicMock(), urlparse("/api/chat/start"))
        assert replies[-1][0] == 403
        assert session.profile == owner
        assert models.get_session(sid).profile == owner
        assert session.path.read_bytes() == original
        assert db.get_session(sid)["profile_name"] == owner
        assert len(db.get_messages(sid)) == 1
        monkeypatch.setattr(routes, "_get_active_profile_name", lambda: owner)
        assert routes._session_visible_to_active_profile(session.profile, MagicMock())
    finally:
        db.close()


@pytest.mark.parametrize("settlement", ["timeout", "other_viewer", "accepted", "accepted_mirror_race"])
def test_real_single_question_ack_through_post(monkeypatch, settlement):
    PendingControls = pytest.importorskip("gateway.session_pending_controls").PendingControls
    producer = pytest.importorskip("tools.clarify_gateway")
    sid, prompt, cid = "single-ack", "real-prompt", "gw-session:single::real-prompt"
    entry = producer.register(prompt, sid, "Continue?", None)
    owner = SimpleNamespace(session_id=sid, run_id="session:single", clarifications={cid: ("clarify.respond", {})})
    def publish(*a, **kw):
        if settlement == "accepted_mirror_race":
            clarify.resolve_clarify_by_id(sid, cid, "yes")
            owner.clarifications.clear()
    pending_controls = PendingControls(SimpleNamespace(lock=threading.RLock(), publish=publish))
    pending_controls.pending[prompt] = (entry, {"kind": "clarify", "prompt_id": prompt, "execution_generation": "generation"})
    monkeypatch.setattr(sessions, "_CONNECTIONS", {owner.run_id: owner})
    results = []
    def control(*a, **kw):
        result = pending_controls.respond(sid, "generation", prompt, {"answer": kw["answer"]}, kind="clarify")
        results.append(result)
        return result
    monkeypatch.setattr(sessions, "control", control)
    monkeypatch.setattr(routes, "_check_csrf", lambda *a: True)
    monkeypatch.setattr(routes, "read_body", lambda *a: {"session_id": sid, "clarify_id": cid, "answer": "yes"})
    replies = []
    monkeypatch.setattr(routes, "j", lambda h, body, status=200: replies.append((status, body)))
    try:
        mirror = clarify.submit_pending(sid, {"clarify_id": cid, "question": "Continue?"})
        if settlement == "timeout":
            producer.wait_for_response(prompt, 0.001)
        elif settlement == "other_viewer":
            assert producer.resolve_gateway_clarify(prompt, "someone else")
        routes.handle_post(MagicMock(), urlparse("/api/clarify/respond"))
        accepted = settlement.startswith("accepted")
        assert results[0]["status"] == ("resolved" if accepted else "already_resolved")
        assert len(replies) == 1
        assert replies[0][0] == (200 if accepted else 409)
        assert replies[0][1]["ok"] is accepted
        assert mirror.event.is_set() is accepted
        assert entry.response == ("yes" if accepted else "someone else" if settlement == "other_viewer" else None)
    finally:
        producer.wait_for_response(prompt, 0.001)
        clarify.clear_pending(sid)


@pytest.mark.parametrize("result", [{"status": "already_resolved"}, {"status": "unknown"}, {}, None, True])
def test_unknown_clarification_ack_fails_closed(monkeypatch, result):
    sid, cid = "unknown-ack", "gw-session:unknown::prompt"
    owner = SimpleNamespace(session_id=sid, run_id="session:unknown", clarifications={cid: ("clarify.respond", {})})
    monkeypatch.setattr(sessions, "_CONNECTIONS", {owner.run_id: owner})
    monkeypatch.setattr(sessions, "control", lambda *a, **kw: result)
    try:
        mirror = clarify.submit_pending(sid, {"clarify_id": cid, "question": "Continue?"})
        assert sessions.respond_clarify(sid, cid, "yes") is False
        assert not mirror.event.is_set()
    finally:
        clarify.clear_pending(sid)


@pytest.mark.parametrize("source,read_only,is_cli,expected", [
    (None, False, False, "retagged"), ("webui", False, False, "retagged"),
    ("desktop", False, False, "external"), ("telegram", False, False, "external"),
    (None, True, False, "external"), (None, False, True, "external"),
])
def test_only_webui_placeholders_can_be_retagged(native_home, source, read_only, is_cli, expected):
    session = models.Session(session_id="placeholder", profile="default", source_tag=source,
                             read_only=read_only, is_cli_session=is_cli)
    _, result = routes._retag_empty_session_profile(session, "josue")
    assert result == expected
    assert session.profile == ("josue" if expected == "retagged" else "default")


@pytest.mark.parametrize("profile", ["default", "josue", "tomo", "tristan"])
def test_same_owner_external_session_is_not_retagged(native_home, profile):
    session = models.Session(session_id="same-owner", profile=profile, source_tag="telegram", read_only=True)
    assert routes._retag_empty_session_profile(session, profile) == (profile, "same_owner")
    assert session.profile == profile


@pytest.mark.parametrize("source,read_only", [("telegram", False), ("desktop", False), ("webui", True)])
def test_goal_cannot_retag_external_placeholder(native_home, monkeypatch, source, read_only):
    session = models.Session(session_id="goal-external", profile="default", source_tag=source,
                             read_only=read_only)
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    replies = []
    monkeypatch.setattr(routes, "bad", lambda h, text, status=400: replies.append((status, text)) or True)
    routes._handle_goal_command(object(), {"session_id": session.session_id, "profile": "josue", "args": "status"})
    assert replies[-1] == (403, "Session belongs to a different profile")
    assert session.profile == "default"
