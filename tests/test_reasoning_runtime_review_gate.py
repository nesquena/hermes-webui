"""Regression coverage for the #8681 runtime-contract gate.

The real installed Agent route rule is exercised when available.  The
capability-absence cases emulate a missing helper; they are not a historical
Agent-version replay or a live provider turn.
"""

import json
import os
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from api import config as cfg


def _require_compatible_agent_rule(module: Any) -> Any:
    """Skip optional real-producer tests on unavailable or older Agents."""
    rule: Any = getattr(module, "route_supported_efforts", None)
    if not callable(rule):
        raise pytest.skip.Exception("installed Agent has no route_supported_efforts")
    try:
        supported: Any = rule("openai-codex", "gpt-6-sol", "codex_app_server")
    except TypeError:
        raise pytest.skip.Exception("installed Agent route rule predates api_mode") from None
    if "ultra" not in supported:
        raise pytest.skip.Exception("installed Agent does not accept ultra for gpt-6-sol")
    return rule


@pytest.fixture
def agent_rule():
    """Use the installed producer rather than a WebUI-owned rule imitation."""
    return _require_compatible_agent_rule(pytest.importorskip("agent.reasoning_effort"))


def test_older_two_argument_agent_is_skipped_without_exception():
    class OlderAgent:
        @staticmethod
        def route_supported_efforts(provider, model):
            return ("low", "high")

    with pytest.raises(pytest.skip.Exception, match="predates api_mode"):
        _require_compatible_agent_rule(OlderAgent)


@pytest.fixture
def installed_route(monkeypatch, agent_rule):
    ladder = (*cfg._WEBUI_BASE_EFFORTS, "ultra")
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", ladder)
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: agent_rule)
    # The catalog is deterministic; the real WebUI filtering/coercion and real
    # Agent route/display producers remain in the path.
    monkeypatch.setattr(cfg, "_resolve_model_reasoning_efforts_impl", lambda *a, **k: list(ladder))
    monkeypatch.setattr(cfg, "_load_yaml_config_file", lambda path: {})
    return ladder


@pytest.mark.parametrize(
    "model", ["gpt-6-sol", "@openai-codex:gpt-6-sol", "@OpenAI-Codex:gpt-6-sol"]
)
@pytest.mark.parametrize(
    "effort,expected", [("ultra", "ultra"), ("high", "high")]
)
def test_model_hint_keeps_agent_capability_and_effective_effort(
    installed_route, agent_rule, model, effort, expected,
):
    provider, runtime = "openai-codex", "codex_app_server"
    # Positive control: this particular installed producer accepts ultra on the
    # bare route. Do not silently replace the rule when an older Agent is used.
    if "ultra" not in agent_rule(provider, "gpt-6-sol", runtime):
        pytest.skip("installed Agent does not accept ultra for gpt-6-sol")
    status = cfg.get_reasoning_status(
        model_id=model, provider_id=provider, effort_override=effort, api_mode=runtime,
    )
    assert status["reasoning_effort"] == expected
    assert status["reasoning_effort_display"] == expected
    assert status["supported_efforts"] == list(installed_route)
    assert cfg.resolve_session_reasoning_effort(
        {}, session_effort=effort, model_id=model, provider_id=provider, api_mode=runtime,
    ) == expected


def test_reasoning_status_over_real_local_http(agent_rule, base_url):
    provider, runtime = "openai-codex", "codex_app_server"
    if "ultra" not in agent_rule(provider, "gpt-6-sol", runtime):
        pytest.skip("installed Agent lacks the gpt-6-sol app-server capability")
    # The session-scoped test server owns this disposable home. Seed a config
    # file there to exercise its real GET handler and installed Agent rule.
    config_path = Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"]) / "config.yaml"
    original = config_path.read_bytes() if config_path.exists() else None
    config_path.write_text(
        "model:\n  provider: openai-codex\n  openai_runtime: codex_app_server\n"
        "agent:\n  reasoning_effort: ultra\n",
        encoding="utf-8",
    )
    try:
        results = []
        for model in ("gpt-6-sol", "@openai-codex:gpt-6-sol"):
            query = urlencode({"model": model, "provider": provider})
            with urlopen(f"{base_url}/api/reasoning?{query}", timeout=10) as response:
                assert response.status == 200
                results.append(json.load(response))
        for status in results:
            assert status["reasoning_effort"] == "ultra"
            assert status["reasoning_effort_display"] == "ultra"
            assert "ultra" in status["supported_efforts"]
        assert results[0]["supported_efforts"] == results[1]["supported_efforts"]
    finally:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original)


@pytest.mark.parametrize(
    "provider,model,bare_model",
    [
        ("openai-codex", "gpt-6-sol", "gpt-6-sol"),
        ("openai-codex", "@openai-codex:gpt-6-sol", "gpt-6-sol"),
        ("custom:reviewer-codex", "@custom:reviewer-codex:vendor/model:free", "vendor/model:free"),
        ("ollama", "@custom:ollama.internal:11434:llama3.2", "llama3.2"),
        ("ollama", "@custom:ollama.internal:11434:llama3.2:latest", "llama3.2:latest"),
        ("ollama", "@CUSTOM:ollama.internal:11434:Model:Latest", "Model:Latest"),
        ("openrouter", "@openrouter:vendor/model:free", "vendor/model:free"),
    ],
)
def test_agent_route_and_display_receive_the_unwrapped_model(
    monkeypatch, provider, model, bare_model,
):
    # Keep this seam test runnable without an installed Agent. A separate test
    # above exercises the actual installed producer when one is available.
    calls = []
    agent = ModuleType("agent")
    agent.__path__ = []
    agent.__spec__ = ModuleSpec("agent", loader=None, is_package=True)
    reasoning = ModuleType("agent.reasoning_effort")
    reasoning.__spec__ = ModuleSpec("agent.reasoning_effort", loader=None)
    monkeypatch.setattr(agent, "reasoning_effort", reasoning, raising=False)

    def recording_rule(provider_id, model_id, api_mode):
        calls.append(("route", provider_id, model_id, api_mode))
        return cfg._WEBUI_BASE_EFFORTS

    def recording_label(effort, provider_id, model_id, api_mode):
        calls.append(("display", provider_id, model_id, api_mode))
        return f"{effort} (sends max on this route)"

    monkeypatch.setattr(reasoning, "effort_display_label", recording_label, raising=False)
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.reasoning_effort", reasoning)
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", (*cfg._WEBUI_BASE_EFFORTS, "ultra"))
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: recording_rule)
    cfg._route_wire_efforts(model, provider, "codex_app_server")
    assert cfg._effort_honest_label(
        "ultra", "max", model, provider, "codex_app_server",
    ) == "ultra (sends max on this route)"
    canonical_provider = cfg._resolve_provider_alias(provider)
    assert calls == [
        ("route", canonical_provider, bare_model, "codex_app_server"),
        ("display", canonical_provider, bare_model, "codex_app_server"),
    ]


@pytest.mark.parametrize(
    "model,provider,effective",
    [
        ("gpt-6-sol", "openai-codex", "max"),
        ("@openai-codex:gpt-6-sol", "openai-codex", "max"),
        ("gemini-4-argon", "gemini", "xhigh"),
    ],
)
def test_six_level_fallback_annotates_previously_saved_ultra(
    monkeypatch, model, provider, effective,
):
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: None)
    # Run the real initialization ranking: the old fixture left ultra in this
    # value and so could never see the missing-label failure.
    ranked = cfg._rank_valid_efforts((*cfg._WEBUI_BASE_EFFORTS, "ultra"))
    assert ranked == cfg._WEBUI_BASE_EFFORTS
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", ranked)
    monkeypatch.setattr(cfg, "_resolve_model_reasoning_efforts_impl", lambda *a, **k: list(ranked))
    monkeypatch.setattr(cfg, "_load_yaml_config_file", lambda path: {})
    # An older installation can lack the Agent's label helper too.
    monkeypatch.setitem(sys.modules, "agent.reasoning_effort", None)

    status = cfg.get_reasoning_status(
        model_id=model, provider_id=provider, effort_override="ultra", api_mode="codex_app_server",
    )
    assert status["reasoning_effort"] == effective
    assert status["reasoning_effort_display"] == f"ultra (sends {effective} on this route)"
    assert "ultra" not in status["supported_efforts"]
    assert cfg.parse_reasoning_effort("ultra") is None
    with pytest.raises(ValueError):
        cfg.normalize_reasoning_effort("ultra")
    assert cfg.resolve_session_reasoning_effort(
        {}, session_effort="ultra", model_id=model, provider_id=provider,
        api_mode="codex_app_server",
    ) == effective


def test_six_level_fallback_status_reads_the_isolated_profile_default(tmp_path, monkeypatch):
    profile_config = tmp_path / "config.yaml"
    profile_config.write_text("agent:\n  reasoning_effort: ultra\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: None)
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", cfg._rank_valid_efforts(
        (*cfg._WEBUI_BASE_EFFORTS, "ultra")
    ))
    monkeypatch.setattr(cfg, "_get_config_path", lambda: profile_config)
    monkeypatch.setattr(cfg, "_active_isolated_config_path", lambda: profile_config)
    monkeypatch.setattr(
        cfg, "_resolve_model_reasoning_efforts_impl", lambda *a, **k: list(cfg._WEBUI_BASE_EFFORTS)
    )

    status = cfg.get_reasoning_status(
        model_id="gpt-6-sol", provider_id="openai-codex", api_mode="codex_app_server",
    )
    assert status["reasoning_effort"] == "max"
    assert status["reasoning_effort_display"] == "ultra (sends max on this route)"
    assert "ultra" not in status["supported_efforts"]
    assert cfg._config_reasoning_effort(cfg._load_yaml_config_file(profile_config)) == "ultra"


@pytest.mark.parametrize("rule_kind", ["raising", "two_arg"])
def test_present_but_unusable_rule_keeps_real_ranked_annotation(monkeypatch, rule_kind):
    def raising(*args):
        raise RuntimeError("route capability unavailable")

    def two_arg(provider, model):
        return cfg._WEBUI_BASE_EFFORTS

    rule = {"raising": raising, "two_arg": two_arg}[rule_kind]
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: rule)
    # Unlike an absent rule, a callable rule allows the real initialization
    # ranking to retain ultra; the later three-argument call fails closed.
    ranked = cfg._rank_valid_efforts((*cfg._WEBUI_BASE_EFFORTS, "ultra"))
    assert ranked == (*cfg._WEBUI_BASE_EFFORTS, "ultra")
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", ranked)
    monkeypatch.setattr(cfg, "_resolve_model_reasoning_efforts_impl", lambda *a, **k: list(ranked))
    monkeypatch.setattr(cfg, "_load_yaml_config_file", lambda path: {})
    status = cfg.get_reasoning_status(
        model_id="gpt-6-sol", provider_id="openai-codex", effort_override="ultra",
        api_mode="codex_app_server",
    )
    assert status["reasoning_effort"] == "max"
    assert status["reasoning_effort_display"] == "ultra (sends max on this route)"
    assert "ultra" not in status["supported_efforts"]


def test_unknown_stored_value_remains_unknown_without_route_rule(monkeypatch):
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: None)
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", cfg._WEBUI_BASE_EFFORTS)
    monkeypatch.setattr(cfg, "_load_yaml_config_file", lambda path: {})
    status = cfg.get_reasoning_status(
        model_id="gpt-6-sol", provider_id="openai-codex", effort_override="hyper",
        api_mode="codex_app_server",
    )
    assert status["reasoning_effort"] == ""
    assert status["reasoning_effort_display"] == ""
    assert cfg._effort_honest_label("hyper", "max", "gpt-6-sol", "openai-codex") == "max"
    assert cfg.parse_reasoning_effort("hyper") is None
    with pytest.raises(ValueError):
        cfg.normalize_reasoning_effort("hyper")
