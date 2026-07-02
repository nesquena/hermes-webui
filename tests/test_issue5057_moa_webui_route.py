"""Tests for /moa WebUI route: resolve_moa_config and GET /api/commands/moa/resolve."""
import json
import re
import sys
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from tests.conftest import TEST_BASE, requires_agent_modules


def _install_fake_moa_config(monkeypatch, *, default_preset="moa-default", usage_text="Usage: /moa <prompt>"):
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    hermes_cli_pkg.__path__ = []
    moa_config = ModuleType("hermes_cli.moa_config")

    def normalize_moa_config(cfg):
        return {"default_preset": default_preset}

    def moa_usage():
        return usage_text

    moa_config_any = cast(Any, moa_config)
    moa_config_any.normalize_moa_config = normalize_moa_config
    moa_config_any.moa_usage = moa_usage
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.moa_config", moa_config)


def _install_fake_hermes_config(monkeypatch, cfg_data=None):
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    hermes_cli_pkg.__path__ = []
    config_mod = ModuleType("hermes_cli.config")

    def load_config():
        if cfg_data is None:
            raise RuntimeError("no config")
        return cfg_data

    config_mod_any = cast(Any, config_mod)
    config_mod_any.load_config = load_config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)


def test_resolve_moa_config_returns_expected_shape(monkeypatch):
    _install_fake_moa_config(monkeypatch, default_preset="moa-fast", usage_text="/moa <prompt> -- run with MoA")
    _install_fake_hermes_config(monkeypatch, cfg_data={"moa": {}})
    from api.commands import resolve_moa_config
    result = resolve_moa_config()
    assert result["default_preset"] == "moa-fast"
    assert result["preset"] == "moa-fast"
    assert result["usage"] == "/moa <prompt> -- run with MoA"
    assert "model" not in result
    assert "model_provider" not in result


def test_resolve_moa_config_degrades_without_config(monkeypatch):
    _install_fake_moa_config(monkeypatch, default_preset="moa-default-cfg")
    _install_fake_hermes_config(monkeypatch, cfg_data=None)
    from api.commands import resolve_moa_config
    result = resolve_moa_config()
    assert result["default_preset"] == "moa-default-cfg"
    assert result["preset"] == "moa-default-cfg"


def test_resolve_moa_config_raises_when_moa_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.moa_config", None)
    from api.commands import resolve_moa_config
    with pytest.raises(RuntimeError, match="MoA runtime unavailable"):
        resolve_moa_config()


@requires_agent_modules
def test_moa_resolve_endpoint_returns_200():
    with urllib.request.urlopen(TEST_BASE + "/api/commands/moa/resolve", timeout=10) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert "default_preset" in body
    assert "preset" in body
    assert isinstance(body.get("usage"), str)
    assert isinstance(body.get("reference_models"), list)


def test_moa_not_in_agent_commands_webui():
    js_path = Path(__file__).resolve().parent.parent / "static" / "messages.js"
    source = js_path.read_text(encoding="utf-8")
    match = re.search(r"_AGENT_COMMANDS_RUN_ON_WEBUI\s*=\s*new\s+Set\(\[([^\]]+)\]\)", source)
    assert match, "_AGENT_COMMANDS_RUN_ON_WEBUI not found in messages.js"
    entries = match.group(1)
    assert "'moa'" not in entries and '"moa"' not in entries


def test_no_subprocess_in_moa_code_paths():
    commands_path = Path(__file__).resolve().parent.parent / "api" / "commands.py"
    source = commands_path.read_text(encoding="utf-8")
    match = re.search(r"def resolve_moa_config\b.*?(?=\ndef |\Z)", source, re.DOTALL)
    assert match, "resolve_moa_config not found in commands.py"
    func_body = match.group(0)
    assert "process_command" not in func_body
    assert "HermesCLI" not in func_body
    assert "subprocess" not in func_body


def test_moa_config_is_per_turn_not_persisted():
    """moa_config stays per-turn, but the server re-resolves it instead of
    trusting a client-echoed dict."""
    streaming_path = Path(__file__).resolve().parent.parent / "api" / "streaming.py"
    source = streaming_path.read_text(encoding="utf-8")
    # moa_config is threaded into the live agent turn as a per-turn kwarg. It is
    # added CONDITIONALLY (only when not None) so a normal send never trips a
    # TypeError on an older hermes-agent whose run_conversation() predates the
    # kwarg — so accept either the direct kwarg form or the conditional-dict form.
    assert (
        re.search(r"run_conversation\([\s\S]*?moa_config=moa_config", source)
        or re.search(r'if moa_config is not None:[\s\S]*?\["moa_config"\]\s*=\s*moa_config', source)
    ), "run_conversation must receive moa_config as a per-turn kwarg (directly or conditionally)"
    routes_path = Path(__file__).resolve().parent.parent / "api" / "routes.py"
    routes_source = routes_path.read_text(encoding="utf-8")
    assert re.search(r"if body\.get\(\"moa_config\"\):[\s\S]*?moa_config = resolve_moa_config\(\)", routes_source), \
        "chat-start must re-resolve MoA config server-side instead of trusting the browser payload"
    assert "MoA override is unavailable on gateway-backed sessions" in routes_source
    js_path = Path(__file__).resolve().parent.parent / "static" / "messages.js"
    js_source = js_path.read_text(encoding="utf-8")
    assert "moa_config:_pendingMoaConfig?true:undefined" in js_source
    assert "_pendingMoaConfig=null" in js_source


def test_moa_gateway_chat_start_fails_closed(monkeypatch, tmp_path):
    """Gateway-backed WebUI sessions must reject /moa until the gateway consumes runtime overrides."""
    import api.commands as commands
    import api.routes as routes

    class _Handler:
        def __init__(self):
            import io

            self.status = None
            self.response_headers = []
            self.wfile = io.BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.response_headers.append((key, value))

        def end_headers(self):
            self.response_headers.append(("__end__", ""))

    class _Session:
        session_id = "sess-moa-gateway"
        workspace = str(tmp_path)
        model = "gpt-5.5"
        model_provider = "openai-codex"
        profile = "default"
        messages = []
        context_messages = []
        pending_user_message = None

    def start_run(*_args, **_kwargs):  # pragma: no cover - should fail before run start
        raise AssertionError("gateway-backed /moa must fail closed before starting a run")

    def resolve_moa_config():  # pragma: no cover - should fail before resolving MoA
        raise AssertionError("gateway-backed /moa must fail before resolving MoA config")

    monkeypatch.setattr(routes, "get_session", lambda _sid: _Session())
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_start_run", start_run)
    monkeypatch.setattr(routes, "get_config", lambda: {"chat_backend": "gateway"})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: True)
    monkeypatch.setattr(commands, "resolve_moa_config", resolve_moa_config)

    handler = _Handler()
    routes._handle_chat_start(
        handler,
        {
            "session_id": "sess-moa-gateway",
            "message": "diagnose issue",
            "workspace": str(tmp_path),
            "moa_config": True,
        },
    )

    body = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.status == 409
    assert body["error"] == "MoA override is unavailable on gateway-backed sessions"
