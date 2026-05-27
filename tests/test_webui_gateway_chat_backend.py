from collections import OrderedDict

import api.gateway_chat as gateway_chat
import api.models as models
from api.config import STREAMS, create_stream_channel
from api.models import new_session
from api.gateway_chat import (
    _gateway_sse_delta,
    _gateway_stream_usage,
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
