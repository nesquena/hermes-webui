from __future__ import annotations

import threading
import time

import api.models as models
import api.profiles as profiles


def _set_active_streams(monkeypatch, ids):
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set(ids))


def test_cli_cache_key_stays_frozen_during_streaming(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_text("", encoding="utf-8")

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        models,
        "_default_claude_code_projects_dir",
        lambda: tmp_path / "projects",
    )

    fp = {"value": 0}
    monkeypatch.setattr(
        models,
        "_sqlite_file_stat_cache_key",
        lambda _p: ("fp", fp["value"]),
    )

    _set_active_streams(monkeypatch, {"live-1"})
    _, _, _, key_streaming_a = models._resolve_cli_sessions_context(None)
    fp["value"] = 1
    _, _, _, key_streaming_b = models._resolve_cli_sessions_context(None)
    fp["value"] = 2
    _, _, _, key_streaming_c = models._resolve_cli_sessions_context(None)

    assert key_streaming_a == key_streaming_b == key_streaming_c

    _set_active_streams(monkeypatch, set())
    fp["value"] = 10
    _, _, _, key_idle_a = models._resolve_cli_sessions_context(None)
    fp["value"] = 11
    _, _, _, key_idle_b = models._resolve_cli_sessions_context(None)

    assert key_idle_a != key_idle_b


def test_get_cli_sessions_follower_reuses_stale_rows_during_slow_rebuild(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    (_, _, _, cache_key) = models._resolve_cli_sessions_context(None)
    cache_stamp = models._cli_sessions_cache_invalidation_stamp()
    with models._CLI_SESSIONS_CACHE_LOCK:
        models._CLI_SESSIONS_CACHE[cache_key] = (
            time.monotonic() - 1.0,
            cache_stamp,
            [{"session_id": "stale", "title": "stale-row"}],
        )

    started = threading.Event()
    owner_block = threading.Event()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        started.set()
        owner_block.wait()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    def _owner():
        results["owner"] = models.get_cli_sessions()

    def _follower():
        results["follower"] = models.get_cli_sessions()

    owner = threading.Thread(target=_owner, daemon=True)
    follower = threading.Thread(target=_follower, daemon=True)

    owner.start()
    assert started.wait(1.0), "owner did not start"
    follower.start()
    follower.join(1.0)

    assert results.get("follower") == [{"session_id": "stale", "title": "stale-row"}]
    assert not follower.is_alive()
    owner_block.set()
    owner.join(2.0)
    assert not owner.is_alive()

    assert results.get("owner") == [{"session_id": "fresh", "title": "fresh-row"}]


def test_get_cli_sessions_cold_followers_join_single_rebuild(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)

    owner_started = threading.Event()
    owner_block = threading.Event()
    load_count = {"value": 0}
    load_count_lock = threading.Lock()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        with load_count_lock:
            load_count["value"] += 1
            call_number = load_count["value"]
        if call_number == 1:
            owner_started.set()
            owner_block.wait()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    def _owner():
        results["owner"] = models.get_cli_sessions()

    def _follower():
        results["follower"] = models.get_cli_sessions()

    owner = threading.Thread(target=_owner, daemon=True)
    follower = threading.Thread(target=_follower, daemon=True)

    owner.start()
    assert owner_started.wait(1.0), "owner did not start"
    follower.start()
    time.sleep(0.2)

    assert load_count["value"] == 1
    assert follower.is_alive()

    owner_block.set()
    owner.join(2.0)
    follower.join(2.0)

    assert not owner.is_alive()
    assert not follower.is_alive()
    assert load_count["value"] == 1
    assert results.get("owner") == [{"session_id": "fresh", "title": "fresh-row"}]
    assert results.get("follower") == [{"session_id": "fresh", "title": "fresh-row"}]


def test_get_cli_sessions_cold_follower_times_out_to_independent_rebuild(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_WAIT_SECONDS", 0.05, raising=False)

    owner_started = threading.Event()
    owner_block = threading.Event()
    fallback_started = threading.Event()
    load_count = {"value": 0}
    load_count_lock = threading.Lock()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        with load_count_lock:
            load_count["value"] += 1
            call_number = load_count["value"]
        if call_number == 1:
            owner_started.set()
            owner_block.wait()
        else:
            fallback_started.set()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    owner = threading.Thread(
        target=lambda: results.setdefault("owner", models.get_cli_sessions()),
        daemon=True,
    )
    follower = threading.Thread(
        target=lambda: results.setdefault("follower", models.get_cli_sessions()),
        daemon=True,
    )

    owner.start()
    assert owner_started.wait(1.0), "owner did not start"
    follower.start()
    assert fallback_started.wait(1.0), "follower did not start fallback rebuild"
    follower.join(1.0)

    assert not follower.is_alive()
    assert owner.is_alive()
    assert load_count["value"] == 2
    assert results.get("follower") == [{"session_id": "fresh", "title": "fresh-row"}]

    owner_block.set()
    owner.join(2.0)
    assert not owner.is_alive()
    assert results.get("owner") == [{"session_id": "fresh", "title": "fresh-row"}]


def test_get_cli_sessions_clear_during_rebuild_does_not_restore_stale_rows(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)

    owner_started = threading.Event()
    owner_block = threading.Event()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        owner_started.set()
        owner_block.wait()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    _, _, _, cache_key = models._resolve_cli_sessions_context(None)

    owner = threading.Thread(
        target=lambda: results.setdefault("owner", models.get_cli_sessions()),
        daemon=True,
    )
    owner.start()

    assert owner_started.wait(1.0), "owner did not enter rebuild"
    models.clear_cli_sessions_cache()

    owner_block.set()
    owner.join(1.0)

    assert results.get("owner") == [{"session_id": "fresh", "title": "fresh-row"}]
    with models._CLI_SESSIONS_CACHE_LOCK:
        assert cache_key not in models._CLI_SESSIONS_CACHE

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", lambda *_args, **_kwargs: [{"session_id": "recovered", "title": "recovered-row"}])
    recovered = models.get_cli_sessions()
    assert recovered == [{"session_id": "recovered", "title": "recovered-row"}]
    with models._CLI_SESSIONS_CACHE_LOCK:
        assert cache_key in models._CLI_SESSIONS_CACHE


def test_get_cli_sessions_clear_during_rebuild_preserves_joiners(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)

    owner_started = threading.Event()
    owner_block = threading.Event()
    second_started = threading.Event()
    second_block = threading.Event()
    load_count = {"value": 0}
    load_count_lock = threading.Lock()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        with load_count_lock:
            load_count["value"] += 1
            call_number = load_count["value"]
        if call_number == 1:
            owner_started.set()
            owner_block.wait()
            return [{"session_id": "owner", "title": "owner-row"}]
        second_started.set()
        second_block.wait()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    def _owner():
        results["owner"] = models.get_cli_sessions()

    def _follower(name):
        results[name] = models.get_cli_sessions()

    owner = threading.Thread(target=_owner, daemon=True)
    follower_a = threading.Thread(target=lambda: _follower("follower_a"), daemon=True)
    follower_b = threading.Thread(target=lambda: _follower("follower_b"), daemon=True)

    owner.start()
    assert owner_started.wait(1.0), "owner did not enter rebuild"
    follower_a.start()
    models.clear_cli_sessions_cache()
    follower_b.start()
    time.sleep(0.2)

    assert load_count["value"] == 1
    assert follower_a.is_alive()
    assert follower_b.is_alive()

    owner_block.set()
    assert second_started.wait(1.0), "second rebuild did not start"
    time.sleep(0.2)

    assert load_count["value"] == 2
    second_block.set()

    owner.join(2.0)
    follower_a.join(2.0)
    follower_b.join(2.0)

    assert not owner.is_alive()
    assert not follower_a.is_alive()
    assert not follower_b.is_alive()
    assert load_count["value"] == 2
    assert results.get("owner") == [{"session_id": "owner", "title": "owner-row"}]
    assert results.get("follower_a") == [{"session_id": "fresh", "title": "fresh-row"}]
    assert results.get("follower_b") == [{"session_id": "fresh", "title": "fresh-row"}]


def test_get_cli_sessions_clear_during_rebuild_reclaims_after_invalidated_wait(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS", 0.2, raising=False)

    (_, _, _, cache_key) = models._resolve_cli_sessions_context(None)
    cache_stamp = models._cli_sessions_cache_invalidation_stamp()
    with models._CLI_SESSIONS_CACHE_LOCK:
        models._CLI_SESSIONS_CACHE[cache_key] = (
            time.monotonic() - 1.0,
            cache_stamp,
            [{"session_id": "stale", "title": "stale-row"}],
        )

    owner_started = threading.Event()
    owner_block = threading.Event()
    second_started = threading.Event()
    load_count = {"value": 0}
    load_count_lock = threading.Lock()
    results = {}

    def _blocking_loader(*_args, **_kwargs):
        with load_count_lock:
            load_count["value"] += 1
            call_number = load_count["value"]
        if call_number == 1:
            owner_started.set()
            owner_block.wait()
            return [{"session_id": "owner", "title": "owner-row"}]
        second_started.set()
        return [{"session_id": "fresh", "title": "fresh-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _blocking_loader)

    owner = threading.Thread(
        target=lambda: results.setdefault("owner", models.get_cli_sessions()),
        daemon=True,
    )
    follower = threading.Thread(
        target=lambda: results.setdefault("follower", models.get_cli_sessions()),
        daemon=True,
    )

    owner.start()
    assert owner_started.wait(1.0), "owner did not enter rebuild"
    follower.start()
    time.sleep(0.05)
    models.clear_cli_sessions_cache()
    owner_block.set()

    assert second_started.wait(1.0), "follower did not reclaim the rebuild"
    owner.join(2.0)
    follower.join(2.0)

    assert not owner.is_alive()
    assert not follower.is_alive()
    assert load_count["value"] == 2
    assert results.get("owner") == [{"session_id": "owner", "title": "owner-row"}]
    assert results.get("follower") == [{"session_id": "fresh", "title": "fresh-row"}]


def test_cache_cli_sessions_if_current_skips_stale_store(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)

    _, _, _, cache_key = models._resolve_cli_sessions_context(None)
    invalidation_stamp = models._cli_sessions_cache_invalidation_stamp()

    models.clear_cli_sessions_cache()

    stored = models._cache_cli_sessions_if_current(
        cache_key,
        60.0,
        invalidation_stamp,
        [{"session_id": "stale", "title": "stale-row"}],
    )

    assert stored is False
    with models._CLI_SESSIONS_CACHE_LOCK:
        assert cache_key not in models._CLI_SESSIONS_CACHE
