"""Regression tests for #4770 - Anthropic OAuth env vars in fallback detection."""

import builtins
import sys

import api.config as config


def _force_env_fallback(monkeypatch):
    """Force get_available_models() into the env-based fallback branch."""
    real_import = builtins.__import__
    monkeypatch.delitem(sys.modules, "hermes_cli.auth", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("hermes_cli.models", "hermes_cli.auth"):
            raise ImportError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _clear_provider_env(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GLM_API_KEY",
        "KIMI_API_KEY",
        "DEEPSEEK_API_KEY",
        "XIAOMI_API_KEY",
        "OPENCODE_ZEN_API_KEY",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY",
        "XAI_API_KEY",
        "MISTRAL_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "LM_API_KEY",
        "LM_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def _run_available_models_with_cfg(monkeypatch, tmp_path, cfg):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_models_cache_path = config._models_cache_path
    old_config_path_getter = config._get_config_path

    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path, raising=False)
    config.cfg.clear()
    config.cfg.update(cfg)
    config._cfg_mtime = 0.0
    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        monkeypatch.setattr(config, "_models_cache_path", old_models_cache_path)
        monkeypatch.setattr(config, "_get_config_path", old_config_path_getter)
        config.invalidate_models_cache()


def _provider_groups(result):
    return {group["provider_id"]: group for group in result["groups"]}


def test_anthropic_token_env_var_surfaces_anthropic_models(monkeypatch, tmp_path):
    """ANTHROPIC_TOKEN should detect the Anthropic provider through fallback."""
    _clear_provider_env(monkeypatch)
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "token-only-value")

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
    groups = _provider_groups(result)
    assert "anthropic" in groups, (
        "ANTHROPIC_TOKEN should surface the Anthropic picker provider when the "
        "hermes_cli path is unavailable."
    )
    assert groups["anthropic"]["provider"] == "Anthropic"
    assert groups["anthropic"]["models"], "Anthropic fallback should include model entries"


def test_whitespace_only_anthropic_oauth_env_vars_do_not_surface_anthropic(monkeypatch, tmp_path):
    """A whitespace-only ANTHROPIC_TOKEN / CLAUDE_CODE_OAUTH_TOKEN must NOT
    false-positive the Anthropic provider (env values are stripped before the
    availability check)."""
    for ws_var in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        _clear_provider_env(monkeypatch)
        _force_env_fallback(monkeypatch)
        monkeypatch.setenv(ws_var, "   \t  ")

        result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
        groups = _provider_groups(result)
        assert "anthropic" not in groups, (
            f"a whitespace-only {ws_var} must not surface the Anthropic provider"
        )


def test_claude_code_oauth_token_env_var_surfaces_anthropic_models(monkeypatch, tmp_path):
    """CLAUDE_CODE_OAUTH_TOKEN should detect the Anthropic provider through fallback."""
    _clear_provider_env(monkeypatch)
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-only-value")

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
    groups = _provider_groups(result)
    assert "anthropic" in groups, (
        "CLAUDE_CODE_OAUTH_TOKEN should surface the Anthropic picker provider when "
        "the hermes_cli path is unavailable."
    )
    assert groups["anthropic"]["provider"] == "Anthropic"
    assert groups["anthropic"]["models"], "Anthropic fallback should include model entries"
