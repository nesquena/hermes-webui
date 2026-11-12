"""A session on the virtual ``moa`` provider must not also get a per-turn moa_config.

hermes-agent (since the change that added the virtual ``moa`` provider and
``hermes_cli.moa_config.resolve_moa_preset``) runs a ``moa`` session's preset
through its MoA facade. Threading a WebUI-resolved ``moa_config`` into the same
run makes ``run_conversation()`` perform a second, legacy reference fan-out and
aggregation on every tool iteration. Older agents without the virtual provider
still need the legacy per-turn path, and ``/moa`` on a non-MoA session is
unchanged.
"""
import io
import sys
from types import ModuleType

import pytest


class _Handler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        self.response_headers.append(("__end__", ""))


def _start_turn(monkeypatch, tmp_path, *, provider, model, virtual_provider, body_extra=None):
    import api.commands as commands
    import api.routes as routes

    class _Session:
        session_id = "sess-moa-virtual"
        workspace = str(tmp_path)
        profile = "default"
        messages = []
        context_messages = []
        pending_user_message = None

        def __init__(self):
            self.model = model
            self.model_provider = provider

    captured = {}
    resolve_calls = []

    def start_run(*_args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    def resolve_moa_config(preset=None):
        resolve_calls.append(preset)
        return {"reference_models": [{"provider": "x", "model": "y"}], "aggregator": {}}

    monkeypatch.setattr(routes, "get_session", lambda _sid: _Session())
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_start_run", start_run)
    for name in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        routes,
        "get_config_snapshot",
        lambda: {"model": {"provider": provider, "default": model}},
    )
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(commands, "resolve_moa_config", resolve_moa_config)
    monkeypatch.setattr(commands, "agent_has_moa_virtual_provider", lambda: virtual_provider)

    body = {
        "session_id": "sess-moa-virtual",
        "message": "diagnose issue",
        "workspace": str(tmp_path),
    }
    body.update(body_extra or {})
    handler = _Handler()
    routes._handle_chat_start(handler, body)
    return handler, captured, resolve_calls


@pytest.mark.parametrize("body_extra", [None, {"moa_config": True}], ids=["plain-turn", "slash-moa-turn"])
def test_virtual_moa_session_never_stacks_per_turn_moa_config(monkeypatch, tmp_path, body_extra):
    handler, captured, _ = _start_turn(
        monkeypatch,
        tmp_path,
        provider="moa",
        model="moa-research",
        virtual_provider=True,
        body_extra=body_extra,
    )

    assert handler.status == 200, handler.wfile.getvalue()
    assert captured["model_provider"] == "moa"
    assert "moa_config" not in captured


def test_legacy_agent_moa_session_keeps_per_turn_moa_config(monkeypatch, tmp_path):
    handler, captured, resolve_calls = _start_turn(
        monkeypatch, tmp_path, provider="moa", model="moa-research", virtual_provider=False
    )

    assert handler.status == 200, handler.wfile.getvalue()
    assert resolve_calls == [captured["model"]]
    assert captured["moa_config"]["reference_models"]


def test_slash_moa_on_regular_session_still_gets_moa_config(monkeypatch, tmp_path):
    handler, captured, resolve_calls = _start_turn(
        monkeypatch,
        tmp_path,
        provider="openai-codex",
        model="gpt-5.5",
        virtual_provider=True,
        body_extra={"moa_config": True},
    )

    assert handler.status == 200, handler.wfile.getvalue()
    assert resolve_calls == [None]
    assert captured["moa_config"]["reference_models"]


def test_agent_has_moa_virtual_provider_probe(monkeypatch):
    import api.commands as commands

    legacy = ModuleType("hermes_cli.moa_config")
    monkeypatch.setitem(sys.modules, "hermes_cli.moa_config", legacy)
    assert commands.agent_has_moa_virtual_provider() is False

    current = ModuleType("hermes_cli.moa_config")
    current.resolve_moa_preset = lambda raw, name: {}
    monkeypatch.setitem(sys.modules, "hermes_cli.moa_config", current)
    assert commands.agent_has_moa_virtual_provider() is True
