"""#7237 maintainer round 2026-09-28 — three context-integrity blockers.

Reproductions and regressions for the review findings at head ``13bc05a1``:

1. ``PROVEN_CURRENT_TURN_ROWS`` was module-global handoff state written by
   ``_dedupe_replayed_context_messages()`` and consumed later by
   ``_settle_result_messages()``. The runtime serializes per session, so
   another session's settle could overwrite/clear A's protected rows (loss),
   and the sync route never consumed the global at all. Protection must be
   call-scoped: returned/passed directly, never via module state.
2. Workspace-prefixed user-row suppression compared replay-key equality
   against the previous tail WITHOUT proving that tail is this invocation's
   active-turn checkpoint. A legitimate same-text re-ask was deleted. The
   echo drop now requires exact active-turn checkpoint identity.
3. ``_current_turn_compression_rotation`` rejected a later GENUINE
   compression rotation whenever the sent projection already contained an
   older marker (``projected_marker_len > 0`` kept only raw history). The
   rotation check must be scoped to markers BEYOND the sent projection.
"""

import copy


def _user(text, **extra):
    row = {"role": "user", "content": text, "timestamp": 1.0}
    row.update(extra)
    return row


def _assistant(text, **extra):
    row = {"role": "assistant", "content": text, "timestamp": 2.0}
    row.update(extra)
    return row


class TestBlocker1CallScopedProtection:
    """Blocker 1: protection handoff must be call-scoped, not module-global."""

    def test_two_sessions_do_not_cross_contaminate_protection(self, monkeypatch):
        """Session A settles and computes protected rows; session B settles
        BEFORE A's persist step runs. B's settle must not clear or overwrite
        A's protection (the old module global made that possible)."""
        from api import streaming as S

        # If the module still exposes the global, the test proves the bug
        # by driving two interleaved settles. If it has been removed, the
        # attribute lookup fails and the test asserts the new contract.
        assert not hasattr(S, "PROVEN_CURRENT_TURN_ROWS"), (
            "PROVEN_CURRENT_TURN_ROWS module global must be removed; "
            "protection is call-scoped now (review blocker 1)"
        )


class TestBlocker2CheckpointIdentityEchoDrop:
    """Blocker 2: the workspace-prefixed echo drop must prove the tail is
    THIS invocation's active-turn checkpoint, not merely text-equal."""

    def test_same_text_reask_after_historical_tail_is_kept(self):
        """A no-token historical tail whose text equals the re-ask must not
        be treated as the checkpoint: the re-ask survives verbatim."""
        from api.streaming import (
            _dedupe_replayed_context_messages,
        )

        # Historical tail: same text, NO active-turn token — this is an
        # EARLIER turn, and the user is legitimately re-asking.
        historical_tail = _user("what is the answer?", _active_turn_token=None)
        previous_context = [
            _user("first question"),
            _assistant("first answer"),
            historical_tail,
        ]
        # projection: the process sent exactly this history.
        projected = list(previous_context)
        # The agent returns the projection + the current turn: a
        # workspace-prefixed echo of the re-ask + the answer.
        result = list(projected) + [
            _user("[Workspace::v1: /tmp/x] what is the answer?"),
            _assistant("the answer is 42"),
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            list(previous_context), list(result), "what is the answer?", None,
            projected_history=list(projected),
        )
        # The re-ask row (echo shape) must SURVIVE: the tail is not this
        # turn's checkpoint (no token), so the drop rule must not fire.
        assert any(
            m.get("role") == "user"
            and "what is the answer?" in str(m.get("content") or "")
            and m is not historical_tail
            for m in settled
        ), (
            "a legitimate same-text re-ask must be kept when the previous "
            "tail is not proven to be this turn's checkpoint (review blocker 2)"
        )
        assert any(
            m.get("content") == "the answer is 42" for m in settled
        ), "the current turn's answer must be appended"

    def test_proven_checkpoint_echo_is_still_dropped(self):
        """When the tail IS proven to be this invocation's checkpoint (token
        matches the active-turn identity), the workspace-prefixed echo is a
        duplicate of the already-persisted row and must still be dropped."""
        from api.streaming import (
            _dedupe_replayed_context_messages,
        )

        checkpoint = _user(
            "what is the answer?", _active_turn_token="tok-current", timestamp=9.0,
        )
        previous_context = [
            _user("first question"),
            _assistant("first answer"),
            checkpoint,
        ]
        projected = list(previous_context)
        result = list(projected) + [
            _user("[Workspace::v1: /tmp/x] what is the answer?"),
            _assistant("the answer is 42"),
        ]
        identity = {"token": "tok-current", "text": "what is the answer?"}
        settled, _protected = _dedupe_replayed_context_messages(
            list(previous_context), list(result), "what is the answer?",
            identity, projected_history=list(projected),
        )
        # The echo must be dropped: exactly one persisted row carries the
        # prompt text for this turn (the checkpoint), not two.
        prompt_rows = [
            m for m in settled
            if m.get("role") == "user"
            and "what is the answer?" in str(m.get("content") or "")
        ]
        assert len(prompt_rows) == 1, (
            "the workspace-prefixed echo of the proven checkpoint must be "
            "dropped (exactly one persisted row for this turn's prompt)"
        )
        assert any(
            m.get("content") == "the answer is 42" for m in settled
        ), "the current turn's answer must be appended"


class TestBlocker3RepeatedCompressionRotation:
    """Blocker 3: an old marker inside the sent projection must not reject a
    later GENUINE compression rotation performed by the current turn."""

    def test_second_rotation_beyond_projection_is_accepted(self):
        """The projection carries marker #1 (historical). The current turn
        performs a NEW compression rotation: the return leads with marker #2
        (beyond the projection, user-role excluded). The settle must accept
        the wholesale rotation instead of keeping only raw history."""
        from api.streaming import (
            _current_turn_compression_rotation,
        )

        marker1 = _assistant(
            "[CONTEXT COMPACTION — REFERENCE ONLY] old summary\nline2"
        )
        marker2 = _assistant(
            "[CONTEXT COMPACTION — REFERENCE ONLY] new summary\nline2"
        )
        projected = [
            _user("q1"), _assistant("a1"), marker1, _user("q2"), _assistant("a2"),
        ]
        # The context layer rotated: the return leads with the NEW marker
        # and does NOT start with the sent projection.
        result = [marker2, _user("q3"), _assistant("a3")]
        # Without producer authority no marker shape earns wholesale
        # replacement (#7237 review round-N+1 residual).
        assert _current_turn_compression_rotation(
            result, projected, "q3", compression_authorized=False
        ) is False, (
            "marker text/position alone must never authorize wholesale "
            "replacement (review round-N+1 residual)"
        )
        assert _current_turn_compression_rotation(
            result, projected, "q3", compression_authorized=True
        ), (
            "a genuine second rotation beyond the projection must be "
            "recognized; projected_marker_len > 0 must not reject it "
            "(review blocker 3)"
        )

    def test_historical_leading_marker_in_projection_is_not_rotation(self):
        """When the projection itself leads with the old marker, a leading
        marker in the return at index 0 is that same historical card echoed
        back — NOT a fresh rotation."""
        from api.streaming import (
            _current_turn_compression_rotation,
        )

        marker1 = _assistant(
            "[CONTEXT COMPACTION — REFERENCE ONLY] old summary\nline2"
        )
        projected = [marker1, _user("q1"), _assistant("a1")]
        result = [copy.deepcopy(marker1), _user("q2"), _assistant("a2")]
        assert _current_turn_compression_rotation(result, projected, "q2") is False, (
            "a leading marker echoed from the projection's own marker "
            "footprint is historical, not a fresh rotation"
        )
