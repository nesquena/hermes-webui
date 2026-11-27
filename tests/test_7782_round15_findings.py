# ruff: noqa: F811 — the imported ``idem_env`` is a pytest fixture; ruff
# reads the same-named test parameters as redefinitions.

"""#7782 round-15 gate contract: rollback must not undo durable expiry work.

greptile-apps[bot] P1 (2026-11-26, thread on api/idempotency.py:1573,
"Rollback undoes saved expiry changes"):

``claim`` captures ``state_before`` BEFORE the TTL sweep, but the sweep's
tombstones are made durable by their OWN append. When the later claim
append fails, the restore used the pre-sweep snapshot, which

1. brought the expired record and the displaced oldest tombstone back
   into memory while dropping the newly saved tombstone, and
2. left those older maps in memory for a later compaction, which saves
   them under the CURRENT sequence watermark — so a restart skips the
   expiry entries that already reached disk and re-admits the key.

The repair re-snapshots after the sweep append succeeds, so a failed new
claim can only roll back itself. Both consequences are pinned below: the
in-memory contract, and the compaction/restart survival of the committed
expiry state. Every test drives the real ``IdempotencyStore.claim``.

RED on the round-14 head (51da0c1b4), GREEN on the repair.
"""

import time

import pytest

from api import idempotency as idem_mod
from api.idempotency import (
    STATUS_COMPLETE,
    IdempotencyRecord,
    IdempotencyStore,
    IdempotencyStoreUnavailable,
)
# The chat-start harness (real route + real store + recorder in place of the
# Agent worker) from the feature's main test module.
from tests.test_idempotency import idem_env  # noqa: F401  (pytest fixture)


def _record(key: str, *, status: str, claimed_at: float) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=key,
        request_fingerprint=f"fp::{key}",
        status=status,
        profile="default",
        claimed_at=claimed_at,
    )


def _sweep_then_fail_claim(store: IdempotencyStore, monkeypatch) -> None:
    """Let the sweep's tombstone append succeed, fail the claim's append.

    ``claim`` performs TWO durable appends when the TTL sweep mints
    tombstones: the sweep batch first, then the claim batch. This wrapper
    passes the first through to the real implementation and raises on the
    second, reproducing the reviewer's failure window exactly (the sweep
    is committed, the new claim is not).
    """
    calls: list[list[dict]] = []
    real_append = store._append_wal_locked

    def controlled(ops: list[dict]) -> None:
        calls.append(list(ops))
        if len(calls) == 1:
            real_append(ops)  # the sweep append must succeed and be durable
            return
        raise IdempotencyStoreUnavailable("simulated claim append failure")

    monkeypatch.setattr(store, "_append_wal_locked", controlled)


def _build_sweep_scenario(tmp_path) -> IdempotencyStore:
    """A store whose next claim sweeps an expired record that mints a
    tombstone, displacing the oldest retained tombstone at the bound."""
    store = IdempotencyStore(
        path=tmp_path / "idempotency" / "store.json",
        ttl_seconds=100.0,
        max_records=10,
        tombstone_max_records=2,
    )
    store._ensure_loaded()
    now = time.time()
    with store._lock:
        # Retained metadata already at its bound: the sweep's new tombstone
        # displaces the OLDEST entry (that eviction is itself durable).
        store._expired_keys["default|oldest-tombstone"] = "complete"
        store._expired_keys["default|newer-tombstone"] = "pending"
        store._records["default|keep-a"] = _record(
            "default|keep-a", status=STATUS_COMPLETE, claimed_at=now
        )
        store._records["default|expire-me"] = _record(
            "default|expire-me", status=STATUS_COMPLETE, claimed_at=now - 200
        )
        store._records["default|keep-b"] = _record(
            "default|keep-b", status=STATUS_COMPLETE, claimed_at=now
        )
    return store


def test_claim_failure_after_durable_sweep_keeps_committed_expiry_changes(
    tmp_path, monkeypatch
):
    """The reviewer's in-memory contract: after the sweep append succeeds,
    a failed claim append must roll back to the POST-sweep state, not to
    the pre-sweep snapshot. The eviction, the minted tombstone and the
    bound's displacement of the oldest tombstone all reached disk and
    must survive; only the failed claim itself is undone."""
    store = _build_sweep_scenario(tmp_path)
    _sweep_then_fail_claim(store, monkeypatch)

    with pytest.raises(IdempotencyStoreUnavailable):
        store.claim("fresh-key", "fp::fresh-key", profile="default")

    # The failed claim itself is rolled back...
    assert "default|fresh-key" not in store._records, (
        "a claim whose append failed survived in memory"
    )
    # ...but the committed expiry sweep is NOT undone: the expired record
    # stays evicted and its tombstone stays minted.
    assert "default|expire-me" not in store._records, (
        "rollback resurrected an expired record whose expiry append "
        "already reached disk"
    )
    assert store._expired_keys.get("default|expire-me") == "complete", (
        "rollback removed a tombstone that was already durable"
    )
    # The oldest retained tombstone the bound displaced during the sweep
    # must stay displaced (its removal is durable too), and the newer
    # retained tombstone must survive.
    assert "default|oldest-tombstone" not in store._expired_keys, (
        "rollback resurrected a tombstone the bound durably evicted"
    )
    assert "default|newer-tombstone" in store._expired_keys, (
        "rollback dropped a retained tombstone that was never displaced"
    )
    # Untouched records survive.
    assert "default|keep-a" in store._records
    assert "default|keep-b" in store._records


def test_failed_claim_cannot_push_stale_maps_through_compaction(
    tmp_path, monkeypatch
):
    """The reviewer's second consequence: with the pre-sweep restore, the
    older maps left in memory are saved by the next compaction under the
    CURRENT sequence watermark, so a restart skips the already-durable
    expiry entries and re-admits the key. After the repair, the committed
    expiry state survives compaction and restart: the expired record stays
    gone and its tombstone keeps answering the refusal."""
    store = _build_sweep_scenario(tmp_path)
    _sweep_then_fail_claim(store, monkeypatch)

    with pytest.raises(IdempotencyStoreUnavailable):
        store.claim("fresh-key", "fp::fresh-key", profile="default")

    # Drive the compaction the reviewer named: the WAL has grown past the
    # threshold, so the next mutation persists the in-memory maps under
    # the current watermark. (Push the counter rather than appending 2048
    # ops — the failure window is the maps, not the log length.)
    with store._lock:
        store._wal_lines = idem_mod._WAL_COMPACT_MIN_OPS + 1
        store._maybe_compact_locked()

    # A restart replays the WAL on top of the compacted snapshot.
    reloaded = IdempotencyStore(
        path=store.path,
        ttl_seconds=100.0,
        max_records=10,
        tombstone_max_records=2,
    )
    reloaded._ensure_loaded()

    assert "default|expire-me" not in reloaded._records, (
        "compaction saved the pre-sweep maps under the current watermark: "
        "the expired record came back after restart"
    )
    assert reloaded._expired_keys.get("default|expire-me") == "complete", (
        "the durable tombstone did not survive compaction + restart"
    )
    # The refusal is what the tombstone exists for: re-claiming the key
    # after restart must fail explicitly, never re-admit the turn.
    with pytest.raises(idem_mod.IdempotencyKeyExpired):
        reloaded.claim("expire-me", "fp::default|expire-me", profile="default")
