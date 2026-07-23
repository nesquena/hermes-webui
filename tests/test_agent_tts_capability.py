"""Exact installed-Agent capability/catalog contract tests."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from api import agent_tts_worker


class _FakeTts:
    def __init__(self, available_by_provider, *, with_limit=True):
        self.available_by_provider = dict(available_by_provider)
        self._config = {}
        self.text_to_speech_tool = lambda text, output_path: None
        if with_limit:
            self._resolve_max_text_length = lambda provider, config: int(
                config.get("max_text_length", 5000)
            )

    def _load_tts_config(self):
        return copy.deepcopy(self._config)

    def _get_provider(self, config):
        return str(config.get("provider") or "edge")

    def check_tts_requirements(self):
        provider = self._get_provider(self._load_tts_config()).lower()
        return bool(self.available_by_provider.get(provider, False))

    def _check_edge_available(self):
        return bool(self.available_by_provider.get("edge", False))

    def _check_neutts_available(self):
        return bool(self.available_by_provider.get("neutts", False))


class _FakeToolsConfig:
    def __init__(self, rows):
        self.TOOL_CATEGORIES = {
            "tts": {"name": "Text-to-Speech", "providers": list(rows)}
        }
        self.saved = []

    def _visible_providers(self, category, config, *, force_fresh=False):
        return list(category["providers"])

    def _is_provider_active(self, row, config, *, force_fresh=False):
        return config.get("tts", {}).get("provider", "edge") == row.get("tts_provider")

    def apply_provider_selection(self, toolset, provider_name, config):
        row = next(
            row
            for row in self.TOOL_CATEGORIES[toolset]["providers"]
            if row["name"] == provider_name
        )
        config.setdefault("tts", {})["provider"] = row["tts_provider"]

    def provider_readiness_status(self, row, config, **kwargs):
        return "ready" if row.get("configured", True) else "needs_keys"


def _modules(config, rows, availability, *, with_limit=True, save=None):
    tts = _FakeTts(availability, with_limit=with_limit)
    tools_config = _FakeToolsConfig(rows)
    config_module = SimpleNamespace(
        load_config=lambda: copy.deepcopy(config),
        save_config=save or (lambda value: (_ for _ in ()).throw(AssertionError("write"))),
        get_config_path=lambda: "/profile/config.yaml",
    )
    return tts, tools_config, config_module


def test_capability_zero_rows_is_deterministic(monkeypatch):
    modules = _modules({"tts": {"provider": "edge"}}, [], {"edge": False})
    monkeypatch.setattr(agent_tts_worker, "_import_agent_modules", lambda: modules)

    result = agent_tts_worker.build_capability_payload()

    assert result["schema_version"] == 1
    assert result["engine"] == "agent"
    assert result["supported"] is True
    assert result["active_provider"] == "edge"
    assert result["active_provider_available"] is False
    assert result["providers"] == []


def test_capability_many_rows_use_exact_candidate_requirements(monkeypatch):
    rows = [
        {
            "name": "Microsoft Edge TTS",
            "tts_provider": "EDGE",
            "badge": "local",
            "tag": "Built-in",
        },
        {
            "name": "OpenAI TTS",
            "tts_provider": "openai",
            "badge": "cloud",
            "tag": "BYOK",
            "env_vars": [{"key": "OPENAI_API_KEY", "prompt": "secret prompt"}],
        },
    ]
    modules = _modules(
        {"tts": {"provider": "edge"}, "secret": "must-not-leak"},
        rows,
        {"edge": False, "openai": True},
    )
    monkeypatch.setattr(agent_tts_worker, "_import_agent_modules", lambda: modules)

    result = agent_tts_worker.build_capability_payload()

    assert result["active_provider"] == "edge"
    assert result["active_provider_available"] is False
    assert [row["available"] for row in result["providers"]] == [False, True]
    assert result["providers"][0]["provider_id"] == "edge"
    assert result["providers"][0]["label_key"] == "tts_provider_edge"
    assert "env_vars" not in repr(result)
    assert "OPENAI_API_KEY" not in repr(result)
    assert "must-not-leak" not in repr(result)


def test_unknown_provider_uses_safe_agent_name_without_dynamic_i18n_key(monkeypatch):
    rows = [
        {
            "name": "Local Piper <Voice>",
            "tts_provider": "PIPER_EN",
            "badge": "custom",
            "tag": "Command",
        }
    ]
    modules = _modules(
        {"tts": {"provider": "piper_en"}}, rows, {"piper_en": True}
    )
    monkeypatch.setattr(agent_tts_worker, "_import_agent_modules", lambda: modules)

    result = agent_tts_worker.build_capability_payload()

    row = result["providers"][0]
    assert row["provider_id"] == "piper_en"
    assert row["label_key"] is None
    assert row["name"] == "Local Piper <Voice>"


def test_active_configured_command_provider_gets_safe_read_only_fallback_row(monkeypatch):
    config = {
        "tts": {
            "provider": "my-command",
            "providers": {
                "my-command": {
                    "type": "command",
                    "command": "secret executable --token must-not-leak",
                }
            },
        }
    }
    modules = _modules(config, [], {"my-command": True})
    monkeypatch.setattr(agent_tts_worker, "_import_agent_modules", lambda: modules)

    result = agent_tts_worker.build_capability_payload()

    assert result["active_provider_name"] == "my-command"
    assert result["providers"] == [
        {
            "name": "my-command",
            "provider_id": "my-command",
            "label_key": None,
            "badge": "",
            "tag": "",
            "configured": True,
            "available": True,
            "active": True,
            "selectable": False,
        }
    ]
    assert "secret executable" not in repr(result)
    assert "must-not-leak" not in repr(result)


def test_edge_to_neutts_is_the_only_reported_fallback(monkeypatch):
    rows = [{"name": "Edge", "tts_provider": "edge"}]
    modules = _modules(
        {"tts": {"provider": "edge"}}, rows, {"edge": False, "neutts": True}
    )
    # Agent's requirements check deliberately reports Edge usable via NeuTTS.
    modules[0].available_by_provider["edge"] = True
    modules[0]._check_edge_available = lambda: False
    monkeypatch.setattr(agent_tts_worker, "_import_agent_modules", lambda: modules)

    result = agent_tts_worker.build_capability_payload()

    assert result["active_provider_available"] is True
    assert result["resolved_provider"] == "neutts"


def test_missing_dynamic_limit_uses_compatibility_ceiling(monkeypatch):
    rows = [{"name": "Edge", "tts_provider": "edge"}]
    modules = _modules(
        {"tts": {"provider": "edge"}}, rows, {"edge": True}, with_limit=False
    )
    monkeypatch.setattr(agent_tts_worker, "_import_agent_modules", lambda: modules)

    result = agent_tts_worker.build_capability_payload()

    assert result["supported"] is True
    assert result["provider_max_text_length"] == 2000
    assert result["request_max_text_length"] == 2000
    assert result["limit_source"] == "compatibility_fallback"


def test_old_agent_missing_required_callable_fails_closed(monkeypatch):
    tts, tools_config, config_module = _modules({}, [], {})
    tts.text_to_speech_tool = lambda text: None
    monkeypatch.setattr(
        agent_tts_worker,
        "_import_agent_modules",
        lambda: (tts, tools_config, config_module),
    )

    result = agent_tts_worker.build_capability_payload()

    assert result == {
        "schema_version": 1,
        "engine": "agent",
        "supported": False,
        "synthesis_supported": False,
        "provider_write_supported": False,
        "code": "agent_contract_unavailable",
        "providers": [],
    }


def test_capability_never_calls_agent_save_config(monkeypatch):
    writes = []
    rows = [{"name": "Edge", "tts_provider": "edge"}]
    modules = _modules(
        {"tts": {"provider": "edge"}},
        rows,
        {"edge": True},
        save=lambda config: writes.append(config),
    )
    monkeypatch.setattr(agent_tts_worker, "_import_agent_modules", lambda: modules)

    agent_tts_worker.build_capability_payload()

    assert writes == []
