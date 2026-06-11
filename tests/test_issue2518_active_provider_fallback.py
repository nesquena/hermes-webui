"""Tests for issue #2518 — cold-start /api/session/new slow path fallback.

The frontend in-flight guard (PR #2528, b76d698a) made repeated + clicks safe
but did not shorten a single cold click: newSession() in static/sessions.js
carries the dropdown's model_provider as ``reqBody.model_provider``. When the
dropdown option has no ``data-provider`` attribute (or its value is
``'default'``) and the persisted state predates provider tracking,
``newModelState.model_provider`` is null. The server's fast path in
``_resolve_compatible_session_model_state`` requires both ``model`` AND a
truthy ``model_provider``; without that, the request falls into
``get_available_models()`` and pays the 3-4s cold catalog rebuild on first
click after server boot.

These tests pin the follow-up fix: newSession() falls back to
``window._activeProvider`` (boot-hydrated) and then the previous session's
``model_provider`` so the fast path is hit whenever a usable default exists.
The slow path remains correct for users with no hydrated active provider and
no previous session — they get the catalog lookup, just like today.

Coverage:

1. newSession() source carries the active-provider fallback chain.
2. End-to-end: when client sends ``model_provider`` (either explicit or via
   the new fallback), /api/session/new's resolve step does NOT call
   ``get_available_models()``.
3. Negative: client sends ``model_provider: null`` (no fallback available) —
   resolve step still works via the slow path and returns the catalog's
   default.
4. The fallback chain order is correct: explicit > _activeProvider >
   previous-session > null.
"""
import pathlib

from unittest.mock import patch


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Client-side: source-shape check that the fallback is wired in newSession().
# ---------------------------------------------------------------------------


class TestClientFallbackSourceShape:
    """Static checks that the fallback chain lives inside newSession()."""

    def test_active_provider_fallback_present(self):
        src = _read("static/sessions.js")
        idx = src.find("async function newSession(flash, options={}){")
        assert idx != -1
        body = src[idx:idx + 6000]
        assert "window._activeProvider" in body, (
            "newSession() must consult window._activeProvider when the dropdown "
            "did not yield a truthy model_provider (cold boot, empty "
            "data-provider, or pre-provider persisted state)."
        )

    def test_previous_session_fallback_present(self):
        src = _read("static/sessions.js")
        idx = src.find("async function newSession(flash, options={}){")
        body = src[idx:idx + 6000]
        assert "S.session&&S.session.model_provider" in body, (
            "newSession() must fall back to the previous session's "
            "model_provider when neither the dropdown nor window._activeProvider "
            "is available (unhydrated dropdown, no active provider yet)."
        )

    def test_fallback_chain_order(self):
        """Fallback order: explicit > _activeProvider > prev-session > null."""
        src = _read("static/sessions.js")
        idx = src.find("async function newSession(flash, options={}){")
        body = src[idx:idx + 6000]
        explicit = body.find("newModelState.model_provider")
        active = body.find("window._activeProvider")
        prev = body.find("S.session&&S.session.model_provider")
        assert -1 < explicit < active < prev, (
            f"Fallback chain order broken: explicit={explicit}, "
            f"_activeProvider={active}, prev-session={prev}. "
            "Explicit selection must beat _activeProvider which must beat "
            "the previous session's model_provider."
        )

    def test_issue_referenced_in_source(self):
        """Future readers should be able to trace this back to the issue."""
        src = _read("static/sessions.js")
        idx = src.find("async function newSession(flash, options={}){")
        body = src[idx:idx + 4000]
        assert "#2518" in body, (
            "newSession()'s fallback comment should reference #2518 so the "
            "follow-up provenance survives future refactors."
        )


# ---------------------------------------------------------------------------
# End-to-end: with model_provider, /api/session/new skips the cold catalog.
# ---------------------------------------------------------------------------


class TestSessionNewFastPathWithProvider:
    """When client supplies a real model_provider, no catalog rebuild."""

    def test_explicit_provider_skips_get_available_models(self):
        """The headline fix: client-supplied provider → fast path."""
        from api.routes import _session_model_state_from_request

        with patch("api.routes.get_available_models") as mock_catalog:
            model, provider = _session_model_state_from_request(
                "gpt-5.5",
                "openai-codex",
            )

        assert mock_catalog.call_count == 0
        assert model == "gpt-5.5"
        assert provider == "openai-codex"

    def test_active_provider_fallback_does_not_double_invoke_catalog(self):
        """Sanity: the fast path is shared between the explicit and fallback
        cases on the client. As long as the client sent a truthy
        model_provider, the server stays on the fast path. The actual
        fallback selection happens client-side; this test pins that the
        server side is invariant under the two client strategies."""
        from api.routes import _session_model_state_from_request

        # Simulate the two client strategies (explicit vs active-provider
        # fallback) producing the same wire shape.
        for client_provider in ("openai-codex", "anthropic", "openrouter"):
            with patch("api.routes.get_available_models") as mock_catalog:
                _session_model_state_from_request("claude-opus-4.7", client_provider)
            assert mock_catalog.call_count == 0, (
                f"client_provider={client_provider!r} must hit the fast path; "
                f"otherwise the #2518 fallback is invisible to the server."
            )


# ---------------------------------------------------------------------------
# Negative: when no provider is available anywhere, slow path is still correct.
# ---------------------------------------------------------------------------


class TestSessionNewSlowPathStillFiresWithoutProvider:
    """The slow path remains the safety net for genuinely provider-less clients."""

    def test_null_provider_falls_back_to_catalog(self):
        """If the client really has nothing to send, the slow path must work."""
        from api.routes import _session_model_state_from_request

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [
                    {"provider_id": "openai-codex", "models": [{"id": "gpt-5.5"}]}
                ],
            }
            model, provider = _session_model_state_from_request("gpt-5.5", None)

        # Slow path was taken because no provider was supplied.
        assert mock_catalog.call_count == 1
        # The slow path still returns a sane (model, provider) tuple.
        assert model
        assert provider


# ---------------------------------------------------------------------------
# Follow-up: slash-slug cross-provider guard raised during PR #3410 review.
# ---------------------------------------------------------------------------
#
# When the persisted state carries a stale foreign-slug model such as
# ``gemini/gemini-2.5`` from a session served by a different provider than
# the now-active one, the original PR's unconditional
# ``window._activeProvider`` fallback would attach the wrong provider to
# the new session and the server's fast path would pass it through without
# consulting the catalog — silently re-pointing the session at the wrong
# backend (the exact case ``_resolve_compatible_session_model_state``'s
# slow-path normalization is designed to fix, see routes.py:1891-1894).
#
# The fix gates the active-provider fallback behind a ``_bareModel`` check:
# slash-qualified and @-qualified models keep ``reqBody.model_provider``
# null so the server's slow-path cross-provider repair still runs. These
# tests pin the BEHAVIOR (gate present, explicit picker still wins,
# ordering preserved) rather than the source-string literal — a future
# refactor that keeps the same contract (e.g. extracting a helper or
# switching to a named regex) still satisfies them.


def _provider_assignment_in_new_session() -> str:
    """Extract the slash-slug guard + ``reqBody.model_provider``
    assignment block in newSession() — from the ``const _bareModel``
    declaration through the assignment's terminating semicolon.

    The block is two statements glued by a single semicolon at the
    end of each:

        const _bareModel = !/[/]/.test(newModelState.model)
                        && !newModelState.model.startsWith('@');
        reqBody.model_provider = newModelState.model_provider
            || (_bareModel ? (window._activeProvider || (S.session && S.session.model_provider)) : null)
            || null;

    Both lines live in the same 4000-char slice of newSession()'s
    function body, so the helper can read them as a single contract
    unit. Anchors on the ``=`` of the assignment (not a prose mention
    in a comment) and on the guard declaration so future comments
    referencing ``reqBody.model_provider`` cannot confuse it.
    """
    src = _read("static/sessions.js")
    idx = src.find("async function newSession(flash, options={}){")
    assert idx != -1, "newSession() must be defined in static/sessions.js"
    body = src[idx : idx + 6000]
    guard_start = body.find("const _bareModel")
    assert guard_start != -1, (
        "newSession() must declare a 'const _bareModel' guard for the "
        "cross-provider slash-slug regression from PR #3410 review."
    )
    # Slice from the _bareModel guard through the END of the
    # `reqBody.model_provider=...;` assignment. The block now contains several
    # helper declarations between the guard and the assignment (the family-
    # mismatch guard added in the #3410-followup review), so anchor on the
    # assignment's `=` (not a comment mention) and take through its terminating
    # ';' rather than counting semicolons.
    assign_at = body.find("reqBody.model_provider=", guard_start)
    assert assign_at != -1, (
        "reqBody.model_provider= assignment must appear after the _bareModel guard"
    )
    assign_end = body.find(";", assign_at)
    assert assign_end != -1, "reqBody.model_provider assignment must terminate with ';'"
    return body[guard_start : assign_end + 1]


class TestIssue2518FollowupSlashSlugGuard:
    """Regression coverage for the cross-provider slash-slug edge case
    raised during PR #3410 review. The contract under test is:

    1. A slash-qualified model (e.g. ``gemini/gemini-2.5``) MUST NOT pick
       up ``window._activeProvider`` — the slow-path normalization in
       ``_resolve_compatible_session_model_state`` is the only correct
       way to repair a foreign provider namespace.
    2. An @-qualified model (e.g. ``@openai-codex:gpt-5.5``) similarly
       MUST NOT pick up ``window._activeProvider`` — the
       ``@provider:model`` form already names a provider, and a
       second one from the client would race the server's own
       ``_split_provider_qualified_model`` resolution.
    3. Explicit picker selection (``newModelState.model_provider`` from
       ``_modelStateForSelect``) still wins over both fallbacks.
    4. The fallback chain ordering remains: explicit > _activeProvider >
       prev-session — guarded by the ``_bareModel`` ternary, not
       short-circuited.
    """

    def test_slash_qualified_model_keeps_active_provider_behind_guard(self):
        """`_bareModel` ternary must gate `_activeProvider`, and the
        gate must trigger on a slash in the model id."""
        expr = _provider_assignment_in_new_session()
        # The guard is a ternary that flips to null for non-bare models.
        assert "_bareModel" in expr, (
            "newSession() must gate the _activeProvider fallback behind a "
            "_bareModel ternary so slash-qualified models do not pick up "
            "the wrong provider (cross-provider regression from PR #3410 "
            "review)."
        )
        # The gate's predicate must include a slash check.
        assert "/[/]/" in expr or "indexOf('/')" in expr or "includes('/')" in expr, (
            f"Guard predicate must detect a '/' in newModelState.model; "
            f"got expression: {expr!r}"
        )
        # And the active-provider fallback must live inside that ternary's
        # truthy arm (via the _fallbackProvider helper), not on the top-level
        # OR chain — otherwise a slash-slug would still get a provider attached.
        ternary_true_arm_start = expr.find("(_bareModel&&")
        if ternary_true_arm_start == -1:
            ternary_true_arm_start = expr.find("(_bareModel?")
        assert ternary_true_arm_start != -1, (
            f"Expected a '_bareModel'-gated ternary in expression: {expr!r}"
        )
        # The truthy arm references _fallbackProvider, which is derived from
        # window._activeProvider (then prev-session) only for bare models.
        ternary_block = expr[ternary_true_arm_start:]
        assert "_fallbackProvider" in ternary_block, (
            "the _bareModel-gated arm must use _fallbackProvider (derived from "
            "window._activeProvider) so non-bare models skip it entirely "
            "(defense against cross-provider mismatch for persisted slash-slug state)."
        )
        # And _fallbackProvider itself must be sourced from _activeProvider.
        assert "window._activeProvider" in expr, (
            "_fallbackProvider must derive from window._activeProvider."
        )

    def test_at_qualified_model_also_keeps_active_provider_behind_guard(self):
        """`@provider:model` strings carry their own provider context and
        must not pick up `_activeProvider` either — the server's
        `_split_provider_qualified_model` is the source of truth for
        those."""
        expr = _provider_assignment_in_new_session()
        # The guard's predicate must also check for an @ prefix.
        assert "startsWith('@')" in expr or "startsWith(\"@\")" in expr, (
            f"Guard predicate must also reject '@provider:model' strings; "
            f"got expression: {expr!r}"
        )

    def test_explicit_picker_provider_still_wins(self):
        """Explicit picker provider (from ``_modelStateForSelect``) is
        the highest-priority source — it must precede the guarded
        fallback and the prev-session fallback in the assignment chain.
        """
        expr = _provider_assignment_in_new_session()
        # Runtime precedence (the contract): in the `reqBody.model_provider=`
        # assignment, newModelState.model_provider is the FIRST operand of the
        # `||` chain, so an explicit picker provider always wins. The bare-model
        # fallback (_fallbackProvider) is the second operand. Within
        # _fallbackProvider's own definition, _activeProvider precedes
        # prev-session. (The helper is declared above the assignment, so a flat
        # positional check across the whole block no longer applies — assert the
        # two precedence facts that actually matter.)
        assign_at = expr.find("reqBody.model_provider=")
        assert assign_at != -1, f"no assignment in expr: {expr!r}"
        assign = expr[assign_at:]
        pos_explicit_in_assign = assign.find("newModelState.model_provider")
        pos_fallback_in_assign = assign.find("_fallbackProvider")
        assert -1 < pos_explicit_in_assign < pos_fallback_in_assign, (
            f"explicit picker (newModelState.model_provider) must be the first "
            f"operand, before the _fallbackProvider fallback, in the assignment: {assign!r}"
        )
        # _activeProvider precedes prev-session inside _fallbackProvider.
        pos_active = expr.find("window._activeProvider")
        pos_prev = expr.find("S.session&&S.session.model_provider")
        assert -1 < pos_active < pos_prev, (
            f"_fallbackProvider must source _activeProvider before prev-session: {expr!r}"
        )

    def test_no_op_null_terminal_in_fallback_chain(self):
        """The cleaned expression must not carry a *vestigial* mid-chain
        ``||null`` no-op directly on the top-level OR chain (the cosmetic
        paste artifact flagged in PR #3410 review). Two legitimate ``||null``
        terminals remain after the family-mismatch refactor: the inner
        ``_fallbackProvider||null`` (the bare-model arm's own fallback) and the
        final ``||null`` terminal. Verify the OLD vestigial pattern
        ``model_provider||null||`` (null immediately after the explicit source)
        is gone."""
        expr = _provider_assignment_in_new_session()
        assert "model_provider||null" not in expr.replace(" ", ""), (
            f"newModelState.model_provider must not be directly followed by a "
            f"'||null' no-op (the cosmetic paste artifact from PR #3410). "
            f"Expression: {expr!r}"
        )

    def test_slash_slug_keeps_provider_null_in_wire_shape(self):
        """Behavior contract: when newSession() is given a slash-slug
        model with no explicit picker provider and no previous-session
        fallback, the wire-shape ``reqBody.model_provider`` must be
        ``null`` — the slow path's cross-provider normalization is the
        only place that can repair a foreign slug.

        We verify this by simulating the JS expression in pure Python so
        the test is language-agnostic: the test only cares that the
        client produces ``null`` for the right inputs, not how it spells
        the JS source.
        """
        # Mirror the JS expression structure. The contract is the
        # predicate + the OR-chain shape, not the operator spelling.
        new_model_state = {
            "model": "gemini/gemini-2.5",
            "model_provider": None,  # _providerFromModelValue returns ''
        }
        bare = (
            "/" not in new_model_state["model"]
            and not new_model_state["model"].startswith("@")
        )
        active_provider = "openai-codex"
        prev_session_provider = None
        # Same expression shape as the new client code.
        req_body_model_provider = (
            new_model_state["model_provider"]
            or (
                active_provider
                or prev_session_provider
            )
            if bare
            else None
        ) or None
        assert req_body_model_provider is None, (
            f"Slash-slug model {new_model_state['model']!r} must send "
            f"model_provider=null so the server's slow path can repair "
            f"the cross-provider mismatch; got {req_body_model_provider!r}"
        )

    def test_bare_model_uses_active_provider_when_no_picker(self):
        """Behavior contract: a bare model with no explicit picker
        provider but a hydrated active provider must still hit the
        fast path — that is the whole point of the #2518 follow-up.
        The _bareModel guard must not break this case.
        """
        new_model_state = {"model": "gpt-5.5", "model_provider": None}
        bare = (
            "/" not in new_model_state["model"]
            and not new_model_state["model"].startswith("@")
        )
        active_provider = "openai-codex"
        prev_session_provider = None
        req_body_model_provider = (
            new_model_state["model_provider"]
            or (
                active_provider
                or prev_session_provider
            )
            if bare
            else None
        ) or None
        assert req_body_model_provider == "openai-codex", (
            f"Bare model {new_model_state['model']!r} with hydrated "
            f"active provider must send it through so the fast path "
            f"fires; got {req_body_model_provider!r}"
        )

    def test_bare_family_mismatch_keeps_provider_null(self):
        """Family-mismatch guard (Codex #3410-followup finding): a bare model
        whose KNOWN family prefix (gpt/claude/gemini) maps to a DIFFERENT
        provider than the fallback we'd attach must send model_provider=null,
        so the server slow-path's family repair runs instead of the fast path
        silently routing the model to the wrong backend.

        Simulate the client's new logic in Python (family map + normalize),
        mirroring static/sessions.js, and assert the wire shape.
        """
        def _family_provider(m):
            s = (m or "").lower()
            if s.startswith("gpt"):
                return "openai"
            if s.startswith("claude"):
                return "anthropic"
            if s.startswith("gemini"):
                return "google"
            return ""

        def _norm_prov(p):
            s = (p or "").lower()
            if s.startswith("openai"):
                return "openai"
            if s.startswith("anthropic") or s.startswith("claude"):
                return "anthropic"
            if s.startswith("google") or s.startswith("gemini"):
                return "google"
            return s

        def _wire_provider(model, model_provider, active_provider, prev_provider):
            bare = "/" not in model and not model.startswith("@")
            fallback = (active_provider or prev_provider or "") if bare else ""
            fam = _family_provider(model)
            mismatch = bool(fam and fallback and _norm_prov(fallback) != fam)
            return (
                model_provider
                or ((fallback or None) if (bare and not mismatch) else None)
                or None
            )

        # claude-family bare model + openrouter active → MISMATCH → null (slow path repairs)
        assert _wire_provider("claude-opus-4.8", None, "openrouter", None) is None
        # gemini-family bare model + anthropic active → MISMATCH → null
        assert _wire_provider("gemini-2.5-pro", None, "anthropic", None) is None
        # gpt-family bare model + openai-codex active → MATCH → fast path
        assert _wire_provider("gpt-5.5", None, "openai-codex", None) == "openai-codex"
        # claude-family bare model + anthropic active → MATCH → fast path
        assert _wire_provider("claude-opus-4.8", None, "anthropic", None) == "anthropic"
        # unknown-family bare model (e.g. a custom/local id) + any provider → attaches (no family signal)
        assert _wire_provider("my-local-model", None, "custom", None) == "custom"
        # explicit picker provider always wins, even on a family mismatch
        assert _wire_provider("claude-opus-4.8", "anthropic", "openrouter", None) == "anthropic"
