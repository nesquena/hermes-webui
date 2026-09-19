"""The OpenRouter setup must not hand out model ids OpenRouter does not serve (#7520).

``_FALLBACK_MODELS`` doubles as the curated OpenRouter list, so every entry
becomes a selectable option in the onboarding wizard.  ``zai/glm-4.5-flash`` is
a Z.AI-direct id with no OpenRouter counterpart: the live catalog serves the
GLM-4.5 generation as ``z-ai/glm-4.5``, ``z-ai/glm-4.5-air`` and
``z-ai/glm-4.5v`` only (audited 2026-09-10 against the 437 ids returned by
``https://openrouter.ai/api/v1/models`` -- zero hits for ``glm-4.5-flash``).
A user who picked it in the wizard got a provider-side failure on the first
message.

These tests drive the real setup catalog that the wizard serves; they do not
match on source text.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.config import _FALLBACK_MODELS
from api.onboarding import (
    _OPENROUTER_UNSERVED_IDS,
    _SUPPORTED_PROVIDER_SETUPS,
    _build_setup_catalog,
)


def _slug(model_id):
    """Last path segment: namespace-agnostic (``zai/x`` == ``z-ai/x``)."""
    return str(model_id or "").rsplit("/", 1)[-1]


def _openrouter_models():
    return _SUPPORTED_PROVIDER_SETUPS["openrouter"]["models"]


def _setup_catalog_openrouter_models():
    catalog = _build_setup_catalog({})
    for provider in catalog["providers"]:
        if provider["id"] == "openrouter":
            return provider["models"]
    raise AssertionError("openrouter missing from the setup catalog payload")


# ── The bug ──────────────────────────────────────────────────────────────


def test_openrouter_setup_never_offers_the_dead_glm_4_5_flash_id():
    """``glm-4.5-flash`` has no OpenRouter counterpart -- it must not be offered.

    Namespace-agnostic on purpose: the entry is dead either way, so both the
    direct-provider spelling (``zai/glm-4.5-flash``) and the OpenRouter one
    (``z-ai/glm-4.5-flash``) are rejected.
    """
    slugs = [_slug(m["id"]) for m in _openrouter_models()]
    assert "glm-4.5-flash" not in slugs, (
        f"OpenRouter setup still offers a model OpenRouter does not serve: {slugs}"
    )


def test_setup_catalog_payload_carries_the_filtered_list():
    """The wizard consumes ``_build_setup_catalog()``, not the raw table."""
    payload = _setup_catalog_openrouter_models()
    assert payload == _openrouter_models()
    assert "glm-4.5-flash" not in [_slug(m["id"]) for m in payload]


# ── Non-regression: only the dead id is dropped ──────────────────────────


def test_openrouter_setup_keeps_every_other_fallback_model():
    """Every other fallback entry still reaches the wizard, same order/labels.

    Compared on the trailing slug so the assertion survives a namespace
    translation at this boundary (``zai/x`` -> ``z-ai/x``): what must hold is
    that nothing else is dropped or reordered.
    """
    expected = [
        (_slug(m["id"]), m["label"])
        for m in _FALLBACK_MODELS
        if m["id"] not in _OPENROUTER_UNSERVED_IDS
    ]
    got = [(_slug(m["id"]), m["label"]) for m in _openrouter_models()]
    assert [i for i, _ in got] == [i for i, _ in expected], (
        "the OpenRouter list must stay a 1:1 projection of the fallback catalog "
        "minus the unserved ids -- order included"
    )
    assert [lbl for _, lbl in got] == [lbl for _, lbl in expected]


def test_every_unserved_id_exists_in_the_fallback_catalog():
    """The drop list is a filter, not a place to park made-up ids."""
    fallback_ids = {m["id"] for m in _FALLBACK_MODELS}
    unknown = _OPENROUTER_UNSERVED_IDS - fallback_ids
    assert not unknown, f"unserved ids not present in _FALLBACK_MODELS: {sorted(unknown)}"
    assert _OPENROUTER_UNSERVED_IDS <= fallback_ids


def test_direct_zai_setup_keeps_its_provider_native_ids():
    """The filter is OpenRouter-only: the direct Z.AI setup is untouched.

    ``glm-4.5-flash`` is a real id for the provider's own endpoint, so dropping
    it from ``_FALLBACK_MODELS`` (instead of from this projection) would have
    broken the direct setup.
    """
    zai_ids = [m["id"] for m in _SUPPORTED_PROVIDER_SETUPS["zai"]["models"]]
    assert "glm-4.5-flash" in zai_ids, (
        f"the direct Z.AI setup must keep the provider-native ids: {zai_ids}"
    )


def test_openrouter_catalog_still_offers_the_glm_4_5_generation():
    """The drop removes one dead id, not the GLM-4.5 family."""
    slugs = {_slug(m["id"]) for m in _openrouter_models()}
    assert {"glm-4.5", "glm-4.7", "glm-5.3"} <= slugs, slugs
