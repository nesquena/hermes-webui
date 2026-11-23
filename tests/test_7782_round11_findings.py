"""Regression coverage for the 2026-11-18 #7782 review findings.

1. A failed append whose rollback also failed left an unfinished WAL line and
   kept the store writable.
2. The WAL was decoded before its torn uncommitted tail was removed, so a torn
   multi-byte UTF-8 character raised ``UnicodeDecodeError`` and every keyed
   request answered 503 until the file was repaired by hand.
3. A tombstone evicted from memory could come back after a restart even when a
   newer accepted record exists for the same key.
4. ``goal_continuation_id`` changes the work a start performs but was absent
   from the fingerprint.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from api.idempotency import (
    IdempotencyRecord,
    IdempotencyStore,
    IdempotencyStoreUnavailable,
    compute_request_fingerprint,
)


def _wal_path(store: IdempotencyStore) -> Path:
    return store.path.with_name(store.path.name + ".wal")


def _snapshot(path: Path, records, expired_keys) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "saved_at": time.time(),
                "seq": 100,
                "records": records,
                "expired_keys": expired_keys,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _complete_record(key: str, fingerprint: str = "fp") -> dict:
    return IdempotencyRecord(
        key=key,
        request_fingerprint=fingerprint,
        status="complete",
        profile="default",
        claimed_at=time.time(),
        completed_at=time.time(),
    ).to_json()


# ── 1. failed rollback latches the WAL unusable ──────────────────────────────


def test_failed_rollback_stops_further_appends(tmp_path, monkeypatch):
    store = IdempotencyStore(path=tmp_path / "idem" / "store.json")
    store.claim("first", "fp-1", profile="default")

    real_fsync = os.fsync
    real_ftruncate = os.ftruncate

    def failing_fsync(fd):
        raise OSError(5, "simulated EIO")

    def failing_ftruncate(fd, size):
        raise OSError(5, "simulated EIO on rollback")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    monkeypatch.setattr(os, "ftruncate", failing_ftruncate)
    try:
        with pytest.raises(IdempotencyStoreUnavailable):
            store.claim("second", "fp-2", profile="default")
    finally:
        monkeypatch.setattr(os, "fsync", real_fsync)
        monkeypatch.setattr(os, "ftruncate", real_ftruncate)

    assert store._wal_broken, "a failed rollback must latch the WAL unusable"
    with pytest.raises(IdempotencyStoreUnavailable) as excinfo:
        store.claim("third", "fp-3", profile="default")
    assert "unusable until repaired" in str(excinfo.value)


def test_reload_clears_the_unusable_latch(tmp_path):
    store = IdempotencyStore(path=tmp_path / "idem" / "store.json")
    store.claim("first", "fp-1", profile="default")
    store._wal_broken = "simulated previous failure"

    store.reload_from_disk()
    assert store._wal_broken is None
    # The append path works again after a durable re-read.
    store.claim("second", "fp-2", profile="default")


# ── 2. torn UTF-8 tail is dropped before decode ──────────────────────────────


def test_torn_multibyte_tail_is_removed_before_decode(tmp_path):
    store = IdempotencyStore(path=tmp_path / "idem" / "store.json")
    store.claim("kept", "fp-kept", profile="default")

    wal = _wal_path(store)
    original = wal.read_bytes()
    # Simulate a crash in the middle of a non-ASCII character: the retained
    # bytes are a valid line plus 2 of the 3 bytes of "中".
    torn = original + b'{"op":"put","seq":9999,"key":"default|torn","record":{"title":"' + "中".encode("utf-8")[:2]
    wal.write_bytes(torn)

    restarted = IdempotencyStore(path=store.path)
    restarted._ensure_loaded()
    assert restarted.lookup("kept") is not None
    assert wal.read_bytes() == original, "the torn tail must be truncated"


# ── 3. stale tombstones do not survive a restart ─────────────────────────────


def test_live_record_wins_over_a_stale_loaded_tombstone(tmp_path):
    path = tmp_path / "idem" / "store.json"
    _snapshot(
        path,
        records=[_complete_record("default|revived")],
        expired_keys=[["default|revived", "complete"]],
    )

    store = IdempotencyStore(path=path)
    store._ensure_loaded()
    assert "default|revived" not in store._expired_keys
    record = store.claim("revived", "fp", profile="default")
    assert record.status == "complete", "the accepted record must be replayed"


def test_loading_applies_the_tombstone_bound(tmp_path):
    path = tmp_path / "idem" / "store.json"
    expired = [[f"default|old-{i}", "complete"] for i in range(20)]
    _snapshot(path, records=[], expired_keys=expired)

    store = IdempotencyStore(path=path, tombstone_max_records=5)
    store._ensure_loaded()
    assert len(store._expired_keys) == 5
    assert list(store._expired_keys) == [f"default|old-{i}" for i in range(15, 20)]


# ── 4. goal_continuation_id is part of the fingerprint ───────────────────────


def test_goal_continuation_id_distinguishes_requests():
    base = {"session_id": "s", "message": "m"}
    omitted = compute_request_fingerprint(base)
    explicit_empty = compute_request_fingerprint({**base, "goal_continuation_id": ""})
    continuation = compute_request_fingerprint(
        {**base, "goal_continuation_id": "goal-1"}
    )
    assert len({omitted, explicit_empty, continuation}) == 3
