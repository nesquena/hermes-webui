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
