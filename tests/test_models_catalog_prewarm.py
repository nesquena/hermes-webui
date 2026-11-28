"""Regression coverage for the models-catalog prewarm sweep and stale claim release.

The session-visit path only trusts the disk catalog while it is younger than
``_SESSION_VISIT_MODELS_FRESHNESS_SECONDS``; past that window the visitor paid a
serial per-provider rebuild on the request thread. These tests pin the sweep
that moves that rebuild off the request path, and the release of a single-flight
claim whose builder never released it.
"""

import json
import time

import pytest

import api.config as config


def _fingerprint_for(profile):
    """Fingerprint the way the real writer records it: under the owning profile."""
    from api import profiles

    profiles.set_request_profile(profile)
    try:
        return config._models_cache_source_fingerprint()
    finally:
        profiles.clear_request_profile()


def _write_cache(path, built_at, profile="default"):
    """Write a cache the due check treats as loadable (real fingerprint)."""
    path.write_text(
        json.dumps(
            {
                "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
                "_webui_version": config._current_webui_version(),
                "_built_at": built_at,
                "_source_fingerprint": _fingerprint_for(profile),
                "active_provider": None,
                "default_model": "",
                "configured_model_badges": {},
                "groups": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def prewarm_env(tmp_path, monkeypatch):
    """Isolated cache dir, boot grace bypassed, workers captured not started."""
    base = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", base)
    monkeypatch.setattr(config, "_models_prewarm_booted_at", time.monotonic() - 10_000)
    monkeypatch.setattr(config, "_MODELS_PREWARM_LAST_STARTED", {})
    monkeypatch.setattr(config, "_MODELS_PREWARM_PROBE_CACHE", {})
    monkeypatch.setattr(config, "_MODELS_PREWARM_THREAD", None)
    monkeypatch.setattr(config, "_MODELS_PREWARM_ENABLED", True)

    calls = []
    monkeypatch.setattr(
        config,
        "get_available_models",
        lambda **kw: calls.append(kw) or {"groups": []},
    )

    workers = []

    class _CapturedWorker:
        def is_alive(self):
            return False

    def _fake_start(profiles):
        workers.append(list(profiles))
        return _CapturedWorker()

    monkeypatch.setattr(config, "_start_prewarm_thread", _fake_start)
    return base, calls, workers


def test_fresh_cache_is_not_prewarmed(prewarm_env):
    base, calls, workers = prewarm_env
    _write_cache(base, time.time())

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []
    assert calls == []


def test_cache_near_expiry_is_prewarmed(prewarm_env):
    base, calls, workers = prewarm_env
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(base, time.time() - due_before - 1)

    assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
    assert len(workers) == 1
    assert [profile for profile, _path in workers[0]] == ["default"]

    config._prewarm_profiles(workers[0])
    assert calls == [{"force_refresh": True}]


def test_scheduled_prewarm_is_backed_off(prewarm_env):
    base, calls, workers = prewarm_env
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(base, time.time() - due_before - 1)

    assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
    # Run the worker to completion before the next tick: the injected catalog
    # call publishes nothing, i.e. a persistent failure. The reservation must
    # survive it, otherwise the sweep retries every minute instead of every
    # _MODELS_PREWARM_RETRY_SECONDS.
    config._prewarm_profiles(workers[0])
    assert calls == [{"force_refresh": True}]
    assert config.maybe_prewarm_models_catalog() == {}
    assert len(workers) == 1


def test_coalesced_attempt_releases_the_backoff(prewarm_env, monkeypatch):
    base, _calls, workers = prewarm_env
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(base, time.time() - due_before - 1)

    def _coalesce(**kwargs):
        # A build is still running when the call returns: it publishes on its
        # own, so the next tick should look again rather than wait out the
        # retry reservation.
        config._cache_build_in_progress = True
        return {"groups": []}

    monkeypatch.setattr(config, "get_available_models", _coalesce)

    try:
        assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
        config._prewarm_profiles(workers[0])
        assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
        assert len(workers) == 2
    finally:
        config._cache_build_in_progress = False


def test_isolated_mode_enumerates_only_the_pinned_profile(prewarm_env, monkeypatch):
    """In isolated single-profile mode the resolver ignores set_request_profile,
    so "default" can never be written: enumerating it schedules a rebuild that
    always looks like a failure."""
    from api import profiles

    base, _calls, _workers = prewarm_env
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "_isolated_profile_name", lambda: "personal")

    listed = config._models_cache_profiles()

    assert [name for name, _path in listed] == ["personal"]
    assert listed[0][1].name == "models_cache.personal.json"


def test_named_profile_cache_is_covered(prewarm_env):
    base, _calls, workers = prewarm_env
    _write_cache(base, time.time())
    named = base.parent / "models_cache.personal.json"
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(named, time.time() - due_before - 1, profile="personal")

    assert config.maybe_prewarm_models_catalog() == {"personal": "scheduled"}
    assert len(workers) == 1


def test_boot_grace_suppresses_prewarm(prewarm_env, monkeypatch):
    base, _calls, workers = prewarm_env
    monkeypatch.setattr(config, "_models_prewarm_booted_at", time.monotonic())
    _write_cache(base, time.time() - 10 * config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS)

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []


def test_stale_build_claim_is_released(monkeypatch):
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(
        config,
        "_cache_build_started_at",
        time.monotonic() - config._STALE_BUILD_CLAIM_SECONDS - 1,
    )

    assert config._release_stale_build_claim() is True
    assert config._cache_build_in_progress is False
    assert config._cache_build_started_at == 0.0


def test_live_build_claim_is_kept(monkeypatch):
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(config, "_cache_build_started_at", time.monotonic())

    assert config._release_stale_build_claim() is False
    assert config._cache_build_in_progress is True


def test_claim_without_stamp_is_adopted_then_released(monkeypatch):
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(config, "_cache_build_started_at", 0.0)

    # Adopted with a fresh stamp, not released immediately: an unstamped claim
    # may be a live build whose setter predates the watchdog.
    assert config._release_stale_build_claim() is False
    assert config._cache_build_in_progress is True
    assert config._cache_build_started_at > 0.0

    # Once the adopted stamp ages past the window it is reclaimed.
    monkeypatch.setattr(
        config,
        "_cache_build_started_at",
        time.monotonic() - config._STALE_BUILD_CLAIM_SECONDS - 1,
    )
    assert config._release_stale_build_claim() is True
    assert config._cache_build_in_progress is False


def test_release_does_not_block_on_a_held_lock(monkeypatch):
    """The watchdog runs before the refresh clock is sampled: waiting would move
    that clock past a build that publishes while we wait, and the caller would
    rebuild again instead of reusing it."""
    import threading

    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(
        config,
        "_cache_build_started_at",
        time.monotonic() - config._STALE_BUILD_CLAIM_SECONDS - 1,
    )

    held = threading.Event()
    release = threading.Event()

    def _hold():
        with config._cache_build_cv:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        assert config._release_stale_build_claim() is False
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        holder.join(5)


def test_prewarm_binds_profile_without_mirroring_env(monkeypatch):
    """The sweep binds the caller's TLS only: get_available_models opens the env
    scopes itself, and a second mirrored scope would nest two restores."""
    import os

    from api import profiles

    observed = {}

    def _capture(**kwargs):
        observed["profile"] = profiles.get_active_profile_name()
        observed["env"] = dict(os.environ)
        return {"groups": []}

    monkeypatch.setattr(config, "get_available_models", _capture)
    monkeypatch.setattr(profiles, "_active_profile", "default", raising=False)

    before = dict(os.environ)
    config._prewarm_profile_catalog("personal", config._models_cache_path)

    assert observed["profile"] == "personal"
    assert observed["env"] == before
    # Restored, so a later sweep does not inherit this profile.
    assert profiles.get_active_profile_name() == "default"


def test_due_profiles_run_sequentially_in_one_worker(monkeypatch, tmp_path):
    """Two due profiles must not prewarm in parallel: the rebuild is a
    process-wide single-flight, so the second would give up and serve stale."""
    from api import profiles

    order = []

    def _record(**kwargs):
        order.append(profiles.get_active_profile_name())
        return {"groups": []}

    monkeypatch.setattr(config, "get_available_models", _record)
    monkeypatch.setattr(config, "_cache_build_in_progress", False)

    captured = []

    class _CapturedWorker:
        def is_alive(self):
            return False

    def _fake_start(profiles_list):
        captured.append(list(profiles_list))
        return _CapturedWorker()

    monkeypatch.setattr(config, "_start_prewarm_thread", _fake_start)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_prewarm_booted_at", time.monotonic() - 10_000)
    monkeypatch.setattr(config, "_MODELS_PREWARM_LAST_STARTED", {})
    monkeypatch.setattr(config, "_MODELS_PREWARM_PROBE_CACHE", {})
    monkeypatch.setattr(config, "_MODELS_PREWARM_THREAD", None)
    monkeypatch.setattr(config, "_MODELS_PREWARM_ENABLED", True)

    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(tmp_path / "models_cache.json", time.time() - due_before - 1)
    _write_cache(
        tmp_path / "models_cache.personal.json", time.time() - due_before - 1, profile="personal"
    )

    report = config.maybe_prewarm_models_catalog()
    assert report == {"default": "scheduled", "personal": "scheduled"}
    assert len(captured) == 1  # single worker for both profiles

    config._prewarm_profiles(captured[0])
    assert order == ["default", "personal"]


def test_wait_for_catalog_idle(monkeypatch):
    import threading

    monkeypatch.setattr(config, "_cache_build_in_progress", False)
    assert config._wait_for_catalog_idle(1.0) is True

    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    held = threading.Event()
    release = threading.Event()

    def _hold():
        with config._cache_build_cv:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        assert config._wait_for_catalog_idle(0.3) is False
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        holder.join(5)


def test_build_admission_stamps_start_time(tmp_path, monkeypatch):
    """The cold path must publish its start time to the module, otherwise every
    claim looks unstamped and the watchdog can only date it from a later caller."""
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 5.0)
    monkeypatch.setattr(config, "_cache_build_in_progress", False)
    monkeypatch.setattr(config, "_cache_build_started_at", 0.0)
    monkeypatch.setattr(config, "_available_models_cache", None)
    monkeypatch.setattr(config, "_available_models_cache_ts", 0.0)
    monkeypatch.setattr(config, "_available_models_cache_source_fingerprint", None)

    observed = {}
    real_allocate = config._allocate_models_rebuild_seq

    def _spy_allocate():
        # Sampled one statement after the claim is taken.
        observed["stamp"] = config._cache_build_started_at
        return real_allocate()

    monkeypatch.setattr(config, "_allocate_models_rebuild_seq", _spy_allocate)
    monkeypatch.setattr(
        config,
        "_invoke_models_rebuild",
        lambda build: {
            "active_provider": None,
            "default_model": "",
            "configured_model_badges": {},
            "groups": [],
        },
    )

    config.get_available_models(force_refresh=True)

    assert observed.get("stamp", 0.0) > 0.0


def test_disabled_by_env_flag(prewarm_env, monkeypatch):
    base, _calls, workers = prewarm_env
    monkeypatch.setattr(config, "_MODELS_PREWARM_ENABLED", False)
    _write_cache(base, time.time() - 10_000)

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []


def test_sync_mode_skips_the_sweep(prewarm_env, monkeypatch):
    base, _calls, workers = prewarm_env
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0)
    _write_cache(base, time.time() - 10_000)

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []


def test_unloadable_young_cache_is_due(prewarm_env):
    base, _calls, workers = prewarm_env
    _write_cache(base, time.time())
    raw = json.loads(base.read_text(encoding="utf-8"))
    raw["_source_fingerprint"] = {"stale": "fingerprint"}
    base.write_text(json.dumps(raw), encoding="utf-8")

    # Young file, unusable catalog: the visitor would still pay a cold build.
    assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
    assert len(workers) == 1


def test_missing_default_cache_is_swept_while_named_cache_is_fresh(prewarm_env):
    base, _calls, workers = prewarm_env
    named = base.parent / "models_cache.personal.json"
    _write_cache(named, time.time(), profile="personal")
    assert not base.exists()

    # A glob of existing files alone would miss the default profile entirely,
    # and its file is what a config save deletes.
    assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
    assert len(workers) == 1


def test_worker_busy_skips_scheduling(prewarm_env, monkeypatch):
    base, _calls, workers = prewarm_env

    class _Alive:
        def is_alive(self):
            return True

    monkeypatch.setattr(config, "_MODELS_PREWARM_THREAD", _Alive())
    _write_cache(base, time.time() - 10_000)

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []


def test_claim_with_a_live_owner_is_not_reclaimed(monkeypatch):
    class _Alive:
        def is_alive(self):
            return True

    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(config, "_cache_build_started_at", time.monotonic() - 10_000)
    monkeypatch.setattr(config, "_cache_build_owner", _Alive())

    # An out-of-band worker can outlive the budget; age alone must not reclaim it.
    assert config._release_stale_build_claim() is False
    assert config._cache_build_in_progress is True


def test_due_probe_validates_under_the_owning_profile(prewarm_env, monkeypatch):
    """The maintenance thread has no request-profile binding, and
    _is_loadable_disk_cache() compares against the ACTIVE profile's fingerprint.
    The probe must bind the owning profile and clear it again."""
    from api import profiles

    base, _calls, _workers = prewarm_env
    named = base.parent / "models_cache.personal.json"
    _write_cache(named, time.time(), profile="personal")
    profiles.clear_request_profile()

    seen = []
    real = config._is_loadable_disk_cache

    def _spy(payload):
        seen.append(profiles.get_active_profile_name())
        return real(payload)

    monkeypatch.setattr(config, "_is_loadable_disk_cache", _spy)
    config._models_cache_due_probe(named, time.time(), "personal")

    assert seen == ["personal"]
    assert profiles.get_active_profile_name() == "default"  # restored


def test_fresh_named_snapshot_is_not_due_without_request_context(prewarm_env):
    """End to end through the real fingerprint producer, writer path and
    validator: a named snapshot written under its own profile must not look
    unloadable to the maintenance thread, which carries no binding."""
    from api import profiles

    base, _calls, workers = prewarm_env
    _write_cache(base, time.time())
    named = base.parent / "models_cache.personal.json"
    _write_cache(named, time.time(), profile="personal")
    profiles.clear_request_profile()

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []


def test_stale_named_snapshot_is_still_due_without_request_context(prewarm_env):
    """Positive control for the test above: same wiring, aged snapshot."""
    from api import profiles

    base, _calls, workers = prewarm_env
    _write_cache(base, time.time())
    named = base.parent / "models_cache.personal.json"
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(named, time.time() - due_before - 1, profile="personal")
    profiles.clear_request_profile()

    assert config.maybe_prewarm_models_catalog() == {"personal": "scheduled"}
    assert len(workers) == 1
