"""
Regression tests for issue #7333 — slash-id catalog picks drop the session
provider and hit the profile default base_url (Nous -> xAI 404).

``model_with_provider_context()`` historically passed ANY slash-bearing
model ID through bare when the session provider was not OpenRouter, not an
ACP/plugin provider, and not listed under ``config.yaml -> providers:``.
That dropped the provider hint for portal providers (``nous``, ``nvidia``,
``opencode-zen``, ``opencode-go``) and named custom providers
(``custom:<slug>``), so ``resolve_model_provider()`` fell back to the
profile default provider + base_url and sent a vendor-scoped id to the
wrong endpoint (HTTP 404, e.g. ``upstage/solar-pro4:free`` to
``api.x.ai``).

The fix emits an explicit ``@<provider>:<model>`` hint for any KNOWN
routable provider that differs from the configured default, while keeping
the bare passthrough for unknown/ambiguous provider slugs (negative
control) so custom/proxy base_url routing stays in charge.

Both the embedded streaming path (api/streaming.py) and the Gateway
request path (api/routes.py) resolve via the same shared boundary:
``model_with_provider_context(...)`` -> ``resolve_model_provider(...)``,
so exercising that boundary covers both constructions.
"""

import pytest

import api.config as config


def _set_config(provider, base_url=None, default=None, custom_providers=None):
    old_cfg = dict(config.cfg)
    model_cfg = {}
    if provider:
        model_cfg["provider"] = provider
    if base_url:
        model_cfg["base_url"] = base_url
    if default:
        model_cfg["default"] = default
    config.cfg["model"] = model_cfg
    config.cfg["providers"] = {}
    if custom_providers is not None:
        config.cfg["custom_providers"] = custom_providers
    return old_cfg


def _restore(old_cfg):
    config.cfg.clear()
    config.cfg.update(old_cfg)


# ── The reported bug: nous pick under an xai-oauth default ───────────────


def test_nous_slash_id_keeps_provider_hint_under_foreign_default():
    """upstage/solar-pro4:free on nous must NOT fall back to the xAI default."""
    old = _set_config(
        provider="xai-oauth", base_url="https://api.x.ai/v1", default="grok-4.6"
    )
    try:
        encoded = config.model_with_provider_context("upstage/solar-pro4:free", "nous")
        assert encoded == "@nous:upstage/solar-pro4:free", (
            f"expected explicit nous hint, got {encoded!r}"
        )
        model, provider, base_url = config.resolve_model_provider(encoded)
        assert provider == "nous", (
            f"session provider must win over xai-oauth default, got {provider!r}"
        )
        assert model == "upstage/solar-pro4:free"
        assert base_url != "https://api.x.ai/v1", "must not inherit the xAI base_url"
    finally:
        _restore(old)


def test_nous_slash_id_roundtrip_via_canonical_lane():
    """The lane-comparison helper must see nous, not xai-oauth."""
    old = _set_config(
        provider="xai-oauth", base_url="https://api.x.ai/v1", default="grok-4.6"
    )
    try:
        lane_model, lane_provider = config.canonical_model_provider_lane(
            "upstage/solar-pro4:free", "nous"
        )
        assert lane_provider == "nous", f"lane provider={lane_provider!r}"
        assert lane_model == "upstage/solar-pro4:free"
    finally:
        _restore(old)


# ── Same slash ID when the portal IS the profile default ─────────────────


def test_nous_slash_id_when_nous_is_default_stays_bare_and_routes_nous():
    """provider == config_provider keeps the bare id; portal handling keeps it on nous."""
    old = _set_config(provider="nous", default="upstage/solar-pro4:free")
    try:
        encoded = config.model_with_provider_context("upstage/solar-pro4:free", "nous")
        assert encoded == "upstage/solar-pro4:free", (
            f"same-provider pick must stay bare, got {encoded!r}"
        )
        model, provider, _ = config.resolve_model_provider(encoded)
        assert provider == "nous", f"got {provider!r}"
        assert model == "upstage/solar-pro4:free"
    finally:
        _restore(old)


# ── OpenRouter and configured OpenAI-compatible providers (unchanged) ────


def test_openrouter_slash_id_still_gets_hint():
    old = _set_config(provider="anthropic")
    try:
        encoded = config.model_with_provider_context(
            "tencent/hy3-preview:free", "openrouter"
        )
        assert encoded == "@openrouter:tencent/hy3-preview:free", (
            f"openrouter hint unchanged, got {encoded!r}"
        )
        model, provider, _ = config.resolve_model_provider(encoded)
        assert provider == "openrouter"
        assert model == "tencent/hy3-preview:free"
    finally:
        _restore(old)


def test_provider_declared_in_config_still_gets_hint():
    """A provider with a config.yaml providers: block keeps its @hint (unchanged)."""
    old = _set_config(provider="openai", base_url="https://api.openai.com/v1")
    config.cfg["providers"] = {"lmstudio": {"base_url": "http://localhost:1234/v1"}}
    try:
        encoded = config.model_with_provider_context(
            "unsloth/gemma-4-12b-it", "lmstudio"
        )
        assert encoded == "@lmstudio:unsloth/gemma-4-12b-it", (
            f"configured provider must keep hint, got {encoded!r}"
        )
        model, provider, base_url = config.resolve_model_provider(encoded)
        assert provider == "lmstudio"
        assert base_url == "http://localhost:1234/v1"
        assert model == "unsloth/gemma-4-12b-it"
    finally:
        _restore(old)


# ── Named custom provider variant (#7346) ────────────────────────────────


def test_named_custom_slash_id_keeps_hint_under_other_custom_default():
    """custom:proxy-alt slash pick must not fall back to custom:proxy-main."""
    custom_providers = [
        {
            "name": "proxy-main",
            "base_url": "https://main.example/v1",
            "models": {"upstage/solar-pro4:free": {}},
        },
        {
            "name": "proxy-alt",
            "base_url": "https://alt.example/v1",
            "models": {"upstage/solar-pro4:free": {}},
        },
    ]
    old = _set_config(
        provider="custom:proxy-main",
        base_url="https://main.example/v1",
        default="upstage/solar-pro4:free",
        custom_providers=custom_providers,
    )
    try:
        encoded = config.model_with_provider_context(
            "upstage/solar-pro4:free", "custom:proxy-alt"
        )
        assert encoded == "@custom:proxy-alt:upstage/solar-pro4:free", (
            f"named custom hint must be preserved, got {encoded!r}"
        )
        model, provider, _ = config.resolve_model_provider(encoded)
        assert provider == "custom:proxy-alt", (
            f"must route to proxy-alt, got {provider!r}"
        )
        assert model == "upstage/solar-pro4:free"
    finally:
        _restore(old)


def test_named_custom_slash_id_legacy_providers_map_shape():
    """Legacy shape: providers: map uses plain slugs while the stored
    session provider identity is custom:<slug> — hint must survive."""
    old = _set_config(
        provider="custom:proxy-main",
        base_url="https://main.example/v1",
        default="x-ai/grok-4.5",
    )
    # Legacy duplicated providers: map with plain slugs; the stored session
    # provider is the canonical custom:<slug> identity.
    config.cfg["providers"] = {"proxy-alt": {"base_url": "https://alt.example/v1"}}
    config.cfg["custom_providers"] = [
        {
            "name": "proxy-alt",
            "base_url": "https://alt.example/v1",
            "models": {"x-ai/grok-4.5": {}},
        },
    ]
    try:
        encoded = config.model_with_provider_context(
            "x-ai/grok-4.5", "custom:proxy-alt"
        )
        assert encoded == "@custom:proxy-alt:x-ai/grok-4.5", (
            f"legacy-shape custom hint must be preserved, got {encoded!r}"
        )
        model, provider, _ = config.resolve_model_provider(encoded)
        assert provider == "custom:proxy-alt", f"got {provider!r}"
        assert model == "x-ai/grok-4.5"
    finally:
        _restore(old)


# ── Negative control: unknown/ambiguous provider not rewritten ───────────


def test_unknown_provider_slash_id_stays_bare():
    """An unknown provider slug must NOT be rewritten into an @hint; the
    bare id keeps existing custom/proxy base_url routing in charge."""
    old = _set_config(provider="openai", base_url="https://proxy.example/v1")
    try:
        encoded = config.model_with_provider_context(
            "vendor/model", "totally-unknown-provider"
        )
        assert encoded == "vendor/model", (
            f"unknown provider must keep bare passthrough, got {encoded!r}"
        )
    finally:
        _restore(old)


def test_bare_custom_provider_slash_id_stays_bare():
    """Bare 'custom' (no slug) is a proxy pseudo-provider — it keeps the bare
    id so the configured base_url routing stays in charge (unchanged)."""
    old = _set_config(
        provider="custom", base_url="https://proxy.example/v1", default="x-ai/grok-4.5"
    )
    try:
        encoded = config.model_with_provider_context("x-ai/grok-4.5", "custom")
        assert encoded == "x-ai/grok-4.5", (
            f"bare custom must keep bare passthrough, got {encoded!r}"
        )
    finally:
        _restore(old)


def test_unknown_named_custom_slug_slash_id_stays_bare():
    """A custom:<slug> with NO matching custom_providers[] entry must keep the
    bare id — it is the custom-provider version of the unknown-slug negative
    control. Routing it as @custom:missing:vendor/model would send the model
    down a named-provider lane with no matching endpoint (maintainer review
    on #7356)."""
    old = _set_config(
        provider="custom:proxy-main",
        base_url="https://main.example/v1",
        default="upstage/solar-pro4:free",
        custom_providers=[
            {
                "name": "proxy-main",
                "base_url": "https://main.example/v1",
                "models": {"upstage/solar-pro4:free": {}},
            },
        ],
    )
    try:
        encoded = config.model_with_provider_context(
            "upstage/solar-pro4:free", "custom:missing"
        )
        assert encoded == "upstage/solar-pro4:free", (
            f"unknown named custom slug must keep bare passthrough, got {encoded!r}"
        )
    finally:
        _restore(old)


def test_ambiguous_named_custom_slug_fails_closed():
    """Two custom_providers[] entries normalizing to the same slug must fail
    closed (AmbiguousCustomProviderError), matching the point-of-return
    collision guard in resolve_model_provider — a slug we cannot resolve to
    one unique endpoint must not be minted into an @custom: route."""
    old = _set_config(
        provider="openai",
        base_url="https://api.openai.com/v1",
        default="upstage/solar-pro4:free",
        custom_providers=[
            {"name": "proxy-main", "base_url": "https://main.example/v1"},
            {"name": "Proxy Main", "base_url": "https://other.example/v1"},
        ],
    )
    # "proxy-main" and "Proxy Main" both normalize to slug "proxy-main".
    assert config._custom_provider_slug_from_name("Proxy Main") == "custom:proxy-main"
    try:
        with pytest.raises(config.AmbiguousCustomProviderError):
            config.model_with_provider_context(
                "upstage/solar-pro4:free", "custom:proxy-main"
            )
    finally:
        _restore(old)
