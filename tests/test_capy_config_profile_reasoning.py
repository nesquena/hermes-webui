"""Capy-specific profile config and reasoning regressions."""
import importlib
import os
from pathlib import Path


REPO = Path(__file__).parent.parent


def _reload_profile_config_modules(monkeypatch, base_home: Path, *, config_override: Path | None = None):
    monkeypatch.setenv("HERMES_BASE_HOME", str(base_home))
    monkeypatch.setenv("HERMES_HOME", str(base_home))
    if config_override is not None:
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_override))
    else:
        monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    import api.profiles as profiles
    import api.config as config

    profiles = importlib.reload(profiles)
    config = importlib.reload(config)
    return profiles, config


def test_named_profile_config_ignores_default_hermes_config_path(monkeypatch, tmp_path):
    """LaunchAgent HERMES_CONFIG_PATH must not pin named profiles to default config."""
    base = tmp_path / ".hermes"
    named = base / "profiles" / "lmstudio"
    named.mkdir(parents=True)
    default_config = base / "config.yaml"
    named_config = named / "config.yaml"
    default_config.write_text("agent:\n  reasoning_effort: low\n", encoding="utf-8")
    named_config.write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")

    profiles, config = _reload_profile_config_modules(
        monkeypatch,
        base,
        config_override=default_config,
    )
    profiles.set_request_profile("lmstudio")
    try:
        assert config._get_config_path() == named_config
        assert config.get_reasoning_status()["reasoning_effort"] == "high"
    finally:
        profiles.clear_request_profile()


def test_default_profile_still_honors_hermes_config_path(monkeypatch, tmp_path):
    """Single-profile installs can still intentionally override config.yaml path."""
    base = tmp_path / ".hermes"
    base.mkdir(parents=True)
    override = tmp_path / "custom-config.yaml"
    override.write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")

    profiles, config = _reload_profile_config_modules(monkeypatch, base, config_override=override)
    profiles.set_request_profile("default")
    try:
        assert config._get_config_path() == override
        assert config.get_reasoning_status()["reasoning_effort"] == "medium"
    finally:
        profiles.clear_request_profile()


def test_streaming_reasoning_config_reads_config_dict_not_missing_cfg_attr():
    """Streaming should pass agent.reasoning_effort through to AIAgent.

    Regression: _run_agent_streaming loaded a plain dict into _cfg, then used
    _cfg.cfg.get(...). AttributeError was swallowed, so reasoning_config was
    never sent to AIAgent.
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "_cfg.cfg" not in src
    assert "_cfg.get('agent', {})" in src or '_cfg.get("agent", {})' in src
