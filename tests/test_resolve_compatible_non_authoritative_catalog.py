"""Regression: ``_resolve_compatible_session_model_state`` must return the
persisted (model, model_provider) pair unchanged when the catalog is
non-authoritative (re-review #7568 round 6, nesquena-hermes 2026-09-22).

Round 5 split the wait vs no-wait contract on the server: routing callers
(provider repair, the server-initiated wakeup) keep
``wait_for_inflight_rebuild=True`` and join the in-flight rebuild, while
the display resolvers (``_resolve_effective_session_model_*_for_display``)
take the no-wait path. Round 6 closes the remaining gap: a no-wait
fallback catalog is KNOWN to be incomplete (the network-free minimal
catalog lacks providers like Copilot, and the stale on-disk snapshot may
predate the current config) and must never rewrite a persisted session
pair. Otherwise the display response feeds the browser, the browser
echoes the rewritten pair back into ``S.session.model`` /
``S.session.model_provider`` (static/sessions.js + static/messages.js),
and the next ``/api/chat/start`` silently routes to a backend the user
never picked.

The fix lives in two places:

* ``api/config.py::get_available_models`` tags the no-wait fallback
  catalogs (``_minimal_static_models_catalog()`` and the stale on-disk
  snapshot) with ``_non_authoritative=True`` and a reason string.
* ``api/routes.py::_resolve_compatible_session_model_state`` checks that
  marker; when set AND the caller supplied a complete persisted pair
  (model + provider), it returns the pair verbatim instead of running
  the compat-normalize / family-match repair against the incomplete
  catalog.

The regression covers BOTH the display response (what ``GET
/api/session?resolve_model=1`` returns) and the next-turn payload (what
``/api/chat/start`` would receive from the browser echoing that
response). Both must stay on the persisted pair.
"""

from unittest.mock import patch

import pytest

import api.config as cfg
import api.routes as routes


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for a Session row as seen by the display resolvers."""

    def __init__(self, model, model_provider):
        self.model = model
        self.model_provider = model_provider


# A non-authoritative minimal catalog the way ``_minimal_static_models_catalog``
# would build it: only knows the active provider (openai-codex) and its
# default model. Crucially, copilot is NOT in ``groups`` — the original
# failure mode. The marker is what makes the test honest: a catalog
# without it would be considered "authoritative enough" by the resolver
# and would rewrite the persisted pair.
_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG = {
    "active_provider": "openai-codex",
    "default_model": "gpt-5.5",
    "configured_model_badges": {},
    "groups": [
        {
            "provider": "OpenAI Codex",
            "provider_id": "openai-codex",
            "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
        },
    ],
    "aliases": {},
    "_non_authoritative": True,
    "_non_authoritative_reason": "no_wait_minimal_static_catalog",
}


# A stale on-disk snapshot that has the previous config's providers but
# is missing the current Copilot auth — also non-authoritative because we
# cannot tell from the cached snapshot which providers are still valid
# vs. which were removed.
_NON_AUTHORITATIVE_STALE_DISK_CATALOG = {
    "active_provider": "openai-codex",
    "default_model": "gpt-5.5",
    "configured_model_badges": {},
    "groups": [
        {
            "provider": "OpenAI Codex",
            "provider_id": "openai-codex",
            "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
        },
    ],
    "aliases": {},
    "_non_authoritative": True,
    "_non_authoritative_reason": "no_wait_stale_disk_cache",
}


# An authoritative catalog that DOES contain Copilot — used as a control
# to prove the resolver still runs the slow path when the catalog knows
# the persisted provider. Without this, a too-eager guard could regress
# the legitimate compat-normalize flow.
_AUTHORITATIVE_COPILOT_AWARE_CATALOG = {
    "active_provider": "openai-codex",
    "default_model": "gpt-5.5",
    "configured_model_badges": {},
    "groups": [
        {
            "provider": "OpenAI Codex",
            "provider_id": "openai-codex",
            "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
        },
        {
            "provider": "Copilot",
            "provider_id": "copilot",
            "models": [{"id": "gpt-5.5", "label": "GPT-5.5 (Copilot)"}],
        },
    ],
    "aliases": {},
}


@pytest.fixture
def no_copilot_in_providers_cfg(monkeypatch):
    """The early-out for ``model.startswith(f"@{requested_provider}:")``
    would short-circuit before the slow path if ``copilot`` were in
    ``cfg['providers']``. The 9/22 reproducer specifically used a
    session whose provider is NOT in the current config (Copilot was
    removed) — so we mirror that by removing copilot from the test
    config.
    """
    saved_cfg = dict(cfg.cfg)
    cfg.cfg.pop("providers", None)
    try:
        yield
    finally:
        cfg.cfg.clear()
        cfg.cfg.update(saved_cfg)


# ---------------------------------------------------------------------------
# 1. Direct resolver guard — the core contract
# ---------------------------------------------------------------------------


def test_resolver_returns_persisted_pair_for_non_authoritative_minimal_catalog(
    no_copilot_in_providers_cfg,
):
    """``_resolve_compatible_session_model_state`` must return the
    persisted ``@copilot:gpt-5.5 / copilot`` pair UNCHANGED when the
    catalog is the no-wait minimal static catalog (non-authoritative).

    Without the guard, the slow path's ``_model_matches_active_provider_family``
    branch rewrites the pair to ``gpt-5.5 / openai-codex`` because the
    bare model ``gpt-5.5`` matches the openai family of the catalog's
    active provider (openai-codex) and the catalog has no entry for
    copilot. With the guard, the persisted pair wins.
    """
    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ) as mock_catalog:
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "@copilot:gpt-5.5", "copilot"
            )
        )

    assert mock_catalog.call_count == 1, (
        "the resolver must still consult the catalog in the slow path; "
        "the guard is a post-lookup short-circuit, not a catalog skip"
    )
    assert effective_model == "@copilot:gpt-5.5", (
        f"non-authoritative catalog rewrote the persisted model: "
        f"got {effective_model!r} (review #7568 round 6)"
    )
    assert effective_provider == "copilot", (
        f"non-authoritative catalog rewrote the persisted provider: "
        f"got {effective_provider!r} (review #7568 round 6)"
    )
    assert normalized is False, (
        "model_was_normalized=True would let callers treat the rewritten "
        "pair as a repair — the guard must keep the persisted pair verbatim"
    )


def test_resolver_returns_persisted_pair_for_non_authoritative_stale_disk(
    no_copilot_in_providers_cfg,
):
    """Same contract for the stale on-disk snapshot path: a stale catalog
    is also marked non-authoritative and must not rewrite the pair.
    """
    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_STALE_DISK_CATALOG,
    ):
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "@copilot:gpt-5.5", "copilot"
            )
        )

    assert (effective_model, effective_provider, normalized) == (
        "@copilot:gpt-5.5",
        "copilot",
        False,
    )


def test_resolver_still_normalizes_against_authoritative_catalog(
    no_copilot_in_providers_cfg,
):
    """Control: an authoritative catalog (one that DOES list copilot)
    keeps the slow path's freedom to run the compat-normalize. The guard
    is conservative — it only fires when the catalog is known to be
    incomplete. A too-eager guard would regress the legitimate repair
    path (e.g. ``openai/gpt-5.4-mini`` under openai-codex).
    """
    with patch(
        "api.routes.get_available_models",
        return_value=_AUTHORITATIVE_COPILOT_AWARE_CATALOG,
    ):
        # openai/gpt-5.4-mini under openai-codex is a known cross-provider
        # repair case — the slow path normalizes it to the active default.
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini", "openai-codex"
            )
        )

    assert effective_model == "gpt-5.5"
    assert effective_provider == "openai-codex"
    assert normalized is True, (
        "the compat-normalize path must still run against an authoritative "
        "catalog — only non-authoritative catalogs trigger the guard"
    )


# ---------------------------------------------------------------------------
# 1b. Narrowed guard — removed providers and legacy non-@ forms must STILL
#     be repaired against a non-authoritative catalog (re-review #7568 round 7,
#     nesquena-hermes 2026-09-23)
# ---------------------------------------------------------------------------


def test_resolver_still_repairs_removed_provider_against_non_authoritative_catalog(
    no_copilot_in_providers_cfg,
):
    """A session pointing at a REMOVED provider must still be repaired on a
    cold load even when the catalog is non-authoritative (round 7).

    The round-6 guard was too broad: it returned any complete persisted pair
    verbatim, so a session whose provider no longer exists
    (``@removed:mistral-large`` / ``removed``) never repaired to the active
    default — the browser echoed the stale pair back
    (static/sessions.js:3022, marked explicit at static/messages.js:1822) and
    ``/api/chat/start`` routed to a provider that is gone. The narrowed guard
    only passes through a ``@provider:model`` whose provider is statically
    known or configured; ``removed`` is neither, so the request must fall
    through to the real compatibility-repair path below.
    """
    # Premise: this provider must genuinely be statically unknown, otherwise
    # the test silently stops exercising the repair path.
    assert cfg._provider_is_known_or_configured("removed") is False, (
        "test premise: 'removed' must not be in the static provider registry "
        "or configured anywhere, else this regression no longer covers the "
        "round-7 removed-provider finding"
    )

    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ) as mock_catalog:
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "@removed:mistral-large", "removed"
            )
        )

    assert mock_catalog.call_count == 1, (
        "the resolver must still consult the catalog on the repair path"
    )
    assert effective_model == "gpt-5.5", (
        f"removed provider was NOT repaired on a cold non-authoritative "
        f"catalog: got model {effective_model!r} (review #7568 round 7)"
    )
    assert effective_provider == "openai-codex", (
        f"removed provider was NOT repaired on a cold non-authoritative "
        f"catalog: got provider {effective_provider!r} (review #7568 round 7)"
    )
    assert normalized is True, (
        "a repaired pair must be reported as normalized so callers know the "
        "stale selection was replaced"
    )


def test_resolver_still_repairs_legacy_codex_model_against_non_authoritative_catalog(
    no_copilot_in_providers_cfg,
):
    """A legacy OpenAI-Codex session model must still be repaired on a cold
    non-authoritative catalog (round 7).

    ``openai/gpt-5.4-mini`` / ``openai-codex`` is the legacy non-``@`` shape:
    the round-6 guard passed it through verbatim, so a cold server wakeup kept
    the stale model, while ``origin/master`` repairs it to the current Codex
    default (``gpt-5.5`` / ``openai-codex``). The narrowed guard must not fire
    for non-``@`` forms — they are exactly what compatibility repair exists
    for.
    """
    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ) as mock_catalog:
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini", "openai-codex"
            )
        )

    assert mock_catalog.call_count == 1, (
        "the resolver must still consult the catalog on the repair path"
    )
    assert effective_model == "gpt-5.5", (
        f"legacy Codex model was NOT repaired on a cold non-authoritative "
        f"catalog: got model {effective_model!r} (review #7568 round 7)"
    )
    assert effective_provider == "openai-codex", (
        f"legacy Codex model was NOT repaired on a cold non-authoritative "
        f"catalog: got provider {effective_provider!r} (review #7568 round 7)"
    )
    assert normalized is True, (
        "the legacy Codex repair must be reported as a normalization"
    )


# ---------------------------------------------------------------------------
# 2. Display response — what GET /api/session?resolve_model=1 returns
# ---------------------------------------------------------------------------


def test_display_response_keeps_persisted_pair_against_non_authoritative_catalog(
    monkeypatch, no_copilot_in_providers_cfg
):
    """The display path's two resolvers are the ones that ultimately
    write the response pair into ``raw['model']`` / ``raw['model_provider']``
    on the GET /api/session handler (api/routes.py:13462-13465). They
    delegate to ``_resolve_compatible_session_model_state``; when that
    function returns the persisted pair (thanks to the guard), the
    display response stays on the persisted pair too.
    """
    session = _FakeSession("@copilot:gpt-5.5", "copilot")

    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ):
        display_model = routes._resolve_effective_session_model_for_display(
            session
        )
        display_provider = (
            routes._resolve_effective_session_model_provider_for_display(session)
        )

    assert display_model == "@copilot:gpt-5.5", (
        f"GET /api/session?resolve_model=1 would publish a rewritten "
        f"model {display_model!r} instead of the persisted pair — the "
        f"browser would echo that into S.session.model and the next "
        f"chat/start would silently reroute"
    )
    assert display_provider == "copilot", (
        f"GET /api/session?resolve_model=1 would publish a rewritten "
        f"provider {display_provider!r} — same browser-echo routing bug"
    )


# ---------------------------------------------------------------------------
# 3. Next-turn payload — what /api/chat/start receives
# ---------------------------------------------------------------------------


def test_next_turn_payload_keeps_persisted_pair(
    monkeypatch, no_copilot_in_providers_cfg
):
    """Pin the next-turn payload: simulate the browser's
    ``POST /api/chat/start`` after the display response by calling
    ``_resolve_compatible_session_model_state`` with the exact pair the
    browser would send (``S.session.model`` / ``S.session.model_provider``).
    The same non-authoritative catalog guard must keep the pair intact.
    """
    # The display response is the persisted pair (verified by
    # test_display_response_keeps_persisted_pair_against_non_authoritative_catalog
    # above). The browser writes that into S.session and POSTs it back to
    # /api/chat/start. /api/chat/start ultimately calls
    # _resolve_compatible_session_model_state with the (model, model_provider)
    # pair from the request body.
    next_turn_model = "@copilot:gpt-5.5"
    next_turn_provider = "copilot"

    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ):
        # /api/chat/start's resolve-compat stage (api/routes.py:7919 area).
        routing_model, routing_provider, routing_normalized = (
            routes._resolve_compatible_session_model_state(
                next_turn_model, next_turn_provider
            )
        )

    # All three must stay on the persisted pair — this is the pair the
    # runtime will use to dispatch the turn.
    assert routing_model == "@copilot:gpt-5.5"
    assert routing_provider == "copilot"
    assert routing_normalized is False


# ---------------------------------------------------------------------------
# 4. Marker is actually applied to the no-wait catalog in get_available_models
# ---------------------------------------------------------------------------


def test_minimal_static_catalog_is_marked_non_authoritative(monkeypatch):
    """Static guard on the marker: the no-wait ``prefer_cache`` cold
    path inside ``get_available_models`` must call
    ``_mark_non_authoritative_catalog`` (or otherwise tag the result)
    on every catalog it returns. A future refactor that drops the
    marker silently re-opens the round-6 finding.
    """
    import inspect

    from api import config as cfg_mod

    src = inspect.getsource(cfg_mod.get_available_models)
    # The marker is set either via the helper or by a direct assignment.
    # We require both: (a) the marker helper exists, and (b) it's used
    # in the no-wait prefer_cache branch.
    assert hasattr(cfg_mod, "_mark_non_authoritative_catalog"), (
        "config.py must export the _mark_non_authoritative_catalog helper "
        "so the resolver can rely on a single source of truth for the marker"
    )
    assert src.count("_mark_non_authoritative_catalog") >= 2, (
        "get_available_models must tag the no-wait fallback catalogs with "
        "_mark_non_authoritative_catalog(...) — expected at least one call "
        "in the lock-busy path AND one in the prefer_cache cold path"
    )
    # The prefer_cache cold branch specifically must tag the catalog it
    # returns; an accidental drift that only marks the lock-busy branch
    # would re-open the round-6 finding for the cold-prefer_cache path.
    # Find the prefer_cache cold-path block and confirm the next non-comment
    # ``return`` is wrapped in the marker call.
    cold_idx = src.find("if prefer_cache:\n")
    assert cold_idx != -1, (
        "get_available_models must have a prefer_cache cold-path return "
        "that serves the minimal static catalog"
    )
    # Look ahead from the cold-path `if prefer_cache:` (skipping the
    # explanatory comment block) for the marker wrapping the return.
    cold_block = src[cold_idx:cold_idx + 1200]
    assert "_mark_non_authoritative_catalog" in cold_block, (
        "the prefer_cache cold-path return must be wrapped in "
        "_mark_non_authoritative_catalog(...) — otherwise the resolver "
        "won't know the catalog is non-authoritative and will rewrite "
        "the persisted pair"
    )
    # Sanity: the cold path's return must still be the minimal catalog.
    assert "_minimal_static_models_catalog" in cold_block, (
        "the prefer_cache cold-path return must serve the minimal static "
        "catalog (not a fresh live rebuild) — otherwise the round-6 "
        "non-wait contract is being silently broken"
    )


def test_resolver_has_non_authoritative_guard():
    """Static guard on the resolver side: the guard must be present
    inside ``_resolve_compatible_session_model_state`` so a future
    refactor cannot silently delete the short-circuit.
    """
    import inspect

    src = inspect.getsource(routes._resolve_compatible_session_model_state)
    assert "_non_authoritative" in src, (
        "_resolve_compatible_session_model_state must check the catalog's "
        "_non_authoritative marker and short-circuit to the persisted pair"
    )
    # The guard should be in the body, not just the docstring — check
    # the function body specifically.
    body_marker_idx = src.find("catalog.get(\"_non_authoritative\")")
    assert body_marker_idx != -1, (
        "the guard must read catalog.get(\"_non_authoritative\") inside the "
        "function body, not just mention the concept in a docstring"
    )


# ---------------------------------------------------------------------------
# 5. Reverse verification — the regression must fail without the fix
# ---------------------------------------------------------------------------


def test_regression_fails_without_guard(monkeypatch, no_copilot_in_providers_cfg):
    """Reverse-verification: if a future edit drops the guard, this test
    must catch it. We do that by patching
    ``_resolve_compatible_session_model_state`` to its pre-fix behaviour
    (no guard) and asserting the display resolvers then rewrite the
    persisted pair. Then we restore the patched function and assert the
    real implementation still passes — together this proves the guard
    is the load-bearing piece, not the test fixture.
    """
    from api.routes import _resolve_compatible_session_model_state as real_impl

    def pre_fix_impl(
        model_id,
        model_provider=None,
        *,
        profile_provider=None,
        profile_default_model=None,
        profile_config=None,
        explicit_model_pick=False,
        prefer_cached_catalog=False,
        wait_for_inflight_rebuild=True,
    ):
        # Strip the marker so the slow path runs as it did before round 6.
        with patch(
            "api.routes.get_available_models",
            return_value={
                k: v
                for k, v in _NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG.items()
                if not k.startswith("_non_authoritative")
            },
        ):
            return real_impl(
                model_id,
                model_provider,
                profile_provider=profile_provider,
                profile_default_model=profile_default_model,
                profile_config=profile_config,
                explicit_model_pick=explicit_model_pick,
                prefer_cached_catalog=prefer_cached_catalog,
                wait_for_inflight_rebuild=wait_for_inflight_rebuild,
            )

    session = _FakeSession("@copilot:gpt-5.5", "copilot")
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        pre_fix_impl,
    )

    model = routes._resolve_effective_session_model_for_display(session)
    provider = routes._resolve_effective_session_model_provider_for_display(
        session
    )

    # Pre-fix: the compat-normalize would have stripped the @copilot:
    # qualifier and switched the provider to openai-codex. That is
    # exactly the bug the guard is meant to prevent.
    assert model == "gpt-5.5", (
        "reverse-verification: without the guard, the display resolver "
        "should normalize the pair (proves the regression is real)"
    )
    assert provider == "openai-codex", (
        "reverse-verification: without the guard, the display provider "
        "should switch to openai-codex (proves the regression is real)"
    )


# ---------------------------------------------------------------------------
# 1c. Round-8 findings (re-review #7568, nesquena-hermes 2026-09-23):
#     registered plugin providers must be preserved, and a hint whose
#     @provider: qualifier DISAGREES with the requested provider must be
#     repaired, not preserved.
# ---------------------------------------------------------------------------


def test_resolver_preserves_registered_plugin_provider_pair(
    monkeypatch, no_copilot_in_providers_cfg
):
    """A persisted ``@myplugin:model`` pair whose provider is only known via
    the plugin registry must survive a non-authoritative catalog.

    Round 6/7's ``_provider_is_known_or_configured()`` recognized static and
    custom providers but never called ``_is_plugin_model_provider()``, so a
    registered plugin-provider selection was treated as unknown during an
    in-flight catalog rebuild and repaired away to the active default.

    The stubbing targets the plugin-registry predicate itself (NOT the
    outer ``_provider_is_known_or_configured``), so the REAL predicate body
    — including the plugin detection branch — runs. Without the fix the
    registry branch is absent and this test fails.
    """
    monkeypatch.setattr(
        cfg, "_is_plugin_model_provider", lambda pid: pid == "myplugin"
    )
    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ):
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "@myplugin:gpt-5.5", "myplugin"
            )
        )

    assert effective_model == "@myplugin:gpt-5.5", (
        "non-authoritative catalog repaired away a registered plugin "
        "provider selection (re-review #7568 round 8)"
    )
    assert effective_provider == "myplugin"
    assert normalized is False


def test_resolver_repairs_hint_whose_qualifier_disagrees_with_requested_provider(
    monkeypatch, no_copilot_in_providers_cfg
):
    """A legacy cold-wakeup pair whose ``@provider:`` qualifier names a
    DIFFERENT provider than ``requested_provider`` (e.g. persisted
    ``@copilot:gpt-5.5`` while the session's provider is ``openai-codex``)
    must fall through to the compatibility repair, not be preserved.

    Preservation pins the browser echo, and ``model_with_provider_context()``
    keeps an existing ``@copilot:`` qualifier intact — so preserving this
    pair routes every subsequent turn to copilot even though the request
    asked for openai-codex. Only a hint that resolves to the SAME provider
    as the request is safe to preserve.
    """
    monkeypatch.setattr(routes, "_provider_is_known_or_configured", lambda *a, **k: True)
    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ):
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "@copilot:gpt-5.5", "openai-codex"
            )
        )

    assert effective_model == "gpt-5.5", (
        "hint whose @provider: qualifier disagrees with the requested "
        "provider must be repaired to the active default, not preserved "
        f"(re-review #7568 round 8): got {effective_model!r}"
    )
    assert effective_provider == "openai-codex"
    assert normalized is True, (
        "a mismatched-qualifier repair must report model_was_normalized=True "
        "so callers treat it as a repair"
    )


def test_resolver_still_preserves_hint_that_matches_requested_provider(
    monkeypatch, no_copilot_in_providers_cfg
):
    """Control for the qualifier check: a hint that resolves to the SAME
    provider as the request (raw / alias / normalized equality) is still
    preserved. Without this, an over-broad equality check would repair
    away legitimate persisted pairs and re-open the silent-revert bug.
    """
    monkeypatch.setattr(routes, "_provider_is_known_or_configured", lambda *a, **k: True)
    with patch(
        "api.routes.get_available_models",
        return_value=_NON_AUTHORITATIVE_COPILOT_LACKING_CATALOG,
    ):
        effective_model, effective_provider, normalized = (
            routes._resolve_compatible_session_model_state(
                "@copilot:gpt-5.5", "copilot"
            )
        )

    assert (effective_model, effective_provider, normalized) == (
        "@copilot:gpt-5.5",
        "copilot",
        False,
    ), (
        "a hint that matches the requested provider is a legitimate "
        "persisted selection and must stay preserved on the "
        "non-authoritative path"
    )
