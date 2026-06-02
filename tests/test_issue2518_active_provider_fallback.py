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
        body = src[idx:idx + 4000]
        assert "window._activeProvider" in body, (
            "newSession() must consult window._activeProvider when the dropdown "
            "did not yield a truthy model_provider (cold boot, empty "
            "data-provider, or pre-provider persisted state)."
        )

    def test_previous_session_fallback_present(self):
        src = _read("static/sessions.js")
        idx = src.find("async function newSession(flash, options={}){")
        body = src[idx:idx + 4000]
        assert "S.session&&S.session.model_provider" in body, (
            "newSession() must fall back to the previous session's "
            "model_provider when neither the dropdown nor window._activeProvider "
            "is available (unhydrated dropdown, no active provider yet)."
        )

    def test_fallback_chain_order(self):
        """Fallback order: explicit > _activeProvider > prev-session > null."""
        src = _read("static/sessions.js")
        idx = src.find("async function newSession(flash, options={}){")
        body = src[idx:idx + 4000]
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
