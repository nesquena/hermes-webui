"""
Regression test for issue #2: gateway model freshness.

Bug
---
``api.config.get_available_models`` cached the full model picker result
for 24 h (``_AVAILABLE_MODELS_CACHE_TTL = 86400``). The cache wrapped
``api.gateway_provider.get_gateway_model_groups``, so any model that the
operator added to (or removed from) the gateway console was invisible to
the WebUI for up to a day.

Fix
---
Gateway groups are no longer baked into the cached model assembly. They
are appended fresh on every ``get_available_models()`` call by
``api.config._attach_fresh_gateway_groups``. Freshness is bounded by the
gateway provider's own short TTL (``api.gateway_provider`` ~30 s), not
the WebUI's 24 h cache.
"""

import pathlib
import sys

import pytest


_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


@pytest.fixture
def fake_gateway(monkeypatch):
    """Replace ``get_gateway_model_groups`` with a mutable stub.

    The stub returns whatever ``state['groups']`` currently contains, so
    the test can simulate the operator adding/removing models on the
    gateway console between calls.
    """
    from api import gateway_provider, config

    state = {"groups": []}

    def _fake():
        # Return a fresh copy so callers can't mutate the stub's state.
        return [dict(g, models=list(g.get("models", []))) for g in state["groups"]]

    monkeypatch.setattr(gateway_provider, "get_gateway_model_groups", _fake)
    # Force re-import in api.config (which imports lazily) by patching there too.
    # _attach_fresh_gateway_groups does ``from api.gateway_provider import …``
    # at call time, so patching the source module is sufficient — no extra
    # patch needed in api.config. Asserted below.
    assert config._attach_fresh_gateway_groups({"groups": []})["groups"] == []
    return state


@pytest.fixture
def warm_cache(monkeypatch):
    """Pre-populate the model cache so we exercise the cached fast-path."""
    from api import config
    monkeypatch.setattr(config, "_available_models_cache",
                        {"active_provider": None, "default_model": "x",
                         "groups": [{"provider": "openai",
                                     "models": [{"id": "gpt-4", "label": "gpt-4"}]}]})
    import time as _t
    monkeypatch.setattr(config, "_available_models_cache_ts", _t.monotonic())


def _gateway_ids(result: dict) -> list[str]:
    """Return all model ids that came from gateway groups, in order."""
    out = []
    for grp in result.get("groups", []):
        pid = (grp.get("provider") or "").lower()
        if pid.startswith("gateway") or pid.startswith("gw"):
            for m in grp.get("models", []):
                out.append(m.get("id"))
    return out


def test_gateway_models_added_after_first_call_are_visible_immediately(
    fake_gateway, warm_cache
):
    """Adding a model on the gateway console must show up in the next
    /api/models call without waiting for the 24 h cache TTL."""
    from api.config import get_available_models

    # First call: no gateway models yet.
    first = get_available_models()
    assert _gateway_ids(first) == [], \
        "no gateway models registered yet — picker should not list any"

    # Operator adds a model on the gateway side.
    fake_gateway["groups"] = [{
        "provider": "gateway-prod",
        "models": [{"id": "gateway-prod/claude-opus-5", "label": "Opus 5"}],
    }]

    # Second call (cache still warm) MUST reflect the new model.
    second = get_available_models()
    assert "gateway-prod/claude-opus-5" in _gateway_ids(second), (
        "gateway model added between calls is invisible — the 24h "
        "_available_models_cache is silently masking gateway changes. "
        "Gateway groups must be re-resolved on every call."
    )


def test_gateway_models_removed_disappear_immediately(fake_gateway, warm_cache):
    """And the dual: removing a model must drop it from the picker too."""
    from api.config import get_available_models

    fake_gateway["groups"] = [{
        "provider": "gateway-prod",
        "models": [
            {"id": "gateway-prod/llama-3", "label": "Llama 3"},
            {"id": "gateway-prod/mistral-l", "label": "Mistral L"},
        ],
    }]
    first = get_available_models()
    assert set(_gateway_ids(first)) == {
        "gateway-prod/llama-3", "gateway-prod/mistral-l"
    }

    # Operator removes one of them.
    fake_gateway["groups"][0]["models"] = [
        {"id": "gateway-prod/llama-3", "label": "Llama 3"},
    ]

    second = get_available_models()
    assert _gateway_ids(second) == ["gateway-prod/llama-3"], (
        "gateway model removed on the console is still listed by the "
        "WebUI picker — stale entries must be purged on every call."
    )


def test_cached_non_gateway_groups_are_preserved(fake_gateway, warm_cache):
    """The cache fast-path still serves the heavy provider list — only
    the gateway slice is recomputed."""
    from api.config import get_available_models

    fake_gateway["groups"] = [{
        "provider": "gateway-prod",
        "models": [{"id": "gateway-prod/claude", "label": "C"}],
    }]
    result = get_available_models()
    providers = [g.get("provider") for g in result.get("groups", [])]
    # The warm-cache fixture seeded an "openai" group; it must survive.
    assert "openai" in providers, "non-gateway cached groups must still be served"
    assert "gateway-prod" in providers, "fresh gateway group must be appended"


def test_attach_fresh_strips_gateway_aliases():
    """Even legacy ``gw`` / ``gw-foo`` provider ids count as gateway groups
    and must be stripped before the fresh list is re-attached, otherwise
    duplicates accumulate as the gateway naming scheme evolves."""
    from api.config import _attach_fresh_gateway_groups
    from api import gateway_provider

    cached = {
        "groups": [
            {"provider": "openai", "models": [{"id": "gpt-4"}]},
            {"provider": "gw-old", "models": [{"id": "stale-model"}]},
            {"provider": "gateway-old", "models": [{"id": "stale-2"}]},
        ]
    }
    # Stub returns nothing — every gateway entry should be stripped.
    import unittest.mock as _mock
    with _mock.patch.object(gateway_provider, "get_gateway_model_groups",
                            return_value=[]):
        out = _attach_fresh_gateway_groups(cached)
    providers = [g.get("provider") for g in out["groups"]]
    assert providers == ["openai"], (
        f"stale gateway aliases were not stripped: {providers}"
    )
