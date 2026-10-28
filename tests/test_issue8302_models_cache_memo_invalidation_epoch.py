"""Regression tests for the #8302 invalidation-epoch finding on the parse memo.

Finding (nesquena-hermes, 2026-10-21 re-gate)
---------------------------------------------
An in-progress session visit can put the PRE-invalidation catalog back into the
one-slot parse memo.

``_read_models_cache_disk_payload()`` parses the file, closes the descriptor
and only THEN publishes its entry into ``_models_disk_payload_memo``. Nothing
checked whether a catalog invalidation happened in between.
``invalidate_models_cache()`` clears the slot, but a reader that parsed before
that call can still publish its entry after the clear. If the rebuilt file then
matches the memo's key -- same coarse-clock mtime tick, same size, and the
inode the delete freed being reused -- the loader in the SAME visit consumes
that pre-invalidation payload and the visit serves the old catalog.

The epoch guard in ``get_available_models_for_session_visit()`` does not catch
it: ``disk_epoch`` is read AFTER the precheck, so it already holds the
post-invalidation value.

The fix fences the memo on the catalog generation (``_models_rebuild_seq``):
the producer captures the generation before it parses, publishes its entry only
while that generation is still current, and the consumer rejects an entry
whose generation is no longer current. BOTH sides are required:
``invalidate_models_cache()`` clears the slot BEFORE it bumps the sequence, so
a producer can publish into that gap and only the consumer-side check can
catch it.

The second test covers the [LOW] finding from the same round: a visit served
from the IN-MEMORY cache still runs the freshness precheck, which parses into
the slot, and the visit then returns from memory without consuming it. The
slot must not keep that parsed catalog resident past the visit.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

import api.config as config


def _disk_payload(label: str, built_at: float) -> dict:
    """A loadable on-disk models cache payload for the given generation label."""
    payload = {
        "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
        "_source_fingerprint": {"profile": "demo"},
        "_built_at": built_at,
        "active_provider": "openai",
        "default_model": f"{label}-0",
        "configured_model_badges": {},
        "groups": [{"name": "all", "models": [f"{label}-{i}" for i in range(6)]}],
    }
    # _is_loadable_disk_cache validates the #1633 version stamp whenever the
    # runtime version resolves.
    runtime_version = config._current_webui_version()
    if runtime_version is not None:
        payload["_webui_version"] = runtime_version
    return payload


def _memory_payload(label: str) -> dict:
    """A valid in-memory catalog (the shape check only, no disk stamps)."""
    return {
        "active_provider": "openai",
        "default_model": f"{label}-0",
        "configured_model_badges": {},
        "groups": [{"name": "all", "models": [f"{label}-{i}" for i in range(6)]}],
    }


class _GatedLock:
    """Wraps the memo lock so its FIRST acquisition blocks the producer.

    The producer parses the file and then takes the lock to publish its entry.
    Blocking that first acquisition parks the producer AFTER it closed the
    descriptor and BEFORE the slot write -- exactly the window the review
    describes. The block happens BEFORE the underlying lock is taken, so the
    invalidation running on the main thread is never blocked by it.
    """

    def __init__(self) -> None:
        self._real = threading.Lock()
        self.entered = threading.Event()
        self.released = threading.Event()
        self._first = True

    def acquire(self, *args, **kwargs) -> bool:
        if self._first:
            self._first = False
            self.entered.set()
            if not self.released.wait(30):
                raise AssertionError("the gated memo lock was never released")
        return self._real.acquire(*args, **kwargs)

    def release(self) -> None:
        self._real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


@pytest.fixture
def durable_cache(tmp_path, monkeypatch):
    """A loadable on-disk models cache with a CURRENT ``_built_at`` stamp."""
    monkeypatch.setattr(config, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(
        config, "_models_cache_source_fingerprint", lambda: {"profile": "demo"}, raising=False
    )
    monkeypatch.setattr(config, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(config, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(config, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(
        config, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(config, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(config, "_models_rebuild_seq", 0, raising=False)

    path = tmp_path / "models_cache.profile.json"
    built_at = time.time() - 10  # fresh: built 10s ago
    path.write_text(json.dumps(_disk_payload("old", built_at)), encoding="utf-8")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: path, raising=False)
    return path


def test_invalidation_during_the_precheck_cannot_serve_the_pre_invalidation_catalog(
    durable_cache, monkeypatch
):
    """An invalidation overlapping the precheck must not resurrect the old catalog.

    The visit is parked between the precheck's parse and the memo store. The
    main thread then invalidates the catalog and rewrites the file with an
    equal-length payload and a pinned mtime, so the memo's stat key STILL
    matches the file on disk -- the key alone cannot tell the two generations
    apart. Only the catalog generation can.

    Before the fix the visit returned and published the pre-invalidation
    catalog ("old-0"); master returns the new one ("new-0").
    """
    gate = _GatedLock()
    monkeypatch.setattr(config, "_models_disk_payload_memo_lock", gate)

    # Captured before the visit starts, so it is exactly the stat the
    # producer's fstat() sees.
    old_stat = durable_cache.stat()
    built_at = json.loads(durable_cache.read_text(encoding="utf-8"))["_built_at"]
    new_bytes = json.dumps(_disk_payload("new", built_at)).encode("utf-8")
    assert len(new_bytes) == old_stat.st_size, (
        "the rebuild must be the same size, otherwise the memo's stat key "
        "cannot match and the test would not exercise the generation fence"
    )

    result: dict = {}
    errors: list = []

    def visit() -> None:
        try:
            result["catalog"] = config.get_available_models_for_session_visit()
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    thread = threading.Thread(target=visit)
    thread.start()
    try:
        assert gate.entered.wait(30), "the precheck never reached the memo store"

        # Invalidate while the producer is parked: the slot is cleared and the
        # catalog generation advances.
        config.invalidate_models_cache(delete_disk=False)

        # Rebuild the catalog in place: same inode, same size, mtime pinned
        # back to the original, so the memo's stat key still matches.
        with open(durable_cache, "r+b") as f:
            f.write(new_bytes)
            f.truncate()
        os.utime(durable_cache, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        new_stat = durable_cache.stat()
        assert (new_stat.st_size, new_stat.st_mtime_ns, new_stat.st_ino, new_stat.st_dev) == (
            old_stat.st_size,
            old_stat.st_mtime_ns,
            old_stat.st_ino,
            old_stat.st_dev,
        ), "the rebuilt file must keep the memo's stat key identical"
    finally:
        gate.released.set()
        thread.join(timeout=30)

    assert not thread.is_alive(), "the visit thread never finished"
    assert errors == [], f"the visit raised: {errors}"

    catalog = result["catalog"]
    assert catalog["default_model"] == "new-0", (
        "the visit served the PRE-invalidation catalog; an invalidation that "
        "overlapped the precheck must not be undone by the parse memo "
        f"-> {catalog['default_model']!r}"
    )
    published = config._available_models_cache
    assert published is not None, "the visit must publish the catalog it served"
    assert published["default_model"] == "new-0", (
        "the published in-memory catalog must be the post-invalidation one, "
        f"got {published['default_model']!r}"
    )


def test_a_memo_entry_from_a_superseded_generation_is_never_served(tmp_path, monkeypatch):
    """A consumer must reject an entry whose generation is no longer current.

    The producer-side check alone is not enough. ``invalidate_models_cache()``
    clears the memo slot BEFORE it advances the catalog generation, so a
    producer that captured the old generation can publish into that gap: the
    entry is published while the generation is still current, and the
    generation advances afterwards. Only the consumer's own check catches
    that entry, because the file's stat identity cannot -- a rebuild is not
    guaranteed to change any part of it.

    The state is built directly: publish an entry, then advance the
    generation with the slot left populated, which is exactly what that gap
    leaves behind.
    """
    cache_path = tmp_path / "models_cache.json"
    payload = {
        "_schema_version": 3,
        "active_provider": "openai",
        "default_model": "old-0",
        "configured_model_badges": {},
        "groups": [{"name": "all", "models": ["old-0"]}],
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    # Publish the entry under the current generation.
    assert config._read_models_cache_disk_payload(cache_path) == payload
    assert config._models_disk_payload_memo is not None

    # Advance the generation while the slot stays populated -- the gap
    # between invalidate_models_cache() clearing the slot and bumping the
    # generation. The file is untouched, so its stat identity still matches
    # the published entry exactly.
    monkeypatch.setattr(
        config, "_models_rebuild_seq", config._models_rebuild_seq + 1, raising=False
    )
    stat_before = cache_path.stat()

    consumed = config._memoized_models_cache_disk_payload(cache_path)

    assert stat_before.st_mtime_ns == cache_path.stat().st_mtime_ns, (
        "the file must be untouched, so the stat identity cannot be what "
        "rejects the entry"
    )
    assert consumed is config._MODELS_DISK_PAYLOAD_UNSET, (
        "an entry from a superseded catalog generation must never be served; "
        f"got {consumed!r}"
    )
    assert config._models_disk_payload_memo is None, (
        "a rejected superseded-generation entry must be dropped, not left "
        f"resident -> {config._models_disk_payload_memo!r}"
    )


def test_a_memory_cache_hit_visit_does_not_leave_its_parse_in_the_memo(durable_cache, monkeypatch):
    """A visit served from the memory cache must not keep a parsed catalog resident.

    The freshness precheck runs on EVERY visit, so it parses the durable cache
    into the memo slot. A visit that is then served from the in-memory cache
    never reaches the loader that would consume that parse, so the entry would
    sit in the slot until some unrelated later read picked it up -- keeping a
    whole parsed catalog (2.2 MB at 5000 models) resident, and contradicting
    the documented "never outlives a single session visit" rule.
    """
    warm = _memory_payload("warm")
    monkeypatch.setattr(config, "_available_models_cache", warm, raising=False)
    monkeypatch.setattr(config, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(
        config, "_available_models_cache_source_fingerprint", {"profile": "demo"}, raising=False
    )

    catalog = config.get_available_models_for_session_visit()

    assert catalog["default_model"] == "warm-0", "the visit must be served from memory"
    assert config._models_disk_payload_memo is None, (
        "a memory-cache hit leaves its precheck parse in the memo slot; the "
        f"slot must be dropped -> {config._models_disk_payload_memo!r}"
    )
