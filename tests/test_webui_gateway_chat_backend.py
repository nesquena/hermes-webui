from collections import OrderedDict
import base64
from email.message import Message
import json
from pathlib import Path
import re
import threading
import time
import urllib.error

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming
from api.config import PENDING_GOAL_CONTINUATION, STREAMS, create_stream_channel
from api.models import new_session
from api.gateway_chat import (
    _gateway_http_error_event,
    _gateway_reasoning_delta,
    _gateway_sse_delta,
    _gateway_sse_reasoning_delta,
    _gateway_stream_usage,
    _gateway_tool_progress_event,
    _gateway_use_runs_api_enabled,
    gateway_chat_config_status,
    webui_chat_backend_mode,
    webui_gateway_chat_enabled,
)
from api.turn_journal import derive_turn_journal_states, read_turn_journal


def test_gateway_chat_backend_is_default_off_for_truthy_values():
    for value in (None, "", "1", "true", "yes", "on", "enabled", "runner-local"):
        env = {}
        if value is not None:
            env["HERMES_WEBUI_CHAT_BACKEND"] = value
        assert webui_chat_backend_mode({}, env) == "legacy"
        assert webui_gateway_chat_enabled({}, env) is False


def test_gateway_chat_backend_only_accepts_explicit_gateway_aliases():
    for value in ("gateway", "api_server", "api-server", " Gateway "):
        assert webui_chat_backend_mode({}, {"HERMES_WEBUI_CHAT_BACKEND": value}) == "gateway"
        assert webui_gateway_chat_enabled({}, {"HERMES_WEBUI_CHAT_BACKEND": value}) is True


def test_gateway_chat_backend_can_be_enabled_from_config_without_env():
    assert webui_chat_backend_mode({"webui_chat_backend": "api_server"}, {}) == "gateway"


def test_gateway_chat_config_status_is_redacted_and_reports_missing_key():
    status = gateway_chat_config_status(
        {},
        {
            "HERMES_WEBUI_CHAT_BACKEND": "gateway",
            "HERMES_WEBUI_GATEWAY_BASE_URL": "http://gateway.local",
        },
    )

    assert status == {
        "enabled": True,
        "backend": "gateway",
        "base_url_configured": True,
        "api_key_configured": False,
    }


def test_gateway_chat_config_status_reports_fallback_api_server_key_without_exposing_value():
    status = gateway_chat_config_status(
        {},
        {
            "HERMES_WEBUI_CHAT_BACKEND": "gateway",
            "API_SERVER_KEY": "secret-token",
        },
    )

    assert status["api_key_configured"] is True
    assert "secret-token" not in repr(status)


def test_gateway_chat_backend_env_wins_over_config_and_stays_safe():
    assert webui_chat_backend_mode(
        {"webui_chat_backend": "gateway"},
        {"HERMES_WEBUI_CHAT_BACKEND": "legacy-direct"},
    ) == "legacy"


def test_gateway_sse_delta_extracts_openai_chat_chunks():
    assert _gateway_sse_delta({"choices": [{"delta": {"content": "hel"}}]}) == "hel"
    assert _gateway_sse_delta({"choices": [{"message": {"content": "done"}}]}) == "done"
    assert _gateway_sse_delta({"choices": [{"delta": {}}]}) == ""


def test_gateway_stream_usage_normalizes_token_names():
    assert _gateway_stream_usage({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}) == {
        "input_tokens": 7,
        "output_tokens": 3,
        "estimated_cost": 0,
    }
    assert _gateway_stream_usage({"usage": {"input_tokens": 5, "output_tokens": 2, "estimated_cost_usd": 0.01}}) == {
        "input_tokens": 5,
        "output_tokens": 2,
        "estimated_cost": 0.01,
    }
    assert _gateway_stream_usage({}) == {}


def test_gateway_tool_progress_event_translates_gateway_lifecycle_payloads():
    assert _gateway_tool_progress_event(
        {
            "tool": "terminal",
            "label": "terminal: pytest",
            "toolCallId": "call-1",
            "status": "running",
        }
    ) == (
        "tool",
        {
            "event_type": "tool.started",
            "name": "terminal",
            "preview": "terminal: pytest",
            "args": {},
            "is_error": False,
            "tid": "call-1",
        },
    )
    assert _gateway_tool_progress_event(
        {"tool": "terminal", "toolCallId": "call-1", "status": "completed"}
    ) == (
        "tool_complete",
        {
            "event_type": "tool.completed",
            "name": "terminal",
            "preview": None,
            "args": {},
            "is_error": False,
            "tid": "call-1",
        },
    )
    assert _gateway_tool_progress_event(
        {"tool": "_thinking", "status": "running", "preview": "Thinking..."}
    ) == (
        "reasoning",
        {
            "text": "Thinking...",
        },
    )
    assert _gateway_tool_progress_event(
        {"tool": "_thinking", "status": "running", "text": "Thinking from text..."}
    ) == (
        "reasoning",
        {
            "text": "Thinking from text...",
        },
    )
    assert _gateway_tool_progress_event({"tool": "_thinking", "status": "running"}) is None


def test_gateway_tool_progress_event_bounds_pathological_args():
    long_command = "python -c " + repr("print('x')\n" * 24)
    event_name, event_payload = _gateway_tool_progress_event(
        {
            "tool": "terminal",
            "toolCallId": "call-huge",
            "status": "running",
            "args": {
                "command": long_command,
                "items": [{"index": i, "payload": "x" * 100} for i in range(50_000)],
            },
        }
    )

    assert event_name == "tool"
    assert event_payload["args"]["command"] == long_command
    assert len(event_payload["args"]["items"]) <= 64
    assert len(json.dumps(event_payload["args"], sort_keys=True)) < 100_000


def test_gateway_reasoning_delta_keeps_string_deltas_and_ignores_structured_payloads():
    assert _gateway_reasoning_delta({"text": " Let me"}) == " Let me"
    assert _gateway_reasoning_delta({"text": "   ", "preview": " think"}) == " think"
    assert _gateway_reasoning_delta({"content": {"text": "safe", "debug": {"note": "x"}}}) == ""
    assert _gateway_reasoning_delta({"text": ["safe"], "preview": " more"}) == " more"


def test_gateway_sse_reasoning_delta_extracts_reasoning_content_chunks():
    assert _gateway_sse_reasoning_delta({"choices": [{"delta": {"reasoning_content": "Let me"}}]}) == "Let me"
    assert _gateway_sse_reasoning_delta({"choices": [{"message": {"reasoning_content": "Done thinking"}}]}) == "Done thinking"
    assert _gateway_sse_reasoning_delta({"choices": [{"delta": {"reasoning_content": "   "}}]}) == ""


def test_gateway_http_401_reports_gateway_auth_not_provider_key():
    exc = urllib.error.HTTPError(
        "http://gateway.local/v1/chat/completions",
        401,
        "Unauthorized",
        hdrs=Message(),
        fp=None,
    )

    event = _gateway_http_error_event(
        exc,
        '{"error":{"message":"Invalid API key","code":"invalid_api_key"}}',
        api_key_configured=False,
    )

    assert event["label"] == "Gateway authentication failed"
    assert event["type"] == "gateway_auth_error"
    assert "HTTP 401" in event["message"]
    assert "HERMES_WEBUI_GATEWAY_API_KEY" in event["hint"]
    assert "API_SERVER_KEY" in event["hint"]
    assert "Invalid API key" not in event["hint"]


def test_gateway_http_401_with_key_suggests_key_mismatch():
    exc = urllib.error.HTTPError(
        "http://gateway.local/v1/chat/completions",
        401,
        "Unauthorized",
        hdrs=Message(),
        fp=None,
    )

    event = _gateway_http_error_event(exc, "", api_key_configured=True)

    assert event["type"] == "gateway_auth_error"
    assert event["hint"] == "Check that HERMES_WEBUI_GATEWAY_API_KEY matches the Hermes Gateway API_SERVER_KEY."


def test_frontend_renders_gateway_auth_error_with_specific_label():
    src = Path("static/messages.js").read_text(encoding="utf-8")
    start = src.find("source.addEventListener('apperror'")
    end = src.find("source.addEventListener('warning'", start)
    assert start != -1 and end != -1, "apperror handler not found"
    block = src[start:end]

    assert "d.type==='gateway_auth_error'" in block
    assert "isGatewayAuthError" in block
    assert "gateway_auth_label" in block
    assert "Gateway authentication failed" in block
    assert "isGatewayAuthError?(typeof t==='function'?t('gateway_auth_label'):'Gateway authentication failed'):isAuthMismatch" in block, (
        "Gateway API key failures should use their own label before generic provider mismatch handling."
    )


def test_gateway_auth_label_i18n_key_exists_for_every_locale():
    src = Path("static/i18n.js").read_text(encoding="utf-8")
    locale_names = [
        match.group("quoted") or match.group("plain")
        for match in re.finditer(
            r"^\s{2}(?:'(?P<quoted>[A-Za-z0-9-]+)'|(?P<plain>[A-Za-z0-9-]+))\s*:\s*\{",
            src,
            re.MULTILINE,
        )
    ]
    assert src.count("gateway_auth_label") >= len(locale_names)


def test_gateway_chat_health_payload_is_documented_as_operator_diagnostic_only():
    # The Gateway-backed-chat operator docs moved out of the README into
    # docs/advanced-chat-setup.md during the v0.51.192 README IA pass (it's a
    # niche self-hosted feature). The contract — that gateway_chat is documented
    # as an operator-only diagnostic, not a user-facing banner — now lives there.
    # CHANGELOG keeps its release-note entry. (Contract test moved with content.)
    advanced = Path("docs/advanced-chat-setup.md").read_text(encoding="utf-8")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    for text in (advanced, changelog):
        assert "gateway_chat" in text
        assert "operator diagnostic" in text
        assert "not currently rendered as a user-facing health banner" in text


def test_gateway_chat_worker_translates_sse_and_persists_session(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'event: hermes.tool.progress\n'
            yield b'data: {"tool":"terminal","label":"terminal: pytest","toolCallId":"call-1","status":"running"}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
            yield b'event: hermes.tool.progress\n'
            yield b'data: {"tool":"_thinking","text":"Thinking from tool progress"}\n\n'
            yield b'event: reasoning.available\n'
            yield b'data: {"text":"Reasoning preview", "preview":"Reasoning preview"}\n\n'
            yield b'event: hermes.tool.progress\n'
            yield b'data: {"tool":"terminal","toolCallId":"call-1","status":"completed"}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
            yield b'data: [DONE]\n\n'

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data.decode("utf-8")
        return FakeResponse()

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "secret-token")
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *args, **kwargs: "high")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "loaded",
        "source": "test",
        "label": "test",
        "message_count": 2,
        "messages": [
            {"role": "assistant", "content": "prefill summary"},
            {"role": "user", "content": "prefill"},
        ],
    })
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: list(ctx["messages"]) + [{"role": "user", "content": "webui session context"}])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    s = new_session()
    stream_id = "stream-gateway-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel

    from api import routes

    monkeypatch.setattr(routes.api_config, "resolve_model_alias_runtime", lambda *_a, **_k: {
        "alias": "sol", "model": "alias-target-model", "provider": "openai-codex",
        "api_key": "ambient-provider-key", "base_url": "https://provider.example.test/v1",
        "base_url_explicit": False, "credential_explicit": False,
    })
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)

    def start_gateway(session, **kw):
        gateway_chat._run_gateway_chat_streaming(
            session.session_id, kw["msg"], kw["model"], kw["workspace"], stream_id,
            kw["attachments"], model_provider=kw["model_provider"],
            persisted_model=kw["persisted_model"],
            persisted_model_provider=kw["persisted_model_provider"],
        )
        return {"stream_id": stream_id}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", start_gateway)
    routes._start_run(
        s, msg="Say hello", model="alias-target-model",
        model_provider="model-alias-profile-bound-lane", workspace=str(tmp_path),
        attachments=[], normalized_model=False, source="webui", route="/api/chat/start",
        gateway_chat_enabled=True,
    )

    saved = models.get_session(s.session_id)
    assert [m["role"] for m in saved.messages] == ["user", "assistant"]
    assert saved.messages[-1]["content"] == "hello"
    assert isinstance(saved.messages[0]["timestamp"], float)
    assert isinstance(saved.messages[1]["timestamp"], float)
    assert saved.messages[0]["timestamp"] < saved.messages[1]["timestamp"]
    assert saved.active_stream_id is None
    assert saved.model == "alias-target-model"
    assert saved.model_provider == "model-alias-profile-bound-lane"
    assert stream_id not in STREAMS
    assert captured["url"] == "http://gateway.local/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["X-hermes-session-id"] == s.session_id
    assert captured["headers"]["X-hermes-session-key"] == f"webui:{s.session_id}"
    assert '"stream": true' in captured["body"]
    payload = json.loads(captured["body"])
    assert payload["reasoning_effort"] == "high"
    assert payload["model"] == "alias-target-model"
    assert payload["provider"] == "openai-codex"
    assert "ambient-provider-key" not in captured["body"]
    # #3324: the gateway path's first system message is now the full WebUI
    # ephemeral system prompt (progress prompt + session/delivery context),
    # NOT the bare _WEBUI_PROGRESS_PROMPT — otherwise the delivery/session
    # context is silently dropped on Gateway-routed WebUI chats.
    system_msg = payload["messages"][0]
    assert system_msg["role"] == "system"
    assert "Final visible assistant replies" in system_msg["content"]
    assert "Need script" in system_msg["content"]
    # The moved session/delivery context must be present in the system prompt.
    assert "Connected Platforms:" in system_msg["content"]
    assert "Delivery options for scheduled tasks:" in system_msg["content"]
    # The gateway path keeps safe recall prefill context while removing
    # terminal user-role prefill before the actual browser user turn.
    assert [m["content"] for m in payload["messages"][1:]] == [
        "prefill summary",
        "Say hello",
    ]
    assert [m["role"] for m in payload["messages"]] == ["system", "assistant", "user"]
    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    event_pairs = [(item[0], item[1]) for item in events]
    assert ("tool", {
        "event_type": "tool.started",
        "name": "terminal",
        "preview": "terminal: pytest",
        "args": {},
        "is_error": False,
        "tid": "call-1",
    }) in event_pairs
    assert ("reasoning", {"text": "Thinking from tool progress"}) in event_pairs
    assert ("reasoning", {"text": "Reasoning preview"}) in event_pairs
    assert ("tool_complete", {
        "event_type": "tool.completed",
        "name": "terminal",
        "preview": None,
        "args": {},
        "is_error": False,
        "tid": "call-1",
    }) in event_pairs
    assert all(len(item) == 3 and item[2] for item in events)


def test_gateway_chat_worker_records_turn_journal_completion(tmp_path, monkeypatch):
    """#6366 re-gate: a successful Gateway run must record durable
    same-stream completion evidence in the crash-safe turn journal.

    The stale-cancel recovery predicate derives its completion evidence
    from the turn journal, so a Gateway run that persisted its final
    answer but lost the run journal's terminal write would otherwise
    have no completion evidence at all — and recovery would re-append a
    duplicate recovered row after the valid final answer.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "secret-token")
    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda req, timeout=0: FakeResponse(),
    )

    s = new_session()
    stream_id = "stream-gateway-turn-journal-completed"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 456
    s.save()
    channel = create_stream_channel()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    journal = read_turn_journal(s.session_id, session_dir=session_dir)
    states, _ = derive_turn_journal_states(journal.get("events") or [])
    stream_events = [
        event
        for event in states.values()
        if str(event.get("stream_id") or "") == stream_id
    ]
    assert stream_events, "expected turn journal events for the gateway stream"
    assert any(
        event.get("event") == "completed" for event in stream_events
    ), "the gateway success writeback must record a completed turn journal event"


def test_gateway_chat_worker_uses_session_profile_endpoint_not_process_profile(tmp_path, monkeypatch):
    """A fresh gateway turn must target the session's profile gateway endpoint.

    With one WebUI serving several profiles against a multiplexing gateway, each
    profile's turns belong on its own ``/p/<profile>`` route with its own key. The
    process-wide URL/key describe whichever profile the process loaded, so using
    them sends every profile's turns to that one profile.
    """
    from unittest.mock import MagicMock

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    # Process-active profile's endpoint: must NOT be used for a 'coder' session.
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "process-profile-key")
    resolved_for = []

    def fake_endpoint_for_profile(profile_name):
        resolved_for.append(profile_name)
        return "http://gateway.local/p/coder", "coder-key"

    monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", fake_endpoint_for_profile)
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])

    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req, timeout=0):
        requests.append(req)
        return FakeResponse()

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    channel = MagicMock()
    channel.put_nowait = lambda item: None
    s = new_session()
    s.profile = "coder"
    stream_id = "stream-gateway-session-profile-endpoint-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hi"
    s.pending_started_at = 333
    s.save()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(s.session_id, "Say hi", "m", str(tmp_path), stream_id, [])

    assert resolved_for == ["coder"]
    assert requests, "the worker made no gateway request"
    assert requests[0].full_url.startswith("http://gateway.local/p/coder/v1/")
    assert requests[0].get_header("Authorization") == "Bearer coder-key"


def test_gateway_chat_worker_classifies_terminal_provider_error_without_text(tmp_path, monkeypatch):
    """Gateway terminal errors must survive an empty assistant stream."""
    from unittest.mock import MagicMock

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    error_text = (
        'HTTP 400: {"detail":"Invalid Request: Invalid model format or no credentials '
        'for provider: <redacted>"}'
    )
    response_error = [error_text]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            if response_error[0] == "partial":
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                yield f'data: {{"error":{json.dumps(error_text)}}}\n\n'.encode()
            elif response_error[0]:
                yield f'data: {{"error":{json.dumps(response_error[0])}}}\n\n'.encode()
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    events = []
    channel = MagicMock()
    channel.put_nowait = lambda item: events.append(item)
    s = new_session()
    stream_id = "stream-gateway-terminal-provider-error-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_started_at = 222
    s.pending_attachments = [{"name": "current.png"}]
    s.messages = [
        {"role": "user", "content": "Say hello", "timestamp": 111, "attachments": [{"name": "old.png"}]},
        {"role": "assistant", "content": "Earlier answer"},
    ]
    s.context_messages = [
        {"role": "user", "content": "Say hello", "timestamp": 111, "attachments": [{"name": "old.png"}]},
        {"role": "assistant", "content": "Earlier answer"},
    ]
    s.save()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "east",
        str(tmp_path),
        stream_id,
        [],
        persisted_model="shared-model",
        persisted_model_provider="model-alias-profile-bound-lane",
    )

    apperrors = [item[1] for item in events if item[0] == "apperror"]
    assert apperrors
    assert apperrors[-1]["type"] in {"model_not_found", "auth_mismatch"}
    assert apperrors[-1]["session_id"] == s.session_id
    saved = models.get_session(s.session_id)
    user_messages = [m for m in saved.messages if m.get("role") == "user"]
    assert len(user_messages) == 2
    assert user_messages[-1]["timestamp"] == 222
    assert user_messages[-1]["attachments"] == [{"name": "current.png"}]
    context_users = [m for m in saved.context_messages if m.get("role") == "user"]
    assert len(context_users) == 2
    assert context_users[-1]["timestamp"] == 222
    assert context_users[-1]["attachments"] == [{"name": "current.png"}]
    assert saved.messages[-1].get("_error") is True
    assert saved.model == "shared-model"
    assert saved.model_provider == "model-alias-profile-bound-lane"

    response_error[0] = ""
    empty_stream_id = "stream-gateway-empty-response-test"
    s = new_session()
    s.active_stream_id = empty_stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.save()
    empty_events = []
    empty_channel = MagicMock()
    empty_channel.put_nowait = lambda item: empty_events.append(item)
    STREAMS[empty_stream_id] = empty_channel
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        empty_stream_id,
        [],
    )
    empty_errors = [item[1] for item in empty_events if item[0] == "apperror"]
    assert empty_errors[-1]["type"] == "gateway_empty_response"
    assert empty_errors[-1]["session_id"] == s.session_id

    response_error[0] = "Gateway provider failed without a known classification"
    unknown_stream_id = "stream-gateway-unknown-terminal-error-test"
    s = new_session()
    s.active_stream_id = unknown_stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.save()
    unknown_events = []
    unknown_channel = MagicMock()
    unknown_channel.put_nowait = lambda item: unknown_events.append(item)
    STREAMS[unknown_stream_id] = unknown_channel
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        unknown_stream_id,
        [],
    )
    unknown_errors = [item[1] for item in unknown_events if item[0] == "apperror"]
    assert unknown_errors[-1]["type"] == "error"
    assert "Gateway provider failed" in unknown_errors[-1]["message"]
    unknown_payload_error = unknown_errors[-1]["session"]["messages"][-1]
    assert unknown_payload_error.get("_error") is True
    assert "_turnDuration" not in unknown_payload_error

    response_error[0] = error_text
    future_stream_id = "stream-gateway-future-duration-terminal-error-test"
    s = new_session()
    s.active_stream_id = future_stream_id
    s.pending_user_message = "Say hello"
    s.pending_started_at = time.time() + 30
    s.pending_attachments = []
    s.save()
    future_events = []
    future_channel = MagicMock()
    future_channel.put_nowait = lambda item: future_events.append(item)
    STREAMS[future_stream_id] = future_channel
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        future_stream_id,
        [],
    )
    future_errors = [item[1] for item in future_events if item[0] == "apperror"]
    assert future_errors[-1]["type"] in {"model_not_found", "auth_mismatch"}
    saved = models.get_session(s.session_id)
    assert saved.messages[-1].get("_error") is True
    assert "_turnDuration" not in saved.messages[-1]
    future_payload_error = future_errors[-1]["session"]["messages"][-1]
    assert future_payload_error.get("_error") is True
    assert "_turnDuration" not in future_payload_error

    response_error[0] = "partial"
    partial_stream_id = "stream-gateway-partial-terminal-error-test"
    s = new_session()
    s.active_stream_id = partial_stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.save()
    partial_events = []
    partial_channel = MagicMock()
    partial_channel.put_nowait = lambda item: partial_events.append(item)
    STREAMS[partial_stream_id] = partial_channel
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        partial_stream_id,
        [],
    )
    partial_errors = [item[1] for item in partial_events if item[0] == "apperror"]
    assert partial_errors[-1]["type"] in {"model_not_found", "auth_mismatch"}
    saved = models.get_session(s.session_id)
    assert [message.get("role") for message in saved.messages[-3:]] == ["user", "assistant", "assistant"]
    partial_message = saved.messages[-2]
    assert partial_message.get("_partial") is True
    assert partial_message["content"] == "partial"
    error_message = saved.messages[-1]
    assert error_message.get("_error") is True
    assert "Invalid Request" in error_message.get("provider_details", "")
    payload_messages = partial_errors[-1]["session"]["messages"]
    assert payload_messages[-2]["_partial"] is True
    assert payload_messages[-2]["content"] == "partial"
    assert payload_messages[-1]["_error"] is True
    assert "_turnDuration" not in payload_messages[-1]


def test_gateway_chat_worker_persists_reasoning_and_tool_state_on_terminal_error(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    error_text = (
        'HTTP 400: {"detail":"Invalid Request: Invalid model format or no credentials '
        'for provider: <redacted>"}'
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"part"}}]}\n\n'
            yield b'event: hermes.tool.progress\n'
            yield b'data: {"tool":"terminal","label":"terminal: pytest","toolCallId":"call-1","status":"running","arguments":{}}\n\n'
            yield b'event: reasoning.available\n'
            yield b'data: {"text":"Preview reasoning"}\n\n'
            yield f'data: {{"error":{json.dumps(error_text)}}}\n\n'.encode()
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    events = []
    channel = MagicMock()
    channel.put_nowait = lambda item: events.append(item)
    s = new_session()
    stream_id = "stream-gateway-terminal-reasoning-tool-error-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.save()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    saved = models.get_session(s.session_id)
    partial_message = saved.messages[-2]
    assert partial_message.get("_partial") is True
    assert partial_message["content"] == "part"
    assert partial_message["reasoning"] == "Preview reasoning"
    assert partial_message["_partial_tool_calls"] == [{
        "name": "terminal",
        "args": {},
        "done": True,
        "tid": "call-1",
        "_sealed_by_terminal_error": True,
    }]
    apperrors = [item[1] for item in events if item[0] == "apperror"]
    assert apperrors[-1]["session"]["messages"][-2]["reasoning"] == "Preview reasoning"
    assert apperrors[-1]["session"]["messages"][-2]["_partial_tool_calls"][0]["name"] == "terminal"
    assert apperrors[-1]["session"]["messages"][-1]["_error"] is True


def test_gateway_chat_worker_preserves_reasoning_delta_whitespace_and_persists_reasoning(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
            yield b'event: hermes.tool.progress\n'
            yield b'data: {"tool":"_thinking","text":"Let me"}\n\n'
            yield b'event: reasoning.available\n'
            yield b'data: {"text":" think", "preview":"should not win"}\n\n'
            yield b'event: reasoning.available\n'
            yield b'data: {"content":{"text":"safe","debug":{"note":"x"}}}\n\n'
            yield b'event: reasoning.available\n'
            yield b'data: {"preview":" more"}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    s = new_session()
    stream_id = "stream-gateway-reasoning-persist-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    saved = models.get_session(s.session_id)
    assert saved.messages[-1]["content"] == "hello"
    assert saved.messages[-1]["reasoning"] == "Let me think more"
    reasoning_events = []
    while not subscriber.empty():
        item = subscriber.get_nowait()
        if item[0] == "reasoning":
            reasoning_events.append(item[1]["text"])
    assert reasoning_events == ["Let me", " think", " more"]
    assert not any("debug" in text for text in reasoning_events)


def test_gateway_chat_worker_reads_reasoning_content_deltas_from_chat_completions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"Let me ","content":"hel"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"reasoning_content":"think","content":"lo"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    s = new_session()
    stream_id = "stream-gateway-reasoning-content-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    saved = models.get_session(s.session_id)
    assert saved.messages[-1]["content"] == "hello"
    assert saved.messages[-1]["reasoning"] == "Let me think"
    reasoning_events = []
    while not subscriber.empty():
        item = subscriber.get_nowait()
        if item[0] == "reasoning":
            reasoning_events.append(item[1]["text"])
    assert reasoning_events == ["Let me ", "think"]


def test_gateway_chat_worker_emits_goal_continue_for_goal_related_turn(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"goal "}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"reply"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    from api import goals as webui_goals

    monkeypatch.setattr(webui_goals, "has_active_goal", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        webui_goals,
        "evaluate_goal_after_turn",
        lambda *args, **kwargs: {
            "should_continue": True,
            "continuation_prompt": "continue the goal",
            "message": "Continuing goal",
            "message_key": "goal_continuing",
            "message_args": ["one step remains"],
        },
    )

    s = new_session()
    stream_id = "stream-gateway-goal-continue"
    s.active_stream_id = stream_id
    s.pending_user_message = "finish it"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel
    PENDING_GOAL_CONTINUATION.discard(s.session_id)

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "finish it",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=True,
    )

    saved = models.get_session(s.session_id)
    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    event_names = [item[0] for item in events]

    assert event_names.count("goal") == 2
    assert "goal_continue" in event_names
    assert "done" in event_names
    assert "stream_end" in event_names
    assert event_names.index("goal_continue") < event_names.index("done")
    assert event_names.index("done") < event_names.index("stream_end")
    assert s.session_id in PENDING_GOAL_CONTINUATION

    goal_continue_event = next(item for item in events if item[0] == "goal_continue")
    assert goal_continue_event[1]["continuation_prompt"] == "continue the goal"
    assert goal_continue_event[1]["message"] == "Continuing goal"
    assert goal_continue_event[1]["message_key"] == "goal_continuing"
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "goal reply"


def test_gateway_goal_eval_keeps_runtime_active_after_success_writeback_before_done(
    tmp_path, monkeypatch
):
    """Persisted idle flags can precede terminal emission while STREAMS is still active."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"goal reply"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(
        gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse()
    )

    from api import goals as webui_goals

    goal_entered = threading.Event()
    release_goal = threading.Event()
    observed = {}

    monkeypatch.setattr(webui_goals, "has_active_goal", lambda *args, **kwargs: True)

    def blocked_goal_eval(*args, **kwargs):
        saved = models.get_session(s.session_id)
        observed["active_stream_id"] = saved.active_stream_id
        observed["pending_user_message"] = saved.pending_user_message
        observed["stream_registered"] = stream_id in STREAMS
        goal_entered.set()
        assert release_goal.wait(timeout=10)
        return {"should_continue": False, "message": "Goal checked"}

    monkeypatch.setattr(webui_goals, "evaluate_goal_after_turn", blocked_goal_eval)

    s = new_session()
    stream_id = "stream-gateway-goal-runtime-window"
    s.active_stream_id = stream_id
    s.pending_user_message = "finish it"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel

    worker = threading.Thread(
        target=gateway_chat._run_gateway_chat_streaming,
        args=(
            s.session_id,
            "finish it",
            "test-model",
            str(tmp_path),
            stream_id,
            [],
        ),
        kwargs={"goal_related": True},
        daemon=True,
    )
    worker.start()
    assert goal_entered.wait(timeout=10), "worker did not reach post-writeback goal evaluation"

    # This is the exact authority split the browser recovery must respect:
    # persisted session metadata already looks idle, while the runtime stream
    # (and therefore /api/chat/stream/status) is still active and can emit
    # goal / goal_continue / done / stream_end.
    assert observed == {
        "active_stream_id": None,
        "pending_user_message": None,
        "stream_registered": True,
    }
    assert stream_id in STREAMS
    assert worker.is_alive()
    assert not any(item[0] == "done" for item in list(subscriber.queue))

    release_goal.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    names = [item[0] for item in events]
    assert "goal" in names
    assert "done" in names
    assert "stream_end" in names
    assert names.index("goal") < names.index("done") < names.index("stream_end")


def test_gateway_chat_worker_skips_goal_judge_for_non_goal_turn(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"plain reply"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    from api import goals as webui_goals

    has_goal_calls = []
    judge_calls = []

    monkeypatch.setattr(
        webui_goals,
        "has_active_goal",
        lambda *args, **kwargs: has_goal_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        webui_goals,
        "evaluate_goal_after_turn",
        lambda *args, **kwargs: judge_calls.append((args, kwargs)),
    )

    s = new_session()
    stream_id = "stream-gateway-no-goal"
    s.active_stream_id = stream_id
    s.pending_user_message = "plain turn"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel
    PENDING_GOAL_CONTINUATION.discard(s.session_id)

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "plain turn",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=False,
    )

    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    event_names = [item[0] for item in events]

    assert "goal" not in event_names
    assert "goal_continue" not in event_names
    assert "done" in event_names
    assert "stream_end" in event_names
    assert has_goal_calls == []
    assert judge_calls == []
    assert s.session_id not in PENDING_GOAL_CONTINUATION


def test_gateway_chat_worker_normalizes_prefill_slice_before_system_prefix(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    prefill_raw = [
        {"role": "assistant", "content": "prefill summary"},
        {"role": "user", "content": "first terminal user"},
        {"role": "user", "content": "second terminal user"},
    ]

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    original_normalizer = streaming._normalize_prefill_messages_before_user_turn

    def recording_normalizer(messages):
        captured["normalizer_input"] = list(messages)
        return original_normalizer(messages)

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "loaded",
        "source": "test",
        "label": "test",
        "message_count": len(prefill_raw),
        "messages": prefill_raw,
    })
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: list(ctx["messages"]))
    monkeypatch.setattr(streaming, "_normalize_prefill_messages_before_user_turn", recording_normalizer)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    s = new_session()
    stream_id = "stream-gateway-prefill-slice-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.save()
    STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    assert captured["normalizer_input"] == prefill_raw
    payload_messages = captured["body"]["messages"]
    assert [m["role"] for m in payload_messages] == ["system", "assistant", "user"]
    assert [m["content"] for m in payload_messages[1:]] == ["prefill summary", "Say hello"]


def test_gateway_chat_worker_backfills_context_only_turns_into_display(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    s = new_session()
    s.context_messages = [
        {
            "role": "assistant",
            "content": "[context compaction] Hidden summary for model continuity.",
            "timestamp": 9.5,
        },
        {"role": "user", "content": "delete the matrix apps", "timestamp": 10.0},
        {"role": "assistant", "content": "I will verify the Matrix cleanup targets.", "timestamp": 10.1},
    ]
    s.messages = [
        {"role": "user", "content": "when done also delete tunesync", "timestamp": 11.0},
    ]
    stream_id = "stream-gateway-context-backfill-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "when done also delete tunesync"
    s.pending_attachments = []
    s.save()
    STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "when done also delete tunesync",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    saved = models.get_session(s.session_id)
    assert [m["content"] for m in saved.messages] == [
        "delete the matrix apps",
        "I will verify the Matrix cleanup targets.",
        "when done also delete tunesync",
        "done",
    ]
    assert len(saved.messages) == 4
    assert not any("context compaction" in m["content"] for m in saved.messages)


def test_gateway_chat_worker_preserves_old_visible_turns_when_context_is_compacted(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"new answer"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    s = new_session()
    old_visible_turns = [
        {"role": "user", "content": "turn one", "timestamp": 1.0},
        {"role": "assistant", "content": "answer one", "timestamp": 1.1},
        {"role": "user", "content": "turn two", "timestamp": 2.0},
        {"role": "assistant", "content": "answer two", "timestamp": 2.1},
        {"role": "user", "content": "recent turn", "timestamp": 3.0},
        {"role": "assistant", "content": "recent answer", "timestamp": 3.1},
    ]
    s.messages = old_visible_turns + [
        {"role": "user", "content": "new question", "timestamp": 4.0},
    ]
    s.context_messages = [
        {
            "role": "assistant",
            "content": "[context compaction] Hidden summary for model continuity.",
            "timestamp": 2.9,
        },
        old_visible_turns[-2],
        old_visible_turns[-1],
    ]
    stream_id = "stream-gateway-compacted-visible-preserve-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "new question"
    s.pending_attachments = []
    s.save()
    STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "new question",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    saved = models.get_session(s.session_id)
    assert [m["content"] for m in saved.messages] == [
        "turn one",
        "answer one",
        "turn two",
        "answer two",
        "recent turn",
        "recent answer",
        "new question",
        "new answer",
    ]
    assert not any("context compaction" in m["content"] for m in saved.messages)


def test_gateway_chat_worker_keeps_repeated_identical_visible_turns(tmp_path, monkeypatch):
    """#3300 regression (Codex gate): two identical visible user turns must BOTH
    survive gateway finalization even when context-only rows are backfilled.
    _message_identity ignores timestamps, so a shared identity must not let the
    backfill dedup suppress the second visible turn."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

    s = new_session()
    # Two identical visible "same" user turns surround a context-only gap that
    # only lives in context_messages (plus a hidden compaction marker).
    s.messages = [
        {"role": "user", "content": "same", "timestamp": 1.0},
        {"role": "assistant", "content": "first reply", "timestamp": 1.1},
        {"role": "user", "content": "same", "timestamp": 3.0},
        {"role": "user", "content": "new question", "timestamp": 4.0},
    ]
    s.context_messages = [
        {"role": "assistant", "content": "[context compaction] hidden", "timestamp": 0.9},
        {"role": "user", "content": "same", "timestamp": 1.0},
        {"role": "assistant", "content": "first reply", "timestamp": 1.1},
        {"role": "user", "content": "context only gap", "timestamp": 2.0},
        {"role": "user", "content": "same", "timestamp": 3.0},
    ]
    stream_id = "stream-gateway-repeated-identical-turns-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "new question"
    s.pending_attachments = []
    s.save()
    STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "new question",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    saved = models.get_session(s.session_id)
    contents = [m["content"] for m in saved.messages]
    # BOTH identical "same" visible turns must survive (the original bug dropped one).
    assert contents.count("same") == 2, contents
    # The context-only gap is backfilled into the visible transcript.
    assert "context only gap" in contents
    # The latest turn + reply are present.
    assert contents[-2:] == ["new question", "answer"]
    # No compaction marker leaks into the visible transcript.
    assert not any("context compaction" in c for c in contents)


def test_gateway_chat_worker_forwards_image_attachments_as_multimodal_parts(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(image_bytes)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"saw it"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [{"role": "user", "content": "webui session context"}])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    s = new_session()
    stream_id = "stream-gateway-image-test"
    s.active_stream_id = stream_id
    s.save()
    STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "What is in this image?",
        "test-model",
        str(tmp_path),
        stream_id,
        [{"path": str(image_path), "mime": "image/png", "is_image": True}],
    )

    content = captured["body"]["messages"][-1]["content"]
    assert captured["body"]["messages"][0]["role"] == "system"
    assert "Final visible assistant replies" in captured["body"]["messages"][0]["content"]
    image_payload = captured["body"]["messages"][1]
    assert image_payload["role"] == "user"
    assert image_payload["content"][0] == {"type": "text", "text": "What is in this image?"}
    assert content[0] == {"type": "text", "text": "What is in this image?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


# ── #4113 (salvage): _resolve_image_input_mode delegates to the agent's
# canonical decide_image_input_mode, but preserves the WebUI carve-out that
# UNKNOWN/custom models still forward images NATIVELY. ──────────────────────
#
# The agent package is not importable in the WebUI standalone test environment
# (``import agent`` raises ModuleNotFoundError), so we inject fake
# ``agent.image_routing`` / ``agent.auxiliary_client`` modules to exercise the
# real delegation branch. The fakes let us control exactly what the canonical
# router returns and what capability lookup reports, so each behaviour is
# pinned independently of models.dev data.

def _install_fake_agent_routing(monkeypatch, *, decision, supports,
                                provider="customcorp", model="mystery-9000"):
    """Inject fake agent.image_routing + agent.auxiliary_client into sys.modules.

    ``decision`` is what the canonical ``decide_image_input_mode`` returns;
    ``supports`` is what ``_lookup_supports_vision`` returns (True / False /
    None — None means "unknown / no capability data").
    """
    import sys
    import types

    img = types.ModuleType("agent.image_routing")
    img.decide_image_input_mode = lambda p, m, cfg, **kw: decision
    img._lookup_supports_vision = lambda p, m, cfg=None, **kw: supports
    aux = types.ModuleType("agent.auxiliary_client")
    aux._read_main_provider = lambda: provider
    aux._read_main_model = lambda: model
    pkg = types.ModuleType("agent")

    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.image_routing", img)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", aux)


def test_resolve_image_input_mode_unknown_model_forwards_native(monkeypatch):
    """WebUI carve-out: an UNKNOWN/custom model (no capability data) forwards
    images natively even though the canonical router conservatively returns
    ``"text"`` for it.

    This is the behaviour the gateway image-forwarding test relies on, and the
    agent's strip-and-retry guard downgrades to text on a provider rejection.
    """
    _install_fake_agent_routing(monkeypatch, decision="text", supports=None)
    cfg = {"agent": {"image_input_mode": "auto"},
           "auxiliary": {"vision": {"provider": "auto"}}}
    assert streaming._resolve_image_input_mode(cfg) == "native"


def test_resolve_image_input_mode_known_text_only_routes_text(monkeypatch):
    """NON-VACUOUS regression for #4113's real divergence.

    The OLD local re-implementation never consulted model capability, so a
    model KNOWN to lack vision (``supports_vision == False``) still got images
    embedded as native ``image_url`` parts — silently sending pixels to a model
    that cannot see them (#21160). Delegating to the canonical router fixes
    this: a known text-only model now routes through the text (vision_analyze)
    pipeline.

    Against master this assertion FAILS — the old code returns ``"native"`` for
    this exact config (auto mode, no explicit vision backend) because it ignored
    capability entirely.
    """
    _install_fake_agent_routing(monkeypatch, decision="text", supports=False)
    cfg = {"agent": {"image_input_mode": "auto"},
           "auxiliary": {"vision": {"provider": "auto"}}}
    assert streaming._resolve_image_input_mode(cfg) == "text"


def test_resolve_image_input_mode_known_vision_model_forwards_native(monkeypatch):
    """A model KNOWN to support vision forwards natively (canonical native)."""
    _install_fake_agent_routing(monkeypatch, decision="native", supports=True)
    cfg = {"agent": {"image_input_mode": "auto"}}
    assert streaming._resolve_image_input_mode(cfg) == "native"


def test_resolve_image_input_mode_explicit_text_signal_honored(monkeypatch):
    """An explicit user choice for the text pipeline is honoured even for an
    unknown model — the carve-out only fires when there is NO explicit signal.

    Both an explicit ``agent.image_input_mode: text`` and a configured
    ``auxiliary.vision`` backend count as explicit signals.
    """
    _install_fake_agent_routing(monkeypatch, decision="text", supports=None)
    assert streaming._resolve_image_input_mode(
        {"agent": {"image_input_mode": "text"}}) == "text"
    assert streaming._resolve_image_input_mode(
        {"agent": {"image_input_mode": "auto"},
         "auxiliary": {"vision": {"provider": "openai", "model": "gpt-4o"}}}) == "text"


def test_resolve_image_input_mode_fallback_when_agent_unavailable(monkeypatch):
    """When the agent package cannot be imported (standalone WebUI env), fall
    back to historical WebUI behaviour: explicit text signal wins, else native.
    """
    import sys

    # Ensure the delegation import fails: stub agent.image_routing as a module
    # that raises on attribute access of the routing fn would still import, so
    # instead force an ImportError by mapping the submodule to None.
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.image_routing", None)

    # No explicit signal -> native (this is what keeps the gateway image test
    # green, since agent is not importable there either).
    assert streaming._resolve_image_input_mode(
        {"agent": {"image_input_mode": "auto"},
         "auxiliary": {"vision": {"provider": "auto"}}}) == "native"
    # Explicit text mode -> text.
    assert streaming._resolve_image_input_mode(
        {"agent": {"image_input_mode": "text"}}) == "text"
    # Explicit auxiliary vision backend -> text.
    assert streaming._resolve_image_input_mode(
        {"auxiliary": {"vision": {"provider": "anthropic"}}}) == "text"


def test_gateway_use_runs_api_is_default_off():
    for env in ({}, {"HERMES_WEBUI_GATEWAY_USE_RUNS_API": ""}):
        assert _gateway_use_runs_api_enabled({}, env) is False


def test_gateway_use_runs_api_only_accepts_explicit_truthy_values():
    for value in ("1", "true", "yes", "on", " True ", " ON "):
        assert _gateway_use_runs_api_enabled({}, {"HERMES_WEBUI_GATEWAY_USE_RUNS_API": value}) is True


def test_gateway_use_runs_api_rejects_generic_truthy_strings():
    for value in ("enabled", "gateway", "api_server", "absolutely"):
        assert _gateway_use_runs_api_enabled({}, {"HERMES_WEBUI_GATEWAY_USE_RUNS_API": value}) is False


def test_gateway_use_runs_api_can_be_enabled_from_config():
    assert _gateway_use_runs_api_enabled({"webui_gateway_use_runs_api": "true"}, {}) is True
    assert _gateway_use_runs_api_enabled({"webui_gateway_use_runs_api": "1"}, {}) is True


def test_gateway_use_runs_api_env_wins_over_config():
    assert _gateway_use_runs_api_enabled(
        {"webui_gateway_use_runs_api": "true"},
        {"HERMES_WEBUI_GATEWAY_USE_RUNS_API": "false"},
    ) is False


def test_gateway_runs_api_body_includes_session_id():
    """#4535: the runs API body must carry session_id so the agent reuses the
    browser session instead of creating a fresh run_<uuid> per message."""
    from unittest.mock import patch, MagicMock
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    captured = {}
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)
    stream_id = "sid-runs-session-id"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    call_count = [0]

    def fake_urlopen(req, *, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["url"] = req.full_url
            resp = MagicMock()
            resp.read = lambda sz=65536: json.dumps({"run_id": "run_abc"}).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp
        resp = MagicMock()
        resp.__iter__ = lambda s: iter([
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b'data: [DONE]\n',
        ])
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    import os
    env = {k: v for k, v in os.environ.items()}
    env["HERMES_WEBUI_CHAT_BACKEND"] = "gateway"
    env["HERMES_WEBUI_GATEWAY_USE_RUNS_API"] = "1"
    env["HERMES_WEBUI_GATEWAY_BASE_URL"] = "http://gateway.local"

    try:
        with patch.dict("os.environ", env, clear=True):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=True), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=MagicMock(
                     active_stream_id=stream_id, workspace="/tmp",
                     profile=None, context_messages=[], messages=[],
                 )):
                _run_gateway_chat_streaming(
                    session_id="sess-stable-uuid",
                    msg_text="hi",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
        assert "/v1/runs" in captured["url"]
        assert captured["body"]["session_id"] == "sess-stable-uuid"
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)


def test_gateway_runs_api_classifies_terminal_provider_error(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    error_text = "HTTP 400: Invalid model format or no credentials for provider"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)
    stream_id = "sid-runs-terminal-provider-error"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    call_count = [0]

    def fake_urlopen(req, *, timeout=None):
        call_count[0] += 1
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        if call_count[0] == 1:
            resp.read = lambda sz=65536: b'{"run_id":"run_error"}'
        else:
            resp.__iter__ = lambda s: iter([
                b'event: run.failed\n',
                f'data: {{"error":{json.dumps(error_text)}}}\n'.encode(),
            ])
        return resp

    try:
        s = new_session()
        s.active_stream_id = stream_id
        s.pending_user_message = "hi"
        s.pending_attachments = []
        s.save()
        with patch.dict("os.environ", {
            "HERMES_WEBUI_CHAT_BACKEND": "gateway",
            "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1",
            "HERMES_WEBUI_GATEWAY_BASE_URL": "http://gateway.local",
        }, clear=True), \
             patch("api.gateway_chat.gateway_supports_approval", return_value=True), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _run_gateway_chat_streaming(
                session_id=s.session_id,
                msg_text="hi",
                model="test",
                workspace="/tmp",
                stream_id=stream_id,
            )
        apperrors = [item[1] for item in events if item[0] == "apperror"]
        assert apperrors[-1]["type"] in {"model_not_found", "auth_mismatch"}
        assert apperrors[-1]["session_id"] == s.session_id
        saved = models.get_session(s.session_id)
        assert [message.get("role") for message in saved.messages[-2:]] == ["user", "assistant"]
        assert saved.messages[-1]["_error"] is True
        assert apperrors[-1]["session"]["messages"][-1]["_error"] is True
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)


def test_gateway_worker_skips_runs_api_when_opt_in_absent():
    """Worker uses chat/completions even when gateway advertises approval support, unless opt-in is set."""
    from unittest.mock import patch, MagicMock
    from api.config import STREAMS, STREAMS_LOCK
    from api.gateway_chat import _run_gateway_chat_streaming

    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)
    stream_id = "sid-optin-gate"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    sse_body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'

    def fake_urlopen(req, *, timeout=None):
        assert "/v1/chat/completions" in req.full_url
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(sse_body.split(b"\n"))
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    import os
    env_override = {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}
    env_without_opt_in = {
        k: v for k, v in os.environ.items()
        if k != "HERMES_WEBUI_GATEWAY_USE_RUNS_API"
    }
    env_without_opt_in.update(env_override)

    try:
        with patch.dict("os.environ", env_without_opt_in, clear=True):
            with patch("api.gateway_chat.gateway_supports_approval", return_value=True), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.gateway_chat.get_session", return_value=MagicMock(
                     active_stream_id=stream_id, workspace="/tmp",
                     profile=None, context_messages=[], messages=[],
                 )):
                _run_gateway_chat_streaming(
                    session_id="sess-optin",
                    msg_text="hi",
                    model="test",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
        event_types = [e[0] for e in events if isinstance(e, tuple) and len(e) >= 2]
        assert "token" in event_types, "expected a token event from chat/completions path"
        assert "apperror" not in event_types, "runs API path fired unexpectedly"
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)


def test_gateway_chat_401_hint_reports_the_session_profile_key_not_the_process_key(tmp_path, monkeypatch):
    """A 401 on a profile's request must be explained by the key that request carried.

    The worker now resolves the endpoint (URL and key) from the session's profile. The
    HTTP error path used to ask the process environment whether a key was configured,
    so a profile with its own key was told "set HERMES_WEBUI_GATEWAY_API_KEY" (and the
    inverse case claimed a mismatch when the profile sent no key at all).
    """
    import io
    from unittest.mock import MagicMock

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    # The process-wide environment has NO key; the 'coder' profile has one.
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)     # the suite may set one; tests that need it set it themselves
    monkeypatch.setattr(
        gateway_chat, "_gateway_endpoint_for_profile",
        lambda profile_name: ("http://gateway.local/p/coder", "coder-key"),
    )
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", Message(), io.BytesIO(b"{}"))

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    events = []
    channel = MagicMock()
    channel.put_nowait = events.append
    s = new_session()
    s.profile = "coder"
    stream_id = "stream-gateway-401-profile-key-hint-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hi"
    s.pending_started_at = 444
    s.save()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(s.session_id, "Say hi", "m", str(tmp_path), stream_id, [])

    blob = repr(events)
    assert "gateway_auth_error" in blob, "the 401 was not classified as an auth error"
    # The profile DID send a key, so the hint is the "check it matches" one, never "Set ...".
    assert "Check that HERMES_WEBUI_GATEWAY_API_KEY matches" in blob
    assert "Set HERMES_WEBUI_GATEWAY_API_KEY to the same value" not in blob


def _profile_homes(tmp_path, monkeypatch, envs, config_url="${GW_ROUTE}"):
    """Throwaway profile homes whose config.yaml names the gateway URL by variable; the real resolver runs."""
    import api.profiles as profiles

    homes = {}
    for name in envs:
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            ('webui_gateway_base_url: "%s"\n' % config_url) if config_url else "{}\n", encoding="utf-8")
        homes[name] = home
    # The root home ("" / "default") and, unless a test says otherwise, the process profile: these fixtures model
    # a WebUI process running on the root profile, so values in os.environ that the loader recorded came from the
    # default profile's .env. Tests of a NAMED process profile use _root_is_the_process_profile(process=...) or
    # _mirror_named_process_profile().
    root = homes.get("default") or tmp_path / "_root"
    root.mkdir(exist_ok=True)
    by_name = {**homes, "": root}
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda n: by_name[str(n)])
    monkeypatch.setattr(profiles, "get_process_profile_home", lambda: root)
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda home: dict(envs.get(Path(home).name, {})))
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    # GW_ROUTE reaches the process env only by way of the active profile's .env, which is what this set records.
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set(getattr(profiles, "_loaded_profile_env_keys", ())) | {"GW_ROUTE"})
    return homes


def test_gateway_endpoint_expands_url_variable_from_the_session_profile_env(tmp_path, monkeypatch):
    envs = {
        "alpha": {"GW_ROUTE": "http://gw.local/p/alpha", "API_SERVER_KEY": "alpha-key"},
        "beta": {"GW_ROUTE": "http://gw.local/p/beta", "API_SERVER_KEY": "beta-key"},
    }
    _profile_homes(tmp_path, monkeypatch, envs)
    monkeypatch.setenv("GW_ROUTE", "http://gw.local/p/PROCESS")        # the process-active profile's value

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/p/beta", "beta-key")
    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/p/alpha", "alpha-key")


def test_gateway_endpoint_never_uses_the_process_env_for_the_url_variable(tmp_path, monkeypatch):
    envs = {"beta": {"API_SERVER_KEY": "beta-key"}}                    # beta defines no GW_ROUTE
    _profile_homes(tmp_path, monkeypatch, envs)
    monkeypatch.setenv("GW_ROUTE", "http://gw.local/p/PROCESS")

    with pytest.raises(RuntimeError, match="unresolved variable"):
        gateway_chat._gateway_endpoint_for_profile("beta")


def test_gateway_endpoint_restores_the_thread_env_state(tmp_path, monkeypatch):
    from api.config import _thread_ctx

    envs = {"beta": {"GW_ROUTE": "http://gw.local/p/beta", "API_SERVER_KEY": "k"}}
    _profile_homes(tmp_path, monkeypatch, envs)
    before_env = getattr(_thread_ctx, "env", {})
    before_block = getattr(_thread_ctx, "block_process_env_fallback", False)
    gateway_chat._gateway_endpoint_for_profile("beta")
    assert getattr(_thread_ctx, "env", {}) == before_env
    assert getattr(_thread_ctx, "block_process_env_fallback", False) == before_block


def test_gateway_endpoint_keeps_the_shared_pair_for_a_profile_with_no_transport_overrides(tmp_path, monkeypatch):
    """A globally exported URL plus API_SERVER_KEY only in the default profile's .env (so the loader recorded and
    would strip it) must still authenticate a named profile that sets nothing of its own: the pair master sends."""
    import api.profiles as profiles

    _profile_homes(tmp_path, monkeypatch, {"beta": {"TERMINAL_CWD": "/work"}}, config_url=None)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gw.local:8642")
    monkeypatch.setenv("API_SERVER_KEY", "shared-root-key")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"API_SERVER_KEY"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "shared-root-key")


def test_gateway_endpoint_reads_the_active_profiles_env_fresh_not_the_cached_config(tmp_path, monkeypatch):
    """Changing the active profile's .env from route A to route B must change the URL with the key, even though
    the ambient config cache was expanded earlier."""
    import api.profiles as profiles

    envs = {"alpha": {"GW_ROUTE": "http://gw.local/p/A", "API_SERVER_KEY": "key-A"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: homes["alpha"])   # alpha is the ambient profile
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(homes["alpha"] / "config.yaml"))     # the suite pins its own; point it at alpha's

    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/p/A", "key-A")
    envs["alpha"] = {"GW_ROUTE": "http://gw.local/p/B", "API_SERVER_KEY": "key-B"}
    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/p/B", "key-B")


def _authoritative_config(path, url="http://gw.local/authoritative"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('webui_gateway_base_url: "%s"\n' % url, encoding="utf-8")
    return path


def test_gateway_endpoint_honours_an_external_directory_hermes_config_path_for_the_active_profile(tmp_path, monkeypatch):
    import api.profiles as profiles

    envs = {"alpha": {"API_SERVER_KEY": "alpha-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url="http://gw.local/WRONG")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: homes["alpha"])
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_authoritative_config(tmp_path / "elsewhere" / "custom.yaml")))

    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/authoritative", "alpha-key")


def test_gateway_endpoint_honours_a_same_home_custom_config_filename(tmp_path, monkeypatch):
    import api.profiles as profiles

    envs = {"alpha": {"API_SERVER_KEY": "alpha-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url="http://gw.local/WRONG")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: homes["alpha"])
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_authoritative_config(homes["alpha"] / "custom.yaml")))

    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/authoritative", "alpha-key")


def test_gateway_endpoint_does_not_let_a_divergent_profile_inherit_the_active_custom_config(tmp_path, monkeypatch):
    import api.profiles as profiles

    envs = {"alpha": {"API_SERVER_KEY": "alpha-key"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url="http://gw.local/own")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: homes["alpha"])
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_authoritative_config(tmp_path / "elsewhere" / "custom.yaml")))

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/own", "beta-key")
    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/authoritative", "alpha-key")


def _shared_gateway(monkeypatch, url="http://gw.local:8642", key="shared-root-key", launcher_key=None):
    """The process pair: the URL from the launcher, the key from the default profile's .env (so the loader recorded it)."""
    import api.profiles as profiles

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", url)
    monkeypatch.setenv("API_SERVER_KEY", key)
    if launcher_key:
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", launcher_key)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"API_SERVER_KEY", "GW_ROUTE"})


def test_gateway_endpoint_a_profile_with_the_same_url_and_no_key_keeps_the_shared_key(tmp_path, monkeypatch):
    _profile_homes(tmp_path, monkeypatch, {"beta": {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local:8642"}}, config_url=None)
    _shared_gateway(monkeypatch)

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "shared-root-key")


def test_gateway_endpoint_a_key_only_profile_keeps_the_shared_url(tmp_path, monkeypatch):
    import api.profiles as profiles

    _profile_homes(tmp_path, monkeypatch, {"beta": {"API_SERVER_KEY": "beta-key"}}, config_url=None)
    _shared_gateway(monkeypatch)
    # the shared URL comes from the default profile's .env too, so the loader recorded it and a profile copy strips it
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"API_SERVER_KEY", "GW_ROUTE", "HERMES_WEBUI_GATEWAY_BASE_URL"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "beta-key")


def test_gateway_endpoint_the_launcher_alias_never_outranks_the_profiles_own_key(tmp_path, monkeypatch):
    envs = {"beta": {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/p/beta", "API_SERVER_KEY": "beta-key"}}
    _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _shared_gateway(monkeypatch, launcher_key="launcher-key")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/p/beta", "beta-key")


def test_gateway_endpoint_the_shared_key_is_never_sent_to_a_different_gateway(tmp_path, monkeypatch):
    _profile_homes(tmp_path, monkeypatch, {"beta": {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/p/beta"}}, config_url=None)
    _shared_gateway(monkeypatch, launcher_key="launcher-key")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/p/beta", "")


def test_gateway_endpoint_the_launcher_alias_still_outranks_a_profiles_api_server_key_on_the_shared_url(tmp_path, monkeypatch):
    """On the SHARED URL the launcher's explicit alias is the key for that Gateway (docs: HERMES_WEBUI_GATEWAY_API_KEY
    first, API_SERVER_KEY as the fallback); a profile's own API_SERVER_KEY is the key of ITS OWN Gateway and must
    not replace it there. This includes the default profile, i.e. the single-profile deployment."""
    envs = {"beta": {"API_SERVER_KEY": "beta-local-key"}, "default": {"API_SERVER_KEY": "local-key"}}
    _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _shared_gateway(monkeypatch, launcher_key="launcher-key")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "launcher-key")
    assert gateway_chat._gateway_endpoint_for_profile("default") == ("http://gw.local:8642", "launcher-key")


def _root_is_the_process_profile(monkeypatch, homes, process="default"):
    import api.profiles as profiles

    named = {**homes, "": homes["default"]}
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda n: named[str(n)])
    monkeypatch.setattr(profiles, "get_process_profile_home", lambda: homes[process])


def test_gateway_endpoint_an_alias_from_the_root_env_is_the_shared_alias_for_a_named_profile(tmp_path, monkeypatch):
    """The process profile is the root one, so HERMES_WEBUI_GATEWAY_API_KEY in os.environ came from ~/.hermes/.env
    (the loader recorded it) and is the shared alias; a named profile carrying a cloned API_SERVER_KEY must not
    replace it on the shared URL."""
    import api.profiles as profiles

    envs = {"beta": {"API_SERVER_KEY": "local-key"}, "default": {"API_SERVER_KEY": "local-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _shared_gateway(monkeypatch, launcher_key="root-alias")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"API_SERVER_KEY", "HERMES_WEBUI_GATEWAY_API_KEY"})
    _root_is_the_process_profile(monkeypatch, homes)

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "root-alias")
    assert gateway_chat._gateway_endpoint_for_profile("default") == ("http://gw.local:8642", "root-alias")


def test_gateway_endpoint_an_alias_mirrored_from_a_named_active_profile_is_not_shared(tmp_path, monkeypatch):
    import api.profiles as profiles

    envs = {"beta": {"API_SERVER_KEY": "beta-local-key"}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"},
            "default": {}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _shared_gateway(monkeypatch, launcher_key="alpha-alias")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"API_SERVER_KEY", "HERMES_WEBUI_GATEWAY_API_KEY"})
    _root_is_the_process_profile(monkeypatch, homes, process="alpha")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "beta-local-key")


def test_gateway_endpoint_a_profiles_own_alias_still_wins_on_the_shared_url(tmp_path, monkeypatch):
    envs = {"beta": {"HERMES_WEBUI_GATEWAY_API_KEY": "beta-alias", "API_SERVER_KEY": "beta-local-key"}}
    _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _shared_gateway(monkeypatch, launcher_key="launcher-key")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "beta-alias")


# ── #8432: never pair a profile's key with a URL the NAMED process profile may own ──────────────────────────────


def _mirror_named_process_profile(monkeypatch, homes, envs, process="alpha"):
    """Run the REAL loader for a named process profile: _reload_dotenv mirrors its .env into os.environ and records
    the keys, exactly as init_profile_state() does at startup on a sticky named profile."""
    import api.profiles as profiles

    proc_home = homes[process]
    (proc_home / ".env").write_text("".join(f"{k}={v}\n" for k, v in envs[process].items()), encoding="utf-8")
    for key in envs[process]:
        monkeypatch.setenv(key, "placeholder")   # registers the key so monkeypatch removes what the loader sets
        monkeypatch.delenv(key)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", "")
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {})
    profiles._reload_dotenv(proc_home)
    assert set(envs[process]) <= profiles._loaded_profile_env_keys
    monkeypatch.setattr(profiles, "get_process_profile_home", lambda: proc_home)


_ALPHA_GW = {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/alpha", "HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}


def test_8432_a_key_only_profile_never_sends_its_key_to_the_named_process_profiles_gateway(tmp_path, monkeypatch):
    envs = {"default": {"API_SERVER_KEY": "root-key"}, "alpha": dict(_ALPHA_GW), "beta": {"API_SERVER_KEY": "beta-key"},
            "gamma": {"HERMES_WEBUI_GATEWAY_API_KEY": "gamma-alias"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)

    # the process pair, exactly what v0.52.492 sends for every fresh turn: URL and key from one source
    for profile in ("beta", "gamma", "default", ""):
        assert gateway_chat._gateway_endpoint_for_profile(profile) == ("http://gw.local/alpha", "alpha-alias")


def test_8432_the_process_profile_and_settings_free_profiles_are_unchanged(tmp_path, monkeypatch):
    envs = {"default": {}, "alpha": dict(_ALPHA_GW), "delta": {}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)

    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/alpha", "alpha-alias")
    assert gateway_chat._gateway_endpoint_for_profile("delta") == ("http://gw.local/alpha", "alpha-alias")


def test_8432_the_process_profile_still_reads_its_own_env_fresh(tmp_path, monkeypatch):
    """The guard never touches the process profile's own sessions. Here the process profile has a key but no URL and
    the URL may be its own (a named launch home preloaded it), the one shape the guard would otherwise cover: a key
    rotated in its .env after startup is still used (master reads the .env fresh), not the stale mirrored value."""
    import api.profiles as profiles

    envs = {"default": {}, "alpha": {"API_SERVER_KEY": "old-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gw.local/alpha-preloaded")
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes["alpha"]))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV",
                        {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/alpha-preloaded"})
    envs["alpha"]["API_SERVER_KEY"] = "rotated-key"

    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/alpha-preloaded", "rotated-key")


def test_8432_a_url_from_the_named_process_profiles_config_is_not_shared(tmp_path, monkeypatch):
    import api.config as config
    import api.profiles as profiles

    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    (homes["alpha"] / "config.yaml").write_text('webui_gateway_base_url: "http://gw.local/alpha-cfg"\n', encoding="utf-8")
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: homes["alpha"])
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(homes["alpha"] / "config.yaml"))
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gw.local/alpha-cfg"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/alpha-cfg", "alpha-alias")
    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/alpha-cfg", "alpha-alias")


def test_8432_a_launcher_exported_url_stays_shared_under_a_named_process_profile(tmp_path, monkeypatch):
    """The launcher exported the URL (not mirrored, no named launch home): a key-only profile keeps the documented
    shared URL + own key, as before."""
    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gw.local:8642")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local:8642", "beta-key")


def test_8432_a_url_a_shell_launcher_preloaded_from_a_named_profile_home_is_not_shared(tmp_path, monkeypatch):
    """ctl.sh sources $HERMES_HOME/.env before Python starts; with HERMES_HOME = <base>/profiles/alpha that URL sits
    in os.environ unmirrored, but it is alpha's."""
    import api.profiles as profiles

    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gw.local/alpha-preloaded")
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes["alpha"]))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV",
                        {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/alpha-preloaded"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/alpha-preloaded", "alpha-alias")


def test_8432_nothing_configured_keeps_the_default_url_with_the_profiles_own_key(tmp_path, monkeypatch):
    envs = {"default": {}, "alpha": {"TERMINAL_CWD": "/work"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://127.0.0.1:8642", "beta-key")


def test_8432_round_review_shapes_keep_masters_pairs(tmp_path, monkeypatch):
    """Shapes the round-1/round-2 reviews reproduced as 401s: the minimal guard leaves all of them on master's pair."""
    # (a) URL in the named process profile's .env, launcher exports only the key: alpha authenticates
    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/alpha"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "launcher-key")
    assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://gw.local/alpha", "launcher-key")
    assert gateway_chat._gateway_endpoint_for_profile("default") == ("http://gw.local/alpha", "launcher-key")


def test_8432_a_profile_that_names_the_same_url_itself_keeps_masters_key_order(tmp_path, monkeypatch):
    envs = {"default": {}, "alpha": dict(_ALPHA_GW), "beta": {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/alpha"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)

    # beta configured that URL itself: master's shared-URL key order (here the shared key) is unchanged
    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/alpha", "alpha-alias")


@pytest.mark.parametrize("alpha_env", [
    dict(_ALPHA_GW),
    {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/alpha", "API_SERVER_KEY": "alpha-key"},
    {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/alpha"},
])
@pytest.mark.parametrize("launcher_key", [None, "launcher-key"])
def test_8432_invariant_no_other_profiles_key_reaches_the_process_profiles_url(tmp_path, monkeypatch, alpha_env, launcher_key):
    others = {"beta": {"API_SERVER_KEY": "beta-key"}, "gamma": {"HERMES_WEBUI_GATEWAY_API_KEY": "gamma-alias"},
              "eps": {"HERMES_WEBUI_GATEWAY_API_KEY": "eps-alias", "API_SERVER_KEY": "eps-key"},
              "default": {"API_SERVER_KEY": "root-key"}}
    envs = {"alpha": dict(alpha_env), **others}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    if launcher_key:
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", launcher_key)
    foreign = {v for env in others.values() for v in env.values()}

    for profile in (*others, ""):
        url, key = gateway_chat._gateway_endpoint_for_profile(profile)
        assert not (url == "http://gw.local/alpha" and key in foreign), (profile, url, key)


def test_8432_a_launcher_url_under_a_named_launch_home_stays_shared(tmp_path, monkeypatch):
    """HERMES_HOME names alpha's home, but the URL is the launcher's own export (alpha's .env never set it): it is
    shared, so beta keeps master's pair (that URL with beta's own key), never alpha's local key (Codex r3, 200->401)."""
    import api.profiles as profiles

    envs = {"default": {}, "alpha": {"API_SERVER_KEY": "alpha-local-key"},
            "beta": {"HERMES_WEBUI_GATEWAY_API_KEY": "beta-remote-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "https://gw.example/remote")
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(homes["alpha"]))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {"API_SERVER_KEY": "alpha-local-key"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("https://gw.example/remote", "beta-remote-key")


def test_8432_a_url_the_root_launch_home_preloaded_is_shared(tmp_path, monkeypatch):
    import api.profiles as profiles

    envs = {"default": {}, "alpha": {"API_SERVER_KEY": "alpha-local-key"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gw.local/root")
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(profiles.get_hermes_home_for_profile("")))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://gw.local/root"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/root", "beta-key")


def _operator_config(tmp_path, monkeypatch, url):
    import os

    import api.config as config

    operator = tmp_path / "etc" / "operator.yaml"
    operator.parent.mkdir(exist_ok=True)
    operator.write_text(f'webui_gateway_base_url: "{url}"\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(operator))
    expanded = url.replace("${GW_HOST}", os.environ.get("GW_HOST", "${GW_HOST}"))
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": expanded})
    return operator


def test_8432_an_operator_config_outside_every_profile_home_is_shared(tmp_path, monkeypatch):
    """A launcher-provided HERMES_CONFIG_PATH outside every profile home is operator configuration: beta keeps
    master's pair, its own key on that URL (Codex r3, 200->401)."""
    envs = {"default": {}, "alpha": {"API_SERVER_KEY": "alpha-local-key"},
            "beta": {"HERMES_WEBUI_GATEWAY_API_KEY": "beta-remote-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    _operator_config(tmp_path, monkeypatch, "https://gw.example/operator")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("https://gw.example/operator", "beta-remote-key")


def test_8432_an_operator_config_url_filled_from_a_profile_env_is_not_shared(tmp_path, monkeypatch):
    envs = {"default": {}, "alpha": {"GW_HOST": "alpha-gw.local", "HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"},
            "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    _operator_config(tmp_path, monkeypatch, "http://${GW_HOST}:8642")

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://alpha-gw.local:8642", "alpha-alias")


def test_8432_a_config_path_a_profile_env_chose_is_not_shared(tmp_path, monkeypatch):
    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    operator = tmp_path / "etc" / "alpha-chosen.yaml"
    envs["alpha"]["HERMES_CONFIG_PATH"] = str(operator)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    _operator_config(tmp_path, monkeypatch, "http://gw.local/alpha-chosen")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(operator))

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/alpha-chosen", "alpha-alias")


def test_8432_the_process_profiles_own_config_url_without_a_config_path_is_not_shared(tmp_path, monkeypatch):
    """No HERMES_CONFIG_PATH: the ambient config IS the named process profile's own config.yaml."""
    import api.config as config

    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gw.local/alpha-own-cfg"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/alpha-own-cfg", "alpha-alias")


def test_8432_an_operator_config_under_some_other_profiles_dir_is_shared(tmp_path, monkeypatch):
    """Codex r4: an operator file at /operator/profiles/shared-gateway/config.yaml is outside the Hermes profiles root;
    a directory merely NAMED "profiles" does not make it a profile's (master 200, a name heuristic gave 401)."""
    import api.config as config

    envs = {"default": {}, "alpha": {"API_SERVER_KEY": "alpha-local-key"},
            "beta": {"HERMES_WEBUI_GATEWAY_API_KEY": "beta-remote-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    operator = tmp_path / "operator" / "profiles" / "shared-gateway" / "config.yaml"
    operator.parent.mkdir(parents=True)
    operator.write_text('webui_gateway_base_url: "https://gw.example/operator"\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(operator))
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "https://gw.example/operator"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("https://gw.example/operator", "beta-remote-key")


def test_8432_a_config_inside_another_named_profile_home_is_not_shared(tmp_path, monkeypatch):
    """A HERMES_CONFIG_PATH inside <root>/profiles/<name> is that profile's configuration, not operator config."""
    import api.config as config

    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    gamma_cfg = homes["default"] / "profiles" / "gamma" / "config.yaml"
    gamma_cfg.parent.mkdir(parents=True)
    gamma_cfg.write_text('webui_gateway_base_url: "http://gw.local/gamma-cfg"\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(gamma_cfg))
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gw.local/gamma-cfg"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/gamma-cfg", "alpha-alias")


def test_8432_a_config_in_a_subdirectory_of_the_process_home_is_not_shared(tmp_path, monkeypatch):
    """Senior r4 S26: a HERMES_CONFIG_PATH in a subdirectory of the named process profile's own home is that profile's
    configuration, whatever the layout (here the home lies outside <root>/profiles, as in isolated/custom homes)."""
    import api.config as config

    envs = {"default": {}, "alpha": {"HERMES_WEBUI_GATEWAY_API_KEY": "alpha-alias"}, "beta": {"API_SERVER_KEY": "beta-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    sub_cfg = homes["alpha"] / "conf" / "webui.yaml"
    sub_cfg.parent.mkdir()
    sub_cfg.write_text('webui_gateway_base_url: "http://gw.local/alpha-sub"\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(sub_cfg))
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gw.local/alpha-sub"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("http://gw.local/alpha-sub", "alpha-alias")


def test_8432_an_operator_config_directly_in_the_profiles_container_is_shared(tmp_path, monkeypatch):
    """Senior r5: ~/.hermes/profiles/shared.yaml sits IN the profiles container, not inside any profile's directory,
    so it is operator configuration (master 200; `is_relative_to` treated it as profile-owned and gave 401)."""
    import api.config as config

    envs = {"default": {}, "alpha": {"API_SERVER_KEY": "alpha-local-key"},
            "beta": {"HERMES_WEBUI_GATEWAY_API_KEY": "beta-remote-key"}}
    homes = _profile_homes(tmp_path, monkeypatch, envs, config_url=None)
    _mirror_named_process_profile(monkeypatch, homes, envs)
    shared = homes["default"] / "profiles" / "shared.yaml"
    shared.parent.mkdir(parents=True)
    shared.write_text('webui_gateway_base_url: "https://gw.example/shared"\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(shared))
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "https://gw.example/shared"})

    assert gateway_chat._gateway_endpoint_for_profile("beta") == ("https://gw.example/shared", "beta-remote-key")

