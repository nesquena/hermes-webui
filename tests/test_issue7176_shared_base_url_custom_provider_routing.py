# Copyright 2025 the Hermes WebUI contributors
# SPDX-License-Identifier: MIT

"""Regression tests for GitHub issue #7176.

Symptom: an internal gateway that fronts several APIs on ONE physical
``base_url`` (e.g. an OpenAI-chat route, an Anthropic-messages route, and a
Responses/codex route all served from the same host) is commonly declared as
several ``custom_providers[]`` entries sharing that ``base_url``, one per
``api_mode``. When ``model.provider: custom`` + ``model.base_url`` point at
that shared endpoint, resolving the ACTIVE provider always returned the
FIRST ``custom_providers[]`` entry with a matching ``base_url`` — regardless
of which entry actually declares the configured ``model.default`` — because
``_named_custom_provider_slug_for_base_url()`` did a plain first-match scan
with no model awareness.

Concretely: with

    model:
      default: claude-sonnet-5@default
      provider: custom
      base_url: https://gateway.example/v1
    custom_providers:
      - name: Gateway OpenAI Chat      # listed first
        base_url: https://gateway.example/v1
        api_mode: chat_completions
        models: {gpt-5: {}, ...}       # does NOT include claude-sonnet-5@default
      - name: Gateway Claude
        base_url: https://gateway.example/v1
        api_mode: anthropic_messages
        models: {claude-sonnet-5@default: {}, ...}

every chat request for ``claude-sonnet-5@default`` was silently routed
through "Gateway OpenAI Chat" (``chat_completions``), which the real gateway
correctly rejects (observed as an opaque upstream 401/400 with no client-side
clue) since that model only exists on the Anthropic-style route.

Fix: ``_named_custom_provider_slug_for_base_url()`` now accepts an optional
``model_id`` and prefers an entry that actually owns that model (by its
``model`` field or ``models`` allowlist) over base_url declaration order,
mirroring the disambiguation ``resolve_model_provider()`` already does when a
model is selected explicitly from the picker. ``resolve_model_provider()``
now forwards its own ``model_id`` argument through
``_resolve_configured_provider_id()`` so this disambiguation actually fires
on the path that resolves ``model.provider: custom`` + ``model.default``.
"""

from api.config import (
    _named_custom_provider_slug_for_base_url,
    resolve_model_provider,
)


SHARED_BASE_URL = "https://gateway.example/v1"

CUSTOM_PROVIDERS = [
    {
        "name": "Gateway OpenAI Chat",
        "base_url": SHARED_BASE_URL,
        "api_mode": "chat_completions",
        "models": {"gpt-5": {}, "gpt-5-mini": {}},
    },
    {
        "name": "Gateway Claude",
        "base_url": SHARED_BASE_URL,
        "api_mode": "anthropic_messages",
        "models": {"claude-sonnet-5@default": {}, "claude-opus-5@default": {}},
    },
]


def _apply_config(cfg_module, model_id: str):
    old_model = cfg_module.cfg.get("model")
    old_custom = cfg_module.cfg.get("custom_providers")
    cfg_module.cfg["model"] = {
        "default": model_id,
        "provider": "custom",
        "base_url": SHARED_BASE_URL,
    }
    cfg_module.cfg["custom_providers"] = CUSTOM_PROVIDERS
    return old_model, old_custom


def _restore(cfg_module, old_model, old_custom):
    if old_model is None:
        cfg_module.cfg.pop("model", None)
    else:
        cfg_module.cfg["model"] = old_model
    if old_custom is None:
        cfg_module.cfg.pop("custom_providers", None)
    else:
        cfg_module.cfg["custom_providers"] = old_custom


def test_slug_for_base_url_prefers_owning_entry_over_declaration_order():
    """Unit-level: passing model_id picks the entry that owns the model,
    not simply the first custom_providers[] entry with a matching base_url.
    """
    import api.config as cfg_mod

    slug = _named_custom_provider_slug_for_base_url(
        SHARED_BASE_URL, {"custom_providers": CUSTOM_PROVIDERS},
        model_id="claude-sonnet-5@default",
    )
    assert slug == "custom:gateway-claude", (
        f"Expected the Claude entry (which owns claude-sonnet-5@default), "
        f"got {slug!r} — likely fell back to declaration-order first match."
    )

    # Sanity: a model owned by the first entry still resolves to it.
    slug_openai = _named_custom_provider_slug_for_base_url(
        SHARED_BASE_URL, {"custom_providers": CUSTOM_PROVIDERS},
        model_id="gpt-5",
    )
    assert slug_openai == "custom:gateway-openai-chat"

    # No model_id: preserves prior first-match behavior for existing callers.
    slug_no_model = _named_custom_provider_slug_for_base_url(
        SHARED_BASE_URL, {"custom_providers": CUSTOM_PROVIDERS},
    )
    assert slug_no_model == "custom:gateway-openai-chat"


def test_resolve_model_provider_routes_configured_default_to_owning_entry():
    """End-to-end: resolve_model_provider() for the configured default model
    must route through the custom_providers[] entry that actually declares
    it, not whichever shared-base_url entry is listed first.
    """
    import api.config as cfg_mod

    old_model, old_custom = _apply_config(cfg_mod, "claude-sonnet-5@default")
    try:
        model, provider, base_url = resolve_model_provider("claude-sonnet-5@default")
        assert provider == "custom:gateway-claude", (
            f"Expected provider=custom:gateway-claude, got {provider!r}. "
            f"The configured Claude model was routed through the wrong "
            f"shared-base_url custom provider entry (issue #7176)."
        )
        assert base_url == SHARED_BASE_URL
        assert model == "claude-sonnet-5@default"
    finally:
        _restore(cfg_mod, old_model, old_custom)


def test_resolve_model_provider_still_routes_other_default_correctly():
    """Symmetry check: when the configured default belongs to the FIRST
    shared-base_url entry instead, resolution must still land there (proves
    the fix is genuinely model-aware, not just order-reversed).
    """
    import api.config as cfg_mod

    old_model, old_custom = _apply_config(cfg_mod, "gpt-5")
    try:
        model, provider, base_url = resolve_model_provider("gpt-5")
        assert provider == "custom:gateway-openai-chat", (
            f"Expected provider=custom:gateway-openai-chat, got {provider!r}"
        )
        assert base_url == SHARED_BASE_URL
        assert model == "gpt-5"
    finally:
        _restore(cfg_mod, old_model, old_custom)


def test_minimal_static_models_catalog_active_provider_owns_default_model():
    """Reproduces the exact real-world failure (#7176): the WebUI's /api/models
    'Default' group and its active_provider must reflect the custom_providers[]
    entry that actually owns the configured default model, not just the first
    entry sharing model.base_url. This was the code path that silently routed
    every chat turn to the wrong endpoint (observed by the reporter as an
    upstream 401 'Invalid ApiKey' with no client-side indication of why).
    """
    import api.config as cfg_mod

    old_model, old_custom = _apply_config(cfg_mod, "claude-sonnet-5@default")
    try:
        result = cfg_mod._minimal_static_models_catalog()
        assert result["active_provider"] == "custom:gateway-claude", (
            f"Expected active_provider=custom:gateway-claude, got "
            f"{result['active_provider']!r}. The configured default model "
            f"was attributed to the wrong shared-base_url custom provider."
        )
        assert result["default_model"] == "claude-sonnet-5@default"
    finally:
        _restore(cfg_mod, old_model, old_custom)


# ---------------------------------------------------------------------------
# Regression coverage for review defects found on the #7176 fix itself.
# ---------------------------------------------------------------------------


def test_malformed_later_entry_does_not_crash_resolution():
    """CORE defect #1 (review): a valid first match followed by a malformed
    ``base_url`` on a LATER shared-base_url entry must not abort resolution
    for the whole scan. Before the fix, ``urlparse("http://[")`` raises
    ``ValueError: Invalid IPv6 URL`` and that exception propagated straight
    out of ``_named_custom_provider_slug_for_base_url``, breaking provider
    resolution (and therefore chat start) entirely — even though a perfectly
    valid earlier entry already matched.
    """
    from api.config import _named_custom_provider_slug_for_base_url

    config_obj = {
        "custom_providers": [
            {"name": "A", "base_url": "http://gw.local", "model": "x"},
            {"name": "B", "base_url": "http://[", "model": "y"},
        ]
    }
    # Must not raise, and must still resolve the valid entry.
    slug = _named_custom_provider_slug_for_base_url(
        "http://gw.local", config_obj, model_id="x"
    )
    assert slug == "custom:a"


def test_configured_model_ids_decodes_json_array_string():
    """CORE defect #2 (review): ``models`` persisted as a JSON-encoded string
    (e.g. ``'["claude-sonnet-5@default"]'``) must be decoded like a native
    list/dict, not silently treated as an empty allowlist. Before the fix,
    ``_configured_model_ids('[\"claude\"]')`` returned ``[]``, so an entry
    whose ``models`` field round-tripped through a string-serializing layer
    would never be recognized as owning any model and ownership matching
    would silently fall back to declaration-order (the exact bug this PR
    fixes for the native dict/list shapes).
    """
    from api.config import _configured_model_ids

    assert _configured_model_ids('["claude-sonnet-5@default", "gpt-5"]') == [
        "claude-sonnet-5@default",
        "gpt-5",
    ]
    # A dict-shaped JSON string also decodes.
    assert _configured_model_ids('{"claude-sonnet-5@default": {}}') == [
        "claude-sonnet-5@default"
    ]
    # Invalid JSON / non-list-or-dict decodes still degrade to empty, not a crash.
    assert _configured_model_ids("not json") == []
    assert _configured_model_ids("42") == []
    assert _configured_model_ids("") == []
    # Existing native shapes are unaffected.
    assert _configured_model_ids(["a", "b"]) == ["a", "b"]
    assert _configured_model_ids({"a": {}}) == ["a"]


def test_slug_for_base_url_prefers_owning_entry_with_json_string_models():
    """End-to-end version of defect #2: the ownership-preferring resolver
    must recognize a JSON-string ``models`` allowlist the same way it
    recognizes a native list/dict one.
    """
    from api.config import _named_custom_provider_slug_for_base_url

    config_obj = {
        "custom_providers": [
            {
                "name": "Gateway OpenAI Chat",
                "base_url": SHARED_BASE_URL,
                "models": '["gpt-5", "gpt-5-mini"]',
            },
            {
                "name": "Gateway Claude",
                "base_url": SHARED_BASE_URL,
                "models": '["claude-sonnet-5@default", "claude-opus-5@default"]',
            },
        ]
    }
    slug = _named_custom_provider_slug_for_base_url(
        SHARED_BASE_URL, config_obj, model_id="claude-sonnet-5@default"
    )
    assert slug == "custom:gateway-claude", (
        f"Expected the Claude entry (JSON-string models allowlist) to be "
        f"recognized as owning the model, got {slug!r}."
    )


def test_static_models_catalog_without_live_probes_passes_model_id_through():
    """CORE defect #1/cold-catalog gap (review): the uncached
    ``_static_models_catalog_without_live_probes()`` builder must also
    resolve the *initial endpoint probe's* provider attribution
    model-aware, not just the already-fixed ``active_provider`` field.
    Before the fix, ``_configured_provider_for_base_url()`` (used to key
    ``auto_detected_models_by_provider`` and named-provider bookkeeping)
    called ``_resolve_configured_provider_id()`` WITHOUT ``model_id``, so a
    shared-base_url probe could still be filed under the sibling provider
    even though ``active_provider`` elsewhere in the same function was
    already correctly resolved to the model-owning entry. This builder is
    network-free by design (its whole point is "no live probes"), so no
    mocking of network calls is needed here.
    """
    import api.config as cfg_mod

    old_model, old_custom = _apply_config(cfg_mod, "claude-sonnet-5@default")
    try:
        result = cfg_mod._static_models_catalog_without_live_probes()
        assert result["active_provider"] == "custom:gateway-claude", (
            f"Expected active_provider=custom:gateway-claude, got "
            f"{result['active_provider']!r}."
        )
    finally:
        _restore(cfg_mod, old_model, old_custom)
