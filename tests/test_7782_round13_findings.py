"""Regression coverage for the 2026-11-23 #7782 round-13 finding.

``IdempotencyStore._append_wal_locked`` used to derive the rollback boundary
from a *path* ``stat()`` whose failure was swallowed as ``start_size = 0``.
When the append then failed, rollback truncated the WAL to zero — destroying
records a previous request had already been acknowledged for — so a cold load
forgot the acknowledged key and re-admitted its turn.

The same helper decided its "was the file created" branch from
``path.exists()`` and rolled that branch back with ``unlink()``, so a failed
``exists()`` probe could erase the whole acknowledged WAL as well.

The boundary must come from the append descriptor itself (``fstat``), and when
that boundary cannot be known the append must fail closed BEFORE writing a
single byte, never guess "empty".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from api.idempotency import (
    STATUS_COMPLETE,
    IdempotencyStore,
    IdempotencyStoreUnavailable,
)

K_FP = "fingerprint-K"
K_IDENTITY = ("session-K", "stream-K", "turn-K")


def _wal_path(store: IdempotencyStore) -> Path:
    return store.path.with_name(store.path.name + ".wal")


def _acknowledge_k(store: IdempotencyStore) -> float:
    """Drive K through the real claim/complete API; return its claimed_at."""
    session_id, stream_id, turn_id = K_IDENTITY
    record = store.claim_with_identity(
        "K",
        K_FP,
        profile="default",
        session_id=session_id,
        stream_id=stream_id,
        turn_id=turn_id,
    )
    store.complete(
        "K",
        profile="default",
        session_id=session_id,
        stream_id=stream_id,
        turn_id=turn_id,
        response_status=200,
        response_payload={"ok": True},
    )
    return record.claimed_at


def _assert_k_replays_after_cold_load(
    store: IdempotencyStore, claimed_at: float
) -> None:
    fresh = IdempotencyStore(path=store.path)
    fresh._ensure_loaded()
    replay = fresh.claim("K", K_FP, profile="default")
    assert replay.status == STATUS_COMPLETE
    assert (replay.session_id, replay.stream_id, replay.turn_id) == K_IDENTITY
    assert replay.claimed_at == claimed_at, (
        "the replayed record must be K's original durable record, not a new admission"
    )
    # One admission: L never reached the store, and replaying K minted no
    # second record.
    assert fresh.lookup("L") is None
    assert len(fresh._records) == 1


def test_unknown_preappend_boundary_fails_closed_without_erasing_acknowledged_wal(
    tmp_path, monkeypatch
):
    store = IdempotencyStore(path=tmp_path / "idem" / "store.json")
    claimed_at = _acknowledge_k(store)
    wal = _wal_path(store)
    wal_before = wal.read_bytes()
    assert wal_before, "K must have durable WAL records before the fault"

    real_stat = Path.stat
    stat_calls = {"wal": 0}

    def faulting_stat(self, *args, **kwargs):
        if self == wal:
            stat_calls["wal"] += 1
            # The pre-fix code stats the path twice: once inside exists()
            # (to decide created/unlink) and once for the start_size read.
            # Fault the size read only, exactly as the finding describes.
            if stat_calls["wal"] == 2:
                raise OSError(5, "simulated EIO on pre-append size read")
        return real_stat(self, *args, **kwargs)

    def failing_fstat(fd):
        raise OSError(5, "simulated EIO on pre-append fstat")

    def failing_fsync(fd):
        raise OSError(5, "simulated EIO on append fsync")

    monkeypatch.setattr(Path, "stat", faulting_stat)
    monkeypatch.setattr(os, "fstat", failing_fstat)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    try:
        with pytest.raises(IdempotencyStoreUnavailable):
            store.claim("L", "fingerprint-L", profile="default")
    finally:
        monkeypatch.undo()

    assert store.lookup("L") is None
    assert wal.read_bytes() == wal_before, (
        "a failed append must never truncate WAL records that were already "
        "acknowledged"
    )
    _assert_k_replays_after_cold_load(store, claimed_at)


def test_lying_created_probe_cannot_unlink_acknowledged_wal(tmp_path, monkeypatch):
    store = IdempotencyStore(path=tmp_path / "idem" / "store.json")
    claimed_at = _acknowledge_k(store)
    wal = _wal_path(store)
    wal_before = wal.read_bytes()
    assert wal_before

    real_exists = Path.exists

    def lying_exists(self, *args, **kwargs):
        if self == wal:
            return False
        return real_exists(self, *args, **kwargs)

    def failing_fsync(fd):
        raise OSError(5, "simulated EIO on append fsync")

    monkeypatch.setattr(Path, "exists", lying_exists)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    try:
        with pytest.raises(IdempotencyStoreUnavailable):
            store.claim("L", "fingerprint-L", profile="default")
    finally:
        monkeypatch.undo()

    assert wal.exists(), "a failed append must not unlink the acknowledged WAL"
    assert wal.read_bytes() == wal_before
    _assert_k_replays_after_cold_load(store, claimed_at)


def test_append_failure_rolls_back_to_the_descriptor_boundary(tmp_path, monkeypatch):
    store = IdempotencyStore(path=tmp_path / "idem" / "store.json")
    claimed_at = _acknowledge_k(store)
    wal = _wal_path(store)
    wal_before = wal.read_bytes()
    assert wal_before

    def failing_fsync(fd):
        raise OSError(5, "simulated EIO on append fsync")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    try:
        with pytest.raises(IdempotencyStoreUnavailable):
            store.claim("L", "fingerprint-L", profile="default")
    finally:
        monkeypatch.undo()

    assert wal.read_bytes() == wal_before
    _assert_k_replays_after_cold_load(store, claimed_at)
    # The rollback must leave the WAL writable: a failed L is not a latched
    # "broken framing" condition.
    assert store._wal_broken is None
    store.claim("M", "fingerprint-M", profile="default")
    assert store.lookup("M") is not None
