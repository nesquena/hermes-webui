"""Tests for _catalog_has_provider() with compound provider IDs.

Compound provider IDs like "custom:zenmux-relay" or "custom:glm-free-relay"
normalize to "custom" via _normalize_provider_id(). The existing three
clauses in _catalog_has_provider() already handle these correctly:

  C1: exact raw match  ("custom:zenmux-relay" ∈ raw_ids)
  C2: normalized ∈ raw_ids  ("custom" ∈ raw_ids)
  C3: normalized ∈ norm_ids ("custom" ∈ norm_ids)

This test guards against regressions and verifies that no 4th clause is
needed for compound IDs.
"""
from __future__ import annotations

from api.routes import _catalog_has_provider


def test_compound_provider_exact_raw_match():
    """C1: exact raw match for a compound ID like 'custom:zenmux-relay'."""
    assert _catalog_has_provider(
        "custom:zenmux-relay",
        "custom",
        {"custom:zenmux-relay"},
        {"custom"},
    )


def test_compound_provider_normalized_in_raw_ids():
    """C2: normalized form 'custom' found in raw_provider_ids."""
    assert _catalog_has_provider(
        "custom:zenmux-relay",
        "custom",
        {"custom"},
        {"custom"},
    )


def test_compound_provider_normalized_in_norm_ids():
    """C3: normalized form 'custom' found in normalized_provider_ids."""
    assert _catalog_has_provider(
        "custom:zenmux-relay",
        "custom",
        {"other"},
        {"custom"},
    )


def test_compound_provider_not_found():
    """Returns False when neither raw nor normalized form is in the catalog."""
    assert not _catalog_has_provider(
        "custom:unknown-relay",
        "custom",
        {"openrouter", "nous"},
        {"openrouter", "nous"},
    )


def test_compound_provider_glm_free_relay():
    """Real-world case: custom:glm-free-relay providing z-ai/glm-5.2-free."""
    assert _catalog_has_provider(
        "custom:glm-free-relay",
        "custom",
        {"custom:glm-free-relay"},
        {"custom"},
    )
