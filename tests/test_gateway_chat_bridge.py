from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")


def test_gateway_chat_mode_is_opt_in_and_default_safe():
    assert "def _webui_gateway_chat_enabled" in ROUTES
    assert 'os.environ.get("HERMES_WEBUI_CHAT_BACKEND")' in ROUTES
    assert 'return value in {"gateway", "api_server", "api-server"}' in ROUTES
    assert "elif _webui_gateway_chat_enabled():" in ROUTES
    assert "else:\n            response = _start_chat_stream_for_session" in ROUTES


def test_gateway_chat_mode_helper_is_default_off(monkeypatch):
    from api.routes import _webui_gateway_chat_enabled

    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    assert _webui_gateway_chat_enabled() is False

    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "")
    assert _webui_gateway_chat_enabled() is False

    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "direct")
    assert _webui_gateway_chat_enabled() is False


def test_gateway_chat_mode_helper_accepts_only_explicit_gateway_opt_in(monkeypatch):
    from api.routes import _webui_gateway_chat_enabled

    for value in ("gateway", "api_server", "api-server", " Gateway "):
        monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", value)
        assert _webui_gateway_chat_enabled() is True

    for value in ("1", "true", "yes", "enabled", "gateway-experimental"):
        monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", value)
        assert _webui_gateway_chat_enabled() is False


def test_gateway_chat_mode_uses_gateway_api_server_streaming_contract():
    assert "def _run_gateway_chat_streaming" in ROUTES
    assert 'url = f"{_webui_gateway_base_url()}/v1/chat/completions"' in ROUTES
    assert '"stream": True' in ROUTES
    assert 'current_event == "hermes.tool.progress"' in ROUTES
    assert 'put("token", {"text": text})' in ROUTES
    assert 'put("done", {"session": redact_session_data(raw_session), "usage": usage})' in ROUTES
    assert 'put("stream_end", {"session_id": session_id})' in ROUTES


def test_gateway_chat_mode_env_vars_are_documented():
    assert "HERMES_WEBUI_CHAT_BACKEND" in ARCHITECTURE
    assert "HERMES_WEBUI_GATEWAY_BASE_URL" in ARCHITECTURE
    assert "HERMES_WEBUI_GATEWAY_API_KEY" in ARCHITECTURE


def test_gateway_api_key_prefers_webui_specific_env(monkeypatch):
    from api.routes import _webui_gateway_api_key

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "webui-key")
    monkeypatch.setenv("API_SERVER_KEY", "shared-key")

    assert _webui_gateway_api_key() == "webui-key"


def test_gateway_api_key_falls_back_to_api_server_key(monkeypatch):
    from api.routes import _webui_gateway_api_key

    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "shared-key")

    assert _webui_gateway_api_key() == "shared-key"


def test_gateway_session_headers_are_auth_gated_in_bridge_source():
    header_block = (
        'api_key = _webui_gateway_api_key()\n'
        '        if api_key:\n'
        '            headers["Authorization"] = f"Bearer {api_key}"\n'
        '            headers["X-Hermes-Session-Id"] = session_id\n'
        '            headers["X-Hermes-Session-Key"] = f"webui:{session_id}"'
    )
    assert header_block in ROUTES


def test_gateway_message_builder_dedupes_eager_saved_current_prompt():
    from api.routes import _gateway_messages_for_webui_session

    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "current prompt"},
        ]
    )

    messages = _gateway_messages_for_webui_session(session, "current prompt", "/tmp/workspace")

    user_contents = [m["content"] for m in messages if m["role"] == "user"]
    assert user_contents == ["previous question", "current prompt"]
