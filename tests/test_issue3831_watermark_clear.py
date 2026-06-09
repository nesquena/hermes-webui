"""Regression tests for #3831 — stale truncation_watermark data loss.

Root cause: retry_last / undo_last / the Edit-truncate handler set
``truncation_watermark`` to suppress the *replaced* tail from the append-only
state.db merge. ``Session.save()`` deliberately does not auto-clear it (#2914),
but nothing cleared it when the user then sent a genuinely new turn either — so
it froze at the old edit boundary. A frozen watermark then dropped post-watermark
state.db rows whenever the sidecar was later reconstructed empty
(recovery/reconcile), permanently losing the turns sent after the edit.

Fix: retire a *positive* watermark to ``None`` once the new user turn is
COMMITTED to ``session.messages`` (the success-merge, eager-checkpoint, and
recovery/cold-load commit points) — NOT at chat-start. Clearing at chat-start
was unsafe: in deferred mode (the default) the new user row isn't in messages
yet, so a merge in that window would resurrect the replaced tail (the
max-sidecar guard hasn't risen past the old boundary). Once the row is committed,
``max_sidecar_timestamp`` rises past the replaced tail and the merge suppresses
it without the watermark, so retiring it is both safe and necessary.

``None`` is distinct from ``0.0``: ``0.0`` is the truncate-to-empty sentinel
(#2914) that must keep blocking all state replay, so the clear is falsy-gated and
never touches ``0.0``.
"""
import api.models as models
import api.streaming as streaming


def _rows(*specs):
    return [
        {"role": role, "content": content, "timestamp": ts}
        for (role, content, ts) in specs
    ]


class _FakeSession:
    def __init__(self, watermark):
        self.truncation_watermark = watermark
        self.messages = []
        self.context_messages = []
        self.pending_user_message = None
        self.pending_attachments = None
        self.pending_started_at = None


# --- The fix: a committed new turn retires a stale positive watermark ---------

def test_retire_helper_clears_positive_watermark():
    s = _FakeSession(100.0)  # stale, from a prior retry/undo/edit
    s.messages = _rows(("user", "new turn", 200))
    streaming._retire_truncation_watermark_after_commit(s)
    assert s.truncation_watermark is None


def test_retire_helper_leaves_zero_watermark_untouched():
    """0.0 is the truncate-to-empty sentinel (#2914), not a stale boundary — the
    falsy guard must leave it alone so #2914 replay-blocking is preserved."""
    s = _FakeSession(0.0)
    s.messages = _rows(("user", "new turn", 200))
    streaming._retire_truncation_watermark_after_commit(s)
    assert s.truncation_watermark == 0.0


def test_retire_helper_noop_when_unset():
    s = _FakeSession(None)
    streaming._retire_truncation_watermark_after_commit(s)
    assert s.truncation_watermark is None


def test_recovery_commit_clears_watermark():
    """The cold-load / recovery commit path (_append_recovered_pending_turn)
    retires a stale positive watermark when it materializes the pending turn."""
    s = _FakeSession(100.0)
    s.pending_user_message = "recovered new turn"
    s.pending_attachments = None
    models._append_recovered_pending_turn(s, timestamp=200)
    assert s.truncation_watermark is None
    # The pending turn was committed to messages.
    assert any(m.get("content") == "recovered new turn" for m in s.messages)


# --- The effect: cleared (None) watermark stops dropping post-edit turns -------

def test_cleared_watermark_keeps_post_edit_state_rows_with_empty_sidecar():
    """Once the watermark is cleared (None), an empty-sidecar reconcile keeps the
    genuinely-new post-edit turns instead of dropping them (the #3831 outcome)."""
    state = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),     # old edit boundary
        ("user", "q2-new", 200),      # sent AFTER the retry/edit
        ("assistant", "a2-new", 300),
    )
    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=None
    )
    assert [m["content"] for m in merged] == ["q1", "a1", "q2-new", "a2-new"]


# --- The guardrails: the fix must NOT regress #2914/#3102 ---------------------

def test_active_watermark_still_filters_replaced_tail_empty_sidecar():
    """A still-active (positive) watermark with an empty sidecar still suppresses
    rows above the boundary — the replaced/edited tail (#2914). The fix only
    changes WHEN the watermark is retired, never the merge semantics."""
    state = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),
        ("user", "replaced-q2", 200),   # above watermark -> filtered
        ("assistant", "replaced-a2", 300),
    )
    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=100.0
    )
    assert [m["content"] for m in merged] == ["q1", "a1"]


def test_zero_watermark_still_blocks_all_replay_empty_sidecar():
    """A 0.0 watermark on a truncate-to-empty session must STILL block all state
    replay (#2914) — unchanged by this fix."""
    state = _rows(
        ("user", "only prompt", 1.0),
        ("assistant", "only reply", 2.0),
    )
    merged = models.merge_session_messages_append_only(
        [], state, truncation_watermark=0.0
    )
    assert merged == []


# --- Inline commit-path coverage (greptile): the two sites that inline the -----
# --- falsy-gated clear instead of calling the helper must behave identically. --

def test_error_path_materialize_clears_positive_watermark():
    """The error/cancel materialization path (_materialize_pending_user_turn_before_error)
    inlines the watermark clear. #3831 was triggered on recovery/reconcile after a
    crash, so the error path is precisely the failure mode — lock it down: a pending
    user turn committed on the error path retires a stale positive watermark."""
    s = _FakeSession(100.0)  # stale, from a prior retry/undo/edit
    s.pending_user_message = "new turn after edit"
    appended = streaming._materialize_pending_user_turn_before_error(s)
    assert appended is True
    assert s.truncation_watermark is None
    assert any(m.get("content") == "new turn after edit" for m in s.messages)


def test_error_path_materialize_preserves_zero_sentinel():
    """The error-path inline must use the same falsy guard as the helper: the 0.0
    truncate-to-empty sentinel (#2914) is preserved, not cleared."""
    s = _FakeSession(0.0)
    s.pending_user_message = "new turn"
    streaming._materialize_pending_user_turn_before_error(s)
    assert s.truncation_watermark == 0.0


def test_eager_checkpoint_clears_positive_watermark():
    """The eager first-turn checkpoint path (_checkpoint_user_message_for_eager_session_save
    in routes.py) inlines the same clear. A committed user turn retires a stale
    positive watermark; the 0.0 sentinel is preserved."""
    import api.routes as routes

    s = _FakeSession(100.0)
    routes._checkpoint_user_message_for_eager_session_save(
        s, "eager new turn", None, started_at=200.0
    )
    assert s.truncation_watermark is None
    assert any(m.get("content") == "eager new turn" for m in s.messages)

    s0 = _FakeSession(0.0)
    routes._checkpoint_user_message_for_eager_session_save(
        s0, "eager new turn", None, started_at=200.0
    )
    assert s0.truncation_watermark == 0.0

