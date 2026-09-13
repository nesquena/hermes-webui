"""Regression coverage for #7007 round-5 publish/ownership races.

The reviewer's round-5 re-gate (exact head 2f1c3d9e) found three concrete
races in the outer catalog build's publish/ownership lifecycle, on top of
the already-fixed bounded-follower-wait and generation-bump issues:

1. A stale detached build's disk write could complete its rename AFTER an
   invalidation deleted the file it was replacing, resurrecting stale data.
2. A late-failing worker could clear a NEWER owner's in-progress flag just
   because it happened to finish after an invalidation retired it and a new
   caller took over.
3. A timed-out non-force follower could start a SECOND competing rebuild
   under the same generation, since nothing checked `_cache_build_in_progress`
   before barging in and setting it True again.

Fixed via an immutable `(generation, owner_token, durable_authority)` identity
(`_try_claim_build_owner` / `_is_current_build_owner` /
`_release_build_owner_if_current`) that every finalizer must own before it
may mutate shared state. These tests reproduce each race deterministically
(explicit events/monkeypatched primitives, not sleeps) and were verified to
fail against the pre-fix code.
"""

from __future__ import annotations

import threading


def _catalog(label: str) -> dict:
    return {
        "active_provider": "openai",
        "default_model": label,
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": label, "label": label, "supports_fast_tier": False}],
            }
        ],
        "aliases": {},
    }


def _reset_models_memory_cache(monkeypatch):
    import api.config as cfg

    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", None, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(cfg, "_active_build_owner", None, raising=False)


def _isolate_disk_and_config(monkeypatch, tmp_path):
    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    cache_path = tmp_path / "models_cache.json"

    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    return cache_path


def test_invalidation_during_disk_write_deletes_stale_resurrected_file(tmp_path, monkeypatch):
    """Finding 1: an invalidation landing WHILE the winning build's disk
    write is in flight must not let that write's rename survive — otherwise
    a stale detached build resurrects data the invalidation meant to
    discard. The old code only re-checked the generation before starting
    the write, never after it actually landed on disk."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    cache_path = _isolate_disk_and_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 5.0, raising=False)

    rebuilt = _catalog("rebuilt-model")
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", lambda _builder: rebuilt)

    real_save = cfg._save_models_cache_to_disk

    def _invalidate_then_save(cache, **kwargs):
        # Simulate the actual race: an invalidation's own disk delete lands
        # FIRST (e.g. a credential edit fired while this write was still in
        # flight), and only THEN does this stale build's rename land on top
        # of it. `invalidate_models_cache()` deletes the file as one of its
        # own side effects — if this write's rename completes afterward
        # with no further fencing, the stale data resurrects right back.
        cfg.invalidate_models_cache()
        real_save(cache, **kwargs)

    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _invalidate_then_save)

    result = cfg.get_available_models(force_refresh=True)

    assert result == rebuilt
    assert not cache_path.exists(), (
        "a build whose disk write raced an invalidation must not leave the "
        "stale file behind — it should be deleted, not resurrected"
    )
    # The invalidation's own generation bump must be the one callers see —
    # confirms this isn't accidentally passing via a totally separate path.
    assert cfg._cache_build_in_progress is False


def test_late_failed_worker_does_not_clear_newer_owners_flag(tmp_path, monkeypatch):
    """Finding 2: G0's worker remains live (blocked), an invalidation
    retires G0 and a fresh G1 build claims ownership, and only THEN does
    G0's stale worker finally raise. G0's failure cleanup must be a no-op —
    it must NOT clear G1's in-progress flag out from under it.

    The interleaving is asserted explicitly, not assumed (#7007 round 6
    audit): an earlier version of this test waited on a single shared
    "a release happened" Event and gave G0's probe a 5s timeout. G0's probe
    therefore timed out and released its ownership on its own, *before* the
    invalidation and before G1 existed — a release by the still-legitimate
    owner, which the guard correctly permits. The shared Event was already
    set by that early call, so the final assertion ran against a flag nobody
    had contended for, and the test passed even with the ownership guard
    deleted. It now tracks releases per owner tuple and asserts that G0's
    cleanup really did run while G1 was the live owner.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    _isolate_disk_and_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)

    g0_release = threading.Event()
    g1_release = threading.Event()
    g1_probing = threading.Event()
    call_count = [0]

    def _invoke(_builder):
        call_count[0] += 1
        if call_count[0] == 1:
            # Generous timeout: this must NEVER expire on its own, or G0
            # would retire itself while still the legitimate owner and the
            # race under test would silently not happen.
            assert g0_release.wait(timeout=30), "test setup failed to release G0"
            raise RuntimeError("G0 probe failed (arrives late, after G1 started)")
        g1_probing.set()
        assert g1_release.wait(timeout=30), "test setup failed to release G1"
        return _catalog("g1-result")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _invoke)

    owners: dict = {}
    release_attempts: list = []
    g0_cleanup_ran = threading.Event()
    real_release = cfg._release_build_owner_if_current

    def _tracking_release(owner):
        live_owner_at_call = cfg._active_build_owner
        real_release(owner)
        release_attempts.append((owner, live_owner_at_call))
        if owner == owners.get("g0"):
            g0_cleanup_ran.set()

    monkeypatch.setattr(cfg, "_release_build_owner_if_current", _tracking_release)

    # G0: budget is tiny (0.1s) so the foreground gives up immediately and
    # returns a fallback while G0's worker keeps running in the background,
    # blocked on g0_release.
    cfg.get_available_models(force_refresh=True)
    assert cfg._cache_build_in_progress is True, "G0's worker should still own the build"
    owners["g0"] = cfg._active_build_owner
    assert owners["g0"] is not None

    # An invalidation retires G0 (e.g. a credential edit landed).
    cfg.invalidate_models_cache()
    assert cfg._cache_build_in_progress is False
    assert not g0_cleanup_ran.is_set(), (
        "G0's cleanup must not have run yet — it has to arrive AFTER G1 "
        "takes over for this test to exercise anything"
    )

    # G1 starts a fresh build and claims ownership. Same tiny budget, so
    # this also returns immediately via the budget-exceeded fallback while
    # G1's own worker (call #2, blocked on g1_release) keeps running.
    cfg.get_available_models(force_refresh=True)
    assert g1_probing.wait(timeout=10), "G1 never reached its probe"
    assert cfg._cache_build_in_progress is True, "G1 should now own the build"
    owners["g1"] = cfg._active_build_owner
    assert owners["g1"] != owners["g0"]

    # NOW let G0's stale worker finally fail.
    g0_release.set()
    assert g0_cleanup_ran.wait(timeout=10), "G0's late failure cleanup never ran"

    # The interleaving actually happened: G0's cleanup ran while G1 owned.
    g0_attempts = [a for a in release_attempts if a[0] == owners["g0"]]
    assert g0_attempts, "no release was attempted for G0's owner tuple"
    assert g0_attempts[-1][1] == owners["g1"], (
        "the race under test never happened — G0's cleanup must run while "
        f"G1 is the live owner, saw live owner {g0_attempts[-1][1]!r}"
    )

    # G1's flag must have survived G0's late (now-stale) failure untouched.
    assert cfg._cache_build_in_progress is True, (
        "a late-failing stale owner (G0) must not be able to clear a "
        "newer owner's (G1) in-progress flag"
    )
    assert cfg._active_build_owner == owners["g1"]

    g1_release.set()


def test_timed_out_follower_does_not_start_second_build_and_fails_open(tmp_path, monkeypatch):
    """Finding 3 + the no-stale-snapshot follower fallback: a non-force
    caller whose bounded wait times out because the owner is STILL
    genuinely building (no invalidation happened) must not barge in and
    start a second competing rebuild under the same generation — and, with
    no stale disk snapshot available either, it must still return a sane
    fallback rather than hang or start that redundant build."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    _isolate_disk_and_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 5.0, raising=False)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)

    g0_hang = threading.Event()
    g0_probing = threading.Event()
    build_calls = []

    def _invoke(_builder):
        build_calls.append(1)
        g0_probing.set()
        g0_hang.wait(timeout=10)
        return _catalog("g0-result")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _invoke)

    # Kick off G0 (force_refresh, tiny-relative-to-real-60s budget so the
    # foreground gives up fast while the worker keeps running, blocked).
    t0 = threading.Thread(
        target=lambda: cfg.get_available_models(force_refresh=True),
        daemon=True,
    )
    t0.start()
    assert g0_probing.wait(timeout=5), "G0 never reached its probe"
    t0.join(timeout=5)
    assert cfg._cache_build_in_progress is True, "G0 should still be building"

    # A non-force follower's wait_for(timeout=60) would time out here
    # because G0 is genuinely still live — fake that outcome instead of
    # actually waiting 60 real seconds.
    def _fake_wait_for(_predicate, timeout=None):
        return False  # "timed out, predicate still false" — owner still live

    monkeypatch.setattr(cfg._cache_build_cv, "wait_for", _fake_wait_for)

    result = cfg.get_available_models()  # plain non-force caller

    assert build_calls == [1], (
        "a timed-out follower must not start a second competing rebuild "
        "while the first owner is still live"
    )
    assert result == cfg._static_models_catalog_without_live_probes(), (
        "with no stale disk snapshot available either, the follower must "
        "fail open with the network-free static fallback, not hang or error"
    )

    g0_hang.set()


def test_disk_read_before_lock_cannot_resurrect_a_generation_the_writer_lost(tmp_path, monkeypatch):
    """#7007 round 6: finding 1's write-then-delete-if-invalidated pair
    narrows the resurrection window but does not close it —
    `_load_models_cache_from_disk()` is deliberately called BEFORE
    acquiring `_available_models_cache_lock` (see its docstring: "lets
    concurrent requests skip entirely"), so a reader can land in the exact
    gap between a stale build's write completing and its own
    delete-on-invalidation cleanup running, and would read the resurrected
    file before the delete ever happens — the delete only prevents FUTURE
    reads, it can't retroactively un-read one that already happened.

    This exercises the real (unmocked) `_load_models_cache_from_disk` /
    `_is_loadable_disk_cache` path directly: a file is written stamped with
    an OLD generation (simulating the stale build's write landing, however
    briefly, before its cleanup deletes it), and the live generation has
    since moved on (simulating the invalidation that retired that build).
    The read must reject it regardless of whether the delete has run yet.
    """
    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "_current_webui_version", lambda: "v-test")

    stale_generation = cfg._available_models_cache_generation
    cfg._save_models_cache_to_disk(_catalog("stale-build"), generation=stale_generation)
    assert cache_path.exists()

    # Simulate the invalidation that retired the build which just wrote
    # this file — its own delete-on-mismatch cleanup has NOT run yet (that's
    # exactly the gap this test targets), so the stale file is still sitting
    # on disk when this read happens.
    monkeypatch.setattr(
        cfg, "_available_models_cache_generation", stale_generation + 1, raising=False
    )

    assert cache_path.exists(), "the file must still be present for this to test the real race"
    loaded = cfg._load_models_cache_from_disk()
    assert loaded is None, (
        "a disk file stamped with an older generation than the current live "
        "one must be rejected on read, independent of whether the writer's "
        "own delete-if-invalidated cleanup has landed yet"
    )

    # Sanity: the same file, re-stamped with the CURRENT generation, loads fine.
    cfg._save_models_cache_to_disk(_catalog("fresh-build"), generation=stale_generation + 1)
    assert cfg._load_models_cache_from_disk() is not None


def test_stale_disk_preload_is_not_published_after_a_concurrent_invalidation(tmp_path, monkeypatch):
    """#7007 round 6: the reader-side twin of the disk-resurrection race.

    `_load_models_cache_from_disk()` is called BEFORE
    `_available_models_cache_lock` is acquired (perf: "lets concurrent
    requests skip entirely"). The round-5/6 fix stamps every write with its
    build-start generation and rejects a mismatch at READ time, which closes
    the case where the file itself was already stale when it was read.

    It does NOT close this case: the file is perfectly *current* at read
    time (its stamp matches the live generation, so the read legitimately
    succeeds), and only THEN does an invalidation land — bumping the
    generation and deleting the file — while this caller is still between
    its pre-lock read and its in-lock publish. The read-time check cannot
    help here; it already passed, correctly. Without a re-check at
    publication the caller writes that now-superseded snapshot straight into
    `_available_models_cache` with a fresh timestamp, so every subsequent
    reader is served the pre-invalidation catalog (e.g. missing a
    just-authenticated provider) for the full TTL.

    This is the "stale disk preload followed by invalidate before
    publication" interleaving the round-4 re-gate asked for by name.
    Deterministic: the invalidation is fired from inside the stubbed disk
    read, which is exactly the gap under test — no sleeps.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    _isolate_disk_and_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0, raising=False)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cfg, "_invoke_models_rebuild", lambda _builder: _catalog("fresh-rebuild")
    )

    invalidated = []

    def _disk_read_then_invalidate():
        # The read itself is legitimate: at this instant the file's stamped
        # generation matches the live one. The invalidation lands right
        # after it returns and before this caller takes the lock.
        if not invalidated:
            invalidated.append(True)
            cfg.invalidate_models_cache()
        return _catalog("stale-preload")

    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", _disk_read_then_invalidate)

    result = cfg.get_available_models()

    assert invalidated, "the interleaving under test never happened"
    assert result["default_model"] != "stale-preload", (
        "a disk snapshot read before the lock must not be published after an "
        "invalidation superseded it — the read-time generation check already "
        "passed and cannot catch an invalidation that lands afterwards"
    )
    assert result["default_model"] == "fresh-rebuild"
    assert (
        cfg._available_models_cache is None
        or cfg._available_models_cache.get("default_model") != "stale-preload"
    ), "the superseded snapshot must not be resident in the memory cache either"


def test_stale_disk_snapshot_is_rejected_after_restart_when_authority_advanced(tmp_path, monkeypatch):
    """A stale rename that survives a crash must not become valid on restart.

    A build captures durable authority A, invalidation advances it to B, and
    then the stale writer completes its rename before crashing ahead of its
    normal delete-on-lost-ownership cleanup.  A new process must compare the
    persisted snapshot authority to B and reject A; process-local generation
    and process identity cannot establish that relationship after restart.
    """
    import api.config as cfg

    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "_current_webui_version", lambda: "v-test")

    monkeypatch.setattr(cfg, "_available_models_cache_generation", 7, raising=False)
    stale_authority = cfg._models_cache_authority()
    assert stale_authority is not None
    cfg._save_models_cache_to_disk(_catalog("stale"), authority=stale_authority)
    assert cache_path.exists()

    # Invalidation is durable, not merely an in-memory generation increment.
    cfg.invalidate_models_cache()
    cfg._save_models_cache_to_disk(
        _catalog("stale"), generation=7, authority=stale_authority
    )
    assert cache_path.exists(), "simulate stale rename after the invalidation delete"

    # Simulate a new process after the stale writer crashed before cleanup.
    monkeypatch.setattr(cfg, "_PROCESS_RUN_ID", "a-different-process-run", raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_generation", 0, raising=False)
    assert cfg._load_models_cache_from_disk() is None


def test_disk_cache_with_current_durable_authority_survives_restart(tmp_path, monkeypatch):
    """A cache written after invalidation still remains loadable after restart."""
    import api.config as cfg

    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "_current_webui_version", lambda: "v-test")

    cfg.invalidate_models_cache()
    cfg._save_models_cache_to_disk(_catalog("fresh"))
    monkeypatch.setattr(cfg, "_PROCESS_RUN_ID", "a-different-process-run", raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_generation", 0, raising=False)
    loaded = cfg._load_models_cache_from_disk()
    assert loaded is not None
    assert loaded["default_model"] == "fresh"


def test_authority_rotation_failure_removes_old_authority(tmp_path, monkeypatch):
    """An interrupted authority rotation must fail closed after restart."""
    import api.config as cfg

    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    assert cfg._models_cache_authority() is not None

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated full disk")

    monkeypatch.setattr(cfg._paths, "_atomic_write_text", fail_write)
    assert cfg._advance_models_cache_authority() is None
    assert not cfg._models_cache_authority_path().exists()


def test_memory_cache_is_rejected_when_another_process_advances_authority(monkeypatch):
    """A live instance must not serve a warm cache after peer invalidation."""
    import time
    import api.config as cfg

    old_authority = cfg._models_cache_authority()
    assert old_authority is not None
    monkeypatch.setattr(cfg, "_available_models_cache", _catalog("stale"), raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", {"profile": "demo"}, raising=False
    )
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(
        cfg, "_available_models_cache_authority", old_authority, raising=False
    )

    assert cfg._advance_models_cache_authority() is not None
    assert cfg._get_fresh_memory_models_cache(time.monotonic()) is None


def test_external_authority_advance_retires_blocked_stale_build(tmp_path, monkeypatch):
    """A peer's durable invalidation must release this instance's stale owner.

    The builder captures authority A, a different process advances the shared
    sidecar to B, then the local worker completes. It must neither publish A nor
    leave `_cache_build_in_progress` stuck, which would make all later callers
    fail open without ever starting a fresh B rebuild.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    _isolate_disk_and_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)

    release = threading.Event()
    probing = threading.Event()

    def _invoke(_builder):
        probing.set()
        assert release.wait(timeout=10), "test setup failed to release stale build"
        return _catalog("stale-build")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _invoke)

    result = cfg.get_available_models(force_refresh=True)
    assert probing.wait(timeout=5), "build never reached the controlled probe"
    assert cfg._cache_build_in_progress is True
    assert result != _catalog("stale-build")

    assert cfg._advance_models_cache_authority() is not None
    release.set()

    with cfg._cache_build_cv:
        assert cfg._cache_build_cv.wait_for(
            lambda: not cfg._cache_build_in_progress, timeout=10
        ), (
            "a worker retired by another process's authority change must release "
            "its local build owner"
        )
    assert cfg._available_models_cache is None
