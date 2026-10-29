"""Regression tests for the two #8302 review findings on the models-cache memo.

Finding 1 — concurrent readers can be handed the SAME catalog object.
    ``_memoized_models_cache_disk_payload`` read the slot, then called
    ``stat()`` (which releases the GIL), then cleared the slot. Two threads
    interleaving in that window both passed validation and both received the
    same ``payload``. Callers such as ``_annotate_fast_tier_model_groups`` and
    ``warm_models_catalog_provenance_if_cold`` mutate what they receive, so one
    caller's in-place edits leak into the other reader's catalog. Fixed by
    holding a lock across the whole read-validate-clear path.

Finding 2 — a same-size rewrite is served as the old catalog.
    The real writer replaces the file via a temp file plus ``os.replace()``,
    which mints a new inode. On Linux, inode timestamps come from the coarse
    clock, so a rapid rewrite can carry the SAME ``st_mtime_ns`` and, for a
    same-size catalog, the SAME ``st_size``. Keyed on those two alone, a
    precheck-then-open window served the previous generation. Fixed by keying
    on ``st_ino``/``st_dev`` taken from ``os.fstat`` of the descriptor the
    payload was actually parsed from.

Both tests are INVARIANTS: they must fail against the unfixed producer/consumer
and pass with the fix, and they must not depend on timing beyond an explicit
barrier or an explicit hook.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

import api.config as config


def _catalog(label: str, n_models: int = 6) -> dict:
    return {
        "active_provider": "openai",
        "default_model": f"{label}-0",
        "configured_model_badges": {},
        "groups": [{"name": "all", "models": [f"{label}-{i}" for i in range(n_models)]}],
        "aliases": {},
        "_built_at": 1000.0,
    }


@pytest.fixture(autouse=True)
def _reset_memo():
    config._models_disk_payload_memo = None
    yield
    config._models_disk_payload_memo = None


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_concurrent_consumers_never_share_one_catalog_object(tmp_path):
    """Two consumers racing the consume path must get DISTINCT objects.

    The invariant is mutual exclusion of the whole read-validate-clear path.
    Reading the slot and then calling ``stat()`` releases the GIL, so without a
    lock two threads are inside the function at the same time, both pass
    validation, and both receive the SAME payload object — the exact aliasing
    the consume step exists to prevent.

    The probe instruments ``Path.stat`` (called inside the critical section)
    to record how many threads are concurrently inside it. A sleep there keeps
    the GIL released long enough for an unlocked implementation to overlap,
    which is what makes this discriminate rather than merely pass.
    """
    cache_path = tmp_path / "models_cache.json"
    _write(cache_path, _catalog("old"))

    config._read_models_cache_disk_payload(cache_path)

    inside = 0
    max_inside = 0
    lock = threading.Lock()
    real_stat = config.Path.stat

    def probing_stat(self, *a, **kw):
        nonlocal inside, max_inside
        with lock:
            inside += 1
            max_inside = max(max_inside, inside)
        try:
            # Yield the GIL: this is the window the lock has to close.
            time.sleep(0.05)
            return real_stat(self, *a, **kw)
        finally:
            with lock:
                inside -= 1

    start = threading.Barrier(2, timeout=10)
    results: list = [None, None]
    errors: list = [None, None]

    def consume(i: int) -> None:
        try:
            start.wait()
            results[i] = config._memoized_models_cache_disk_payload(cache_path)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors[i] = exc

    config.Path.stat = probing_stat
    try:
        threads = [threading.Thread(target=consume, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        config.Path.stat = real_stat

    assert errors == [None, None], f"consumer raised: {errors}"
    assert max_inside == 1, (
        f"the memo slot must be read, validated and cleared under one lock; "
        f"{max_inside} threads were inside the critical section at once"
    )

    # Exactly one consumer may win the single-use slot; the other must miss and
    # fall through to its own read, so no two readers share one catalog.
    winners = [r for r in results if r is not config._MODELS_DISK_PAYLOAD_UNSET]
    assert len(winners) == 1, (
        f"exactly one consumer must win the single-use memo slot; "
        f"got {len(winners)} -> both received the same object: "
        f"{[id(w) for w in winners]}"
    )

    # Mutating the winner's catalog must not be observable by a later read.
    winner = winners[0]
    winner["groups"][0]["models"].append("MUTATED")
    assert config._memoized_models_cache_disk_payload(cache_path) is config._MODELS_DISK_PAYLOAD_UNSET
    assert json.loads(cache_path.read_text(encoding="utf-8")) == _catalog("old")


def test_same_size_rewrite_between_precheck_and_loader_is_not_served(tmp_path):
    """A same-size ``os.replace`` rewrite must invalidate the memo.

    The real writer replaces the file with a temp file plus ``os.replace()``,
    which mints a NEW inode. On Linux, inode timestamps come from the coarse
    clock, so a rapid rewrite can carry the SAME ``st_mtime_ns`` and, for a
    same-length catalog, the SAME ``st_size``.

    The coarse clock is reproduced deterministically rather than by racing it:
    the replacement is written, its mtime is pinned to the original file's
    mtime_ns, and the sizes are made equal (``old-N`` -> ``new-N``, both three
    characters). The two generations then differ ONLY by inode, which is
    exactly the input class that keyed-on-(mtime, size) cannot see.
    """
    cache_path = tmp_path / "models_cache.json"
    old = _catalog("old")
    _write(cache_path, old)

    # Producer memoises the OLD generation.
    memoised = config._read_models_cache_disk_payload(cache_path)
    assert memoised == old
    old_stat = cache_path.stat()

    # The real writer's shape: a new temp file with identical length, an
    # identical coarse-clock mtime, then os.replace(). Same size, same
    # mtime_ns, different inode.
    new = _catalog("new")  # "old"/"new" are both 3 chars -> identical length
    tmp = cache_path.with_suffix(".tmp")
    _write(tmp, new)
    assert tmp.stat().st_size == old_stat.st_size
    os.utime(tmp, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    os.replace(tmp, cache_path)

    new_stat = cache_path.stat()
    assert (new_stat.st_size, new_stat.st_mtime_ns) == (old_stat.st_size, old_stat.st_mtime_ns)
    assert new_stat.st_ino != old_stat.st_ino, "os.replace must mint a new inode for this test to mean anything"

    consumed = config._memoized_models_cache_disk_payload(cache_path)
    assert consumed is config._MODELS_DISK_PAYLOAD_UNSET, (
        "a same-size, same-mtime os.replace rewrite must not be served from the "
        f"memo; got {consumed!r}"
    )


def test_invalidate_models_cache_drops_the_parse_memo(tmp_path, monkeypatch):
    """``invalidate_models_cache()`` must clear the memo slot too.

    The slot is keyed on (path, mtime_ns, size, st_ino, st_dev), none of which
    a rebuild is guaranteed to change: the writer can land inside the same
    coarse-clock mtime tick at an identical size, and a filesystem that reuses
    inodes can reissue the same st_ino/st_dev pair. So an entry that survives
    invalidation keeps matching the freshly rebuilt file and is served to the
    next reader -- handing that reader the PRE-invalidation catalog, which the
    consume step passes on without a copy.

    This is the cross-test pollution that turned
    ``tests/test_credential_pool_providers.py::test_seeded_config_survives_a_stale_loaded_config_path``
    red: an earlier test in the shard memoised its own catalog, this test's
    ``invalidate_models_cache()`` cleared the memory cache and unlinked the
    disk file but left the memo populated, and the seeded ``bothub`` provider
    was then masked by the previous test's ``OpenAI Codex``-only catalog.
    """
    cache_path = tmp_path / "models_cache.json"
    _write(cache_path, _catalog("old"))

    # A previous visit memoises the old generation.
    assert config._read_models_cache_disk_payload(cache_path) == _catalog("old")

    monkeypatch.setattr(config, "_get_models_cache_path", lambda: cache_path)
    config.invalidate_models_cache()

    assert config._models_disk_payload_memo is None, (
        "invalidate_models_cache() must drop the one-slot parse memo; a surviving "
        f"entry kept a key that still matches the rebuilt file -> {config._models_disk_payload_memo!r}"
    )

    # And the memoised pre-invalidation catalog is unreachable afterwards.
    rebuilt = _catalog("new")
    _write(cache_path, rebuilt)
    assert config._memoized_models_cache_disk_payload(cache_path) is config._MODELS_DISK_PAYLOAD_UNSET
    assert config._read_models_cache_disk_payload(cache_path) == rebuilt


def test_memo_hit_still_works_for_the_unchanged_file(tmp_path):
    """The single-parse fast path must survive the lock + inode key."""
    cache_path = tmp_path / "models_cache.json"
    payload = _catalog("same")
    _write(cache_path, payload)

    assert config._read_models_cache_disk_payload(cache_path) == payload
    hit = config._memoized_models_cache_disk_payload(cache_path)
    assert hit is not config._MODELS_DISK_PAYLOAD_UNSET
    assert hit == payload

    # Single-use: a second consume misses and the reader re-parses.
    assert config._memoized_models_cache_disk_payload(cache_path) is config._MODELS_DISK_PAYLOAD_UNSET