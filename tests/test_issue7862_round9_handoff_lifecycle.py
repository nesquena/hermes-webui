"""#7862 review round 9 — the coordinated admission/recovery design.

Round 8 closed the two CORE items it could close with point fixes and the
reviewer said the rest needed "one durable record per attempt, created
atomically with the removal, discharged only by launch/rejection/abandonment,
and honoured by retirement and adoption". This file is that design, applied to
the handoff lifecycle.

A **handoff** is the durable evidence that one start attempt consumed an
intent. Its lifecycle is now:

    consume ──> handoff(sid, attempt)      [same os.replace as the removal]
                    │
       ┌────────────┼─────────────────────┬──────────────────────┐
       ▼            ▼                     ▼                      ▼
   launch ok    launch rejected      /goal clear            process lost
   discharge    restore the record   retire drops BOTH      cold restore
   (gone)       + drop the handoff   record and handoff     adopts, unless
                                                             retired since

Four findings were live at the head reviewed here, and every one is a case where
a handoff outlived the state that made it meaningful:

1. **[CORE] A cleared goal comes back after the next restart.** ``retire``
   dropped the session's rollback receipts but not its handoffs, and adoption
   compared tombstones against a record generation it had just re-derived at
   adoption time — so the tombstone never matched. ``/goal clear`` followed by a
   restart resurrected the continuation.

2. **[CORE] Successful retries leave claimable handoffs.** A rejected-start
   rollback restored the intent as a RECORD but kept the handoff for the attempt
   that consumed it. A successful retry followed by a restart then restored the
   continuation a second time.

3. **[CORE] A failed discharge is ignored after launch.** ``discard_..._handoff``
   returned ``None`` whether or not the snapshot committed, so an admitted
   attempt whose discharge write failed became claimable intent again.

4. **[CORE] The 64-entry cap evicts the only durable owner.** The cap popped
   unconditionally FIFO, so the one outstanding handoff a crash would need could
   be evicted by unrelated traffic.

Each test drives the real store against a real temp state dir, and the crash
paths are exercised by reloading the registry from disk in a fresh module state
rather than by mocking the loader.
"""

from __future__ import annotations

import json
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)


def _store():
    import api.goal_continuation_store as store

    return store


@pytest.fixture()
def clean_registry():
    """Isolate the registry between cases, INCLUDING the handoff tokens.

    Inherited from the round-8 fixture: the earlier ones never cleared
    ``_CONTINUATION_HANDOFF_TOKENS``, so a leaked token survived into every later
    snapshot in the same process and was masked during adoption by the in-memory
    presence skip.
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._LAST_LOAD_ERROR = None
        store._LAST_WRITE_ERROR = None
        store._RETIRED_LOG.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._GENERATION = 0
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    for tmp in store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"):
        tmp.unlink(missing_ok=True)
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)


def _cold_reload(store):
    """Simulate a process restart: wipe live state, then restore from disk.

    This is the real crash path — the module globals are what a fresh process
    would start with, and ``restore_goal_continuations`` is the real startup
    entry point.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._GENERATION = 0
    return store.restore_goal_continuations()


def _handoffs_on_disk(store):
    """Read the handoff section straight out of the durable payload."""
    try:
        raw = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw.get("continuation_handoffs") or {}


# ── finding 1: /goal clear must survive a restart ───────────────────────────


def test_a_cleared_goal_does_not_come_back_after_a_restart(clean_registry):
    """Restart → launch → /goal clear → restart must NOT restore the intent.

    The exact sequence in the finding: launch discharges only its own attempt
    id, retire dropped receipts but not handoffs, and adoption ignored
    tombstones. The restored continuation then consumed the retry text as a
    goal continuation even though the user had stopped the goal.
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r9-clear-restart"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    outcome = store.consume_pending_goal_continuation(sid, "Continue the standing goal.")
    assert outcome == store.CONSUME_COMMITTED
    assert (sid, "attempt") in store._CONTINUATION_HANDOFF_TOKENS or any(
        k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS
    ), "the consume left no handoff, so this scenario cannot be exercised"

    # The user clears the goal.
    assert store.retire_pending_goal_continuation(sid, reason="cleared") is True
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "retire left the session's handoff behind in memory"
    )
    assert _handoffs_on_disk(store) == {}, (
        "retire left the session's handoff behind on disk"
    )

    # Restart. The cleared continuation must not be restored.
    restored = _cold_reload(store)
    assert sid not in PENDING_GOAL_CONTINUATION_RECORDS, (
        "a cleared goal was restored after a restart"
    )
    assert sid not in PENDING_GOAL_CONTINUATION, "a cleared goal's marker was restored"
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "a cleared goal's handoff was adopted after a restart"
    )
    assert restored == 0, f"cold restore reported {restored} sessions for a cleared goal"


def test_a_handoff_written_before_a_clear_is_not_adopted(clean_registry):
    """The tombstone must beat the handoff even when the handoff is newer on disk.

    Adoption used to compare the tombstone against the RECORD's generation, and
    a handoff-restored record derives that generation at adoption time — so the
    comparison was against a number the adoption itself had just produced. This
    writes the handoff, clears, reloads from disk, and asserts the handoff is
    still refused.
    """
    store = _store()
    sid = "sess-r9-handoff-then-clear"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert (
        store.consume_pending_goal_continuation(sid, "Continue the standing goal.")
        == store.CONSUME_COMMITTED
    )
    handoff_generation = next(
        int(rec.get("generation") or 0)
        for (h_sid, _attempt), rec in store._CONTINUATION_HANDOFF_TOKENS.items()
        if h_sid == sid
    )
    store.retire_pending_goal_continuation(sid, reason="cleared")

    restored = _cold_reload(store)
    assert restored == 0
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "a handoff older than the tombstone was adopted"
    )
    # Sanity: the handoff really did carry the pre-clear generation.
    assert handoff_generation > 0, "the handoff carried no generation to compare"


def test_a_retire_before_any_handoff_is_still_a_clean_clear(clean_registry):
    """Negative control: a plain clear with no in-flight start."""
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION

    sid = "sess-r9-plain-clear"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert store.retire_pending_goal_continuation(sid, reason="cleared") is True
    assert _cold_reload(store) == 0
    assert sid not in PENDING_GOAL_CONTINUATION


# ── finding 2: a successful retry must not be restorable twice ──────────────


def test_a_successful_retry_is_not_restored_again_after_a_restart(clean_registry):
    """Rejected start → rollback restores the record → retry consumes → restart.

    The rollback used to keep the handoff for the attempt that consumed the
    intent, so a successful retry followed by a restart restored the
    continuation a SECOND time — the goal loop ran the retry text twice.
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r9-retry-twice"
    prompt = "Continue the standing goal."
    store.arm_pending_goal_continuation(sid, prompt)
    assert store.consume_pending_goal_continuation(sid, prompt, attempt_id="a1") == (
        store.CONSUME_COMMITTED
    )

    # The start is rejected (409 / registration failure) → rollback.
    receipt = store.pop_goal_continuation_rollback_receipt(sid, "a1")
    assert receipt is not None, "the consume left no rollback receipt"
    assert store.restore_pending_goal_continuation(sid, dict(receipt)) is True
    assert sid in PENDING_GOAL_CONTINUATION_RECORDS, "the rollback restored no record"
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "the rollback left the consumed attempt's handoff behind"
    )

    # The retry succeeds and consumes the restored intent.
    assert store.consume_pending_goal_continuation(sid, prompt, attempt_id="a2") == (
        store.CONSUME_COMMITTED
    )
    # The retry's launch SUCCEEDED, so its own handoff is discharged — exactly
    # what the worker-start path does.
    assert store.discard_goal_continuation_handoff(sid, "a2") is True
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "the retry's handoff survived a successful launch"
    )
    # Restart. The continuation must not come back a second time.
    assert _cold_reload(store) == 0, (
        "a continuation that was already consumed by the retry was restored again"
    )
    assert sid not in PENDING_GOAL_CONTINUATION


def test_a_rollback_that_refuses_on_a_tombstone_leaves_no_handoff(clean_registry):
    """A rollback refused because the goal was cleared must not leave a handoff."""
    store = _store()
    sid = "sess-r9-rollback-refused"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert (
        store.consume_pending_goal_continuation(sid, "Continue the standing goal.")
        == store.CONSUME_COMMITTED
    )
    receipt = store.pop_goal_continuation_rollback_receipt(sid, "attempt")
    assert receipt is not None
    store.retire_pending_goal_continuation(sid, reason="cleared")
    # The receipt describes an intent that no longer exists.
    assert store.restore_pending_goal_continuation(sid, dict(receipt)) is False
    assert _cold_reload(store) == 0
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS)


# ── finding 3: a failed discharge must be visible ───────────────────────────


def test_a_failed_discharge_is_reported(clean_registry, monkeypatch):
    """A launch that succeeded must be able to see that the discharge failed."""
    store = _store()
    sid = "sess-r9-discharge-fail"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert (
        store.consume_pending_goal_continuation(sid, "Continue the standing goal.")
        == store.CONSUME_COMMITTED
    )

    monkeypatch.setattr(store, "_write_registry_unlocked", lambda *a, **k: False)
    committed = store.discard_goal_continuation_handoff(sid, "attempt")

    assert committed is False, (
        "a failed discharge reported success, so an admitted attempt became "
        "claimable intent again with nobody able to tell"
    )


def test_a_failed_discharge_keeps_the_token_for_reconciliation(clean_registry, monkeypatch):
    """The in-memory token must survive a failed write so the state is honest.

    Dropping it would leave this process claiming a discharge that never became
    durable, and the next startup would find a handoff this process believes is
    gone.
    """
    store = _store()
    sid = "sess-r9-discharge-token"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert (
        store.consume_pending_goal_continuation(sid, "Continue the standing goal.")
        == store.CONSUME_COMMITTED
    )

    monkeypatch.setattr(store, "_write_registry_unlocked", lambda *a, **k: False)
    assert store.discard_goal_continuation_handoff(sid, "attempt") is False
    assert any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "the token was dropped despite the write failing, so this process now "
        "disagrees with the durable state"
    )


def test_a_successful_discharge_removes_the_token(clean_registry):
    """Negative control: the happy path really does discharge."""
    store = _store()
    sid = "sess-r9-discharge-ok"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert (
        store.consume_pending_goal_continuation(sid, "Continue the standing goal.")
        == store.CONSUME_COMMITTED
    )
    assert store.discard_goal_continuation_handoff(sid, "attempt") is True
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS)
    assert _handoffs_on_disk(store) == {}


def test_a_discharge_with_no_token_is_a_no_op_success(clean_registry):
    """Negative control: nothing to discharge is not a failure."""
    store = _store()
    assert store.discard_goal_continuation_handoff("never-seen", "attempt") is True


# ── finding 4: the cap must not evict an outstanding handoff ────────────────


def test_the_cap_does_not_evict_an_outstanding_handoff(clean_registry):
    """65 unfinalized consumes must not drop the first one's durable owner.

    The cap popped unconditionally FIFO, so unrelated in-flight traffic could
    evict the one handoff a crash would need. Entries whose session is provably
    finished (live record or retirement tombstone) are evicted first; when every
    entry is still outstanding the registry is allowed to grow.
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    # 70 outstanding consumes, none discharged.
    for i in range(70):
        sid = f"sess-r9-cap-{i}"
        store.arm_pending_goal_continuation(sid, f"Continue goal {i}.")
        assert (
            store.consume_pending_goal_continuation(sid, f"Continue goal {i}.")
            == store.CONSUME_COMMITTED
        )
    assert len(store._CONTINUATION_HANDOFF_TOKENS) == 70, (
        "outstanding handoffs were evicted: a crash could no longer restore them"
    )
    # The very first one — the one the old FIFO pop would have dropped — is
    # still there.
    assert any(k[0] == "sess-r9-cap-0" for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "the first outstanding handoff was evicted by unrelated traffic"
    )
    # Nothing was retired, so there is nothing provably spent to evict.
    assert not any(k[0] in PENDING_GOAL_CONTINUATION_RECORDS for k in store._CONTINUATION_HANDOFF_TOKENS)


def test_the_cap_does_evict_a_spent_handoff(clean_registry):
    """Negative control: a retired session's handoff is provably spent."""
    store = _store()
    retired = []
    for i in range(70):
        sid = f"sess-r9-cap2-{i}"
        store.arm_pending_goal_continuation(sid, f"Continue goal {i}.")
        assert (
            store.consume_pending_goal_continuation(sid, f"Continue goal {i}.")
            == store.CONSUME_COMMITTED
        )
        if i % 2 == 0:
            store.retire_pending_goal_continuation(sid, reason="cleared")
            retired.append(sid)
    # Every retired session's handoff is gone; the outstanding ones survive.
    for sid in retired:
        assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
            f"a retired session's handoff survived: {sid}"
        )
    outstanding = [k[0] for k in store._CONTINUATION_HANDOFF_TOKENS]
    assert outstanding, "no outstanding handoffs survived"
    assert len(outstanding) <= 70, "the registry grew without bound"


# ── the lifecycle as a whole ────────────────────────────────────────────────


def test_the_handoff_lifecycle_is_consistent_end_to_end(clean_registry):
    """One session through arm → consume → reject → retry → clear → restart.

    Every transition must leave the durable state describing exactly one of the
    legal states: "record present, no handoff" or "record absent, handoff
    present". Never both, never neither (for a session that had an intent).
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r9-lifecycle"
    prompt = "Continue the standing goal."

    def _legal(label):
        has_record = sid in PENDING_GOAL_CONTINUATION_RECORDS
        has_handoff = any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS)
        # Legal states: exactly one of (record, handoff) — the intent is either
        # pending, or spent by an attempt that has not launched yet — OR
        # neither, once the attempt launched (discharge) or the intent was
        # cleared. Both at once is never legal: that is the double-owner state
        # that let a continuation be restored twice.
        assert not (has_record and has_handoff), (
            f"illegal state after {label}: both a record and a handoff are live"
        )

    store.arm_pending_goal_continuation(sid, prompt)
    assert sid in PENDING_GOAL_CONTINUATION_RECORDS, "arm left no record"
    _legal("arm")

    assert store.consume_pending_goal_continuation(sid, prompt, attempt_id="a1") == (
        store.CONSUME_COMMITTED
    )
    assert any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "consume left no handoff"
    )
    _legal("consume")

    receipt = store.pop_goal_continuation_rollback_receipt(sid, "a1")
    assert receipt is not None
    assert store.restore_pending_goal_continuation(sid, dict(receipt)) is True
    _legal("rollback")

    assert store.consume_pending_goal_continuation(sid, prompt, attempt_id="a2") == (
        store.CONSUME_COMMITTED
    )
    _legal("retry consume")

    assert store.discard_goal_continuation_handoff(sid, "a2") is True
    _legal("discharge")

    assert store.retire_pending_goal_continuation(sid, reason="cleared") is True
    _legal("clear")

    assert _cold_reload(store) == 0
    assert sid not in PENDING_GOAL_CONTINUATION
    assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS)
