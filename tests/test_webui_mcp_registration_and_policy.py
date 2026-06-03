"""Regression tests for WebUI plan-first policy and bundled MCP registration."""

from types import SimpleNamespace


def test_register_webui_mcp_server_config_adds_bundled_server():
    from api.config import REPO_ROOT, STATE_DIR, register_webui_mcp_server_config

    cfg, changed = register_webui_mcp_server_config({"mcp_servers": {}})
    assert changed is True
    server = cfg["mcp_servers"]["hermes-webui"]
    assert server["args"] == [str(REPO_ROOT / "mcp_server.py")]
    assert server["env"]["HERMES_WEBUI_STATE_DIR"] == str(STATE_DIR)
    assert server["env"]["HERMES_WEBUI_MCP_TOKEN_FILE"] == str(STATE_DIR / ".mcp_token")
    assert "HERMES_WEBUI_PASSWORD" not in server["env"]
    assert server["env"]["HERMES_WEBUI_PORT"]
    assert "command" in server


def test_register_webui_mcp_server_config_is_idempotent():
    from api.config import register_webui_mcp_server_config

    cfg, changed = register_webui_mcp_server_config({"mcp_servers": {}})
    assert changed is True
    cfg2, changed2 = register_webui_mcp_server_config(cfg)
    assert changed2 is False
    assert cfg2 == cfg


def test_mcp_token_header_allows_cache_safe_session_mutation(monkeypatch, tmp_path):
    """The bundled MCP can call cache-safe APIs without storing the WebUI password."""
    import api.auth as auth

    token_file = tmp_path / ".mcp_token"
    token_file.write_text("tok_123", encoding="utf-8")
    monkeypatch.setattr(auth, "_MCP_TOKEN_FILE", token_file)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)

    handler = SimpleNamespace(
        headers={"X-Hermes-WebUI-MCP-Token": "tok_123"},
        send_response=lambda *_args, **_kwargs: None,
        send_header=lambda *_args, **_kwargs: None,
        end_headers=lambda: None,
        wfile=SimpleNamespace(write=lambda _data: None),
    )

    assert auth.check_auth(handler, SimpleNamespace(path="/api/session/move", query="")) is True


def test_mcp_token_header_is_limited_to_session_mutation_routes(monkeypatch, tmp_path):
    """The internal MCP token is not a general-purpose WebUI API bearer token."""
    import api.auth as auth

    responses = []
    token_file = tmp_path / ".mcp_token"
    token_file.write_text("tok_123", encoding="utf-8")
    monkeypatch.setattr(auth, "_MCP_TOKEN_FILE", token_file)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)

    handler = SimpleNamespace(
        headers={"X-Hermes-WebUI-MCP-Token": "tok_123"},
        send_response=lambda code: responses.append(code),
        send_header=lambda *_args, **_kwargs: None,
        end_headers=lambda: None,
        wfile=SimpleNamespace(write=lambda _data: None),
    )

    assert auth.check_auth(handler, SimpleNamespace(path="/api/settings", query="")) is False
    assert responses == [401]
