from collections import OrderedDict
import base64
from email.message import Message
import json
from pathlib import Path
import re
import urllib.error

import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming
from api.config import STREAMS, create_stream_channel
from api.models import new_session
from api.gateway_chat import (
    _gateway_http_error_event,
    _gateway_reasoning_delta,
    _gateway_sse_delta,
    _gateway_sse_reasoning_delta,
    _gateway_stream_usage,
    _gateway_tool_progress_event,
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
    assert stream_id not in STREAMS
    assert captured["url"] == "http://gateway.local/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["X-hermes-session-id"] == s.session_id
    assert captured["headers"]["X-hermes-session-key"] == f"webui:{s.session_id}"
    assert '"stream": true' in captured["body"]
    payload = json.loads(captured["body"])
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
