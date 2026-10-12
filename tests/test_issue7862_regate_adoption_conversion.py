"""#7862 re-gate CORE: the crash-adoption must CONVERT the handoff, not copy it.

The re-gate's finding, quoted:

> **Goal continuation restored twice.** 1. arm → consume → crash before the
> pending-start write. 2. Restart: the handoff is adopted into a live record.
> 3. A retry consumes the goal and discharges its own attempt's handoff. 4.
> Restart again: the handoff from step 1 — still on disk, because adoption did
> not remove it — is adopted again, restoring a continuation that was already
> spent in step 3. Verified in Chromium and over real HTTP: master ends with the
> continuation live and its record present; this branch ends with two admissions
> and a retired goal's record back in the registry.

The round-7/8/9 tests never caught it because each one looks at a single cold
restart. This file drives TWO.

The fix makes adoption a conversion: the handoff token is popped BEFORE
``_write_registry_unlocked``, so the single ``os.replace`` that publishes the
restored record also publishes the emptied handoff table. A crash in between
therefore leaves the pre-adoption state (handoff, no record — re-adoptable)
rather than the double-spendable state (both).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "api" / "goal_continuation_store.py").exists(),
    reason="goal continuation store unavailable",
)


def _store():
    from api import goal_continuation_store as store

    return store


def _handoffs_on_disk(store) -> dict:
    import json

    if not store._PENDING_GOAL_FILE.exists():
        return {}
    try:
        payload = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload.get("continuation_handoffs") or {}


def _cold_reload(store) -> int:
    """Drop every in-memory trace, then reload from disk exactly as a restart."""
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._TOMBSTONES.clear()
        store._RETIRED_GENERATIONS.clear()
        store._GENERATION = 0
    return store.restore_goal_continuations()


@pytest.fixture
def clean_registry():
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
    yield store
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()


def test_adoption_converts_the_handoff_instead_of_copying_it(clean_registry):
    """The exact re-gate sequence, with TWO cold restarts.

    Pre-fix: the second cold restore adopts the handoff that step 1 left on disk,
    even though step 3 already spent that continuation — so the goal loop restarts
    after the goal was retired.
    """
    store = clean_registry
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-7862-double-restore"
    goal = "Continue the standing goal."

    # Step 1: arm, consume, crash before the pending-start write.
    assert store.arm_pending_goal_continuation(sid, goal) is True
    assert store.consume_pending_goal_continuation(sid, goal) == store.CONSUME_COMMITTED
    assert any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "the consume left no handoff, so this scenario cannot be exercised"
    )
    assert _handoffs_on_disk(store), (
        "the handoff is not durable, so the crash scenario cannot be exercised"
    )

    # Step 2: restart — the handoff is adopted into a live record.
    restored = _cold_reload(store)
    assert restored == 1, f"expected the handoff to be adopted once, got {restored}"
    assert sid in PENDING_GOAL_CONTINUATION_RECORDS
    # The adoption must have consumed the DURABLE handoff. The in-memory token is
    # deliberately kept — ``_receipt_attempt_is_live`` anchors a rollback receipt to
    # its clock, and dropping it makes a just-adopted attempt read as abandoned so
    # the next sweep drops the receipt and the retry runs as an ordinary turn
    # (round 10, ``test_the_handoff_clock_survives_a_restart``). What must be gone
    # is the copy a cold process would re-adopt.
    converted = [
        k for k in store._CONTINUATION_HANDOFF_TOKENS
        if k[0] == sid and store._CONTINUATION_HANDOFF_TOKENS[k].get(
            "_handoff_adopted_into_record"
        )
    ]
    assert converted, (
        "adoption dropped the in-memory handoff entirely; "
        "_receipt_attempt_is_live() anchors a rollback receipt to that token's "
        "clock, so a just-adopted attempt now reads as abandoned and the next "
        "sweep drops the receipt (the round-10 regression this branch already "
        "fixed)"
    )
    unconverted = [
        k for k in store._CONTINUATION_HANDOFF_TOKENS
        if k[0] == sid and not store._CONTINUATION_HANDOFF_TOKENS[k].get(
            "_handoff_adopted_into_record"
        )
    ]
    assert unconverted == [], (
        f"adoption left an unconverted handoff in memory: {unconverted}"
    )
    assert not any(k.startswith(sid + "\x00") for k in _handoffs_on_disk(store)), (
        "adoption left the handoff on disk — a later restart re-adopts a spent "
        "continuation (#7862 re-gate CORE)"
    )
    assert sid in store._ADOPTED_HANDOFF_SESSIONS, (
        "the conversion marker is not set, so the next snapshot write republishes "
        "the handoff to disk"
    )

    # Step 3: the retry consumes the goal, then the user clears it.
    assert (
        store.consume_pending_goal_continuation(sid, goal)
        == store.CONSUME_COMMITTED
    )
    assert store.retire_pending_goal_continuation(sid, reason="cleared") is True

    # Step 4: restart AGAIN. Nothing may come back.
    restored = _cold_reload(store)
    assert restored == 0, (
        f"a cleared continuation was restored after the second restart: {restored}"
    )
    assert sid not in PENDING_GOAL_CONTINUATION_RECORDS, (
        "a retired goal's record is back in the registry after two restarts"
    )
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS), (
        "a cleared goal's handoff was adopted after the second restart"
    )


def test_a_successful_retry_before_the_second_restart_is_not_resurrected(clean_registry):
    """Counterpart: adoption of a LIVE goal is still real and still reconciles.

    Narrowing the fix must not turn adoption into a no-op — the whole point of the
    durable handoff is that a crash between consume and the pending-start write is
    still reconcilable.
    """
    store = clean_registry
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-7862-adoption-still-works"
    goal = "Finish the migration."

    store.arm_pending_goal_continuation(sid, goal)
    assert store.consume_pending_goal_continuation(sid, goal) == store.CONSUME_COMMITTED

    assert _cold_reload(store) == 1, "adoption stopped working"
    assert sid in PENDING_GOAL_CONTINUATION_RECORDS
    assert not any(k.startswith(sid + "\x00") for k in _handoffs_on_disk(store)), (
        "adoption left the durable handoff behind"
    )
    # And the adopted intent is genuinely consumable by the retry.
    assert (
        store.consume_pending_goal_continuation(sid, goal)
        == store.CONSUME_COMMITTED
    )


def test_a_failed_snapshot_write_puts_the_handoff_token_back(clean_registry, monkeypatch):
    """If the publishing write fails, the adoption must not silently vanish.

    Popping the token before the write is what makes the swap atomic, but it also
    means a failed write has destroyed the in-memory evidence. The tokens are put
    back and the failure is reported, so the adoption stays visible and the next
    reconciliation can retry it.
    """
    store = clean_registry
    sid = "sess-7862-adoption-write-fails"
    goal = "Continue the standing goal."

    store.arm_pending_goal_continuation(sid, goal)
    assert store.consume_pending_goal_continuation(sid, goal) == store.CONSUME_COMMITTED

    # A real write failure, not a stub: make the tmp write land somewhere
    # unwritable so the production error path records _LAST_WRITE_ERROR exactly as
    # it would for a genuine disk problem. A bare `lambda: False` would leave that
    # diagnostic empty and make the observability half of this test vacuous.
    def _boom(records, *, context=""):
        store._LAST_WRITE_ERROR = "test-forced: unwritable state dir"
        return False

    monkeypatch.setattr(store, "_write_registry_unlocked", _boom)
    restored = _cold_reload(store)

    assert restored == 0, (
        f"an adoption whose snapshot write failed reported success: {restored}"
    )
    # The token survives either way — it anchors the receipt's liveness clock — but
    # it must NOT be marked as converted, because nothing was published. Leaving the
    # marker on would withhold the handoff from every later snapshot too, and the
    # crash-recovery evidence would be gone for good.
    surviving = [
        k for k in store._CONTINUATION_HANDOFF_TOKENS if k[0] == sid
    ]
    assert surviving, (
        "the handoff token is gone even though the adoption never became durable — "
        "the next reconciliation has nothing to act on"
    )
    assert not any(
        store._CONTINUATION_HANDOFF_TOKENS[k].get("_handoff_adopted_into_record")
        for k in surviving
    ), (
        "the handoff is marked as converted even though the snapshot write failed, "
        "so the durable crash-recovery evidence is withheld from every later write"
    )
    assert sid not in store._ADOPTED_HANDOFF_SESSIONS, (
        "the session is marked as adopted even though nothing was published"
    )
    assert store._LAST_WRITE_ERROR is not None, (
        "the failed write is not observable through durability_diagnostics()"
    )
    assert "test-forced" in (store._LAST_WRITE_ERROR or ""), (
        f"the recorded write error is not the one this scenario produced: "
        f"{store._LAST_WRITE_ERROR!r}"
    )
