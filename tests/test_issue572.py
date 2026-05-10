"""Tests for issue #572: onboarding must not fire or overwrite config for
providers not in the quick-setup list (minimax-cn, deepseek, xai, etc.).

Root cause: _provider_api_key_present() only knew about the four providers in
_SUPPORTED_PROVIDER_SETUPS.  For any other provider it returned False, causing
chat_ready=False, which made the wizard fire even when the user was fully
configured.  The second part of the fix ensures _saveOnboardingProviderSetup()
in the frontend also skips the POST when current_is_oauth is set.

Covers:
  1. _provider_api_key_present returns True for minimax-cn when
     MINIMAX_CN_API_KEY is in env (via hermes_cli.auth.get_auth_status)
  2. _status_from_runtime gives chat_ready=True for minimax-cn with a key set
  3. get_onboarding_status returns completed=True for a fully-configured
     unsupported provider when config.yaml exists
  4. The hermes_cli import failure path is safe (falls back gracefully)
"""

from __future__ import annotations

import pathlib
import sys
import types
from unittest import mock


def _inject_hermes_cli_auth(get_auth_status_return):
    """Inject a minimal hermes_cli.auth stub into sys.modules.

    CI doesn't install hermes_cli (it's a separate package).  Tests that
    exercise the hermes_cli fallback path must inject the module themselves
    rather than relying on mock.patch('hermes_cli.auth.get_auth_status')
    which fails with ModuleNotFoundError when the module isn't installed.
    """
    mock_auth = types.ModuleType("hermes_cli.auth")
    mock_auth.get_auth_status = mock.MagicMock(return_value=get_auth_status_return)
    mock_hermes_cli = types.ModuleType("hermes_cli")

    return mock.patch.dict(
        sys.modules,
        {
            "hermes_cli": mock_hermes_cli,
            "hermes_cli.auth": mock_auth,
        },
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _call_provider_api_key_present(
    provider: str, cfg: dict = None, env_values: dict = None
):
    from api.onboarding import _provider_api_key_present

    return _provider_api_key_present(provider, cfg or {}, env_values or {})


# ---------------------------------------------------------------------------
# 1. _provider_api_key_present via hermes_cli fallback
# ---------------------------------------------------------------------------


class TestProviderApiKeyPresentFallback:
    def test_minimax_cn_logged_in_returns_true(self):
        """minimax-cn: if hermes_cli.auth.get_auth_status returns logged_in, must be True."""
        with mock.patch(
            "api.onboarding._SUPPORTED_PROVIDER_SETUPS",
            {"openrouter": {}, "anthropic": {}, "openai": {}, "custom": {}},
        ):
            with _inject_hermes_cli_auth({"logged_in": True}):
                result = _call_provider_api_key_present("minimax-cn")
                assert result is True

    def test_unsupported_provider_logged_out_returns_false(self):
        """Unsupported provider with no key → False, no crash."""
        with mock.patch(
            "api.onboarding._SUPPORTED_PROVIDER_SETUPS",
            {"openrouter": {}, "anthropic": {}, "openai": {}, "custom": {}},
        ):
            with _inject_hermes_cli_auth({"logged_in": False}):
                result = _call_provider_api_key_present("deepseek")
                assert result is False

    def test_hermes_cli_import_failure_is_safe(self):
        """If hermes_cli is unavailable, falls back silently to False."""
        import builtins

        real_import = builtins.__import__

        def _block_hermes_cli(name, *args, **kwargs):
            if name.startswith("hermes_cli"):
                raise ImportError("hermes_cli not available")
            return real_import(name, *args, **kwargs)

        with mock.patch(
            "api.onboarding._SUPPORTED_PROVIDER_SETUPS",
            {"openrouter": {}, "anthropic": {}, "openai": {}, "custom": {}},
        ):
            with mock.patch("builtins.__import__", side_effect=_block_hermes_cli):
                result = _call_provider_api_key_present("minimax-cn")
                assert result is False  # safe fallback

    def test_supported_provider_still_works_without_fallback(self):
        """openrouter with env key must still succeed via the original path."""
        from api.onboarding import _provider_api_key_present

        env_values = {"OPENROUTER_API_KEY": "sk-test"}
        result = _provider_api_key_present("openrouter", {}, env_values)
        assert result is True

    def test_inline_api_key_in_cfg_still_works(self):
        """model.api_key in config.yaml must be recognized for any provider."""
        cfg = {
            "model": {
                "provider": "minimax-cn",
                "default": "MiniMax-M2.7",
                "api_key": "key123",
            }
        }
        result = _call_provider_api_key_present("minimax-cn", cfg)
        assert result is True


# ---------------------------------------------------------------------------
# 2. _status_from_runtime: unsupported provider with key → chat_ready=True
# ---------------------------------------------------------------------------


class TestStatusFromRuntimeUnsupportedProvider:
    def _run(
        self,
        provider: str,
        model: str,
        api_key_present: bool,
        oauth_present: bool = False,
    ):
        from api.onboarding import _status_from_runtime

        cfg = {"model": {"provider": provider, "default": model}}
        with (
            mock.patch("api.onboarding._HERMES_FOUND", True),
            mock.patch("api.onboarding._load_env_file", return_value={}),
            mock.patch(
                "api.onboarding._get_active_hermes_home",
                return_value=pathlib.Path("/tmp"),
            ),
            mock.patch(
                "api.onboarding._provider_api_key_present", return_value=api_key_present
            ),
            mock.patch(
                "api.onboarding._provider_oauth_authenticated",
                return_value=oauth_present,
            ),
        ):
            return _status_from_runtime(cfg, True)

    def test_minimax_cn_with_key_gives_chat_ready(self):
        """minimax-cn + api key present → chat_ready must be True."""
        result = self._run("minimax-cn", "MiniMax-M2.7", api_key_present=True)
        assert result["chat_ready"] is True, f"Expected chat_ready=True, got: {result}"
        assert result["provider_ready"] is True
        assert result["setup_state"] == "ready"

    def test_deepseek_with_key_gives_chat_ready(self):
        """deepseek + api key → chat_ready."""
        result = self._run("deepseek", "deepseek-chat", api_key_present=True)
        assert result["chat_ready"] is True

    def test_unsupported_provider_no_key_no_oauth_gives_not_ready(self):
        """No key, no oauth → provider_ready=False."""
        result = self._run(
            "minimax-cn", "MiniMax-M2.7", api_key_present=False, oauth_present=False
        )
        assert result["chat_ready"] is False
        assert result["provider_ready"] is False

    def test_oauth_provider_still_works_via_oauth_path(self):
        """openai-codex (OAuth) with no api_key but oauth present → ready."""
        result = self._run(
            "openai-codex", "codex-model", api_key_present=False, oauth_present=True
        )
        assert result["chat_ready"] is True


# ---------------------------------------------------------------------------
# 3. get_onboarding_status: minimax-cn fully configured → completed=True
# ---------------------------------------------------------------------------


class TestOnboardingStatusUnsupportedProvider:
    def _make_status(self, chat_ready: bool, provider: str = "minimax-cn"):
        import api.onboarding as mod

        fake_config_path = pathlib.Path("/tmp/_test_572_config.yaml")
        cfg = {"model": {"provider": provider, "default": "MiniMax-M2.7"}}
        runtime = {
            "chat_ready": chat_ready,
            "provider_configured": True,
            "provider_ready": chat_ready,
            "setup_state": "ready" if chat_ready else "provider_incomplete",
            "provider_note": "test",
            "current_provider": provider,
            "current_model": "MiniMax-M2.7",
            "current_base_url": None,
            "env_path": "/tmp/.env",
        }
        with (
            mock.patch.object(mod, "load_settings", return_value={}),
            mock.patch.object(mod, "get_config", return_value=cfg),
            mock.patch.object(
                mod, "verify_hermes_imports", return_value=(True, [], {})
            ),
            mock.patch.object(mod, "_status_from_runtime", return_value=runtime),
            mock.patch.object(mod, "load_workspaces", return_value=[]),
            mock.patch.object(mod, "get_last_workspace", return_value=None),
            mock.patch.object(mod, "get_available_models", return_value=[]),
            mock.patch.object(mod, "_get_config_path", return_value=fake_config_path),
            mock.patch.object(pathlib.Path, "exists", return_value=True),
        ):
            return mod.get_onboarding_status()

    def test_minimax_cn_chat_ready_skips_wizard(self):
        """minimax-cn + chat_ready=True + config.yaml exists → wizard must NOT fire."""
        result = self._make_status(chat_ready=True)
        assert result["completed"] is True, (
            "Wizard fired for minimax-cn user with valid config! "
            "config.yaml + chat_ready=True must auto-complete onboarding regardless of provider."
        )

    def test_minimax_cn_not_ready_skips_wizard(self):
        """minimax-cn + chat_ready=False → wizard still skipped for non-wizard providers.

        The onboarding wizard has no minimax-cn option — showing it would only confuse
        the user or let them accidentally overwrite their config with an OpenAI/Anthropic
        provider.  For any provider not in _SUPPORTED_PROVIDER_SETUPS, onboarding is
        auto-completed as long as provider_configured is True, regardless of chat_ready.
        Users on non-wizard providers with no API key should fix credentials via
        Settings → Providers, not via the first-run wizard.  (#1020)
        """
        result = self._make_status(chat_ready=False)
        assert result["completed"] is True, (
            "Wizard fired for minimax-cn user with provider_configured=True! "
            "Non-wizard providers must auto-complete onboarding because the wizard "
            "cannot configure them and would silently overwrite their config."
        )

    def test_current_is_oauth_set_for_unsupported_provider(self):
        """setup.current_is_oauth must be True for minimax-cn (not in quick-setup list)."""
        result = self._make_status(chat_ready=True)
        assert result["setup"]["current_is_oauth"] is True, (
            "current_is_oauth should be True for providers not in _SUPPORTED_PROVIDER_SETUPS"
        )
