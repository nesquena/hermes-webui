"""Tests for issue #1855 — /api/chat/start wedge on resolve_model_provider stage.

When the model catalog is cold and a rebuild has to make network calls
(custom OpenAI-compat endpoints, OpenRouter /models, LM Studio /models, or a
credential pool refresh), get_available_models() can take 100s+ to return.

POST /api/chat/start used to call _resolve_compatible_session_model_state(),
which unconditionally invoked get_available_models() — wedging the request
past the typical 60s reverse-proxy timeout. Users behind nginx/Apache/Caddy
saw 502 Proxy Error while WebUI eventually started the run anyway, creating
a duplicate-send risk if the user retried.

The fix: when the caller supplies an explicit model_provider AND the model is
not an @provider:model-qualified string, skip the catalog build entirely and
return (model, requested_provider, False) verbatim. The slow path would have
reached the same answer; we just avoid paying for the cold catalog.

Coverage:

1. The fast path returns without calling get_available_models() when both
   inputs are present and the model has no @-prefix.
2. The fast path correctly preserves the model and provider unchanged.
3. The slow path still fires for @provider:model qualified IDs (those need
   the catalog to validate the qualifier against the active provider).
4. The slow path still fires when no requested_provider is given (cross-
   provider repair is the slow path's job).
5. The slow path still fires when model is empty (default-model lookup
   requires the catalog).
6. The fast path returns model_was_normalized=False (no repair, just pass-
   through).
"""
import pathlib
from unittest.mock import patch


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


class TestFastPathInvocation:
    """The fast path must skip get_available_models() in the happy case."""

    def test_fast_path_with_bare_model_and_provider_skips_catalog(self):
        """Caller-supplied (model, model_provider) returns without catalog."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "gpt-5.5",
                "openai-codex",
            )

        assert mock_catalog.call_count == 0, (
            "get_available_models() must not be called when both model and "
            "model_provider are supplied and the model has no @provider:model "
            "qualifier — that's the point of the fast path."
        )
        assert result == ("gpt-5.5", "openai-codex", False)

    def test_fast_path_with_openrouter_slash_qualified_model_skips_catalog(self):
        """OpenRouter slash-qualified IDs still hit the fast path.

        Slash-qualified IDs are valid picker output for OpenRouter and a stored
        model_provider is the authoritative routing decision. This remains fast
        for explicit OpenRouter selections.
        """
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "anthropic/claude-opus-4.7",
                "openrouter",
            )

        assert mock_catalog.call_count == 0
        assert result == ("anthropic/claude-opus-4.7", "openrouter", False)

    def test_codex_with_stale_openai_slash_id_uses_catalog_repair(self):
        """Codex must repair stale OpenRouter-shaped OpenAI IDs.

        Browser/localStorage state can submit ``openai/gpt-...`` while the
        session/provider is ``openai-codex``. If the fast path preserves that
        pair, runtime resolution routes through OpenRouter instead of Codex.
        The Codex + ``openai/...`` shape must therefore use the slow-path repair
        and normalize back to the active Codex default.
        """
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [
                    {"provider_id": "openai-codex", "models": [{"id": "gpt-5.5"}]}
                ],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                "openai-codex",
            )

        assert mock_catalog.call_count == 1
        assert result == ("gpt-5.5", "openai-codex", True)

    def test_fast_path_normalizes_provider_default_alias(self):
        """`'default'` is treated as None by _clean_session_model_provider.

        When the requested_provider cleans to None, the fast path must NOT
        fire — the slow path needs to read the catalog to find the active
        provider.
        """
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [
                    {"provider_id": "openai-codex", "models": [{"id": "gpt-5.5"}]}
                ],
            }
            _resolve_compatible_session_model_state("gpt-5.5", "default")

        assert mock_catalog.call_count == 1, (
            "'default' is a sentinel meaning 'no explicit provider'; the slow "
            "path must run so the catalog can supply the active provider."
        )

    def test_fast_path_preserves_explicit_provider_unchanged(self):
        """Caller's explicit provider passes through verbatim, no aliasing."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            # Even a non-canonical provider slug must pass through — this is
            # the contract that resolve_model_provider() in config.py relies on
            # to route through custom: providers.
            result = _resolve_compatible_session_model_state(
                "GLM-4.5-Air-FP8",
                "custom:siliconflow",
            )

        assert mock_catalog.call_count == 0
        assert result[0] == "GLM-4.5-Air-FP8"
        assert result[1] == "custom:siliconflow"
        assert result[2] is False  # model_was_normalized=False


class TestSlowPathStillFires:
    """The slow path must still fire for inputs that require catalog work."""

    def test_at_provider_qualified_model_goes_to_slow_path(self):
        """`@openrouter:foo/bar` strings need the catalog to validate the qualifier."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "anthropic/claude-opus-4.7",
                "groups": [
                    {"provider_id": "openrouter", "models": [{"id": "anthropic/claude-opus-4.7"}]}
                ],
            }
            _resolve_compatible_session_model_state(
                "@openrouter:anthropic/claude-opus-4.7",
                "openrouter",
            )

        assert mock_catalog.call_count == 1, (
            "@provider:model qualified strings need the catalog to verify "
            "the qualifier matches the active provider and to detect stale "
            "cross-provider artifacts (#1253)."
        )

    def test_configured_provider_qualified_model_skips_catalog(self):
        """Configured providers already carry trusted routing context."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        old_cfg = dict(config.cfg)
        config.cfg["model"] = {
            "provider": "openai-codex",
            "default": "gpt-5.5",
        }
        config.cfg["providers"] = {
            "local-llama": {
                "base_url": "http://127.0.0.1:8088/v1",
                "api_key": "test-key",
            },
        }
        try:
            with patch("api.routes.get_available_models") as mock_catalog:
                mock_catalog.side_effect = AssertionError("catalog should not be called")
                result = _resolve_compatible_session_model_state(
                    "@local-llama:unsloth/gemma-4-12b-it-GGUF:UD-Q4_K_XL",
                    "local-llama",
                )
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)

        assert result == (
            "@local-llama:unsloth/gemma-4-12b-it-GGUF:UD-Q4_K_XL",
            "local-llama",
            False,
        )
        assert mock_catalog.call_count == 0

    def test_no_requested_provider_goes_to_slow_path(self):
        """When provider isn't supplied, slow path must repair from catalog."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "anthropic",
                "default_model": "claude-opus-4.7",
                "groups": [
                    {"provider_id": "anthropic", "models": [{"id": "claude-opus-4.7"}]}
                ],
            }
            _resolve_compatible_session_model_state("claude-opus-4.7", None)

        assert mock_catalog.call_count == 1

    def test_empty_model_goes_to_slow_path_for_default_lookup(self):
        """Empty model means 'use the default' — needs catalog to find it."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "default_model": "gpt-5.5",
                "active_provider": "openai-codex",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state("", "openai-codex")

        assert mock_catalog.call_count == 1, (
            "Empty model must consult the catalog to find the configured "
            "default model — the fast path can only short-circuit when a "
            "concrete model is supplied."
        )
        assert result[0] == "gpt-5.5"


class TestFastPathSourceShape:
    """Static checks that the fast path is wired correctly in the source.

    Belt-and-suspenders to ensure a future refactor can't silently remove the
    short-circuit without flipping a test.
    """

    def test_fast_path_branch_present_in_source(self):
        """The fast-path early-return must be in _resolve_compatible_session_model_state."""
        src = _read("api/routes.py")
        idx = src.find("def _resolve_compatible_session_model_state(")
        assert idx != -1
        # Limit search to the function body (~150 lines is enough for the
        # whole helper; the fast path is in the first 50 lines).
        body = src[idx:idx + 6000]
        assert "if model and requested_provider:" in body, (
            "Fast-path guard missing — _resolve_compatible_session_model_state "
            "should short-circuit before get_available_models() when both "
            "inputs are supplied."
        )

    def test_fast_path_runs_before_get_available_models_call(self):
        """The fast-path return must come BEFORE the catalog lookup."""
        src = _read("api/routes.py")
        idx = src.find("def _resolve_compatible_session_model_state(")
        # Helper grew (profile_config, custom repair); 6k window no longer reaches
        # the slow-path catalog call — use a bounded slice through the next def.
        body = src[idx:idx + 12000]
        next_def = body.find("\ndef ", 100)
        if next_def != -1:
            body = body[:next_def]
        fast_path_idx = body.find("if model and requested_provider:")
        catalog_idx = body.find("catalog = get_available_models()")
        assert fast_path_idx != -1 and catalog_idx != -1
        assert fast_path_idx < catalog_idx, (
            "Fast-path guard must precede the catalog call — otherwise the "
            "POST /api/chat/start wedge from #1855 will recur."
        )

    def test_issue_1855_referenced_in_fast_path_docstring(self):
        """The fast-path docstring must reference #1855 for future readers."""
        src = _read("api/routes.py")
        idx = src.find("def _resolve_compatible_session_model_state(")
        body = src[idx:idx + 6000]
        # Stop at the next def to bound the search to this function's body.
        next_def = body.find("\ndef ", 100)
        if next_def != -1:
            body = body[:next_def]
        assert "#1855" in body, (
            "Fast-path docstring should reference #1855 so future readers "
            "understand why the short-circuit exists and what it prevents."
        )


class TestSplitProviderQualifiedModel:
    """Sanity check on the helper used to detect @provider:model strings."""

    def test_at_prefix_with_colon_returns_provider(self):
        from api.routes import _split_provider_qualified_model
        bare, provider = _split_provider_qualified_model("@openrouter:anthropic/claude-opus-4.7")
        assert bare == "anthropic/claude-opus-4.7"
        assert provider == "openrouter"

    def test_bare_model_returns_no_provider(self):
        from api.routes import _split_provider_qualified_model
        bare, provider = _split_provider_qualified_model("gpt-5.5")
        assert bare == "gpt-5.5"
        assert provider is None

    def test_slash_qualified_no_at_returns_no_provider(self):
        """Slash-qualified IDs without @ prefix are not provider-qualified."""
        from api.routes import _split_provider_qualified_model
        bare, provider = _split_provider_qualified_model("anthropic/claude-opus-4.7")
        assert bare == "anthropic/claude-opus-4.7"
        assert provider is None


class TestChatStartHandlerStillStagesResolveModelProvider:
    """The diagnostic stage name must still be 'resolve_model_provider'.

    Production deployments grep stage names for slow-request alerts (#1911).
    Renaming or removing the stage would break those.
    """

    def test_chat_start_emits_resolve_model_provider_stage(self):
        src = _read("api/routes.py")
        # /api/chat/start handler — locate by the resolve_model_provider diag.stage call.
        assert 'diag.stage("resolve_model_provider")' in src, (
            "/api/chat/start handler must emit a 'resolve_model_provider' "
            "diagnostic stage so production slow-request alerts (PR #1911) "
            "continue to surface this stage when it's slow."
        )


class TestValidateResolvedModel:
    """Post-resolution model validation guard.

    _validate_resolved_model() checks the resolved model against the same catalog
    the model picker uses. These tests cover the four scenarios the reviewer
    requested for PR #5205.
    """

    def test_local_provider_model_from_catalog_passes_through(self):
        """A configured local provider whose models come from the catalog /
        live discovery must pass through — the validator rejects only truly
        unknown models, not models that the picker legitimately offered.
        """
        from api.routes import _validate_resolved_model

        with patch("api.config.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "ollama",
                "default_model": "gemma4:e2b",
                "groups": [
                    {"provider_id": "ollama", "models": [
                        {"id": "gemma4:e2b"},
                        {"id": "phi4-mini"},
                    ]},
                ],
            }
            result = _validate_resolved_model("gemma4:e2b", "ollama")

        assert result == "gemma4:e2b", (
            "A model present in the catalog must pass through unchanged."
        )

    def test_invalid_model_falls_back_to_provider_first_model(self):
        """A genuinely invalid model gets a provider-coherent fallback — the
        provider's first catalog model, never the global default.
        """
        from api.routes import _validate_resolved_model

        with patch("api.config.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "ollama",
                "default_model": "gemma4:e2b",
                "groups": [
                    {"provider_id": "ollama", "models": [
                        {"id": "gemma4:e2b"},
                        {"id": "phi4-mini"},
                    ]},
                ],
            }
            result = _validate_resolved_model("nonexistent-model", "ollama")

        assert result == "gemma4:e2b", (
            "Fallback should return the provider's first catalog model, "
            "not the global default."
        )

    def test_empty_model_returns_empty(self):
        """No-model / onboarding-not-done — empty string passes through
        unchanged (no catalog look-up needed).
        """
        from api.routes import _validate_resolved_model

        result = _validate_resolved_model("", "ollama")
        assert result == "", (
            "Empty model string must pass through unchanged — the validator "
            "only acts on concrete model IDs."
        )

    def test_cloud_provider_model_passes_through(self):
        """A cloud provider (openai, openrouter, anthropic) that is present
        in the catalog must not be wrongly rejected. Known models pass through.
        """
        from api.routes import _validate_resolved_model

        with patch("api.config.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai",
                "default_model": "gpt-5.5",
                "groups": [
                    {"provider_id": "openai", "models": [
                        {"id": "gpt-5.5"},
                        {"id": "gpt-5.5-mini"},
                    ]},
                ],
            }
            result = _validate_resolved_model("gpt-5.5", "openai")

        assert result == "gpt-5.5", (
            "A known cloud provider model must pass through."
        )

    def test_provider_not_in_catalog_passes_through(self):
        """A provider not present in the picker catalog must pass through
        untouched — the validator only acts on configured local providers
        that appear in the catalog.
        """
        from api.routes import _validate_resolved_model

        with patch("api.config.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai",
                "default_model": "gpt-5.5",
                "groups": [
                    {"provider_id": "openai", "models": [{"id": "gpt-5.5"}]},
                ],
            }
            result = _validate_resolved_model("claude-opus-4.7", "anthropic")

        assert result == "claude-opus-4.7", (
            "A provider not in the catalog must pass through — the validator "
            "cannot verify models for unknown providers."
        )

    def test_no_model_provider_passes_through(self):
        """When no model_provider is supplied, pass through — the validator
        needs a provider to check against the catalog.
        """
        from api.routes import _validate_resolved_model

        result = _validate_resolved_model("some-model", None)
        assert result == "some-model"

    def test_prefixed_model_for_non_active_provider_passes_through(self):
        """A @provider:model selection for a non-active provider must pass
        through — session-persisted prefixed IDs should match catalog entries
        that went through _apply_provider_prefix.
        """
        from api.routes import _validate_resolved_model

        with patch("api.config.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai",
                "default_model": "gpt-5.5",
                "groups": [
                    {"provider_id": "openai", "models": [{"id": "gpt-5.5"}]},
                    {"provider_id": "ollama", "models": [
                        {"id": "@ollama:gemma4:e2b"},
                        {"id": "@ollama:phi4-mini"},
                    ]},
                ],
            }
            result = _validate_resolved_model("@ollama:gemma4:e2b", "ollama")

        assert result == "@ollama:gemma4:e2b", (
            "Prefixed @provider:model for non-active provider must pass through."
        )

    def test_bare_model_matches_prefixed_catalog_entry(self):
        """A bare model ID persisted in a session must match a catalog entry
        that was rewritten to @provider:model by _apply_provider_prefix
        (non-active provider scenario).
        """
        from api.routes import _validate_resolved_model

        with patch("api.config.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai",
                "default_model": "gpt-5.5",
                "groups": [
                    {"provider_id": "openai", "models": [{"id": "gpt-5.5"}]},
                    {"provider_id": "ollama", "models": [
                        {"id": "@ollama:gemma4:e2b"},
                        {"id": "@ollama:phi4-mini"},
                    ]},
                ],
            }
            result = _validate_resolved_model("gemma4:e2b", "ollama")

        assert result == "gemma4:e2b", (
            "Bare model must match prefixed catalog entry via @provider:bare_model check."
        )

    def test_catalog_failure_passes_through_gracefully(self):
        """If get_available_models() raises, pass through gracefully —
        don't block chat start on a catalog issue.
        """
        from api.routes import _validate_resolved_model

        with patch("api.config.get_available_models") as mock_catalog:
            mock_catalog.side_effect = RuntimeError("catalog timeout")
            result = _validate_resolved_model("gemma4:e2b", "ollama")

        assert result == "gemma4:e2b", (
            "Catalog failure must not block chat start — pass through."
        )
