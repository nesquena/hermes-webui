"""Tests for issue #5127 — process-wakeup bare custom-provider model repair.

When ``_resolve_compatible_session_model_state`` takes the #1855 fast path
(both model and model_provider set), a bare runtime model that matches the
suffix of a slash-qualified ``profile_default_model`` must repair to the full
qualified ID for named custom providers.
"""
from unittest.mock import patch


class TestIssue5127CustomProviderBareSuffixRepair:
    def test_fast_path_repairs_bare_suffix_to_profile_default(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_fast_path_skips_repair_when_profile_provider_mismatches(self):
        """Regression: custom:other-proxy must not inherit my-proxy's qualified default."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:other-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("grok-composer-2.5-fast", "custom:other-proxy", False)

    def test_fast_path_repairs_generic_custom_provider(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "some-model",
                "custom",
                profile_default_model="vendor/some-model",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("vendor/some-model", "custom", True)

    def test_fast_path_does_not_rewrite_unrelated_bare_model(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "other-model",
                "custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("other-model", "custom:my-proxy", False)

    def test_fast_path_keeps_qualified_model_unchanged(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "x-ai/grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", False)

    def test_slow_path_repairs_bare_suffix_to_profile_default_for_custom_provider(self):
        """Regression #5225: async continuations may arrive without usable model_provider."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_slow_path_does_not_rewrite_unrelated_bare_model_for_custom_provider(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "other-model",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("other-model", "custom:my-proxy", False)

    def test_openai_codex_stale_openai_slash_still_uses_slow_path(self):
        """Regression: openai/... under openai-codex must not take bare fast return."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                "openai-codex",
                profile_default_model="gpt-5.5",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count >= 1
        assert result[0] == "gpt-5.5"
        assert result[2] is True


class TestReadProfileModelConfigWithExplicitProvider:
    def test_returns_profile_default_when_session_has_model_provider(self, tmp_path, monkeypatch):
        from api.routes import _read_profile_model_config

        profile_home = tmp_path / "prof"
        profile_home.mkdir()
        (profile_home / "config.yaml").write_text(
            "model:\n  provider: custom:my-proxy\n  default: x-ai/grok-composer-2.5-fast\n",
            encoding="utf-8",
        )

        class _Session:
            profile = "testprof"

        monkeypatch.setattr(
            "api.profiles.get_hermes_home_for_profile",
            lambda _p: str(profile_home),
        )

        provider, default = _read_profile_model_config(_Session(), "custom:my-proxy")
        assert provider is None
        assert default == "x-ai/grok-composer-2.5-fast"

    def test_returns_none_default_when_session_provider_differs_from_profile(
        self, tmp_path, monkeypatch,
    ):
        from api.routes import _read_profile_model_config

        profile_home = tmp_path / "prof"
        profile_home.mkdir()
        (profile_home / "config.yaml").write_text(
            "model:\n  provider: custom:my-proxy\n  default: x-ai/grok-composer-2.5-fast\n",
            encoding="utf-8",
        )

        class _Session:
            profile = "testprof"

        monkeypatch.setattr(
            "api.profiles.get_hermes_home_for_profile",
            lambda _p: str(profile_home),
        )

        provider, default = _read_profile_model_config(_Session(), "custom:other-proxy")
        assert provider is None
        assert default is None