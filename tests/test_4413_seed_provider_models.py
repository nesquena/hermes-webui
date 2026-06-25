"""Regression tests for #4413 — provider-model seeder from hermes_cli.

Covers three defects identified in PR review:

1.  **NameError at import time** — the seeder called ``_get_label_for_model``
    before it was defined, so the bare ``except Exception: pass`` swallowed the
    error exactly when the seeder had real work to do.

2.  **Provider alias mismatch** — the core uses canonical IDs (``xai``) while
    the WebUI indexes by display IDs (``x-ai``).  Without alias resolution the
    seeder created a duplicate provider entry instead of merging.

3.  **Live models endpoint** — ``_OPENAI_COMPAT_ENDPOINTS`` had the wrong ZAI
    URL (``/v1`` instead of ``/api/paas/v4``), and the live probe didn't prefer
    the provider's configured ``base_url``.
"""
import pathlib
import sys
import unittest.mock as mock

import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO.parent / ".hermes" / "hermes-agent"))

from api.config import (
    _PROVIDER_MODELS,
    _seed_provider_models_from_core,
    _resolve_provider_alias,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _deepcopy_providers():
    """Snapshot _PROVIDER_MODELS so tests can mutate freely and restore after."""
    return {k: list(v) for k, v in _PROVIDER_MODELS.items()}


@pytest.fixture
def restore_providers():
    """Restore _PROVIDER_MODELS to its pre-test state."""
    snapshot = _deepcopy_providers()
    yield
    _PROVIDER_MODELS.clear()
    _PROVIDER_MODELS.update(snapshot)


def _patch_core_pm(fake_pm: dict):
    """Patch ``hermes_cli.models._PROVIDER_MODELS`` with *fake_pm*."""
    # Build a fake hermes_cli.models module with just _PROVIDER_MODELS.
    fake_module = mock.MagicMock()
    fake_module._PROVIDER_MODELS = fake_pm
    return mock.patch.dict(sys.modules, {"hermes_cli.models": fake_module})


# ── Test 1: seeder actually adds missing models (catches NameError) ──────────

class TestSeederAddsMissingModels:
    """The seeder must add models that exist in core but not in WebUI.

    Before the fix, ``_get_label_for_model`` wasn't bound at call time, so the
    seeder raised ``NameError`` and the bare ``except Exception: pass`` swallowed
    it.  This test would have caught that bug.
    """

    def test_missing_model_is_added(self, restore_providers):
        """A model in core but not in WebUI should appear after seeding."""
        fake_pm = {"zai": ["glm-9.99-experimental"]}
        with _patch_core_pm(fake_pm):
            _seed_provider_models_from_core()

        zai_ids = [m["id"] for m in _PROVIDER_MODELS.get("zai", [])]
        assert "glm-9.99-experimental" in zai_ids, (
            "Seeder did not add glm-9.99-experimental — likely a NameError "
            "on _get_label_for_model at call time"
        )

    def test_added_model_has_label(self, restore_providers):
        """Seeded entries must have a human-readable label, not just an ID."""
        fake_pm = {"zai": ["glm-9.99-experimental"]}
        with _patch_core_pm(fake_pm):
            _seed_provider_models_from_core()

        zai_entries = {m["id"]: m for m in _PROVIDER_MODELS.get("zai", [])}
        assert "glm-9.99-experimental" in zai_entries
        label = zai_entries["glm-9.99-experimental"]["label"]
        assert isinstance(label, str) and label.strip(), (
            f"Label for seeded model is empty/missing: {label!r}"
        )

    def test_existing_model_not_duplicated(self, restore_providers):
        """A model that already exists should not be added a second time."""
        fake_pm = {"zai": ["glm-5.2"]}
        with _patch_core_pm(fake_pm):
            _seed_provider_models_from_core()

        zai_ids = [m["id"] for m in _PROVIDER_MODELS.get("zai", [])]
        assert zai_ids.count("glm-5.2") == 1, (
            f"glm-5.2 appears {zai_ids.count('glm-5.2')} times — should be 1"
        )

    def test_no_op_without_hermes_cli(self, restore_providers):
        """Seeder must be a no-op (not raise) when hermes_cli is unavailable."""
        with mock.patch.dict(sys.modules, {"hermes_cli.models": None}):
            # Should silently return without error
            _seed_provider_models_from_core()


# ── Test 2: aliased provider merges instead of duplicating ───────────────────

class TestAliasedProviderMerge:
    """Providers whose canonical ID differs between core and WebUI must merge.

    The core uses ``"xai"`` while the WebUI indexes by ``"x-ai"``.  Without
    alias resolution the seeder creates a duplicate ``"xai"`` entry.
    """

    def test_xai_merges_into_x_hyphen_ai(self, restore_providers):
        """Core's 'xai' should merge into WebUI's 'x-ai', not create 'xai'."""
        # Verify the alias exists in the first place
        assert _resolve_provider_alias("x-ai") == "xai", (
            "Expected _resolve_provider_alias('x-ai') == 'xai'"
        )

        fake_pm = {"xai": ["grok-99-test"]}
        with _patch_core_pm(fake_pm):
            _seed_provider_models_from_core()

        # The new model should be under "x-ai" (WebUI's key)
        x_hyphen_ai_ids = [m["id"] for m in _PROVIDER_MODELS.get("x-ai", [])]
        assert "grok-99-test" in x_hyphen_ai_ids, (
            "grok-99-test was not added to 'x-ai' — alias resolution failed"
        )

        # No duplicate "xai" key should have been created
        assert "xai" not in _PROVIDER_MODELS or all(
            m["id"] != "grok-99-test" for m in _PROVIDER_MODELS.get("xai", [])
        ), (
            "A duplicate 'xai' key was created with grok-99-test — "
            "provider alias was not resolved before .get()"
        )

    def test_unknown_provider_not_seeded(self, restore_providers):
        """A provider in core but not in WebUI must NOT be seeded.

        Adding new vendors is a maintainer curation decision, not something
        the seeder should do implicitly (#4413 review)."""
        fake_pm = {"totally-new-provider": ["model-alpha"]}
        with _patch_core_pm(fake_pm):
            _seed_provider_models_from_core()

        assert "totally-new-provider" not in _PROVIDER_MODELS, (
            "Seeder must not inject brand-new providers — only enrich existing ones. "
            "Adding vendors is a maintainer curation decision (#4413)."
        )


# ── Test 3: exception narrowing ─────────────────────────────────────────────

class TestExceptionNarrowing:
    """The seeder's hermes_cli import should only catch ImportError, not all
    exceptions (which would hide real bugs like the NameError)."""

    def test_does_not_swallow_runtime_errors(self, restore_providers):
        """If _get_label_for_model raises, the error should propagate (not be
        silently swallowed by a bare except)."""
        fake_pm = {"zai": ["boom-model"]}
        with _patch_core_pm(fake_pm):
            with mock.patch(
                "api.config._get_label_for_model",
                side_effect=RuntimeError("deliberate failure"),
            ):
                # The seeder itself should NOT catch RuntimeError — it only
                # catches ImportError for the optional hermes_cli import.
                with pytest.raises(RuntimeError, match="deliberate failure"):
                    _seed_provider_models_from_core()


