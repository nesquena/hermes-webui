"""#7170 round-6 review: coercion must thread the PROFILE config, not ``cfg``.

Two scope leaks in the reasoning-capability coercion chain:

1. ``configured_reasoning_effort_for_model(config_data)`` correctly received
   the profile-scoped ``config_data`` and chose the right effort, but handed it
   to ``coerce_reasoning_effort_for_model()`` which internally consulted the
   MODULE-GLOBAL ``cfg`` for custom-provider metadata and the LM Studio
   endpoint/key. On a multi-profile instance the chosen effort was then coerced
   against ANOTHER profile's capability ladder.

2. The no-hermes-core fallback resolver only expanded the MODEL side of an
   override key, never the OVERRIDE side: model ``gpt-5.4-mini`` with override
   ``openai/gpt-5.4-mini: low`` and global ``high`` resolved to ``high`` because
   the provider-qualified key was never a candidate. Core's
   ``_canonical_model_variants`` adds known provider prefixes (and known
   aggregator prefixes to the resulting single-slash forms) — the fallback must
   mirror both passes.

"""

import sys

import pytest

from api import config as cfg
from api import gateway_chat


# ─────────────────────────── Fix A: coercion threading ─────────────────────────


class _FakeProbe:
    """Stand-in for ``_lmstudio_model_reasoning_options`` recording calls."""

    def __init__(self, options):
        self._options = list(options)
        self.calls: list[dict] = []

    def __call__(self, model, base_url, *, api_key=None, timeout=5.0):
        # Record the effective endpoint/key the coercion chain resolved from
        # the CALLER'S profile config, not the ambient module-global cfg.
        self.calls.append(
            {
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
            }
        )
        return list(self._options)


def test_coercion_uses_profile_config_lmstudio_endpoint_not_ambient(monkeypatch):
    """The LM Studio probe must hit the PROFILE's endpoint, not the ambient one.

    ``configured_reasoning_effort_for_model(config_data)`` picked the effort
    from ``config_data`` but the coercion called the module-global ``cfg``, so
    a profile-B session was probed against profile A's LM Studio endpoint (and
    could even send A's API key) — clamping B's effort against A's ladder.
    """
    profile_b_lmstudio = "http://profile-b-host:1234/v1"
    ambient_lmstudio = "http://profile-a-host:1234/v1"
    # A's key must NEVER be forwarded to B's endpoint.
    profile_b_key = "key-profile-b"
    ambient_key = "key-profile-a"

    probe = _FakeProbe(["low", "medium"])  # ladder tops out at "medium"
    monkeypatch.setattr(cfg, "_lmstudio_model_reasoning_options", probe)

    # Ambient (module-global) cfg: different endpoint AND different key.
    monkeypatch.setattr(
        cfg, "cfg",
        {
            "model": {"provider": "lmstudio", "base_url": ambient_lmstudio, "api_key": ambient_key},
            "providers": {"lmstudio": {"base_url": ambient_lmstudio, "api_key": ambient_key}},
        },
    )

    # Profile B's snapshot: its OWN LM Studio endpoint + key.
    profile_b_cfg = {
        "model": {"provider": "lmstudio", "base_url": profile_b_lmstudio, "api_key": profile_b_key},
        "providers": {"lmstudio": {"base_url": profile_b_lmstudio, "api_key": profile_b_key}},
        # B stores "high"; A's probe ladder tops out at "medium".
        "agent": {"reasoning_effort": "low", "reasoning_overrides": {"local-thinker": "high"}},
    }

    effort = cfg.configured_reasoning_effort_for_model(
        profile_b_cfg, model_id="local-thinker", provider_id="lmstudio"
    )

    assert probe.calls, "LM Studio probe was never invoked"
    last = probe.calls[-1]
    # Profile B's endpoint, not the ambient profile A's.
    assert cfg._normalize_base_url_for_match(last["base_url"]) == cfg._normalize_base_url_for_match(
        profile_b_lmstudio
    ), f"probe hit {last['base_url']!r} instead of the profile's endpoint"
    # Profile B's key, not the ambient profile A's.
    assert last["api_key"] == profile_b_key, (
        f"probe sent {last['api_key']!r} — the ambient profile's credential leaked"
    )
    # Clamped against B's ladder (medium), not A's.
    assert effort == "medium"


def test_coercion_uses_profile_config_custom_provider_metadata(monkeypatch):
    """Custom-provider ``reasoning_efforts`` metadata must come from the profile.

    ``_resolve_model_reasoning_efforts_impl`` enumerated ``custom_providers``
    via the module-global ``cfg``, so profile A's custom-provider capability
    list decided profile B's clamp.
    """
    # Ambient cfg declares the same-named custom provider with a ladder that
    # tops out at "low" — if coercion still reads cfg, "medium" clamps to "low".
    monkeypatch.setattr(
        cfg, "cfg",
        {
            "custom_providers": [
                {
                    "name": "My Gateway",
                    "base_url": "https://ambient.example.com/v1",
                    "reasoning_efforts": ["low"],
                }
            ]
        },
    )

    # Profile B's snapshot: the SAME slug carries a ladder up to "high".
    profile_b_cfg = {
        "custom_providers": [
            {
                "name": "My Gateway",
                "base_url": "https://profile-b.example.com/v1",
                "reasoning_efforts": ["low", "medium", "high"],
            }
        ],
        "agent": {"reasoning_effort": "low", "reasoning_overrides": {"some-model": "medium"}},
    }

    effort = cfg.configured_reasoning_effort_for_model(
        profile_b_cfg,
        model_id="some-model",
        provider_id="custom:my-gateway",
    )
    # Profile B's ladder allows "medium"; A's ["low"] would have clamped it.
    assert effort == "medium"


def test_coercion_ambient_metadata_does_not_clamp_profile_effort(monkeypatch):
    """Differential: the ambient ladder must not reach the profile's resolution."""
    monkeypatch.setattr(
        cfg, "cfg",
        {
            "custom_providers": [
                {
                    "name": "My Gateway",
                    "base_url": "https://ambient.example.com/v1",
                    "reasoning_efforts": ["minimal"],
                }
            ]
        },
    )
    profile_cfg = {
        "custom_providers": [
            {
                "name": "My Gateway",
                "base_url": "https://profile.example.com/v1",
                "reasoning_efforts": ["low", "medium", "high", "xhigh"],
            }
        ],
        "agent": {"reasoning_effort": "minimal", "reasoning_overrides": {"m": "high"}},
    }
    assert (
        cfg.configured_reasoning_effort_for_model(
            profile_cfg, model_id="m", provider_id="custom:my-gateway"
        )
        == "high"
    )


def test_gateway_path_coercion_uses_dispatched_snapshot(monkeypatch):
    """The gateway bridge path must coerce against the dispatched profile cfg.

    ``_gateway_reasoning_effort_for_request`` passes the snapshot it was handed;
    the coercion chain must resolve custom-provider metadata from that snapshot
    and never from the ambient module-global cfg.
    """
    monkeypatch.setattr(
        cfg, "cfg",
        {"custom_providers": [{"name": "Gw", "base_url": "https://a/v1", "reasoning_efforts": ["low"]}]},
    )
    dispatched = {
        "custom_providers": [{"name": "Gw", "base_url": "https://b/v1", "reasoning_efforts": ["low", "high"]}],
        "agent": {"reasoning_effort": "low", "reasoning_overrides": {"m": "high"}},
    }
    assert (
        gateway_chat._gateway_reasoning_effort_for_request(
            dispatched, model="m", model_provider="custom:gw"
        )
        == "high"
    )


# ───────────────── Fix B: no-core provider-qualified override keys ─────────────


@pytest.fixture
def no_core(monkeypatch):
    """Simulate an install without the hermes companion agent tree.

    Core's resolver is stashed under a module key the WebUI never imports, so
    the differential test can still ask core for the expected answer after the
    import inside ``configured_reasoning_effort_for_model`` has been suppressed.
    """
    core_resolve = None
    try:
        import hermes_constants as _core  # type: ignore[import-not-found]

        core_resolve = _core.resolve_per_model_reasoning_effort
    except Exception:
        core_resolve = None
    sys.modules["_hermes_core_stash"] = core_resolve  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "hermes_constants", None)


def _write_overrides(overrides, global_effort="high"):
    return {"agent": {"reasoning_effort": global_effort, "reasoning_overrides": overrides}}


def test_fallback_matches_provider_qualified_override_for_bare_model(no_core):
    """Reviewer repro: bare model + provider-qualified override key.

    model ``gpt-5.4-mini`` with override ``openai/gpt-5.4-mini: low`` and
    global ``high`` resolved to ``high`` — the qualified override key was never
    a candidate because only the model side was expanded.
    """
    data = _write_overrides({"openai/gpt-5.4-mini": "low"})

    assert cfg.configured_reasoning_effort_for_model(data, model_id="gpt-5.4-mini") == "low"
    # Same through the gateway bridge path.
    assert (
        gateway_chat._gateway_reasoning_effort_for_request(
            data, model="gpt-5.4-mini", model_provider="openai"
        )
        == "low"
    )


def test_fallback_matches_aggregator_qualified_override_for_provider_model(no_core):
    """Aggregator-qualified key ``openrouter/openai/gpt-5.4-mini``.

    The model id ``openai/gpt-5.4-mini`` lost its aggregator segment; core's
    second prefix pass re-adds known aggregators to the single-slash forms, and
    the fallback must do the same.
    """
    data = _write_overrides({"openrouter/openai/gpt-5.4-mini": "low"})
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="openai/gpt-5.4-mini"
        )
        == "low"
    )


def test_fallback_matches_aggregator_qualified_override_for_bare_model(no_core):
    """Bare model must also match a fully aggregator-qualified override key."""
    data = _write_overrides({"openrouter/openai/gpt-5.4-mini": "low"})
    assert (
        cfg.configured_reasoning_effort_for_model(data, model_id="gpt-5.4-mini") == "low"
    )


def test_fallback_exact_override_outranks_provider_qualified(no_core):
    """Precedence must match core: the exact/explicit key wins.

    Core emits the exact form first, so a full-id override still outranks a
    bare-tail one — and adding the prefixed candidates must not reorder that.
    """
    data = _write_overrides(
        {
            "openai/gpt-5.4-mini": "minimal",  # prefixed, added later
            "gpt-5.4-mini": "low",  # bare, base candidate
        }
    )
    assert (
        cfg.configured_reasoning_effort_for_model(data, model_id="gpt-5.4-mini")
        == "low"
    ), "bare base candidate must outrank the provider-prefixed form"


def test_fallback_provider_qualified_key_does_not_match_unrelated_provider(no_core):
    """Only the model's OWN known provider prefixes may match.

    ``claude-opus-4-5`` with override ``openai/claude-opus-4-5`` exists only as
    a prefixed spelling of the bare candidate, so core DOES match it — but an
    override keyed on a provider-prefixed form of a DIFFERENT model must never
    match.
    """
    data = _write_overrides({"openai/gpt-5.4-mini": "low"})
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="claude-opus-4-5"
        )
        == "high"
    ), "unrelated provider-qualified override must not match"


def test_fallback_provider_qualified_disable_matches(no_core):
    """A provider-qualified key with a boolean-false value must still disable."""
    data = _write_overrides({"openai/gpt-5.4-mini": False})
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="gpt-5.4-mini"
        )
        == "none"
    )


def test_fallback_provider_qualified_dot_dash_matches(no_core):
    """Dot/dash tolerance must survive the provider-qualified key expansion."""
    data = _write_overrides({"openai/gpt-5.4-mini": "low"})
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="gpt-5-4-mini"
        )
        == "low"
    )


def test_fallback_unmatched_model_still_retains_global_with_qualified_keys(no_core):
    """Adding prefixed candidates must not create false positives."""
    data = _write_overrides({"openai/gpt-5.4-mini": "low"})
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="totally-different-model"
        )
        == "high"
    )


def test_fallback_matches_at_qualified_override_for_at_model(no_core):
    """Reviewer repro: @-form override key vs @-qualified model id.

    ``configured_reasoning_effort_for_model`` strips ``@openrouter:gpt-5.4-mini``
    to the bare id before building the fallback candidates, so an override key
    written in the SAME ``@provider:model`` form was never a candidate and the
    global effort won (greptile P1). The caller's original @ spelling must be
    a candidate, ahead of the stripped bare form.
    """
    data = _write_overrides({"@openrouter:gpt-5.4-mini": "low"})

    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="@openrouter:gpt-5.4-mini"
        )
        == "low"
    )
    # Same through the gateway bridge path.
    assert (
        gateway_chat._gateway_reasoning_effort_for_request(
            data, model="@openrouter:gpt-5.4-mini", model_provider="openrouter"
        )
        == "low"
    )


def test_fallback_at_override_outranks_bare_for_at_model(no_core):
    """The @-qualified spelling wins over the bare key when both are present.

    The @ form is the most specific spelling the caller gave us, so it must
    outrank the stripped bare-id candidates (exact-first ordering).
    """
    data = _write_overrides(
        {
            "@openrouter:gpt-5.4-mini": "minimal",
            "gpt-5.4-mini": "low",
        }
    )
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="@openrouter:gpt-5.4-mini"
        )
        == "minimal"
    ), "@-qualified spelling must outrank the bare spelling (exact first)"


def test_fallback_bare_override_still_matches_at_model(no_core):
    """Adding @ candidates must not break the historical bare-key match."""
    data = _write_overrides({"gpt-5.4-mini": "low"})
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="@openrouter:gpt-5.4-mini"
        )
        == "low"
    )


def test_reasoning_override_keys_for_model_enumerates_at_spelling():
    """The setter-side enumeration must include the @ spelling the reader now
    matches, so a level write / Default clears it too (the historical
    ``model.startswith("@")`` branch only saw the stripped form, so an
    @-form entry was never removable)."""
    keys = cfg._reasoning_override_keys_for_model(
        "@openrouter:gpt-5.4-mini",
        {"@openrouter:gpt-5.4-mini": "low", "gpt-5.4-mini": "high"},
        provider_id="openrouter",
    )
    assert "@openrouter:gpt-5.4-mini" in keys
    assert "gpt-5.4-mini" in keys


def test_reasoning_override_keys_for_model_enumerates_dot_dash_equivalents():
    """droopanu (2026-10-27): the setter-side enumeration must cover the
    reader's dot/dash equivalence class, or Default leaves ``gpt-5-4`` (and
    ``gpt.5.4``) answering for ``gpt-5.4``."""
    keys = cfg._reasoning_override_keys_for_model(
        "gpt-5.4",
        {
            "gpt-5.4": "low",
            "gpt-5-4": "low",
            "gpt.5.4": "low",
            "claude-opus-4-5": "medium",
        },
        provider_id="openai",
    )
    assert "gpt-5-4" in keys, "the dash spelling must be enumerated for the dot model"
    assert "gpt.5.4" in keys, "the dotted spelling must be enumerated for the dashed model"
    assert "claude-opus-4-5" not in keys, (
        "an unrelated model's override must not be enumerated"
    )


def test_reasoning_override_keys_for_model_enumerates_aggregator_and_custom_qualified():
    """droopanu (2026-10-27): aggregator-qualified keys — and any slash key
    whose stripped tail is the model (core's reverse lookup) — must be
    enumerated for the bare model, while unrelated qualified keys stay out."""
    keys = cfg._reasoning_override_keys_for_model(
        "gpt-5.4",
        {
            "gpt-5.4": "low",
            "openai/gpt-5.4": "low",
            "openrouter/openai/gpt-5.4": "low",
            "my-gateway/gpt-5.4": "low",
            "openrouter/openai/claude-opus-4-5": "medium",
        },
        provider_id="openai",
    )
    assert "openai/gpt-5.4" in keys
    assert "openrouter/openai/gpt-5.4" in keys, (
        "the aggregator-qualified spelling must be enumerated for the bare model"
    )
    assert "my-gateway/gpt-5.4" in keys, (
        "a custom-provider-prefixed key whose tail is the model (core's "
        "reverse lookup) must be enumerated too"
    )
    assert "openrouter/openai/claude-opus-4-5" not in keys, (
        "an unrelated model's aggregator-qualified override must not be enumerated"
    )


def test_fallback_matches_core_differentially(no_core):
    """No-core fallback must agree with the core resolver, both directions.

    For a matrix of (model id, override key, value) triples the fallback must
    produce the same per-model decision core's
    ``resolve_per_model_reasoning_effort`` produces.

    Core's function is imported by direct reference BEFORE the ``no_core``
    fixture suppresses ``sys.modules['hermes_constants']``, so the same process
    can ask both resolvers. The model id is stripped through WebUI's own
    ``_parse_provider_qualified_model_id`` first, mirroring the real call site
    (core sees the bare model, never the ``@provider:`` route hint).
    """
    core_resolve = sys.modules.get("_hermes_core_stash")

    if core_resolve is None:
        pytest.skip("hermes_constants unavailable for differential comparison")

    models = [
        "gpt-5.4-mini",
        "openai/gpt-5.4-mini",
        "openrouter/openai/gpt-5.4-mini",
        "claude-opus-4.5",
        "claude-opus-4-5",
        "@openrouter:gemini-3.6-flash-tiered",
    ]
    keys = [
        "gpt-5.4-mini",
        "openai/gpt-5.4-mini",
        "openrouter/openai/gpt-5.4-mini",
        "claude-opus-4.5",
        "claude-opus-4-5",
        "gemini-3.6-flash-tiered",
        "openai/gpt.5.4.mini",
    ]
    values = ["low", False]

    for model in models:
        # Mirror the real call site: a ``@provider:model`` route hint is
        # stripped to the bare model before either resolver sees it.
        parsed = cfg._parse_provider_qualified_model_id(model)
        core_model = parsed[0] if parsed else model
        for key in keys:
            for value in values:
                core = core_resolve(core_model, {key: value})
                if core is None:
                    expected = "__global__"
                elif core.get("enabled") is False:
                    expected = "none"
                else:
                    expected = core.get("effort")
                sentinel_cfg = {
                    "agent": {
                        "reasoning_effort": "__global__",
                        "reasoning_overrides": {key: value},
                    }
                }
                resolved = cfg.configured_reasoning_effort_for_model(
                    sentinel_cfg, model_id=model
                )
                if expected in (None, "__global__"):
                    assert resolved in ("__global__", ""), (
                        f"model={model} key={key} value={value!r}: "
                        f"core kept the global, fallback resolved {resolved!r}"
                    )
                elif expected == "none":
                    assert resolved == "none", (
                        f"model={model} key={key} value={value!r}: "
                        f"core disabled, fallback resolved {resolved!r}"
                    )
                else:
                    assert resolved == expected, (
                        f"model={model} key={key} value={value!r}: "
                        f"core={expected!r} fallback={resolved!r}"
                    )


# ──────────── Fix C: colon-scoped override keys (``provider:model``) ────────────
#
# CharlesMcquade (2026-11-09): executing ``6556b155`` directly showed one
# remaining scoped-selection gap. ``configured_reasoning_effort_for_model``
# calls the shared ``resolve_per_model_reasoning_effort(model, overrides)``
# WITHOUT a provider, and core's variant generator emits slash and dot/dash
# forms only — so a colon-scoped key (``prov:m1``) can never participate.
# With global ``low``, ``prov:m1: medium`` and bare ``m1: high`` the resolver
# returned ``high`` (expected ``medium``); with only ``prov:m1`` present it fell
# back to global ``low`` (expected ``medium``). The bare-only control worked.


def test_scoped_key_wins_over_bare_model_key():
    """``prov:m1: medium`` must beat bare ``m1: high`` for (prov, m1).

    A scoped key names the provider too, so it is strictly more specific than
    the bare model spelling. Returning ``high`` meant the scoped selection was
    silently ignored whenever a bare key also existed.
    """
    data = _write_overrides({"prov:m1": "medium", "m1": "high"}, global_effort="low")

    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="m1", provider_id="prov"
        )
        == "medium"
    )
    # Same decision through the Gateway bridge path.
    assert (
        gateway_chat._gateway_reasoning_effort_for_request(
            data, model="m1", model_provider="prov"
        )
        == "medium"
    )


def test_scoped_key_participates_without_bare_key():
    """``prov:m1: medium`` alone must resolve ``medium``, not the global ``low``.

    Core never sees the provider, so the scoped key was invisible and the
    global effort won — the override looked configured but did nothing.
    """
    data = _write_overrides({"prov:m1": "medium"}, global_effort="low")

    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="m1", provider_id="prov"
        )
        == "medium"
    )
    assert (
        gateway_chat._gateway_reasoning_effort_for_request(
            data, model="m1", model_provider="prov"
        )
        == "medium"
    )


def test_scoped_key_control_bare_and_global_still_work():
    """The two control shapes the reviewer confirmed working must stay working."""
    bare_only = _write_overrides({"m1": "high"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            bare_only, model_id="m1", provider_id="prov"
        )
        == "high"
    )
    global_only = _write_overrides({}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            global_only, model_id="m1", provider_id="prov"
        )
        == "low"
    )


def test_scoped_key_ignores_other_providers():
    """A scoped key must only match the model's OWN provider.

    Otherwise one provider's override would leak into a same-named model on
    another provider — the exact cross-path inconsistency this PR exists to
    remove.
    """
    data = _write_overrides({"other-provider:m1": "medium"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="m1", provider_id="prov"
        )
        == "low"
    )


def test_scoped_key_needs_a_provider_to_apply():
    """Without a provider there is no scope: the bare key keeps winning."""
    data = _write_overrides({"prov:m1": "medium", "m1": "high"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(data, model_id="m1") == "high"
    )


def test_scoped_key_does_not_capture_a_colon_in_the_model_name():
    """A colon inside a model id is not a provider scope.

    ``qwen3.5:397b`` is a legitimate model spelling (Ollama-style tags), so it
    must stay a plain key rather than being read as ``provider=qwen3.5``.
    """
    data = _write_overrides({"qwen3.5:397b": "high"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="qwen3.5", provider_id="prov"
        )
        == "low"
    )
    # …and the tagged model still resolves its own key when it is the model.
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="qwen3.5:397b", provider_id="prov"
        )
        == "high"
    )


def test_at_qualified_spelling_still_outranks_scoped_key():
    """The caller's own ``@provider:model`` spelling stays most specific.

    It names the exact routing lane, so adding scoped keys must not demote it.
    """
    data = _write_overrides(
        {"@prov:m1": "minimal", "prov:m1": "medium"}, global_effort="low"
    )
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="@prov:m1", provider_id="prov"
        )
        == "minimal"
    )


def test_scoped_key_honors_disable_and_dot_dash_tolerance():
    """Scoped keys must parse like every other override key."""
    disabled = _write_overrides({"prov:m1": False}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            disabled, model_id="m1", provider_id="prov"
        )
        == "none"
    )
    dotted = _write_overrides({"prov:gpt-5-4": "medium"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            dotted, model_id="gpt-5.4", provider_id="prov"
        )
        == "medium"
    )


def test_scoped_key_custom_provider_slug():
    """A ``custom:<slug>:<model>`` provider id must scope its own keys.

    The provider segment itself contains a colon, so the key has two — the
    split must take the LAST colon as the model boundary.
    """
    data = _write_overrides(
        {"custom:newapi:ve-gpt": "medium"}, global_effort="low"
    )
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="ve-gpt", provider_id="custom:newapi"
        )
        == "medium"
    )


def test_scoped_key_retains_global_when_value_unrepresentable():
    """An unrepresentable scoped value must retain the global, not blank it."""
    data = _write_overrides({"prov:m1": "ultra"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="m1", provider_id="prov"
        )
        == "low"
    )


def test_scoped_key_works_without_hermes_core(no_core):
    """The scoped lookup is WebUI-side, so it must work with no core tree.

    Both of the reviewer's reproductions are re-checked on the no-core
    fallback path: the scoped key must still participate and still outrank the
    bare spelling.
    """
    both = _write_overrides({"prov:m1": "medium", "m1": "high"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            both, model_id="m1", provider_id="prov"
        )
        == "medium"
    )
    scoped_only = _write_overrides({"prov:m1": "medium"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            scoped_only, model_id="m1", provider_id="prov"
        )
        == "medium"
    )
    # Boolean-false disable must survive the scoped path too.
    disabled = _write_overrides({"prov:m1": False}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            disabled, model_id="m1", provider_id="prov"
        )
        == "none"
    )


def test_reasoning_override_keys_enumerates_scoped_spelling():
    """The setter must enumerate the scoped spelling the reader now matches.

    A level write / ``Default`` for ``(prov, m1)`` has to supersede and clear
    ``prov:m1``, otherwise the stale scoped entry survives and keeps answering
    on read — the chip snaps back to the old level, which is precisely the
    write/read identity split this enumeration exists to prevent.
    """
    overrides = {"prov:m1": "medium", "m1": "high", "other:m1": "max"}
    keys = cfg._reasoning_override_keys_for_model(
        "m1", overrides, provider_id="prov"
    )
    assert "prov:m1" in keys
    assert "m1" in keys
    # Another provider's same-named model must never be cleared.
    assert "other:m1" not in keys
    # Without a provider there is no scope, so only the bare spelling matches.
    assert cfg._reasoning_override_keys_for_model(
        "m1", overrides, provider_id=None
    ) == ["m1"]


def test_scoped_key_beats_bare_for_at_qualified_model():
    """For an ``@provider:model`` model, the scoped key must beat the bare one.

    A bare-key hit also populates ``per_model``, so gating the scoped lookup on
    "did anything match" let the bare spelling outrank the scoped one for an
    @-qualified model. Only a literal ``@provider:model`` KEY may outrank the
    scoped key, because it names the exact routing lane.
    """
    data = _write_overrides({"prov:m1": "medium", "m1": "high"}, global_effort="low")
    assert (
        cfg.configured_reasoning_effort_for_model(
            data, model_id="@prov:m1", provider_id="prov"
        )
        == "medium"
    ), "scoped key must outrank the bare spelling for an @-qualified model"
    # …but a literal @ key still wins outright.
    exact = _write_overrides(
        {"@prov:m1": "minimal", "prov:m1": "medium"}, global_effort="low"
    )
    assert (
        cfg.configured_reasoning_effort_for_model(
            exact, model_id="@prov:m1", provider_id="prov"
        )
        == "minimal"
    ), "the caller's own @ spelling stays most specific"
