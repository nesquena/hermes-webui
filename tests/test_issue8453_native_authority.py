"""Native continuation keeps profile, model, prompt and lineage authority upstream."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import sqlite3
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest

from api import clarify, config, gateway_chat, gateway_sessions as sessions, models, profiles, routes


@pytest.fixture
def owner_profiles(tmp_path, monkeypatch):
    for name in ("default", "work", "family", "lab"):
        home = tmp_path if name == "default" else tmp_path / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("webui_chat_backend: gateway\nwebui_gateway_session_transport: local\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name:
        tmp_path if name in {None, "default", "renamed-root"} else tmp_path / "profiles" / name)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in {"default", "renamed-root"})
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path / "ambient")
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "ambient" / "config.yaml")
    monkeypatch.setattr(config, "get_config", lambda: {"webui_chat_backend": "gateway"})
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    state = tmp_path / "webui"
    state.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", state)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    return tmp_path


@pytest.mark.parametrize("profile", ["default", "renamed-root", "work", "family", "lab"])
def test_native_worker_uses_explicit_owner_config(owner_profiles, monkeypatch, profile):
    home = owner_profiles
    session = models.new_session(profile=profile, workspace=str(home))
    session.source_tag, session.session_source = "desktop", "other"
    session.active_stream_id, session.pending_user_message = "owned-config", "hello"
    session.save()
    monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", lambda _: ("http://unused.invalid", ""))
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a: False)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", MagicMock(side_effect=RuntimeError("unexpected REST fallback")))
    calls, events = [], []

    def native(s, stream, text, **kw):
        calls.append(s.profile)
        return "reply", {}, {"messages": [{"role": "assistant", "content": "reply"}]}

    monkeypatch.setattr(sessions, "run_session", native)
    gateway_chat.STREAMS["owned-config"] = SimpleNamespace(put_nowait=events.append)
    try:
        gateway_chat._run_gateway_chat_streaming(session.session_id, "hello", "model", str(home), "owned-config", [])
        assert calls == [profile]
        assert any(event[0] == "done" for event in events)
        assert not any(event[0] == "apperror" for event in events)
    finally:
        gateway_chat.STREAMS.pop("owned-config", None)


@pytest.mark.parametrize("update,accepted", [
    ({"model": "new"}, False), ({"model_provider": "new"}, False),
    ({"model_provider": None}, False), ({"model": "old", "model_provider": "old-provider"}, True),
])
def test_native_picker_update_cannot_rewrite_authority(owner_profiles, monkeypatch, update, accepted):
    session = models.Session(session_id="native-picker", profile="default", workspace=str(owner_profiles),
                             model="old", model_provider="old-provider", source_tag="desktop", session_source="other")
    session.save = MagicMock()
    original_workspace = session.workspace
    body = {"session_id": session.session_id, **update}
    monkeypatch.setattr(routes, "_check_csrf", lambda *a: True)
    monkeypatch.setattr(routes, "read_body", lambda *a: body)
    monkeypatch.setattr(routes, "get_session", lambda *a, **k: session)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *a: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda *a, **k: original_workspace)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(config, "_evict_session_agent", lambda *a: None)
    replies = []
    monkeypatch.setattr(routes, "j", lambda h, body, status=200, **kw: replies.append((status, body)) or True)
    monkeypatch.setattr(routes, "bad", lambda h, text, status=400: replies.append((status, text)) or True)
    routes.handle_post(MagicMock(), urlparse("/api/session/update"))
    assert replies[-1][0] == (200 if accepted else 400)
    assert (session.model, session.model_provider, session.workspace) == ("old", "old-provider", original_workspace)
    assert session.save.call_count == int(accepted)


def test_expired_clarify_lock_is_not_accepted(monkeypatch):
    sid, cid = "expired-clarify", "gw-session:stable:batch:one"
    relay = SimpleNamespace(session_id=sid, run_id="session:stable", clarifications={cid: ("clarify.lock", {})})
    monkeypatch.setattr(sessions, "_CONNECTIONS", {relay.run_id: relay})
    monkeypatch.setattr(sessions, "control", lambda *a, **k: {"status": "expired"})
    try:
        pending = clarify.submit_pending(sid, {"clarify_id": cid, "question": "Continue?"})
        assert sessions.respond_clarify(sid, cid, "yes") is False
        assert not pending.event.is_set()
    finally:
        clarify.clear_pending(sid)


@pytest.mark.parametrize("reason", ["timeout", "resolved", "interrupted"])
def test_canonical_request_cancel_retires_only_matching_batch(reason):
    relay = sessions.SessionRelay("withdrawal", "stream", "home", {}, lambda *a: None, threading.Event(), lambda *a: None)
    try:
        relay.clarify({"questions": [{"qid": "one", "question": "A?"}]}, request_id="batch-one")
        relay.clarify({"questions": [{"qid": "two", "question": "B?"}]}, request_id="batch-two")
        relay.event({"session_id": "withdrawal", "type": "request.cancel", "payload": {"id": "batch-one", "reason": reason}})
        assert list(relay.clarifications) == ["gw-session:stream:batch-two:two"]
    finally:
        clarify.clear_pending("withdrawal")


@pytest.fixture(params=["default", "work", "family", "lab"])
def compression_db(tmp_path, request):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", "telegram", session_key="telegram:fixture", profile_name=request.param)
    db.end_session("root", "compression")
    db.create_session("tip", "telegram", session_key="telegram:fixture", parent_session_id="root", profile_name=request.param)
    yield db, tmp_path
    db.close()


@pytest.mark.parametrize("reason", ["timeout", "interrupted", "resolved"])
def test_real_gateway_batch_withdrawal_reaches_webui(reason):
    producer = pytest.importorskip("gateway.session_server_requests")
    sid = "producer-clarify"
    relay = sessions.SessionRelay(sid, "producer", "home", {}, lambda *a: None,
                                  threading.Event(), lambda *a: None)
    published, request_ids = [], []
    relay.clarify({"questions": [{"qid": "other", "question": "Unrelated?"}]}, request_id="other-batch")
    unrelated = set(relay.clarifications)

    def write(frame):
        request_ids.append(frame["id"])
        relay.clarify(frame["params"], request_id=frame["id"])
        assert len(relay.clarifications) == 2
        # A different session cannot withdraw this batch even with its exact ID.
        relay.event({"session_id": "foreign", "type": "request.cancel", "payload": {"id": frame["id"]}})
        assert len(relay.clarifications) == 2
        if reason == "resolved":
            assert producer.resolve({"id": frame["id"], "result": {"answers": {"one": "yes"}}})

    def publish(session_id, payload, *, event_type):
        published.append(payload)
        relay.event({"session_id": session_id, "type": event_type, "payload": payload})

    stream = SimpleNamespace(fanout=SimpleNamespace(transports=lambda: [object()], write=write), publish=publish)
    try:
        producer.ask_viewer_result(stream, sid, "clarify", {"questions": [{"qid": "one", "question": "Continue?"}]},
                                   timeout=0, cancelled=lambda: reason == "interrupted", qids=["one"])
        assert published == [{"id": request_ids[0], "method": "clarify", "reason": reason}]
        assert set(relay.clarifications) == unrelated
        assert clarify.get_pending(sid)["clarify_id"] in unrelated
        assert not producer.open_requests(stream, sid)
    finally:
        clarify.clear_pending(sid)


@pytest.mark.parametrize("source", ["telegram", "discord"])
def test_compressed_messaging_uses_owner_for_rpc_and_display_for_mirrors(compression_db, monkeypatch, source):
    db, home = compression_db
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("UPDATE sessions SET source=?, session_key=?", (source, source + ":fixture"))
    calls, records, events = [], [], []
    protocol = ModuleType("hermes_cli.gateway_client")
    protocol.GatewayRPCError = type("GatewayRPCError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", protocol)
    class Client:
        events = None
        async def rpc(self, method, **params):
            calls.append((method, params))
            if method == "session.resume":
                return {"session_id": "root", "messages": [{"role": "assistant", "content": "reply"}]}
            return {"admission_id": "admission", "status": "terminal", "outcome": "completed", "result": {"final_response": "reply"}}
    @asynccontextmanager
    async def connect():
        yield Client()
    request = {"session_id": "tip", "input_id": "stable", "text": "hello"}
    relay = sessions.SessionRelay("tip", "stream", str(home), request, lambda *a: events.append(a),
        threading.Event(), lambda *a: records.append(a), connect)
    result = asyncio.run(relay.run())
    assert result[0] == "reply"
    assert relay.session_id == "tip"
    assert all(params["session_id"] == "root" for method, params in calls if method != "session.resume")
    assert records[0][1]["session_id"] == "root"
    assert records[0][1]["input_id"] == "stable"
    # Another compression advances the transcript without changing admission identity.
    db.end_session("tip", "compression")
    db.create_session("new-tip", source, session_key=source + ":fixture", parent_session_id="tip",
                      profile_name=db.get_session("tip")["profile_name"])
    # Reopening the WebUI sidecar must recover the same canonical request.
    resumed = sessions.SessionRelay("tip", "stream", str(home), dict(records[-1][1]), lambda *a: None,
        threading.Event(), lambda *a: None, connect, recovering=True)
    assert asyncio.run(resumed.run())[0] == "reply"
    assert [p for method, p in calls if method == "prompt.submit"] == [records[0][1]] * 2


def test_canonical_owner_scopes_events_controls_but_not_webui_mirrors(monkeypatch):
    protocol = ModuleType("hermes_cli.gateway_client")
    protocol.GatewayRPCError = type("GatewayRPCError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", protocol)
    calls, events = [], []
    relay = sessions.SessionRelay("display-tip", "owner-stream", "home", {"session_id": "canonical-root"},
        lambda *a: events.append(a), threading.Event(), lambda *a: None, recovering=True)
    relay.receipt = {"admission_id": "admission"}
    class Client:
        async def rpc(self, method, **params):
            calls.append((method, params))
            return {"admission_id": "admission", "status": "started", "execution_generation": 2}
    relay.client = Client()
    relay.approvals["approval"] = 2
    try:
        relay.event({"session_id": "canonical-root", "admission_id": "admission", "type": "clarify.request",
                     "execution_generation": 2, "payload": {"prompt_id": "question", "question": "Continue?"}})
        cid = next(iter(relay.clarifications))
        for action, params in [("steer", {"text": "guide"}), ("stop", {}),
                               ("approval", {"approval_id": "approval", "choice": "once"}),
                               ("clarify", {"clarify_id": cid, "answer": "yes"})]:
            asyncio.run(relay.control(action, **params))
        assert all(params["session_id"] == "canonical-root" for _, params in calls)
        assert clarify.resolve_clarify_by_id("display-tip", cid, "yes")
        relay.event({"session_id": "foreign", "type": "request.cancel", "payload": {"id": "question"}})
        assert cid in relay.clarifications
    finally:
        clarify.clear_pending("display-tip")


@pytest.mark.parametrize("bad", ["unrelated", "foreign-profile", "closed", "stale", "branch"])
def test_unverified_resume_owner_never_submits(compression_db, monkeypatch, bad):
    db, home = compression_db
    owner = "root"
    if bad == "unrelated":
        owner = "other"
    elif bad == "foreign-profile":
        with sqlite3.connect(home / "state.db") as conn:
            conn.execute("UPDATE sessions SET profile_name='foreign' WHERE id='tip'")
    elif bad == "closed":
        db.end_session("tip", "user_closed")
    elif bad == "stale":
        db.end_session("tip", "compression")
        db.create_session("new-tip", "telegram", session_key="telegram:fixture", parent_session_id="tip")
    else:
        with sqlite3.connect(home / "state.db") as conn:
            conn.execute('UPDATE sessions SET model_config=? WHERE id=?', ('{"_branched_from":"root"}', 'tip'))
    protocol = ModuleType("hermes_cli.gateway_client")
    protocol.GatewayRPCError = type("GatewayRPCError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", protocol)
    calls = []
    class Client:
        async def rpc(self, method, **params):
            calls.append(method)
            assert method == "session.resume"
            return {"session_id": owner}
    @asynccontextmanager
    async def connect():
        yield Client()
    relay = sessions.SessionRelay("tip", "stream", str(home), {"session_id": "tip", "input_id": "stable", "text": "hello"},
        lambda *a: None, threading.Event(), lambda *a: None, connect)
    with pytest.raises(sessions.SessionTransportError):
        asyncio.run(relay.run())
    assert calls == ["session.resume"]
