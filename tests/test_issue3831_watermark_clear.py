"""Regression tests for #3831 — stale truncation_watermark data loss.

Root cause: retry_last / undo_last / the Edit-truncate handler set
``truncation_watermark`` to suppress the *replaced* tail from the append-only
state.db merge. ``Session.save()`` deliberately does not auto-clear it (#2914),
but nothing cleared it when the user then sent a genuinely new turn either — so
it froze at the old edit boundary. A frozen watermark then dropped post-watermark
state.db rows whenever the sidecar was later reconstructed empty
(recovery/reconcile), permanently losing the turns sent after the edit.

Fix: advance a *positive* watermark to the newest user message timestamp once
the new user turn is COMMITTED to ``session.messages`` (the success-merge,
eager-checkpoint, and recovery/cold-load commit points) — NOT at chat-start.
Clearing at chat-start was unsafe: in deferred mode (the default) the new user
row isn't in messages yet, so a merge in that window would resurrect the replaced
tail (the max-sidecar guard hasn't risen past the old boundary). Once the row is
committed, ``max_sidecar_timestamp`` rises past the replaced tail and the merge
suppresses it without the watermark, so advancing it is both safe and necessary.

Advancing to the new message timestamp (instead of clearing to None) keeps the
merge's watermark filter active for replaced pre-edit rows from state.db whose
timestamps fall below the boundary, while the sidecar_advanced_past_watermark
guard allows newer state.db rows to merge in.

``0.0`` is the truncate-to-empty sentinel (#2914) that must keep blocking all
state replay, so the advance is falsy-gated and never touches ``0.0``.
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


# --- The fix: a committed new turn advances a stale positive watermark --------

def test_advance_helper_advances_positive_watermark():
    """After a new user turn is committed, the watermark advances to the
    newest user message timestamp — NOT cleared to None."""
    s = _FakeSession(100.0)  # stale, from a prior retry/undo/edit
    s.messages = _rows(("user", "new turn", 200))
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark == 200.0


def test_advance_helper_leaves_zero_watermark_untouched():
    """0.0 is the truncate-to-empty sentinel (#2914), not a stale boundary — the
    falsy guard must leave it alone so #2914 replay-blocking is preserved."""
    s = _FakeSession(0.0)
    s.messages = _rows(("user", "new turn", 200))
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark == 0.0


def test_advance_helper_noop_when_unset():
    s = _FakeSession(None)
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark is None


def test_advance_helper_uses_current_time_when_no_timestamp():
    """When the newest user message has no timestamp, the helper falls back to
    time.time()."""
    s = _FakeSession(100.0)
    s.messages = _rows(("user", "new turn", None))
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark is not None
    assert s.truncation_watermark > 100.0  # advanced past the old watermark


def test_advance_helper_picks_newest_user_timestamp():
    """When there are multiple user messages, the helper picks the newest one."""
    s = _FakeSession(50.0)
    s.messages = _rows(
        ("user", "first", 100),
        ("assistant", "reply", 101),
        ("user", "second", 200),
        ("assistant", "reply2", 201),
    )
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark == 200.0


def test_recovery_commit_advances_watermark():
    """The cold-load / recovery commit path (_append_recovered_pending_turn)
    advances a stale positive watermark when it materializes the pending turn."""
    s = _FakeSession(100.0)
    s.pending_user_message = "recovered new turn"
    s.pending_attachments = None
    models._append_recovered_pending_turn(s, timestamp=200)
    assert s.truncation_watermark == 200
    # The pending turn was committed to messages.
    assert any(m.get("content") == "recovered new turn" for m in s.messages)


# --- The effect: advanced watermark still merges post-edit turns --------------

def test_advanced_watermark_keeps_post_edit_state_rows():
    """With a sidecar whose max timestamp exceeds the watermark (the real
    post-edit state after the assistant reply was appended), post-edit state.db
    rows are merged in because sidecar_advanced_past_watermark is True."""
    sidecar = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),     # old edit boundary
        ("user", "q2-new", 200),      # new message after edit (watermark=200)
        ("assistant", "a2-new", 250), # assistant reply — pushes sidecar past watermark
    )
    state = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),
        ("user", "q2-new", 200),
        ("assistant", "a2-new", 250),
    )
    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=200.0
    )
    # max_sidecar_timestamp=250 > watermark=200 → sidecar_advanced_past_watermark=True
    # → watermark filter does not block a2-new
    assert [m["content"] for m in merged] == ["q1", "a1", "q2-new", "a2-new"]


# --- The guardrails: the fix must NOT regress #2914/#3102 ---------------------

def test_active_watermark_still_filters_replaced_tail_empty_sidecar():
    """A still-active (positive) watermark with an empty sidecar still suppresses
    rows above the boundary — the replaced/edited tail (#2914). The fix only
    changes WHEN the watermark is advanced, never the merge semantics."""
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


# --- Inline commit-path coverage: the two sites that inline the advance --------

def test_error_path_materialize_advances_positive_watermark():
    """The error/cancel materialization path (_materialize_pending_user_turn_before_error)
    inlines the watermark advance. A pending user turn committed on the error
    path advances a stale positive watermark to the recovered timestamp."""
    s = _FakeSession(100.0)  # stale, from a prior retry/undo/edit
    s.pending_user_message = "new turn after edit"
    appended = streaming._materialize_pending_user_turn_before_error(s)
    assert appended is True
    assert s.truncation_watermark is not None
    assert s.truncation_watermark >= 100.0  # advanced past old watermark
    assert any(m.get("content") == "new turn after edit" for m in s.messages)


def test_error_path_materialize_preserves_zero_sentinel():
    """The error-path inline must use the same falsy guard as the helper: the 0.0
    truncate-to-empty sentinel (#2914) is preserved, not cleared."""
    s = _FakeSession(0.0)
    s.pending_user_message = "new turn"
    streaming._materialize_pending_user_turn_before_error(s)
    assert s.truncation_watermark == 0.0


def test_eager_checkpoint_advances_positive_watermark():
    """The eager first-turn checkpoint path (_checkpoint_user_message_for_eager_session_save
    in routes.py) inlines the same advance. A committed user turn advances a
    stale positive watermark to the new message timestamp; the 0.0 sentinel is
    preserved."""
    import api.routes as routes

    s = _FakeSession(100.0)
    routes._checkpoint_user_message_for_eager_session_save(
        s, "eager new turn", None, started_at=200.0
    )
    assert s.truncation_watermark == 200.0
    assert any(m.get("content") == "eager new turn" for m in s.messages)

    s0 = _FakeSession(0.0)
    routes._checkpoint_user_message_for_eager_session_save(
        s0, "eager new turn", None, started_at=200.0
    )
    assert s0.truncation_watermark == 0.0


# --- Advanced watermark filters pre-edit state.db rows -------------------------

def test_advanced_watermark_filters_pre_edit_state_db_rows():
    """With the watermark advanced to the new message timestamp, pre-edit state.db
    rows whose timestamps are below the watermark AND whose content is not in the
    sidecar are still filtered out."""
    sidecar = _rows(
        ("user", "square", 200),
        ("assistant", "360", 201),
    )
    state_db = _rows(
        ("user", "triangle", 100),    # pre-edit, below watermark, not in sidecar
        ("assistant", "180", 101),    # pre-edit assistant, below watermark
        ("user", "square", 200),      # in sidecar
        ("assistant", "360", 201),    # in sidecar
    )
    merged = models.merge_session_messages_append_only(
        sidecar, state_db, truncation_watermark=200.0
    )
    contents = [m["content"] for m in merged]
    assert "triangle" not in contents
    assert "180" not in contents
    assert "square" in contents
    assert "360" in contents
