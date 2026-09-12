from collections import OrderedDict
import base64
from email.message import Message
import json
from pathlib import Path
import re
import time
import urllib.error

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

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
    )

    saved = models.get_session(s.session_id)
    assert [m["role"] for m in saved.messages] == ["user", "assistant"]
    assert saved.messages[-1]["content"] == "hello"
    assert isinstance(saved.messages[0]["timestamp"], float)
    assert isinstance(saved.messages[1]["timestamp"], float)
    assert saved.messages[0]["timestamp"] < saved.messages[1]["timestamp"]
    assert saved.active_stream_id is None
    # Provider-reported usage must reach the session record; it persisted 0 for
    # every gateway-backed session before this. last_prompt_tokens stays unset:
    # gateway usage is summed across the turn's API calls, so it is a billing
    # total, not the context size ui.js needs for the gauge (#1436).
    assert saved.input_tokens == 4
    assert saved.output_tokens == 2
    # The fixture is a TOOL turn, so the gate deliberately leaves the context
    # numerator unset: gateway usage is summed across the turn's API calls and
    # would over-report the prompt size. Tool-free turns do set it.
    assert not getattr(saved, "last_prompt_tokens", 0)
    assert stream_id not in STREAMS
    assert captured["url"] == "http://gateway.local/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["X-hermes-session-id"] == s.session_id
    assert captured["headers"]["X-hermes-session-key"] == f"webui:{s.session_id}"
    assert '"stream": true' in captured["body"]
    payload = json.loads(captured["body"])
    assert payload["reasoning_effort"] == "high"
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
        "test-model",
        str(tmp_path),
        stream_id,
        [],
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


def test_gateway_stream_usage_survives_junk_and_overflow():
    """A bad provider token count must not cost the turn its transcript.

    int() on a string/dict/overflowing usage field used to raise inside the SSE
    read loop, where the outer `except Exception` converted it into "Gateway
    request failed" and discarded everything the turn had streamed. Each junk
    key must be skipped, and a good key later in the same chunk still wins.
    """
    from api.gateway_chat import _gateway_stream_usage

    # Junk of every shape the wire can carry, including 1e999 -> inf.
    assert _gateway_stream_usage({"usage": {"prompt_tokens": "not-a-number"}}) == {
        "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0,
    }
    assert _gateway_stream_usage({"usage": {"prompt_tokens": {"nested": 1}}})["input_tokens"] == 0
    assert _gateway_stream_usage({"usage": {"prompt_tokens": 1e999}})["input_tokens"] == 0
    assert _gateway_stream_usage({"usage": {"prompt_tokens": float("nan")}})["input_tokens"] == 0
    # Junk in the first key must fall through to the good alias, not abort.
    assert _gateway_stream_usage(
        {"usage": {"prompt_tokens": "junk", "input_tokens": 25546}}
    )["input_tokens"] == 25546
    # A non-numeric cost must not poison the whole dict either.
    assert _gateway_stream_usage(
        {"usage": {"prompt_tokens": 7, "estimated_cost": "free"}}
    ) == {"input_tokens": 7, "output_tokens": 0, "estimated_cost": 0}
    # Hermes gateway extras appear only when sent, so an older gateway keeps the
    # legacy three-key shape and cannot zero a good session value.
    assert "last_prompt_tokens" not in _gateway_stream_usage({"usage": {"prompt_tokens": 7}})
    _rich = _gateway_stream_usage(
        {"usage": {"prompt_tokens": 7, "last_prompt_tokens": 26194, "threshold_tokens": 750000}}
    )
    assert _rich["last_prompt_tokens"] == 26194
    assert _rich["threshold_tokens"] == 750000
    # Junk in an extra must be dropped, not raised and not written as garbage.
    assert "threshold_tokens" not in _gateway_stream_usage(
        {"usage": {"prompt_tokens": 7, "threshold_tokens": "lots"}}
    )


def _run_gateway_turn(tmp_path, monkeypatch, session, usage_json, *, model="test-model", out=None):
    """Drive one gateway turn against a canned terminal usage block.

    Returns the request body the worker actually sent, so the provider-prefix
    strip can be asserted from the wire rather than from an internal. Pass a
    dict as `out` to also get the "done" SSE event's `usage` payload back via
    out["done_usage"] - what static/ui.js's context ring actually reads on a
    session's first turn, before the browser has any cached session to
    backfill from (see the comment above the done-event backfill block in
    api/gateway_chat.py).
    """
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}],"usage":' + usage_json.encode() + b'}\n\n'
            yield b'data: [DONE]\n\n'

    def fake_urlopen(req, timeout=0):
        captured["body"] = req.data.decode("utf-8")
        return FakeResponse()

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "not_configured", "source": "none", "label": "",
        "message_count": 0, "messages": [],
    })
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    stream_id = f"stream-{id(usage_json)}-{len(session.messages)}"
    session.active_stream_id = stream_id
    session.pending_user_message = "hi"
    session.pending_attachments = []
    session.pending_started_at = 123
    session.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        session.session_id, "hi", model, str(tmp_path), stream_id, [],
    )
    if out is not None:
        out["done_usage"] = None
        while not subscriber.empty():
            item = subscriber.get_nowait()
            event, data = item[0], item[1]
            if event == "done":
                out["done_usage"] = (data or {}).get("usage")
    return captured.get("body", "")


def test_gateway_absent_last_prompt_tokens_never_inherits_the_billing_total(tmp_path, monkeypatch):
    """An older gateway that never sends last_prompt_tokens must leave the
    context-ring numerator alone across turns - never let it drift to the
    cumulative billing total (regression #1436 originally introduced
    last_prompt_tokens to stop this exact failure mode).

    This used to be guarded by a tool-free heuristic keyed on
    STREAM_LIVE_TOOL_CALLS being empty, which a maintainer re-gate flagged as
    unreliable: an empty list only proves no tool-progress events were
    *received*, not that no tools ran, on a gateway/proxy that omits those
    optional events (defect #3). The fix removes the guess entirely - absent
    means untouched, full stop.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    s = new_session()
    # No last_prompt_tokens on the wire at all: an older gateway.
    _run_gateway_turn(tmp_path, monkeypatch, s, '{"prompt_tokens":1000,"completion_tokens":10}')
    saved = models.get_session(s.session_id)
    assert saved.last_prompt_tokens is None, "no wire signal means no numerator, not a guess"
    assert saved.input_tokens == 1000, "the billing total still accumulates independently"

    _run_gateway_turn(tmp_path, monkeypatch, saved, '{"prompt_tokens":1200,"completion_tokens":10}')
    saved = models.get_session(s.session_id)
    assert saved.input_tokens == 2200, "billing total keeps accumulating"
    assert saved.last_prompt_tokens is None, (
        "must never silently become the 2200 lifetime sum just because two "
        "tool-free-looking turns went by"
    )


def test_gateway_persists_the_cache_token_split_across_turns(tmp_path, monkeypatch):
    """cache_read/cache_write are parsed, so they must also be persisted.

    static/messages.js:6055/6134 derives the per-turn cache badge by
    subtracting the pre-turn session totals, exactly as it does for
    input/output - so these belong in the same cumulative accumulation, or a
    reload shows zero.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    s = new_session()
    usage = '{"prompt_tokens":10,"completion_tokens":2,"cache_read_tokens":300,"cache_write_tokens":40}'
    _run_gateway_turn(tmp_path, monkeypatch, s, usage)
    _run_gateway_turn(tmp_path, monkeypatch, models.get_session(s.session_id), usage)

    saved = models.get_session(s.session_id)
    assert saved.cache_read_tokens == 600
    assert saved.cache_write_tokens == 80


def test_gateway_provider_prefix_strip_keeps_colon_tagged_and_host_port_models(tmp_path, monkeypatch):
    """@provider:model must not lose the model's own colon segment.

    rsplit(":", 1) turns "@openrouter:meta/llama-4:free" into "free"; a plain
    split(":", 1) would instead turn "@custom:myhost:8080:m" into
    "myhost:8080:m". The shared #6722 parser knows both grammars, so both the
    request body and the context-length lookup delegate to it.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    s = new_session()
    body = _run_gateway_turn(
        tmp_path, monkeypatch, s, '{"prompt_tokens":5,"completion_tokens":1}',
        model="@openrouter:meta/llama-4:free",
    )
    assert json.loads(body)["model"] == "meta/llama-4:free"

    from api.routes import _split_provider_qualified_model

    assert _split_provider_qualified_model("@openrouter:meta/llama-4:free")[0] == "meta/llama-4:free"
    # #1776's example: a custom-provider slug derived from base_url authority
    # (host:port) must not be mistaken for an eaten model-tag colon.
    assert _split_provider_qualified_model("@custom:10.8.71.41:8080:Qwen3") == (
        "Qwen3", "custom:10.8.71.41:8080",
    )


def test_gateway_stream_usage_rejects_non_finite_cost():
    """estimated_cost: 1e999 becomes +inf in JSON, which then can't round-trip
    through json.dumps for session persistence/restore - a poisoned session
    that no longer loads in the browser. Reject non-finite, negative, and
    bool costs at the parse boundary; a normal float still passes through.
    """
    assert _gateway_stream_usage(
        {"usage": {"prompt_tokens": 1, "estimated_cost": 1e999}}
    )["estimated_cost"] == 0
    assert _gateway_stream_usage(
        {"usage": {"prompt_tokens": 1, "estimated_cost": float("nan")}}
    )["estimated_cost"] == 0
    assert _gateway_stream_usage(
        {"usage": {"prompt_tokens": 1, "estimated_cost": -5.0}}
    )["estimated_cost"] == 0
    assert _gateway_stream_usage(
        {"usage": {"prompt_tokens": 1, "estimated_cost": True}}
    )["estimated_cost"] == 0
    assert _gateway_stream_usage(
        {"usage": {"prompt_tokens": 1, "estimated_cost": 0.42}}
    )["estimated_cost"] == 0.42

    import json as _json
    _json.dumps(_gateway_stream_usage({"usage": {"estimated_cost": 1e999}}))


def test_gateway_stream_usage_preserves_explicit_zero_context_ring_fields():
    """The producing gateway's ContextCompressor clamps its post-compaction
    sentinel to a real 0 (hermes-agent#105905's max(0, ...)). That 0 must
    survive parsing as a present key, not collapse into "key absent" the way
    a falsy check would - otherwise WebUI can't tell "just compacted, numerator
    is genuinely 0" from "older gateway never sent this field at all".
    """
    present_zero = _gateway_stream_usage(
        {"usage": {"prompt_tokens": 1, "last_prompt_tokens": 0, "threshold_tokens": 0}}
    )
    assert "last_prompt_tokens" in present_zero and present_zero["last_prompt_tokens"] == 0
    assert "threshold_tokens" in present_zero and present_zero["threshold_tokens"] == 0

    absent = _gateway_stream_usage({"usage": {"prompt_tokens": 1}})
    assert "last_prompt_tokens" not in absent
    assert "threshold_tokens" not in absent

    # cache tokens keep the old truthy-only behaviour - they are billing
    # totals accumulated frame-by-frame, not a context-ring presence signal,
    # and the review didn't flag them.
    assert "cache_read_tokens" not in _gateway_stream_usage(
        {"usage": {"prompt_tokens": 1, "cache_read_tokens": 0}}
    )


def test_gateway_context_ring_trusts_explicit_zero_and_leaves_absent_field_untouched(
    tmp_path, monkeypatch
):
    """End-to-end through _run_gateway_chat_streaming: an explicit
    last_prompt_tokens=0 (post-compaction) must overwrite a stale nonzero
    numerator; an older gateway that omits the field entirely must leave the
    previous numerator standing rather than have this side guess at one from
    an empty tool-call list (defect #3 - unreliable tool-free inference).
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    s = new_session()
    _run_gateway_turn(
        tmp_path, monkeypatch, s,
        '{"prompt_tokens":1000,"completion_tokens":10,"last_prompt_tokens":26257}',
    )
    saved = models.get_session(s.session_id)
    assert saved.last_prompt_tokens == 26257

    # Post-compaction turn: gateway sends an authoritative 0.
    _run_gateway_turn(
        tmp_path, monkeypatch, saved,
        '{"prompt_tokens":50,"completion_tokens":5,"last_prompt_tokens":0}',
    )
    saved = models.get_session(s.session_id)
    assert saved.last_prompt_tokens == 0, "an explicit 0 must be trusted, not treated as absent"

    # Bump it back up, then simulate an older gateway that omits the field on
    # a tool-free turn. Old behaviour would have inferred a fresh numerator
    # from input_tokens using the empty-tool-call-list heuristic (defect #3);
    # the fix leaves last_prompt_tokens exactly where it was instead.
    saved.last_prompt_tokens = 26257
    saved.save()
    _run_gateway_turn(
        tmp_path, monkeypatch, saved, '{"prompt_tokens":9000,"completion_tokens":10}',
    )
    saved = models.get_session(s.session_id)
    assert saved.last_prompt_tokens == 26257, (
        "no last_prompt_tokens on the wire means no authoritative per-turn "
        "signal - the old numerator must stand, not a guess derived from "
        "input_tokens and an empty tool-call list"
    )


def test_gateway_explicit_zero_threshold_survives_the_75_percent_default_and_done_event(
    tmp_path, monkeypatch
):
    """threshold_tokens: 0 on the wire must reach BOTH the saved session and
    the "done" event's usage payload untouched by the fabricated 75%-of-window
    default - two separate spots downstream of the presence-sensitive parse
    fix both re-introduced a truthiness check on an already-correct 0:

      1. The "Auto-compress at X" tooltip default (`not (... or 0)`) treated
         a persisted 0 as "never set" and overwrote it with
         int(context_length * 0.75).
      2. The done-event backfill (`not usage.get(_ck)`) treated a present-but-
         zero usage["threshold_tokens"] the same way and replaced it with the
         session's now-fabricated value.

    A resolved positive context_length is required to reproduce both - the
    75% default is a no-op without one.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    s = new_session()
    s.context_length = 1_000_000  # already resolved, so the denominator block no-ops
    s.save()

    out = {}
    _run_gateway_turn(
        tmp_path, monkeypatch, s,
        '{"prompt_tokens":1000,"completion_tokens":10,"last_prompt_tokens":1000,"threshold_tokens":0}',
        out=out,
    )
    saved = models.get_session(s.session_id)
    assert saved.threshold_tokens == 0, "an explicit wire zero must not become the 750000 default"
    assert out["done_usage"]["threshold_tokens"] == 0, (
        "the done event ui.js reads on a session's first turn must carry the same "
        "zero the session persisted, not a value backfilled from a stale default"
    )

    # Follow-up turn from an older gateway that omits the field entirely: the
    # persisted zero must stand, not be reinterpreted as "unset" a turn later.
    _run_gateway_turn(
        tmp_path, monkeypatch, models.get_session(s.session_id),
        '{"prompt_tokens":50,"completion_tokens":5}',
    )
    saved = models.get_session(s.session_id)
    assert saved.threshold_tokens == 0, "an omitted field on a later turn must not revive the default"
