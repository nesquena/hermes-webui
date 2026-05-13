"""Regression: LM Studio missing from Settings → Providers after onboarding (#1420).

The bug: users who completed the onboarding wizard with the LM Studio provider —
either via the "Open / self-hosted" category in the wizard, or by hand-editing
config.yaml + .env to point at an LM Studio instance — would see LM Studio
listed in the model picker and could chat just fine, but Settings → Providers
showed *no* LM Studio entry, or showed it with `has_key=False, configurable=False`
even when LMSTUDIO_API_KEY was already in `~/.hermes/.env`.

Root cause (verified by reproduction in the original investigation, then by
the regression tests below):

`api/providers.py:_PROVIDER_ENV_VAR` is the dict that maps each provider id
to the .env / os.environ key the WebUI should look for. It's used by:

  1. `_provider_has_key(pid)` — does an env-var-based detection lookup, returns
     False (and `key_source='none'`) if the provider id isn't in the dict.
  2. `get_providers()` line 364 — sets `configurable = pid in _PROVIDER_ENV_VAR`
     so the UI knows whether to render the "Add API key" form for that provider.

Without an `lmstudio: "LMSTUDIO_API_KEY"` entry, both checks miss: the env var
is invisible to the Settings panel, AND the UI hides the surface that would
let the user fix the situation by typing a new key.

This is the same bug-shape as #1410 (Ollama Cloud / local Ollama env var
collision) and is fixed by the same kind of edit: add the missing mapping in
`_PROVIDER_ENV_VAR`. Unlike #1410, there's no collision concern for LM Studio
because LMSTUDIO_API_KEY isn't shared with any other provider's runtime.

Reporters: @chwps, @AdoneyGalvan (#1420 thread).
"""

import sys
import types

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline.

    Mirrors the helper in test_provider_management.py — kept inline here so
    this regression test stays self-contained and survives refactors there.
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


def _swap_in_test_config(extra_cfg):
    """Snapshot config.cfg, replace with a minimal test config; return restore-fn."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg.update(extra_cfg)
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    def _restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    return _restore


class TestIssue1420LMStudioProviderEnvVar:
    """LM Studio's env var must be in `_PROVIDER_ENV_VAR` so Settings detects it.

    Three angles of the same fix:
      1. The dict literally contains the mapping (catches accidental removal).
      2. With `LMSTUDIO_API_KEY` in env, get_providers() reports has_key=True.
      3. Without env but with `providers.lmstudio.api_key` in config.yaml,
         get_providers() also reports has_key=True (defense for users who
         configured via config.yaml directly).
      4. LM Studio is rendered as `configurable=True` so the UI shows the
         "Add API key" form when no key is configured.
    """

    def test_lmstudio_in_provider_env_var_dict(self):
        """`_PROVIDER_ENV_VAR['lmstudio']` must equal `'LM_API_KEY'` (canonical, agent-aligned).

        The original #1420 fix used `'LMSTUDIO_API_KEY'`. After #1500 (cross-tool
        env-var alignment with the agent CLI) the canonical name is `LM_API_KEY`,
        and `LMSTUDIO_API_KEY` is preserved as a read-only legacy alias in
        `_PROVIDER_ENV_VAR_ALIASES` so existing users don't lose detection.
        """
        from api.providers import _PROVIDER_ENV_VAR, _PROVIDER_ENV_VAR_ALIASES
        assert "lmstudio" in _PROVIDER_ENV_VAR, (
            "_PROVIDER_ENV_VAR is missing the 'lmstudio' entry — Settings → "
            "Providers will render LM Studio as has_key=False / "
            "configurable=False. See #1420."
        )
        assert _PROVIDER_ENV_VAR["lmstudio"] == "LM_API_KEY", (
            f"_PROVIDER_ENV_VAR['lmstudio'] = {_PROVIDER_ENV_VAR['lmstudio']!r}, "
            f"expected 'LM_API_KEY' to match the agent CLI's "
            f"hermes_cli/auth.py:lmstudio.api_key_env_vars. See #1500."
        )
        # The legacy alias must still be registered so users with the pre-#1500
        # env var don't lose detection on upgrade.
        assert "lmstudio" in _PROVIDER_ENV_VAR_ALIASES, (
            "_PROVIDER_ENV_VAR_ALIASES['lmstudio'] missing — pre-#1500 users "
            "with LMSTUDIO_API_KEY in their .env will see Settings flip to "
            "'no key' on upgrade.  Keep the alias for at least a few releases."
        )
        assert "LMSTUDIO_API_KEY" in _PROVIDER_ENV_VAR_ALIASES["lmstudio"], (
            f"Expected 'LMSTUDIO_API_KEY' as a legacy alias for lmstudio, got "
            f"{_PROVIDER_ENV_VAR_ALIASES['lmstudio']!r}."
        )

    def test_lmstudio_has_key_true_when_env_var_set(self, monkeypatch, tmp_path):
        """`LM_API_KEY` in env should mark LM Studio configured in Settings (canonical, post-#1500)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
        monkeypatch.delenv("LM_API_KEY", raising=False)
        monkeypatch.setenv("LM_API_KEY", "lm-studio")

        restore = _swap_in_test_config({"model": {"provider": "lmstudio"}})
        try:
            from api.providers import get_providers
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert "lmstudio" in by_id, (
                "lmstudio should appear in the provider list "
                "(it's in _PROVIDER_DISPLAY)"
            )
            assert by_id["lmstudio"]["has_key"] is True, (
                "Settings → Providers must report LM Studio as has_key=True when "
                "LMSTUDIO_API_KEY is set in .env / os.environ. Pre-fix, the "
                "lmstudio entry was missing from _PROVIDER_ENV_VAR so the env-var "
                "check was skipped entirely. See #1420."
            )
            assert by_id["lmstudio"]["configurable"] is True, (
                "Settings → Providers must render LM Studio as configurable=True "
                "(so the 'Add API key' UI surface is shown). Pre-fix, "
                "configurable was False because lmstudio wasn't in "
                "_PROVIDER_ENV_VAR."
            )
            assert by_id["lmstudio"]["key_source"] in {"env_file", "env_var"}, (
                f"key_source should reflect that the key came from env, "
                f"got {by_id['lmstudio']['key_source']!r}"
            )
        finally:
            restore()

    def test_lmstudio_has_key_true_via_config_yaml(self, monkeypatch, tmp_path):
        """providers.lmstudio.api_key in config.yaml should also count.

        This is the fallback for users who ran onboarding before the fix
        landed — their .env may have LMSTUDIO_API_KEY=... but the reload path
        could have lost it on a profile switch. The config.yaml fallback in
        `_provider_has_key` should still detect them.
        """
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
        monkeypatch.delenv("LM_API_KEY", raising=False)

        restore = _swap_in_test_config({
            "model": {"provider": "lmstudio"},
            "providers": {"lmstudio": {"api_key": "lm-studio"}},
        })
        try:
            from api.providers import get_providers
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert by_id["lmstudio"]["has_key"] is True, (
                "providers.lmstudio.api_key in config.yaml must also flip "
                "has_key=True for the Settings panel."
            )
            assert by_id["lmstudio"]["key_source"] == "config_yaml", (
                f"key_source should be 'config_yaml', got "
                f"{by_id['lmstudio']['key_source']!r}"
            )
        finally:
            restore()

    def test_lmstudio_has_key_false_when_no_signal(self, monkeypatch, tmp_path):
        """No env var, no config — has_key=False but card is still shown + configurable.

        Pre-fix this case ALSO showed has_key=False, so this test alone doesn't
        catch the bug. It pins the interaction with the other tests: with the
        fix, the card is `configurable=True` (so the user can add a key), which
        is exactly what was missing pre-fix.
        """
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
        monkeypatch.delenv("LM_API_KEY", raising=False)

        restore = _swap_in_test_config({"model": {"provider": "lmstudio"}})
        try:
            from api.providers import get_providers
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert by_id["lmstudio"]["has_key"] is False
            assert by_id["lmstudio"]["configurable"] is True, (
                "Even with no key configured, the LM Studio card must be "
                "configurable=True so the user can add a key from the UI. "
                "Pre-fix this was False — the user had no UI surface to "
                "configure LM Studio after onboarding (#1420)."
            )
        finally:
            restore()

    def test_lmstudio_does_not_collide_with_other_providers(self, monkeypatch, tmp_path):
        """LMSTUDIO_API_KEY must NOT cross-detect any other provider.

        Sibling-defense modeled on the #1410 (OLLAMA_API_KEY / Ollama Cloud /
        local Ollama) test: confirm LM Studio's env var doesn't accidentally
        mark any other provider as configured. LMSTUDIO_API_KEY isn't shared
        with another provider's runtime, but pinning this prevents a future
        edit that does share it from regressing.
        """
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        # Strip every other detection signal so LMSTUDIO_API_KEY is the only
        # input — any other provider showing has_key=True must be a leak.
        for var in (
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
            "GH_TOKEN", "GITHUB_TOKEN", "OLLAMA_API_KEY", "GOOGLE_API_KEY",
            "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY",
            "MINIMAX_CN_API_KEY", "XIAOMI_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY",
            "GLM_API_KEY", "KIMI_API_KEY", "OPENCODE_ZEN_API_KEY",
            "OPENCODE_GO_API_KEY", "NVIDIA_API_KEY", "LMSTUDIO_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        # Set the canonical post-#1500 env var; sibling providers must not
        # cross-detect.
        monkeypatch.setenv("LM_API_KEY", "lm-studio")

        restore = _swap_in_test_config({"model": {"provider": "lmstudio"}})
        try:
            from api.providers import get_providers
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}

            assert by_id["lmstudio"]["has_key"] is True, "lmstudio itself should be configured"
            for pid, entry in by_id.items():
                if pid == "lmstudio":
                    continue
                # OAuth providers may report has_key=True via the gh_token /
                # auth fallback; that's not LMSTUDIO_API_KEY's fault.
                if entry.get("is_oauth"):
                    continue
                # custom_providers isn't relevant here (we set none in the cfg).
                if pid.startswith("custom"):
                    continue
                assert entry["has_key"] is False, (
                    f"LM_API_KEY in env caused {pid!r} to flip "
                    f"has_key=True (cross-detection leak). LM_API_KEY is "
                    f"unique to lmstudio in _PROVIDER_ENV_VAR; this test "
                    f"future-proofs against a regression that adds it to "
                    f"a sibling."
                )
        finally:
            restore()
