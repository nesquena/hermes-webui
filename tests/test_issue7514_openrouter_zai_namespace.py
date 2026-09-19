"""Regression tests for #7514: OpenRouter onboarding must serve Z.AI models
under OpenRouter's canonical ``z-ai/`` namespace, not the direct provider's
``zai/`` namespace.

``_FALLBACK_MODELS`` (api/config.py) is authored for the *direct* provider
endpoints, and its Z.AI entries use that provider's own ``zai/`` prefix.
The OpenRouter onboarding setup reused the list verbatim, so the wizard
offered ids such as ``zai/glm-5.3`` — which 404 on openrouter.ai, where the
canonical slug is ``z-ai/glm-5.3``.

These tests pin the boundary:

  1. The ``openrouter`` setup translates ``zai/`` → ``z-ai/`` and never
     serves a ``zai/`` id.
  2. The translation is namespace-only: same models, same order, same labels.
  3. The direct ``zai`` setup keeps ``zai/`` (it talks to the Z.AI endpoint,
     not to OpenRouter).
  4. ``_FALLBACK_MODELS`` itself is never rewritten — other consumers still
     see the direct-provider namespace.
"""

from __future__ import annotations

import api.config as config
import api.onboarding as onboarding


def _models(provider_id: str) -> list[dict]:
    return onboarding._SUPPORTED_PROVIDER_SETUPS[provider_id]["models"]


def _ids(provider_id: str) -> list[str]:
    return [model["id"] for model in _models(provider_id)]


def _expected_openrouter_id(model_id: str) -> str:
    """Independent reference implementation of the namespace translation."""
    if model_id.startswith("zai/"):
        return "z-ai/" + model_id[len("zai/") :]
    return model_id


def test_openrouter_setup_never_serves_direct_zai_namespace():
    served = [model_id for model_id in _ids("openrouter") if model_id.startswith("zai/")]
    assert served == [], (
        "OpenRouter onboarding served ids from the direct-provider namespace; "
        f"these 404 on the OpenRouter API: {served}"
    )


def test_openrouter_setup_exposes_z_ai_models():
    ids = _ids("openrouter")
    assert "z-ai/glm-5.3" in ids
    assert "z-ai/glm-5.3-flash" in ids

    expected = {
        _expected_openrouter_id(model["id"])
        for model in config._FALLBACK_MODELS
        if model["id"].startswith("zai/")
        and model["id"] not in onboarding._OPENROUTER_UNSERVED_IDS
    }
    assert expected, "expected the fallback catalog to carry Z.AI models"
    assert expected.issubset(set(ids)), (
        "every Z.AI model from the fallback catalog must be offered under the "
        f"OpenRouter namespace; missing: {sorted(expected - set(ids))}"
    )


def test_openrouter_projection_is_namespace_only():
    """The OpenRouter list stays a 1:1, order-preserving projection of the
    served models: every fallback entry except the ones OpenRouter does not
    serve (#7520) is projected with the same label and the translated id."""
    fallback = [
        model
        for model in config._FALLBACK_MODELS
        if model["id"] not in onboarding._OPENROUTER_UNSERVED_IDS
    ]
    openrouter = _models("openrouter")

    assert len(openrouter) == len(fallback)
    for source, projected in zip(fallback, openrouter, strict=True):
        assert projected["label"] == source["label"]
        assert projected["id"] == _expected_openrouter_id(source["id"])


def test_direct_zai_setup_keeps_direct_provider_ids():
    """The direct Z.AI setup talks to the Z.AI endpoint (base_url is set on the
    setup), so it keeps its own bare provider ids and must not inherit the
    OpenRouter translation."""
    ids = _ids("zai")
    assert ids == [model["id"] for model in config._PROVIDER_MODELS["zai"]]
    assert "glm-5.3" in ids
    assert [model_id for model_id in ids if model_id.startswith("z-ai/")] == []


def test_fallback_models_are_untouched():
    ids = [model["id"] for model in config._FALLBACK_MODELS]
    assert "zai/glm-5.3" in ids
    assert [model_id for model_id in ids if model_id.startswith("z-ai/")] == []


def test_namespace_helper_is_a_noop_for_other_namespaces_and_bare_ids():
    for model_id in (
        "anthropic/claude-sonnet-4.6",
        "x-ai/grok-4.20",
        "minimax/MiniMax-M3",
        "openrouter/elephant-alpha",
        "gpt-4o",
    ):
        assert onboarding._to_openrouter_namespace(model_id) == model_id
    assert onboarding._to_openrouter_namespace("zai/glm-5.3") == "z-ai/glm-5.3"


def test_setup_catalog_serves_the_translated_namespace():
    """The payload the wizard consumes carries the OpenRouter namespace."""
    catalog = onboarding._build_setup_catalog({})
    entries = {provider["id"]: provider for provider in catalog["providers"]}
    ids = [model["id"] for model in entries["openrouter"]["models"]]

    assert [model_id for model_id in ids if model_id.startswith("zai/")] == []
    assert "z-ai/glm-5.3" in ids
