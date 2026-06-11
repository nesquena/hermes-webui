"""Regression: GET /api/session display resolvers must never trigger the
live provider-catalog rebuild.

Root cause (multi-tab streaming interlock RCA, task t_d127953d):
``_resolve_effective_session_model_for_display`` /
``_resolve_effective_session_model_provider_for_display`` are called by the
hot, side-effect-free ``GET /api/session?...&resolve_model=1`` path. When a
session has no persisted ``model_provider`` (common — e.g. kanban/imported
sessions), the fast path in ``_resolve_compatible_session_model_state`` is
skipped and the resolver fell through to ``get_available_models()`` WITHOUT
``prefer_cache``. On a non-AWS / WSL / corp network that cold rebuild blocks
~10s on a botocore IMDS probe (plus anthropic/openrouter /models) and, run
concurrently across browser tabs, serializes on the models-cache lock and
starves SSE/streaming -> BrokenPipe/Cancelled storm.

This is an INVARIANT test, not a change-detector: it asserts the resolvers
resolve from the cache-only path and never reach the live-rebuild seam
``api.config._invoke_models_rebuild`` — regardless of whether the session
carries a model_provider.
"""

import ast
import inspect

import pytest

import api.config as cfg
import api.routes as routes


class _FakeSession:
    """Minimal stand-in for a Session row as seen by the display resolvers."""

    def __init__(self, model, model_provider):
        self.model = model
        self.model_provider = model_provider


@pytest.fixture
def cold_models_cache(monkeypatch):
    """Force a cold in-memory + disk models cache without touching real state.

    Cold cache is what makes the regression observable: a warm cache short-
    circuits before any rebuild decision, hiding the prefer_cache contract.
    """
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    # Never read/write the real on-disk cache during the test.
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", lambda: None)
    yield


@pytest.fixture
def rebuild_seam_tripwire(monkeypatch):
    """Make the live provider-catalog rebuild seam fail loudly if reached.

    ``_invoke_models_rebuild`` is the documented indirection seam around the
    cold, network-touching per-provider rebuild. The display resolvers must
    never reach it (prefer_cache returns the network-free minimal catalog
    *before* this seam). If a future edit drops ``prefer_cached_catalog=True``,
    the resolver falls into the cold rebuild and trips this wire.
    """
    calls = {"n": 0}

    def _boom(_builder):
        calls["n"] += 1
        raise AssertionError(
            "live provider-catalog rebuild ran on the hot GET /api/session "
            "display path — prefer_cached_catalog regression"
        )

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _boom)
    return calls


@pytest.mark.parametrize(
    "model_provider",
    [None, "", "anthropic"],
    ids=["no-provider", "empty-provider", "with-provider"],
)
def test_session_display_resolvers_never_trigger_live_rebuild(
    cold_models_cache, rebuild_seam_tripwire, model_provider
):
    session = _FakeSession("claude-opus-4-7", model_provider)

    # Must not raise (the tripwire raises AssertionError if the live rebuild
    # path is entered) and must return the persisted model verbatim.
    model = routes._resolve_effective_session_model_for_display(session)
    provider = routes._resolve_effective_session_model_provider_for_display(session)

    assert model == "claude-opus-4-7"
    # provider is best-effort; the contract under test is "no live rebuild",
    # not a specific provider string. It must at least be None or a str.
    assert provider is None or isinstance(provider, str)
    assert rebuild_seam_tripwire["n"] == 0


def _has_prefer_cached_catalog_true_call(fn) -> bool:
    tree = ast.parse(inspect.getsource(fn))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_resolve_compatible_session_model_state":
            continue
        for keyword in node.keywords:
            if keyword.arg == "prefer_cached_catalog" and isinstance(
                keyword.value, ast.Constant
            ):
                return keyword.value.value is True
    return False


def test_resolver_signature_passes_prefer_cached_catalog():
    """Static guard: both resolvers must opt into the cache-only catalog.

    A pure behavioural test can be satisfied by an unrelated short-circuit;
    this pins the explicit contract at the call site so the intent survives
    refactors.
    """
    assert _has_prefer_cached_catalog_true_call(
        routes._resolve_effective_session_model_for_display
    )
    assert _has_prefer_cached_catalog_true_call(
        routes._resolve_effective_session_model_provider_for_display
    )
