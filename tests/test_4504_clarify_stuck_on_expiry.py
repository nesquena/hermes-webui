"""Regression coverage for #4504 — clarify card / composer stuck after expiry.

Two compounding gaps the user experienced:

  1. Server-side ``clear_pending(sid)`` ran on silent timeout but emitted no
     SSE notify, so the browser never knew the prompt was gone (the visible
     card stayed up and the composer stayed locked).

  2. The client's ``respondClarify`` catch-block treated every 409 (including
     ``stale: true``) as retryable, leaving the card + draft visible and the
     controls re-enabled — but every retry returned 409, so the session was
     permanently stuck. The user had zero affordance to dismiss the card.

This file pins:

  - ``clear_pending`` notifies SSE subscribers (head=None, total=0) so the
    silent-timeout path takes the card down via the existing pending=null
    branch in ``_handleClarifyEvent`` if/when an SSE consumer is re-attached.
    (As of v0.51.340 the WebUI clarify transport is HTTP-poll only — the SSE
    notify is still ordering-correct and drives the sessions-list attention
    badge via the pre-existing ``publish_session_list_changed`` call.)
  - The client's ``respondClarify`` catch-block, on ``e.status === 409``:
      * matches the success path's ``_clarifyId === clarifyId`` guard so a
        late 409 for prompt A does not dismiss a rendered prompt B
        (#2639-style regression),
      * clears the loading/disabled state *before* ``hideClarifyCard`` so
        ``_stashClarifyDraft`` does not bail on the loading class (reviewer
        P1 from the first review pass — see PR #4524),
      * routes the same-id case through ``hideClarifyCard(true, 'expired')``
        so the draft is rescued into the now-unlocked composer.

The tests intentionally mirror the static-analysis + unit pattern already in
``test_clarify_sse.py`` so they ride the existing clarify suite layout.
"""

from __future__ import annotations

import os
import queue

import pytest


_CLARIFY = os.path.join(os.path.dirname(__file__), "..", "api", "clarify.py")
_MESSAGES = os.path.join(os.path.dirname(__file__), "..", "static", "messages.js")


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# ═════════════════════════════════════════════════════════════════════════════
# 1. Server-side fix — clear_pending must emit an SSE notify so the silent
#    timeout path actually wakes the browser. (Phase A in the issue.)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def clarify_mod():
    from api import clarify
    return clarify


@pytest.fixture(autouse=True)
def _cleanup_subscribers(clarify_mod):
    yield
    clarify_mod._clarify_sse_subscribers.clear()
    clarify_mod._gateway_queues.clear()
    clarify_mod._pending.clear()


class TestClearPendingNotifiesSSE:
    """clear_pending must push (head=None, total=0) so any SSE subscriber
    (or future SSE reattachment) gets a take-down event for the card."""

    def test_clear_pending_pushes_none_head_to_subscriber(self, clarify_mod):
        sid = "sess-4504-a"
        # Pre-load a pending clarify entry the way submit_pending does.
        entry = clarify_mod.submit_pending(sid, {"question": "y/n?"})
        assert entry is not None
        # Subscribe AFTER the submit so we don't have to drain its notify.
        sub = clarify_mod.sse_subscribe(sid)
        # Now expire it.
        cleared = clarify_mod.clear_pending(sid)
        assert cleared == 1
        # We should receive a clear push (head=None, total=0).
        msg = sub.get(timeout=1.0)
        assert msg == {"pending": None, "pending_count": 0}, (
            "clear_pending must emit a head=None / total=0 SSE notify so the "
            "silent-timeout path tells any SSE subscriber to take the card "
            "down (#4504)."
        )

    def test_clear_pending_no_op_does_not_notify(self, clarify_mod):
        sid = "sess-4504-b"
        sub = clarify_mod.sse_subscribe(sid)
        # No pending entry → no clear → no spurious notify.
        cleared = clarify_mod.clear_pending(sid)
        assert cleared == 0
        with pytest.raises(queue.Empty):
            sub.get(timeout=0.1)

    def test_clear_pending_unblocks_caller_event(self, clarify_mod):
        """The existing event.set() on the cleared entry stays in place."""
        sid = "sess-4504-c"
        entry = clarify_mod.submit_pending(sid, {"question": "ok?"})
        assert not entry.event.is_set()
        clarify_mod.clear_pending(sid)
        assert entry.event.is_set(), (
            "Clearing must still unblock the agent-side wait() so the "
            "_clarify_callback_impl timeout branch returns its fallback string."
        )


class TestClarifyClearPendingSourceMarkers:
    """Static-analysis pin for the Phase A fix (#4504)."""

    def test_clear_pending_calls_notify(self):
        src = _read(_CLARIFY)
        # The fix adds _clarify_sse_notify(session_key, None, 0) inside
        # clear_pending's _lock block.
        assert "_clarify_sse_notify(session_key, None, 0)" in src, (
            "clear_pending must invoke _clarify_sse_notify with None head so "
            "the silent-timeout path notifies SSE subscribers (#4504)."
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2. Client-side fix — respondClarify catch must treat 409 as terminal
#    *only* for the visible card, and rescue the typed draft into the
#    composer in the same-id case. (Phase B in the issue, plus reviewer P1s.)
# ══════════════════════════════════════════════════════════════════════════════
class TestRespondClarify409Terminal:
    """The 409 branch must guard on id, clear loading first, and dismiss only
    the visible card — never a newer prompt B that rendered while A's
    response was in flight."""

    @pytest.fixture(autouse=True)
    def _load_js(self):
        self.js = _read(_MESSAGES)

    def _respond_clarify_body(self) -> str:
        start = self.js.index("async function respondClarify(")
        # The function ends at the next top-level "function " or "var ".
        end_fn = self.js.index("\nfunction ", start + 1)
        end_var = self.js.index("\nvar ", start + 1)
        end = min(end_fn, end_var)
        return self.js[start:end]

    def _catch_409_branch(self, body: str) -> str:
        """Return the source of the 409 branch in respondClarify's catch."""
        idx = body.index("e.status === 409")
        # Pull a generous window covering both the same-id and different-id
        # paths inside the 409 branch, before the network-error fallback.
        return body[idx : idx + 1500]

    def test_409_routes_to_hide_clarify_card_expired(self):
        body = self._respond_clarify_body()
        assert "e.status === 409" in body, (
            "respondClarify catch should branch on e.status === 409 to handle "
            "the stale/expired case distinctly from network errors (#4504)."
        )
        assert 'hideClarifyCard(true, "expired")' in body, (
            "On 409 (same-id) the card must be dismissed via "
            "hideClarifyCard(true, 'expired') so _stashClarifyDraft('expired') "
            "routes the draft into the now-unlocked composer (#4504)."
        )

    def test_409_clears_loading_before_hide_so_draft_is_rescued(self):
        """Reviewer P1: _stashClarifyDraft bails when #clarifySubmit still
        carries the ``loading`` class set by _clarifySetControlsDisabled(true,
        true) at the top of respondClarify. The 409 same-id branch must
        call ``_clarifySetControlsDisabled(false, false)`` *before*
        ``hideClarifyCard(true, "expired")`` — otherwise the typed answer is
        silently dropped and no toast fires."""
        body = self._respond_clarify_body()
        branch = self._catch_409_branch(body)
        # Both calls must be present.
        assert "_clarifySetControlsDisabled(false, false)" in branch, (
            "409 same-id branch must clear the loading/disabled state so the "
            "_stashClarifyDraft loading-class guard does not bail (reviewer P1)."
        )
        assert 'hideClarifyCard(true, "expired")' in branch
        # And the clear MUST precede the hide.
        clear_idx = branch.index("_clarifySetControlsDisabled(false, false)")
        hide_idx = branch.index('hideClarifyCard(true, "expired")')
        assert clear_idx < hide_idx, (
            "_clarifySetControlsDisabled(false, false) must run before "
            "hideClarifyCard(true, 'expired') in the 409 same-id branch — "
            "otherwise _stashClarifyDraft bails on the loading class and the "
            "user's typed draft is silently dropped (PR #4524 reviewer P1)."
        )

    def test_409_is_guarded_by_clarify_id_match(self):
        """Reviewer P1: the success path is gated by ``_clarifyId === clarifyId``
        so a parallel poll that already rendered the next queued prompt B
        isn't clobbered (#2639). The 409 branch must follow the same
        contract — otherwise A's late 409 will dismiss B."""
        body = self._respond_clarify_body()
        branch = self._catch_409_branch(body)
        assert "_clarifyId === clarifyId" in branch, (
            "409 branch must mirror the success path's _clarifyId === "
            "clarifyId guard so a late 409 for prompt A does not dismiss a "
            "newer prompt B that rendered while A's response was in flight "
            "(#2639 regression flagged in PR #4524 review)."
        )

    def test_409_same_id_branch_clears_session_cache(self):
        """In the same-id case the stale per-session pending cache must be
        cleared so the SSE/poll path can't re-render the just-dismissed card.
        Mirrors the success path's _clearClarifyPendingForSession(sid) call."""
        body = self._respond_clarify_body()
        branch = self._catch_409_branch(body)
        assert "_clearClarifyPendingForSession(sid)" in branch, (
            "409 same-id branch must call _clearClarifyPendingForSession(sid) "
            "so the cached pending entry for this session cannot re-render "
            "the card we just dismissed (mirrors success-path contract)."
        )

    def test_409_different_id_branch_does_not_dismiss(self):
        """When _clarifyId != clarifyId at the time the 409 returns, a newer
        prompt B is showing. The branch must re-enable controls and return
        without touching the visible card (no hideClarifyCard, no
        _clearClarifyPendingForSession)."""
        body = self._respond_clarify_body()
        branch = self._catch_409_branch(body)
        # The 409 branch should dismiss the card in exactly one arm — the
        # same-id arm — so the different-id arm leaves the visible newer
        # prompt alone (#2639-style regression P1).
        assert branch.count('hideClarifyCard(true, "expired")') == 1, (
            "409 branch must dismiss the card in exactly one arm (the "
            "same-id arm); the different-id arm must leave the visible "
            "newer prompt alone (PR #4524 reviewer P1)."
        )
        # Both arms (same-id and different-id) must call
        # _clarifySetControlsDisabled(false, false) so the user can keep
        # interacting with whichever prompt is now visible.
        assert branch.count("_clarifySetControlsDisabled(false, false)") >= 2, (
            "Both 409 arms (same-id and different-id) must call "
            "_clarifySetControlsDisabled(false, false) so the user can keep "
            "interacting with whichever prompt is now visible."
        )

    def test_409_early_returns_before_network_fallback(self):
        body = self._respond_clarify_body()
        branch = self._catch_409_branch(body)
        # The 409 branch must early-return so the network-error fallback
        # below it does not double-touch state.
        assert "return;" in branch, (
            "The 409 branch must early-return so the network-error fallback "
            "does not re-enable the controls of a card we just dismissed."
        )

    def test_non_409_still_keeps_card_visible(self):
        """Network/transient errors keep the existing retry-friendly behavior."""
        body = self._respond_clarify_body()
        # The non-409 branch still calls _clarifySetControlsDisabled(false, false)
        # and re-focuses the input — that path must remain reachable.
        assert "_clarifySetControlsDisabled(false, false)" in body, (
            "Non-409 transient errors must still re-enable the controls so "
            "the user can retry once connectivity returns."
        )
