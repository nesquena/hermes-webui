"""Regression tests for the self-locked truncation_watermark data loss.

Repro (real session 20260929_085828_15c6e6, "Improve image generation
quality via apikey fan"): after a Feishu-handoff turn committed without a
timestamped user row, the advance helper's fallback stamped
``truncation_watermark = time.time()`` -- a WALL-CLOCK value newer than every
row in the sidecar. From then on::

    max_sidecar_timestamp (02:28) > watermark (05:53)   -> False
    watermark_advanced_by_boundary                    -> False
    => sidecar_advanced_past_watermark                -> False

so ``_state_row_is_truncated`` skipped every state.db row newer than the
watermark, and the "replaced tail" filter skipped every unseen row below it.
The rows needed to advance the sidecar past the watermark are exactly the rows
the watermark hides, so the transcript could never recover: 3412 real messages
(including all user turns after the freeze) were silently dropped while the
session kept running.

Two fixes, both covered here:

1. The advance helper only ever advances to a REAL message timestamp
   (``max`` of real rows) instead of inventing a wall-clock boundary. Same
   change at the inlined eager-checkpoint site in routes.py.
2. The merge detects a stale wall-clock watermark (newer than every sidecar
   row, which a legitimate truncate cutoff can never be) and ignores it,
   failing OPEN toward data.

The #2914 zero sentinel, the legit edit-watermark replaced-tail suppression,
and the same-second replaced-user guard must all still hold.
"""
import api.models as models
import api.routes as routes
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
        self.active_stream_id = None
        self.session_id = "test-session"


# --- Fix 1: the advance helper must never invent a wall-clock boundary -------

def test_advance_helper_clamps_to_newest_real_timestamp_when_user_row_untimestamped():
    """No timestamped user row for the new turn -> fall through to the
    newest-real-timestamp clamp.

    Wall-clock time here produced a watermark newer than every sidecar row,
    which permanently starved sidecar_advanced_past_watermark and self-locked
    the append-only merge."""
    s = _FakeSession(100.0)
    # A committed turn that carries no timestamp (handoff/background-notification
    # shape), alongside older rows that DO have timestamps.
    s.messages = _rows(
        ("user", "older", 200),
        ("assistant", "reply", 250),
        ("user", "untimestamped new turn", None),
    )
    streaming._advance_truncation_watermark_after_commit(s)
    # The newest *timestamped user* row still wins (250 is an assistant row, so
    # the backward scan lands on the user turn at 200).
    assert s.truncation_watermark == 200.0
    # The invariant that makes the merge able to advance again:
    # the watermark can never exceed the newest real message.
    newest_real = max(m["timestamp"] for m in s.messages if m.get("timestamp"))
    assert s.truncation_watermark <= newest_real


def test_advance_helper_clamps_to_newest_real_ts_when_no_user_row_at_all():
    """With no timestamped user row, the helper clamps to the newest REAL
    message (any role) rather than inventing a wall-clock boundary."""
    s = _FakeSession(100.0)
    s.messages = _rows(
        ("assistant", "older reply", 200),
        ("assistant", "newest reply", 250),
    )
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark == 250.0
    newest_real = max(m["timestamp"] for m in s.messages if m.get("timestamp"))
    assert s.truncation_watermark <= newest_real


def test_advance_helper_ignores_zero_and_non_numeric_timestamps():
    """A 0 / non-numeric timestamp must not become the watermark boundary."""
    s = _FakeSession(100.0)
    s.messages = _rows(
        ("user", "a", 200),
        ("assistant", "b", 0),
        ("assistant", "c", "not-a-number"),
    )
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark == 200.0


def test_advance_helper_keeps_existing_value_when_no_timestamped_row():
    """With no timestamped row at all, keep the stale value rather than
    inventing one: a too-old watermark only over-filters, it cannot self-lock."""
    s = _FakeSession(100.0)
    s.messages = [{"role": "user", "content": "no timestamps anywhere"}]
    streaming._advance_truncation_watermark_after_commit(s)
    assert s.truncation_watermark == 100.0


def test_eager_checkpoint_never_stamps_wall_clock_watermark():
    """The inlined eager-checkpoint site must obey the same invariant."""
    s = _FakeSession(100.0)
    s.messages = _rows(("user", "older", 200), ("assistant", "reply", 250))
    # started_at=None -> user_msg gets no timestamp.
    routes._checkpoint_user_message_for_eager_session_save(
        s, "eager turn without started_at", None, started_at=None
    )
    assert s.truncation_watermark == 250.0
    newest_real = max(
        m["timestamp"] for m in s.messages if isinstance(m.get("timestamp"), (int, float))
    )
    assert s.truncation_watermark <= newest_real


def test_eager_checkpoint_still_uses_started_at_when_present():
    """The normal path is unchanged: started_at is the committed turn's ts."""
    s = _FakeSession(100.0)
    routes._checkpoint_user_message_for_eager_session_save(
        s, "eager new turn", None, started_at=300.0
    )
    assert s.truncation_watermark == 300.0


# --- Fix 2: the merge self-heals a stale wall-clock watermark ---------------

def test_stale_watermark_newer_than_sidecar_is_ignored():
    """A watermark newer than EVERY sidecar row is not a real truncate cutoff.

    It is ignored, so the post-watermark state.db rows merge back instead of
    being silently dropped."""
    sidecar = _rows(("user", "q1", 50), ("assistant", "a1", 100))
    state = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),
        ("user", "q2", 200),
        ("assistant", "a2", 250),
    )
    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=9999.0, truncation_boundary=9999.0
    )
    assert [m["content"] for m in merged] == ["q1", "a1", "q2", "a2"]


def test_legitimate_edit_watermark_still_filters_replaced_tail():
    """A genuine edit watermark (<= max sidecar ts) must keep suppressing the
    replaced suffix -- the self-heal must not weaken edit/retry/undo."""
    sidecar = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),
        ("user", "q2-new", 150),
    )
    state = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),
        ("user", "replaced-q2", 150),
        ("assistant", "replaced-a2", 160),
    )
    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=150.0, truncation_boundary=100.0
    )
    assert [m["content"] for m in merged] == ["q1", "a1", "q2-new"]


def test_advanced_watermark_past_boundary_still_filters_pre_edit_rows():
    """#3831's advanced-watermark behaviour is unchanged: pre-edit state.db
    rows below the watermark and absent from the sidecar are still dropped."""
    sidecar = _rows(("user", "square", 200), ("assistant", "360", 201))
    state = _rows(
        ("user", "triangle", 100),
        ("assistant", "180", 101),
        ("user", "square", 200),
        ("assistant", "360", 201),
    )
    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=200.0
    )
    contents = [m["content"] for m in merged]
    assert "triangle" not in contents
    assert "180" not in contents
    assert "square" in contents
    assert "360" in contents


def test_same_second_replaced_user_still_skipped_at_legit_watermark():
    """The same-second user guard still drops a replaced user row that shares
    the watermark second, while keeping the edited version in the sidecar."""
    sidecar = _rows(("user", "q1", 50), ("user", "edited", 200))
    state = _rows(
        ("user", "q1", 50),
        ("user", "old-same-second", 200),
        ("assistant", "reply", 201),
    )
    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=200.0, truncation_boundary=50.0
    )
    assert [m["content"] for m in merged] == ["q1", "edited"]


# --- The #2914 truncate-to-empty sentinel must be untouched -----------------

def test_zero_sentinel_still_blocks_all_replay():
    """0.0 is the truncate-to-empty sentinel: with an EMPTY sidecar it must keep
    blocking all replay."""
    merged = models.merge_session_messages_append_only(
        [], _rows(("user", "p", 1.0), ("assistant", "a", 2.0)),
        truncation_watermark=0.0,
    )
    assert merged == []


def test_zero_sentinel_still_blocks_state_rows_older_than_it():
    """The sentinel also blocks state.db rows below it (ts <= 0 never happens,
    but the below-watermark stale filter must not be bypassed for 0.0)."""
    sidecar = _rows(("user", "old", 10.0))
    state = _rows(("user", "p", 100.0), ("assistant", "a", 200.0))
    merged = models.merge_session_messages_append_only(
        sidecar, state, truncation_watermark=0.0
    )
    # Pre-existing behaviour, unchanged by this fix: a non-empty sidecar merges
    # state.db rows normally; the sentinel governs the EMPTY-sidecar replay
    # gate above. Asserted here to pin the boundary so a future change cannot
    # silently widen the sentinel's reach.
    assert [m["content"] for m in merged] == ["old", "p", "a"]


# --- The real-world signature, end to end ----------------------------------

def test_frozen_session_signature_recovers_all_later_turns():
    """The exact repro shape: a sidecar frozen at 02:28 whose watermark is a
    wall-clock 05:53 value. Every later turn must merge back."""
    sidecar = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),
        ("assistant", "last before freeze", 900),
    )
    state = _rows(
        ("user", "q1", 50),
        ("assistant", "a1", 100),
        ("assistant", "last before freeze", 900),
        ("user", "q2 after freeze", 1000),
        ("assistant", "a2 after freeze", 1100),
        ("user", "q3 much later", 5000),
        ("assistant", "a3 much later", 5100),
    )
    merged = models.merge_session_messages_append_only(
        sidecar, state,
        truncation_watermark=2500.0,   # wall-clock: > every sidecar ts
        truncation_boundary=2500.0,
    )
    assert [m["content"] for m in merged] == [
        "q1", "a1", "last before freeze",
        "q2 after freeze", "a2 after freeze",
        "q3 much later", "a3 much later",
    ]
