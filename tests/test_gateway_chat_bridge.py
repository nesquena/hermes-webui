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
