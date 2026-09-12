"""Behavioral coverage for Workstream C latency boundaries."""

import re
import sys
import threading
import time
import types

import pytest

import api.profiles as profiles
import api.routes as routes


@pytest.fixture(autouse=True)
def _clean_workstream_c_caches():
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()
    with profiles._PROFILE_EXPENSIVE_METADATA_LOCK:
        profiles._PROFILE_EXPENSIVE_METADATA_CACHE.clear()
        profiles._PROFILE_EXPENSIVE_METADATA_INFLIGHT.clear()
        profiles._PROFILE_EXPENSIVE_METADATA_PROFILE_VERSION.clear()
        profiles._PROFILE_EXPENSIVE_METADATA_GLOBAL_VERSION = 0
    profiles._LIST_PROFILES_CACHE = None
    yield
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()
    with profiles._PROFILE_EXPENSIVE_METADATA_LOCK:
        profiles._PROFILE_EXPENSIVE_METADATA_CACHE.clear()
        profiles._PROFILE_EXPENSIVE_METADATA_INFLIGHT.clear()


def _session_key(profile="default", *, all_profiles=False):
    return routes._session_list_cache_key(
        active_profile=profile,
        all_profiles=all_profiles,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )


def _payload(marker, *, all_profiles=False):
    return {
        "sessions": [{"session_id": marker}],
        "cli_count": 0,
        "all_profiles": all_profiles,
        "active_profile": "default",
        "other_profile_count": 0,
    }


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _age_session_cache_entry(key, seconds):
    with routes._SESSIONS_CACHE_LOCK:
        timestamp, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (timestamp - seconds, stamp, payload)


def test_stale_source_snapshot_is_singleflight_for_concurrent_callers(monkeypatch):
    source = ["before"]
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: (source[0],))
    key = _session_key()
    stale = _payload("last-known")
    routes._session_list_cache_set(key, stale)
    source[0] = "after"

    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def builder():
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        release.wait(2.0)
        return _payload("rebuilt")

    n_callers = 12
    barrier = threading.Barrier(n_callers)
    results = []
    errors = []
    result_lock = threading.Lock()

    def caller():
        try:
            barrier.wait()
            value = routes._get_cached_session_list_payload(key=key, builder=builder)
            with result_lock:
                results.append(value)
        except Exception as exc:  # pragma: no cover - makes thread failures visible
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(n_callers)]
    try:
        for thread in threads:
            thread.start()
        assert started.wait(1.0)
        for thread in threads:
            thread.join(1.0)
            assert not thread.is_alive()
        assert errors == []
        assert results == [stale] * n_callers
        assert calls == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(2.0)

    assert _wait_until(
        lambda: routes._session_list_cache_get(key, allow_stale=True)[0]
        == _payload("rebuilt")
    )


def test_profile_invalidation_marks_scoped_snapshot_stale_without_eviction(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key_default = _session_key("default")
    key_other = _session_key("other")
    key_all = _session_key("default", all_profiles=True)
    routes._session_list_cache_set(key_default, _payload("default"))
    routes._session_list_cache_set(key_other, _payload("other"))
    routes._session_list_cache_set(key_all, _payload("all", all_profiles=True))

    routes._session_list_cache_invalidate("default")

    cached_default, fresh_default = routes._session_list_cache_get(
        key_default, allow_stale=True
    )
    cached_other, fresh_other = routes._session_list_cache_get(
        key_other, allow_stale=True
    )
    cached_all, fresh_all = routes._session_list_cache_get(key_all, allow_stale=True)
    assert cached_default == _payload("default")
    assert fresh_default is False
    assert cached_other == _payload("other")
    assert fresh_other is True
    assert cached_all == _payload("all", all_profiles=True)
    assert fresh_all is False

    started = threading.Event()
    release = threading.Event()

    def builder():
        started.set()
        release.wait(2.0)
        return _payload("default-rebuilt")

    begin = time.monotonic()
    returned = routes._get_cached_session_list_payload(key=key_default, builder=builder)
    elapsed = time.monotonic() - begin
    try:
        assert returned == _payload("default")
        assert elapsed < 0.15
        assert started.wait(1.0)
    finally:
        release.set()

    assert _wait_until(
        lambda: routes._session_list_cache_get(key_default, allow_stale=True)[0]
        == _payload("default-rebuilt")
    )


def test_three_large_stale_sessions_return_before_historical_rebuild(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key = _session_key()
    snapshot = {
        "sessions": [
            {"session_id": f"large-{index}", "title": "x" * 80_000}
            for index in range(3)
        ],
        "cli_count": 3,
    }
    routes._session_list_cache_set(key, snapshot)
    _age_session_cache_entry(key, routes._SESSIONS_CACHE_TTL_SECONDS + 1.0)

    started = threading.Event()
    release = threading.Event()

    def builder():
        started.set()
        release.wait(2.0)
        return {**snapshot, "sessions": [{"session_id": "fresh"}]}

    begin = time.monotonic()
    returned = routes._get_cached_session_list_payload(key=key, builder=builder)
    elapsed = time.monotonic() - begin
    try:
        assert returned == snapshot
        assert elapsed < 0.15
        assert started.wait(1.0)
    finally:
        release.set()


def test_active_run_overlay_refreshes_cached_rows_without_projection(monkeypatch):
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"stream-live"})
    monkeypatch.setattr(
        routes,
        "ACTIVE_RUNS",
        {"stream-live": {"session_id": "sid-1", "phase": "running"}},
    )
    monkeypatch.setattr(routes, "ACTIVE_RUNS_LOCK", threading.Lock())
    rows = [
        {"session_id": "sid-1", "updated_at": 1},
        {"session_id": "sid-2", "updated_at": 2},
        {"session_id": "sid-3", "updated_at": 3},
    ]

    overlaid = routes._session_list_cache_overlay_runtime_rows(rows)

    by_id = {row["session_id"]: row for row in overlaid}
    assert by_id["sid-1"]["active_stream_id"] == "stream-live"
    assert by_id["sid-1"]["is_streaming"] is True


def test_fast_profile_rows_do_not_run_gateway_or_skill_probes(monkeypatch, tmp_path):
    default_home = tmp_path / "default"
    profiles_root = tmp_path / "profiles"
    default_home.mkdir()
    profiles_root.mkdir()
    fake_cli = types.ModuleType("hermes_cli")
    fake_cli_profiles = types.ModuleType("hermes_cli.profiles")
    fake_cli.profiles = fake_cli_profiles
    fake_cli_profiles._get_default_hermes_home = lambda: default_home
    fake_cli_profiles._get_profiles_root = lambda: profiles_root
    fake_cli_profiles._read_config_model = lambda _home: ("model", "provider")
    fake_cli_profiles._PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    gateway_called = threading.Event()
    stats_called = threading.Event()
    release = threading.Event()

    def slow_gateway(_home):
        gateway_called.set()
        release.wait(1.0)
        return True

    def slow_stats(_home):
        stats_called.set()
        release.wait(1.0)
        return (9, 10)

    fake_cli_profiles._check_gateway_running = slow_gateway
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_cli_profiles)
    monkeypatch.setattr(profiles, "_get_profile_skills_stats", slow_stats)

    finished = threading.Event()
    result = {}

    def build():
        try:
            result["rows"] = profiles._build_profile_rows_fast()
        finally:
            finished.set()

    thread = threading.Thread(target=build)
    thread.start()
    try:
        assert finished.wait(0.25), "cheap profile discovery must not wait on probes"
        assert not gateway_called.is_set()
        assert not stats_called.is_set()
        assert result["rows"][0]["name"] == "default"
    finally:
        release.set()
        thread.join(1.0)


def test_profile_expensive_metadata_is_bounded_and_singleflight(monkeypatch, tmp_path):
    rows = [
        {"name": f"profile-{index}", "path": str(tmp_path / f"p-{index}")}
        for index in range(3)
    ]
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-0")
    monkeypatch.setattr(profiles, "_build_profile_rows_fast", lambda: rows)
    started = threading.Event()
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def slow_details(path):
        with calls_lock:
            calls.append(path)
        started.set()
        release.wait(2.0)
        return {
            "gateway_running": True,
            "skill_count": 7,
            "enabled_skills": 7,
            "total_skills": 8,
        }

    monkeypatch.setattr(profiles, "_compute_profile_expensive_metadata", slow_details)
    n_callers = 8
    barrier = threading.Barrier(n_callers)
    results = []
    result_lock = threading.Lock()

    def caller():
        barrier.wait()
        value = profiles.list_profiles_api()
        with result_lock:
            results.append(value)

    threads = [threading.Thread(target=caller) for _ in range(n_callers)]
    begin = time.monotonic()
    try:
        for thread in threads:
            thread.start()
        assert started.wait(1.0)
        for thread in threads:
            thread.join(1.0)
            assert not thread.is_alive()
        elapsed = time.monotonic() - begin
        assert elapsed < 0.5
        assert len(results) == n_callers
        assert len(calls) == 3
        assert all(row["skill_count"] == 0 for result in results for row in result)
    finally:
        release.set()
        for thread in threads:
            thread.join(2.0)

    assert _wait_until(
        lambda: all(
            profiles._profile_expensive_metadata_cache_get(
                profiles._profile_expensive_metadata_path(row["path"])
            )
            for row in rows
        )
    )
    refreshed = profiles.list_profiles_api()
    assert all(row["skill_count"] == 7 for row in refreshed)
    assert len(calls) == 3


def test_profile_metadata_ttl_stale_while_revalidate(monkeypatch, tmp_path):
    profile_path = profiles._profile_expensive_metadata_path(tmp_path / "profile")
    calls = []
    second_started = threading.Event()
    second_release = threading.Event()

    def details(_path):
        calls.append(1)
        if len(calls) == 1:
            return {
                "gateway_running": False,
                "skill_count": 1,
                "enabled_skills": 1,
                "total_skills": 2,
            }
        second_started.set()
        second_release.wait(2.0)
        return {
            "gateway_running": True,
            "skill_count": 2,
            "enabled_skills": 2,
            "total_skills": 3,
        }

    monkeypatch.setattr(profiles, "_compute_profile_expensive_metadata", details)
    _initial, initial_event = profiles._request_profile_expensive_metadata(
        profile_path,
        {"gateway_running": False, "skill_count": 0, "enabled_skills": 0, "total_skills": 0},
    )
    assert initial_event is not None
    assert initial_event.wait(1.0)
    assert _wait_until(
        lambda: profiles._profile_expensive_metadata_cache_get(profile_path)
        == {
            "gateway_running": False,
            "skill_count": 1,
            "enabled_skills": 1,
            "total_skills": 2,
        }
    )

    with profiles._PROFILE_EXPENSIVE_METADATA_LOCK:
        expiry, stamp, cached = profiles._PROFILE_EXPENSIVE_METADATA_CACHE[profile_path]
        profiles._PROFILE_EXPENSIVE_METADATA_CACHE[profile_path] = (
            time.monotonic() - 1.0,
            stamp,
            cached,
        )

    begin = time.monotonic()
    stale, refresh_event = profiles._request_profile_expensive_metadata(
        profile_path,
        {"gateway_running": False, "skill_count": 0, "enabled_skills": 0, "total_skills": 0},
    )
    elapsed = time.monotonic() - begin
    try:
        assert stale["skill_count"] == 1
        assert elapsed < 0.15
        assert refresh_event is not None
        assert second_started.wait(1.0)
    finally:
        second_release.set()
        if refresh_event is not None:
            refresh_event.wait(2.0)

    assert _wait_until(
        lambda: profiles._profile_expensive_metadata_cache_get(profile_path)
        == {
            "gateway_running": True,
            "skill_count": 2,
            "enabled_skills": 2,
            "total_skills": 3,
        }
    )
    assert len(calls) == 2
