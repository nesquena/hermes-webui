"""Per-model reasoning-effort override resolution (config → coercion).

Covers the shared resolver ``configured_reasoning_effort_for_model()`` plus the
Gateway request path, which must never hand the Hermes Gateway transport
address to a provider capability probe.
"""

import pytest

from api import config as cfg
from api import gateway_chat


def test_configured_reasoning_effort_prefers_model_override():
    config_data = {
        "agent": {
            "reasoning_effort": "max",
            "reasoning_overrides": {
                "gemini-3.6-flash-tiered": "low",
            },
        },
    }

    assert cfg.configured_reasoning_effort_for_model(
        config_data,
        model_id="gemini-3.6-flash-tiered",
        provider_id="custom:example-gateway",
    ) == "low"


def test_configured_reasoning_effort_keeps_global_for_unmatched_model():
    config_data = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {
                "gemini-3.6-flash-tiered": "low",
            },
        },
    }

    assert cfg.configured_reasoning_effort_for_model(
        config_data,
        model_id="claude-sonnet-4-6",
        provider_id="custom:example-gateway",
    ) == "high"


@pytest.mark.parametrize(
    "model_id",
    [
        "@openrouter:gemini-3.6-flash-tiered",  # qualified companion form
        "GEMINI-3.6-FLASH-TIERED",              # case-insensitive
    ],
)
def test_configured_reasoning_effort_matches_normalized_model_ids(model_id):
    """Override keys must survive qualified/cased model identifiers.

    The WebUI receives model ids in several shapes (``@provider:model`` from the
    picker, raw upstream casing from a custom gateway). A stored override keyed
    on the plain lowercase id must still win over the global value.
    """
    config_data = {
        "agent": {
            "reasoning_effort": "max",
            "reasoning_overrides": {
                "gemini-3.6-flash-tiered": "low",
            },
        },
    }

    assert cfg.configured_reasoning_effort_for_model(
        config_data,
        model_id=model_id,
        provider_id="custom:example-gateway",
    ) == "low"


def _install_lmstudio_probe_recorder(monkeypatch, *, options):
    """Record every base_url handed to the LM Studio capability probe."""
    seen: list[str | None] = []

    def _fake_probe(model, base_url, *, api_key=None, timeout=5.0):
        seen.append(base_url)
        return list(options)

    monkeypatch.setattr(cfg, "_lmstudio_model_reasoning_options", _fake_probe)
    return seen


def test_gateway_reasoning_effort_probes_provider_endpoint_not_gateway(monkeypatch):
    """Gateway requests must probe the LM Studio endpoint, never the Gateway.

    ``_gateway_base_url()`` is the Hermes Gateway transport address (where
    WebUI POSTs ``/v1/chat/completions``). Forwarding it as the provider
    capability endpoint made ``resolve_model_reasoning_efforts()`` probe the
    Gateway as though it were LM Studio, coercing the configured override
    against the wrong capability set.
    """
    gateway_url = "http://127.0.0.1:8642"
    lmstudio_url = "http://192.168.1.50:1234/v1"

    # Configured LM Studio endpoint, distinct from the Gateway transport.
    monkeypatch.setitem(cfg.cfg, "providers", {"lmstudio": {"base_url": lmstudio_url}})
    # LM Studio advertises a ladder that tops out below the configured "max".
    seen = _install_lmstudio_probe_recorder(
        monkeypatch, options=["low", "medium", "high"]
    )

    config_data = {
        "webui_gateway_base_url": gateway_url,
        "agent": {
            "reasoning_effort": "low",
            "reasoning_overrides": {"local-thinker": "max"},
        },
    }

    effort = gateway_chat._gateway_reasoning_effort_for_request(
        config_data,
        model="local-thinker",
        model_provider="lmstudio",
    )

    assert seen, "LM Studio capability probe was never invoked"
    normalized = [cfg._normalize_base_url_for_match(url) for url in seen]
    assert cfg._normalize_base_url_for_match(gateway_url) not in normalized, (
        f"Gateway transport address leaked into the provider probe: {seen}"
    )
    assert normalized == [cfg._normalize_base_url_for_match(lmstudio_url)]
    # The per-model "max" override must clamp down to the probed ceiling.
    assert effort == "high"


def test_gateway_reasoning_effort_ignores_gateway_url_for_non_probed_provider(
    monkeypatch,
):
    """Non-probed providers keep the resolved override untouched."""
    config_data = {
        "webui_gateway_base_url": "http://127.0.0.1:8642",
        "agent": {
            "reasoning_effort": "max",
            "reasoning_overrides": {"gemini-3.6-flash-tiered": "low"},
        },
    }

    assert gateway_chat._gateway_reasoning_effort_for_request(
        config_data,
        model="gemini-3.6-flash-tiered",
        model_provider="custom:example-gateway",
    ) == "low"


def test_configured_reasoning_effort_qualified_custom_provider_models_resolve_consistently():
    """Qualified custom-provider models must resolve overrides consistently across paths."""
    config_data = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {
                "claude-opus-4.5": "low",
            },
        },
    }

    # Native / shared config resolver
    resolved_native = cfg.configured_reasoning_effort_for_model(
        config_data,
        model_id="@custom:example-gateway:claude-opus-4-5",
    )
    assert resolved_native == "low"

    # Gateway path
    resolved_gateway = gateway_chat._gateway_reasoning_effort_for_request(
        config_data,
        model="@custom:example-gateway:claude-opus-4-5",
        model_provider="custom:example-gateway",
    )
    assert resolved_gateway == "low"


def test_configured_reasoning_effort_unrepresentable_override_retains_global():
    """An unrepresentable per-model override must retain the working global effort."""
    config_data = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {
                "some-model": "ultra",
            },
        },
    }

    resolved = cfg.configured_reasoning_effort_for_model(
        config_data,
        model_id="some-model",
    )
    assert resolved == "high"


def test_fallback_reasoning_override_bidirectional_dot_dash_matching(monkeypatch):
    """When the companion resolver is unavailable, dot-dash matching must work in both directions."""
    import sys
    monkeypatch.setitem(sys.modules, "hermes_constants", None)

    # 1. Incoming dashed, override dotted
    cfg_dotted = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"claude-opus-4.5": "low"},
        }
    }
    assert cfg.configured_reasoning_effort_for_model(
        cfg_dotted, model_id="claude-opus-4-5"
    ) == "low"

    # 2. Incoming dotted, override dashed
    cfg_dashed = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"claude-opus-4-5": "low"},
        }
    }
    assert cfg.configured_reasoning_effort_for_model(
        cfg_dashed, model_id="claude-opus-4.5"
    ) == "low"

    # 3. Unmatched model retains global
    assert cfg.configured_reasoning_effort_for_model(
        cfg_dotted, model_id="unmatched-model"
    ) == "high"




def test_fallback_slash_qualified_model_matches_bare_override(monkeypatch):
    """Fix #1: a slash-qualified model id must match a BARE override key when
    the companion core resolver is unavailable.

    Reviewer repro (r3): model ``openai/gpt-5.4-mini`` with bare override
    ``gpt-5.4-mini: low`` and global ``high`` resolved ``high`` on all three
    paths — the fallback built ``@prov:model`` bare-tail candidates but never
    slash-suffix candidates, so the override was silently dropped.
    """
    import sys
    monkeypatch.setitem(sys.modules, "hermes_constants", None)

    config_data = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"gpt-5.4-mini": "low"},
        },
    }

    # Native / shared config resolver
    assert cfg.configured_reasoning_effort_for_model(
        config_data, model_id="openai/gpt-5.4-mini",
    ) == "low"

    # Gateway path
    assert gateway_chat._gateway_reasoning_effort_for_request(
        config_data, model="openai/gpt-5.4-mini", model_provider="openai",
    ) == "low"

    # Status path (same chokepoint, provider hint supplied)
    assert cfg.configured_reasoning_effort_for_model(
        config_data, model_id="openai/gpt-5.4-mini", provider_id="openai",
    ) == "low"

    # Aggregator-qualified ids (3+ segments) must resolve too.
    assert cfg.configured_reasoning_effort_for_model(
        config_data, model_id="openrouter/openai/gpt-5.4-mini",
    ) == "low"

    # A full-id override must still outrank a bare-tail one.
    config_full_wins = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {
                "openai/gpt-5.4-mini": "minimal",
                "gpt-5.4-mini": "low",
            },
        },
    }
    assert cfg.configured_reasoning_effort_for_model(
        config_full_wins, model_id="openai/gpt-5.4-mini",
    ) == "minimal"

    # An unmatched slash-qualified model still retains the global effort.
    assert cfg.configured_reasoning_effort_for_model(
        config_data, model_id="openai/some-other-model",
    ) == "high"


def test_fallback_boolean_false_override_disables_reasoning(monkeypatch):
    """Fix #2: the fallback must treat a YAML boolean ``false`` (and the
    ``"false"``/``"disabled"`` spellings) as *reasoning disabled*, matching core.

    Reviewer repro (r3): a matching override value of boolean ``False`` produced
    native ``{"enabled": True, "effort": "high"}`` and Gateway/status ``high``
    — the explicit disable was ignored because this module's
    ``parse_reasoning_effort()`` returns ``None`` for a bool.
    """
    import sys
    monkeypatch.setitem(sys.modules, "hermes_constants", None)

    # Boolean False — YAML ``reasoning_overrides: {some-model: false}``
    cfg_bool = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"some-model": False},
        },
    }
    assert cfg.configured_reasoning_effort_for_model(
        cfg_bool, model_id="some-model",
    ) == "none"
    assert gateway_chat._gateway_reasoning_effort_for_request(
        cfg_bool, model="some-model", model_provider="openai",
    ) == "none"

    # String spellings core also treats as disabled.
    for spelling in ("false", "disabled", "none", "FALSE", " Disabled "):
        cfg_str = {
            "agent": {
                "reasoning_effort": "high",
                "reasoning_overrides": {"some-model": spelling},
            },
        }
        assert cfg.configured_reasoning_effort_for_model(
            cfg_str, model_id="some-model",
        ) == "none", f"spelling {spelling!r} must disable reasoning"

    # Boolean True is NOT a disable and must not be parsed as an effort; the
    # global effort is retained (core returns None for True).
    cfg_true = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"some-model": True},
        },
    }
    assert cfg.configured_reasoning_effort_for_model(
        cfg_true, model_id="some-model",
    ) == "high"

    # The disable must also resolve through the dot→dash canonical branch.
    cfg_canon = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"claude-opus-4.5": False},
        },
    }
    assert cfg.configured_reasoning_effort_for_model(
        cfg_canon, model_id="claude-opus-4-5",
    ) == "none"

    # And for a slash-qualified model matching a bare boolean-false override.
    cfg_slash = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"gpt-5.4-mini": False},
        },
    }
    assert cfg.configured_reasoning_effort_for_model(
        cfg_slash, model_id="openai/gpt-5.4-mini",
    ) == "none"
