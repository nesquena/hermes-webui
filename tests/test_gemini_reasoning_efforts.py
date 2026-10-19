"""Regression tests: the reasoning-effort picker is version-gated for Gemini.

Gemini thinking controls exist for 2.5 and later (3.x, 4). The fallback
heuristic used to recognise Gemini only through the ``google/gemini-2`` slash
prefix, so every Gemini 3.x/4 id resolved to ``[]`` and the WebUI hid the
effort picker, while non-thinking Gemini 2.0 ids were wrongly shown it.
"""

import pytest

import api.config as cfg


@pytest.fixture(autouse=True)
def _no_models_dev_metadata(monkeypatch):
    # The real-world failure is a model models.dev has no entry for yet, so the
    # resolver falls through to the heuristic under test.
    monkeypatch.setattr(cfg, "_models_dev_reasoning_efforts", lambda *_a, **_kw: None)


@pytest.mark.parametrize(
    "model_id, provider_id",
    [
        ("google/gemini-4-argon", "openrouter"),
        ("google/gemini-3.1-pro-preview", "openrouter"),
        ("google/gemini-3.8-flash", "openrouter"),
        ("google/gemini-3-flash-preview", "openrouter"),
        ("google/gemini-2.5-pro", "openrouter"),
        ("gemini-4-argon", "gemini"),
        ("gemini-3.1-pro-preview", "gemini"),
        ("gemini-4-argon", "custom:newapi"),
        ("vertex/gemini-4-argon", "custom:newapi"),
    ],
)
def test_thinking_capable_gemini_exposes_efforts(model_id, provider_id):
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id} via {provider_id} should expose reasoning efforts, got {efforts}"
    )


@pytest.mark.parametrize(
    "model_id, provider_id",
    [
        ("google/gemini-2.0-flash-001", "openrouter"),
        ("google/gemini-1.5-pro", "openrouter"),
        ("google/gemini-3-pro-image-preview", "openrouter"),
        ("google/gemini-3.1-flash-image", "openrouter"),
        ("gemini-embedding-001", "gemini"),
        ("vertex/gemini-3-pro-image-preview", "custom:newapi"),
    ],
)
def test_non_thinking_gemini_hides_efforts(model_id, provider_id):
    assert cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id) == [], (
        f"{model_id} via {provider_id} must not expose reasoning efforts"
    )


def test_native_gemini_ladder_never_offers_max():
    efforts = cfg.resolve_model_reasoning_efforts("gemini-4-argon", provider_id="gemini")
    assert efforts and "max" not in efforts
