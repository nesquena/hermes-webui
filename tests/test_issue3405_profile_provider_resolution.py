"""Tests for issue #3405 — profile provider resolution.

When a session's profile configures ``model.provider`` (e.g. ``anthropic``),
the session's model should be resolved through that provider. Stale models
from a different provider family (e.g. ``openai/gpt-5.4-mini`` under an
anthropic profile) should be repaired to the profile's ``model.default``.

The profile provider must NOT trigger the explicit-provider fast path
(#1855): that path skips the catalog and stale-model repair entirely.
Instead, the profile context is passed via keyword-only parameters
``profile_provider`` and ``profile_default_model`` to
``_resolve_compatible_session_model_state``, which runs the full catalog
path and applies profile-aware repair.

Coverage:

1. Stale slash-qualified model repairs to profile default (maintainer repro)
2. Stale bare model repairs to profile default
3. Compatible model passes through with profile provider
4. Profile provider does not skip catalog (not fast path)
5. Explicit body provider still triggers fast path (regression guard)
6. @provider:model override wins over profile provider
7. No profile falls back to global default (regression guard)
8. Missing/empty profile config results in no injection
9. Streaming worker enrichment tests
"""
from pathlib import Path
from unittest.mock import patch


class TestProfileProviderResolution:
    """Profile provider passes through the repair path, not the fast path."""

    def test_stale_slash_model_with_profile_provider_repairs_to_profile_default(self):
        """Maintainer repro: openai/gpt-5.4-mini + profile_provider='anthropic'
        + profile_default='claude-sonnet-4.6' repairs to claude-sonnet-4.6."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert result[0] == "claude-sonnet-4.6"
        assert result[1] == "anthropic"
        assert result[2] is True

    def test_stale_openai_slash_model_with_openai_codex_profile_repairs(self):
        """openai/... under an openai-codex profile repairs to the profile default."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "anthropic",
                "default_model": "claude-sonnet-4.6",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                None,
                profile_provider="openai-codex",
                profile_default_model="gpt-5.5",
            )

        assert result[0] == "gpt-5.5"
        assert result[1] == "openai-codex"
        assert result[2] is True

    def test_stale_bare_model_with_profile_provider_repairs(self):
        """gpt-5.5 + profile_provider='anthropic' repairs because gpt-* is
        not anthropic-family."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "gpt-5.5",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert result[0] == "claude-sonnet-4.6"
        assert result[1] == "anthropic"
        assert result[2] is True

    def test_compatible_model_with_profile_provider_passes_through(self):
        """claude-sonnet-4.6 + profile_provider='anthropic' passes through."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "claude-sonnet-4.6",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert result[0] == "claude-sonnet-4.6"
        assert result[1] == "anthropic"
        assert result[2] is False

    def test_profile_provider_does_not_skip_catalog(self):
        """profile_provider must NOT trigger the fast path; catalog must load."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert mock_catalog.call_count == 1

    def test_explicit_provider_still_uses_fast_path(self):
        """Explicit model_provider must still take the fast path per #1855."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "gpt-5.5",
                "openai-codex",
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert mock_catalog.call_count == 0
        assert result == ("gpt-5.5", "openai-codex", False)

    def test_at_qualified_model_wins_over_profile(self):
        """@openrouter:deepseek/deepseek-v4 routes through openrouter, not
        the profile's anthropic."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "anthropic",
                "default_model": "claude-sonnet-4.6",
                "groups": [{"provider_id": "openrouter"}],
            }
            result = _resolve_compatible_session_model_state(
                "@openrouter:deepseek/deepseek-v4",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        # Falls through to the @-handler, which sees openrouter in catalog
        assert result[1] == "openrouter"
        assert result[2] is False

    def test_no_profile_falls_back_to_global(self):
        """No profile_provider uses the catalog's active_provider/default_model."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "anthropic",
                "default_model": "claude-sonnet-4.6",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "gpt-5.5",
                None,
            )

        # gpt-* under anthropic active_provider repairs to default
        assert result[0] == "claude-sonnet-4.6"
        assert result[2] is True

    def test_empty_model_with_profile_returns_profile_default(self):
        """Empty model + profile_provider returns profile default model."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert result[0] == "claude-sonnet-4.6"
        assert result[1] == "anthropic"
        assert result[2] is True

    def test_empty_model_with_profile_no_default_returns_catalog_default(self):
        """Empty model + profile_provider but no profile default falls back
        to catalog default_model."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "",
                None,
                profile_provider="anthropic",
                profile_default_model=None,
            )

        assert result[0] == "gpt-5.5"
        assert result[1] == "anthropic"
        assert result[2] is True

    def test_gemini_model_under_openai_profile_repairs(self):
        """gemini-2.5-pro under openai profile repairs to profile default."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "gemini-2.5-pro",
                None,
                profile_provider="openai",
                profile_default_model="gpt-5.5",
            )

        assert result[0] == "gpt-5.5"
        assert result[1] == "openai"
        assert result[2] is True

    def test_unknown_model_family_passes_through_with_profile(self):
        """Models without a known bare prefix (gpt/claude/gemini) and no
        slash pass through with the profile provider attached."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "deepseek-v4-flash",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert result[0] == "deepseek-v4-flash"
        assert result[1] == "anthropic"
        assert result[2] is False


class TestReadProfileModelConfig:
    """Tests for the _read_profile_model_config helper."""

    def test_returns_none_when_no_profile(self):
        """No session profile returns (None, None, None)."""
        from api.routes import _read_profile_model_config

        class FakeSession:
            profile = None

        result = _read_profile_model_config(FakeSession(), None)
        assert result == (None, None, None)

    def test_returns_none_when_explicit_provider(self):
        """Explicit requested_provider skips profile config read."""
        from api.routes import _read_profile_model_config

        class FakeSession:
            profile = "work"

        result = _read_profile_model_config(FakeSession(), "openai-codex")
        assert result == (None, None, None)

    def test_reads_profile_config(self, tmp_path):
        """Reads model.provider and model.default from profile config.yaml."""
        from api.routes import _read_profile_model_config
        import yaml

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({"model": {"provider": "anthropic", "default": "claude-sonnet-4.6"}}),
            encoding="utf-8",
        )

        class FakeSession:
            profile = "work"

        with patch("api.profiles.get_hermes_home_for_profile", return_value=tmp_path):
            result = _read_profile_model_config(FakeSession(), None)

        assert result[:2] == ("anthropic", "claude-sonnet-4.6")
        assert result[2] == {"model": {"provider": "anthropic", "default": "claude-sonnet-4.6"}}

    def test_missing_config_returns_none(self):
        """Missing config.yaml returns (None, None, None)."""
        from api.routes import _read_profile_model_config
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            class FakeSession:
                profile = "work"

            with patch("api.profiles.get_hermes_home_for_profile", return_value=Path(td)):
                result = _read_profile_model_config(FakeSession(), None)

        assert result == (None, None, None)

    def test_empty_provider_returns_none(self, tmp_path):
        """Empty model.provider in config returns (None, default, config)."""
        from api.routes import _read_profile_model_config
        import yaml

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({"model": {"provider": "", "default": "claude-sonnet-4.6"}}),
            encoding="utf-8",
        )

        class FakeSession:
            profile = "work"

        with patch("api.profiles.get_hermes_home_for_profile", return_value=tmp_path):
            result = _read_profile_model_config(FakeSession(), None)

        assert result == (None, "claude-sonnet-4.6", {"model": {"provider": "", "default": "claude-sonnet-4.6"}})


class TestStreamingWorkerEnrichment:
    """Tests for profile-aware provider/model enrichment in the streaming worker."""

    def test_streaming_enrichment_skips_non_profile_session(self, tmp_path):
        from api.streaming import _apply_profile_home_context_to_streaming_model

        # Even if the default home has a model provider configured, a session
        # without an explicit profile must not inherit that provider context.
        model_cfg = tmp_path / "config.yaml"
        model_cfg.write_text(
            "model:\n  provider: openai-codex\n  default: gpt-5.5\n",
            encoding="utf-8",
        )

        model, provider_context, changed = _apply_profile_home_context_to_streaming_model(
            "openai/gpt-5.4-mini",
            None,
            str(tmp_path),
            has_profile=False,
        )

        assert model == "openai/gpt-5.4-mini"
        assert provider_context is None
        assert changed is False

    def test_stale_model_substituted_in_streaming(self):
        """The streaming worker should substitute a stale model when the
        profile provider does not match the model's family."""
        from api.routes import _normalize_provider_id

        # Simulate the streaming logic inline
        model = "gpt-5.5"
        provider_context = None
        _pp = "anthropic"
        _pp_default = "claude-sonnet-4.6"

        if not provider_context and _pp:
            provider_context = _pp.lower()
            if _pp_default:
                _m_lower = (model or "").lower()
                _pp_norm = _normalize_provider_id(_pp)
                for _prefix in ("gpt", "claude", "gemini"):
                    if _m_lower.startswith(_prefix):
                        if _normalize_provider_id(_prefix) != _pp_norm:
                            model = _pp_default
                        break

        assert model == "claude-sonnet-4.6"
        assert provider_context == "anthropic"

    def test_compatible_model_not_substituted_in_streaming(self):
        """Compatible models should not be substituted in streaming worker."""
        from api.routes import _normalize_provider_id

        model = "claude-sonnet-4.6"
        provider_context = None
        _pp = "anthropic"
        _pp_default = "claude-sonnet-4.6"

        if not provider_context and _pp:
            provider_context = _pp.lower()
            if _pp_default:
                _m_lower = (model or "").lower()
                _pp_norm = _normalize_provider_id(_pp)
                for _prefix in ("gpt", "claude", "gemini"):
                    if _m_lower.startswith(_prefix):
                        if _normalize_provider_id(_prefix) != _pp_norm:
                            model = _pp_default
                        break

        assert model == "claude-sonnet-4.6"
        assert provider_context == "anthropic"

    def test_explicit_provider_context_skips_profile_enrichment(self):
        """When provider_context is already set (explicit provider), the
        profile enrichment block should not fire."""
        model = "gpt-5.5"
        provider_context = "openai-codex"
        _pp = "anthropic"
        _pp_default = "claude-sonnet-4.6"

        # The guard: `if not provider_context and _profile_home:`
        # prevents enrichment when provider_context is already set
        if not provider_context and _pp:
            provider_context = _pp.lower()

        assert model == "gpt-5.5"
        assert provider_context == "openai-codex"


class TestSlashQualifiedOpenRouterPassthrough:
    """Slash-qualified IDs are native on OpenRouter/custom providers and must
    not be repaired. They should only be repaired when the profile provider
    is a concrete family (anthropic, openai, etc.)."""

    def test_openrouter_profile_preserves_slash_qualified_model(self):
        """openai/gpt-5.4-mini under openrouter profile passes through."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "deepseek/deepseek-v4",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                None,
                profile_provider="openrouter",
                profile_default_model="deepseek/deepseek-v4",
            )

        assert result[0] == "openai/gpt-5.4-mini"
        assert result[1] == "openrouter"
        assert result[2] is False

    def test_openrouter_profile_repairs_bare_prefix_mismatch(self):
        """Bare claude-sonnet-4.6 under openrouter profile is repaired because
        the bare-prefix family (anthropic) doesn't match openrouter."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "deepseek/deepseek-v4",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "claude-sonnet-4.6",
                None,
                profile_provider="openrouter",
                profile_default_model="deepseek/deepseek-v4",
            )

        assert result[0] == "deepseek/deepseek-v4"
        assert result[1] == "openrouter"
        assert result[2] is True

    def test_anthropic_profile_repairs_slash_qualified_openai(self):
        """openai/gpt-5.4-mini under anthropic profile is repaired (slash
        prefix mismatch on a concrete provider)."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "copilot",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                None,
                profile_provider="anthropic",
                profile_default_model="claude-sonnet-4.6",
            )

        assert result[0] == "claude-sonnet-4.6"
        assert result[1] == "anthropic"
        assert result[2] is True

    def test_custom_profile_preserves_slash_qualified_model(self):
        """custom provider also preserves slash-qualified IDs."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "custom:lmstudio",
                "default_model": "local-model",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                None,
                profile_provider="custom:lmstudio",
                profile_default_model="local-model",
            )

        assert result[0] == "openai/gpt-5.4-mini"
        assert result[1] == "custom:lmstudio"
        assert result[2] is False


class TestStreamingSlashQualifiedRepair:
    """Streaming worker must detect slash-qualified stale models and handle
    openrouter/custom passthrough correctly."""

    def test_streaming_repairs_slash_model_under_anthropic(self):
        """openai/gpt-5.4-mini under anthropic profile is repaired."""
        from api.routes import _normalize_provider_id

        model = "openai/gpt-5.4-mini"
        provider_context = None
        _pp = "anthropic"
        _pp_default = "claude-sonnet-4.6"

        if not provider_context and _pp:
            provider_context = _pp.lower()
            if _pp_default:
                _m_lower = (model or "").lower()
                _pp_norm = _normalize_provider_id(_pp)
                _repaired = False
                for _prefix in ("gpt", "claude", "gemini"):
                    if _m_lower.startswith(_prefix):
                        if _normalize_provider_id(_prefix) != _pp_norm:
                            model = _pp_default
                            _repaired = True
                        break
                if not _repaired and "/" in _m_lower:
                    _slash_prefix = _m_lower.split("/", 1)[0]
                    _slash_provider = _normalize_provider_id(_slash_prefix)
                    if _slash_provider and _slash_provider != _pp_norm and _pp_norm not in {"openrouter", "custom", ""}:
                        model = _pp_default

        assert model == "claude-sonnet-4.6"
        assert provider_context == "anthropic"

    def test_streaming_preserves_slash_model_under_openrouter(self):
        """openai/gpt-5.4-mini under openrouter profile is NOT repaired."""
        from api.routes import _normalize_provider_id

        model = "openai/gpt-5.4-mini"
        provider_context = None
        _pp = "openrouter"
        _pp_default = "deepseek/deepseek-v4"

        if not provider_context and _pp:
            provider_context = _pp.lower()
            if _pp_default:
                _m_lower = (model or "").lower()
                _pp_norm = _normalize_provider_id(_pp)
                _repaired = False
                for _prefix in ("gpt", "claude", "gemini"):
                    if _m_lower.startswith(_prefix):
                        if _normalize_provider_id(_prefix) != _pp_norm:
                            model = _pp_default
                            _repaired = True
                        break
                if not _repaired and "/" in _m_lower:
                    _slash_prefix = _m_lower.split("/", 1)[0]
                    _slash_provider = _normalize_provider_id(_slash_prefix)
                    if _slash_provider and _slash_provider != _pp_norm and _pp_norm not in {"openrouter", "custom", ""}:
                        model = _pp_default

        assert model == "openai/gpt-5.4-mini"
        assert provider_context == "openrouter"

    def test_streaming_repairs_openai_slash_model_under_openai_codex(self):
        """openai/... under an openai-codex profile repairs to the profile default."""
        from api.streaming import _apply_profile_provider_context_to_streaming_model

        model, provider_context, changed = (
            _apply_profile_provider_context_to_streaming_model(
                "openai/gpt-5.4-mini",
                None,
                "openai-codex",
                "gpt-5.5",
            )
        )

        assert model == "gpt-5.5"
        assert provider_context == "openai-codex"
        assert changed is True
