"""Regression coverage for #4504 — clarify card / composer stuck after expiry.

Two compounding gaps the user experienced:

  1. Server-side ``clear_pending(sid)`` ran on silent timeout but emitted no
     SSE notify, so the browser never knew the prompt was gone — the visible
     card stayed up and the composer stayed locked.

  2. The client's ``respondClarify`` catch-block treated every 409 (including
     ``stale: true``) as retryable, leaving the card + draft visible and the
     controls re-enabled — but every retry returned 409, so the session was
     permanently stuck. The user had zero affordance to dismiss the card.

This file pins:

  - ``clear_pending`` notifies SSE subscribers (head=None, total=0) so the
    silent-timeout path takes the card down via the existing pending-=-null
    branch in ``_handleClarifyEvent``.
  - The client's ``respondClarify`` catch-block, on ``e.status === 409``,
    routes to ``hideClarifyCard(true, 'expired')`` so the draft is moved into
    the now-unlocked composer (via ``_stashClarifyDraft('expired')``) instead
    of being re-enabled for an impossible retry.

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
    """clear_pending must push (head=None, total=0) so the browser hides the card."""

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
            "silent-timeout path tells the browser to take the card down (#4504)."
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
# 2. Client-side fix — respondClarify catch must treat 409 as terminal.
#    (Phase B in the issue.)
# ══════════════════════════════════════════════════════════════════════════════
class TestRespondClarify409Terminal:
    """The catch block must call hideClarifyCard(true, 'expired') on 409."""

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

    def test_409_routes_to_hide_clarify_card_expired(self):
        body = self._respond_clarify_body()
        assert "e.status === 409" in body, (
            "respondClarify catch should branch on e.status === 409 to handle "
            "the stale/expired case distinctly from network errors (#4504)."
        )
        assert 'hideClarifyCard(true, "expired")' in body, (
            "On 409 the card must be dismissed via hideClarifyCard(true, "
            "'expired') so _stashClarifyDraft('expired') routes the draft into "
            "the now-unlocked composer (#4504). The old 'keep card + re-enable "
            "controls' behavior left the user permanently stuck."
        )

    def test_409_does_not_re_enable_controls(self):
        body = self._respond_clarify_body()
        # Find the 409 branch (between "e.status === 409" and the next "}")
        idx = body.index("e.status === 409")
        # Pull a window large enough to cover the 409 branch but stop before
        # the network-error branch that legitimately re-enables controls.
        branch = body[idx : idx + 800]
        # The "early return" inside the 409 branch must short-circuit before
        # the network-error branch's _clarifySetControlsDisabled(false, false).
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
