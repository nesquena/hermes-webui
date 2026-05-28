from collections import OrderedDict
import base64
from email.message import Message
import json
from pathlib import Path
import re
import urllib.error

import api.gateway_chat as gateway_chat
import api.models as models
from api.config import STREAMS, create_stream_channel
from api.models import new_session
from api.gateway_chat import (
    _gateway_http_error_event,
    _gateway_sse_delta,
    _gateway_stream_usage,
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
    readme = Path("README.md").read_text(encoding="utf-8")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    for text in (readme, changelog):
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
            yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
            yield b'data: [DONE]\n\n'

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data.decode("utf-8")
        return FakeResponse()

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "secret-token")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    s = new_session()
    stream_id = "stream-gateway-test"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 123
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

    saved = models.get_session(s.session_id)
    assert [m["role"] for m in saved.messages] == ["user", "assistant"]
    assert saved.messages[-1]["content"] == "hello"
    assert saved.active_stream_id is None
    assert stream_id not in STREAMS
    assert captured["url"] == "http://gateway.local/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["X-hermes-session-id"] == s.session_id
    assert captured["headers"]["X-hermes-session-key"] == f"webui:{s.session_id}"
    assert '"stream": true' in captured["body"]


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

    content = captured["body"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What is in this image?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
