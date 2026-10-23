"""Gateway async-delivery opt-in: omit caller conversation history so the runs API
declares a server-history consumer and re-enables background subagent delivery.

The runs API denies async delegation delivery whenever the caller supplies
conversation_history (caller-supplied history is authoritative for the turn and
never reads the SessionDB delivery row). With HERMES_WEBUI_GATEWAY_ASYNC_DELIVERY
set and an explicit session_id, the WebUI omits the history and the gateway
loads the authoritative one itself (#4362 regression guard: without the opt-in
the history is still sent, so gateway chat keeps its context)."""
import io
import json
from collections import OrderedDict
import threading

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
from api.config import STREAMS, STREAMS_LOCK, create_stream_channel
from api.models import new_session

STREAM_ID = "stream-async"


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_ASYNC_DELIVERY", raising=False)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None)
    yield session_dir
    with STREAMS_LOCK:
        STREAMS.pop(STREAM_ID, None)


def _session_with_history():
    s = new_session()
    s.messages = [
        {"role": "user", "content": "earlier question", "timestamp": 1.0},
        {"role": "assistant", "content": "earlier answer", "timestamp": 2.0},
    ]
    s.active_stream_id = STREAM_ID
    s.pending_user_message = "next"
    s.pending_attachments = []
    s.pending_started_at = 3.0
    # Gateway runs source the caller history from context_messages, not messages.
    s.context_messages = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    s.save()
    return s


def _run_stream(s, cfg=None, env=None):
    for key, value in (env or {}).items():
        gateway_chat.os.environ[key] = value
    captured = {}

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "POST":
            captured["post_body"] = json.loads(req.data.decode("utf-8"))
            return io.BytesIO(b'{"run_id":"run_async"}')
        return io.BytesIO(
            b'data: {"event":"message.delta","delta":"done"}\n'
            b'data: {"event":"run.completed","output":"done"}\n'
            b"data: [DONE]\n"
        )

    original_urlopen = gateway_chat.urllib.request.urlopen
    gateway_chat.urllib.request.urlopen = fake_urlopen
    try:
        final_text, _usage = gateway_chat._run_gateway_runs_api_streaming(
            s.session_id, "next", "test-model", "/tmp", STREAM_ID,
            "http://gateway.local", "test-key", [], {},
            put_gateway_event=lambda name, payload: None,
            cancel_event=threading.Event(),
            attachments=None,
            cfg=cfg or {},
            session=s,
            active_provider="",
        )
    finally:
        gateway_chat.urllib.request.urlopen = original_urlopen
        for key in (env or {}):
            gateway_chat.os.environ.pop(key, None)
    assert final_text == "done"
    return captured["post_body"]


def test_async_optin_omits_conversation_history(isolated_sessions):
    s = _session_with_history()
    with STREAMS_LOCK:
        STREAMS[STREAM_ID] = create_stream_channel()
    body = _run_stream(s, env={"HERMES_WEBUI_GATEWAY_ASYNC_DELIVERY": "1"})
    assert "conversation_history" not in body
    # The gateway keys the authoritative history and delivery row on the explicit session id.
    assert body["session_id"] == s.session_id
    assert body["input"] == "next"


def test_default_still_sends_conversation_history(isolated_sessions):
    s = _session_with_history()
    with STREAMS_LOCK:
        STREAMS[STREAM_ID] = create_stream_channel()
    body = _run_stream(s)
    # #4362 regression guard: without the opt-in the caller history keeps the turn contextual.
    assert body["conversation_history"] == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]


def test_async_delivery_helper_env_over_config_and_truthy_values(isolated_sessions, monkeypatch):
    # Truthy spellings match the existing runs-API opt-in convention.
    for raw in ("1", "true", "yes", "on"):
        assert gateway_chat._gateway_async_delivery_enabled({"webui_gateway_async_delivery": raw}) is True
    # Off values stay off.
    for raw in ("", "0", "false", "no", "off"):
        assert gateway_chat._gateway_async_delivery_enabled({"webui_gateway_async_delivery": raw}) is False
    assert gateway_chat._gateway_async_delivery_enabled({}) is False
    # Env wins over config, mirroring webui_gateway_use_runs_api.
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_ASYNC_DELIVERY", "1")
    assert gateway_chat._gateway_async_delivery_enabled({"webui_gateway_async_delivery": "false"}) is True


def test_async_optin_via_config_key(isolated_sessions):
    s = _session_with_history()
    with STREAMS_LOCK:
        STREAMS[STREAM_ID] = create_stream_channel()
    body = _run_stream(s, cfg={"webui_gateway_async_delivery": "true"})
    assert "conversation_history" not in body
