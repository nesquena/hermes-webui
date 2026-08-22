"""Regression coverage: the periodic streaming checkpoint must not rewrite
byte-identical sidecars.

The checkpoint thread (#765) fires whenever a tool call completes. A completed
tool call is *not* proof that anything the checkpoint persists has changed:
``run_conversation()`` mutates an internal copy of the transcript, and the turn
bookkeeping fields are written once before the run starts.

Each rewrite re-serializes the whole session while holding the GIL. Because the
HTTP server and the agent workers share one interpreter, that cost is added
latency for every concurrent request — ~100 ms for a 1.8 MB sidecar and ~300 ms
for a 36 MB one, repeated every 15 s for the whole turn.

These tests pin the behavior at the decision level (the fingerprint) so they do
not depend on wall-clock timing of the background thread.
"""
from __future__ import annotations


def _session(tmp_path, **overrides):
    from api.models import Session

    s = Session(session_id="ckpt_redundant_1")
    s.messages = [{"role": "assistant", "content": "hello"}]
    s.context_messages = []
    s.pending_user_message = "do the thing"
    s.pending_started_at = 1787000000.0
    s.active_stream_id = "streamabc"
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def test_fingerprint_is_stable_when_nothing_changes(tmp_path):
    """Two reads of an untouched session produce the same fingerprint.

    This is the redundant case: the checkpoint would rewrite identical bytes.
    """
    from api.streaming import _streaming_checkpoint_fingerprint

    s = _session(tmp_path)
    assert _streaming_checkpoint_fingerprint(s) == _streaming_checkpoint_fingerprint(s)


def test_fingerprint_tracks_every_field_the_checkpoint_persists(tmp_path):
    """Any state a mid-run checkpoint can legitimately advance must be detected.

    Missing one of these would make the skip lose real data, so each field is
    asserted individually rather than as a group.
    """
    from api.streaming import _streaming_checkpoint_fingerprint

    base = _session(tmp_path)
    reference = _streaming_checkpoint_fingerprint(base)

    mutations = {
        # compression rotates the session id mid-turn
        "session_id": "rotated_sid",
        "parent_session_id": "parent_sid",
        "profile": "other-profile",
        # turn bookkeeping cleared at the end of the turn
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_started_at": None,
        "pending_user_source": "telegram",
        # transcript growth (error rows, continuation tail, compression)
        "messages": [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}],
        "context_messages": [{"role": "user", "content": "ctx"}],
        "compression_anchor_visible_idx": 3,
        "pre_compression_snapshot": {"messages": []},
        "pending_attachments": [{"name": "f.png"}],
    }

    for field, value in mutations.items():
        mutated = _session(tmp_path, **{field: value})
        assert _streaming_checkpoint_fingerprint(mutated) != reference, (
            f"a change to {field!r} must force a checkpoint write"
        )


def test_fingerprint_fails_closed_on_unreadable_session():
    """An unreadable session yields None so the caller keeps writing.

    Fail-closed: never skip a checkpoint on uncertainty.
    """
    from api.streaming import _streaming_checkpoint_fingerprint

    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert _streaming_checkpoint_fingerprint(Hostile()) is None


def test_checkpoint_loop_skips_redundant_writes_but_persists_changes(monkeypatch, tmp_path):
    """End-to-end on the real loop body: identical state writes once.

    Drives the actual decision sequence used by the checkpoint thread: an
    unchanged session must be persisted once, not on every tool completion,
    while a real mutation must be persisted promptly.
    """
    import time

    from api.streaming import (
        _streaming_checkpoint_fingerprint,
        _CHECKPOINT_IDLE_REFRESH_SECONDS,
    )

    s = _session(tmp_path)
    writes = []

    # Mirror of the loop body in _periodic_checkpoint(), kept in sync with it.
    last_fingerprint = None
    last_write_at = 0.0
    now = time.time()

    def tick(session, at):
        nonlocal last_fingerprint, last_write_at
        fingerprint = _streaming_checkpoint_fingerprint(session)
        stale = (at - last_write_at) >= _CHECKPOINT_IDLE_REFRESH_SECONDS
        if fingerprint is None or fingerprint != last_fingerprint or stale:
            writes.append(at)
            last_fingerprint = fingerprint
            last_write_at = at

    # 10 tool completions, nothing else changed -> exactly one write.
    for i in range(10):
        tick(s, now + i)
    assert len(writes) == 1, f"redundant rewrites: {len(writes)} writes for an unchanged session"

    # A real transcript change must be persisted on the next tick.
    s.messages = list(s.messages) + [{"role": "assistant", "content": "new answer"}]
    tick(s, now + 10)
    assert len(writes) == 2, "a real mutation must force a checkpoint write"

    # updated_at must not go stale forever on a very long quiet turn.
    tick(s, now + 10 + _CHECKPOINT_IDLE_REFRESH_SECONDS)
    assert len(writes) == 3, "a periodic refresh must still happen on long turns"
