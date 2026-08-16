"""Regression coverage for restart-safety run lifecycle reporting."""

import time


def test_health_counts_active_runs_even_when_no_sse_streams():
    """A worker run can outlive its SSE channel; health must expose the run."""
    from api import config, routes

    with config.STREAMS_LOCK:
        config.STREAMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stream-1"] = {
            "stream_id": "stream-1",
            "session_id": "session-1",
            "workspace": "/private/workspace",
            "started_at": time.time() - 42,
            "phase": "running",
        }

    try:
        stream_check = routes._streams_lock_health()
        run_check = routes._run_lifecycle_health()

        assert stream_check["active_streams"] == 0
        assert run_check["active_runs"] == 1
        assert run_check["oldest_run_age_seconds"] >= 40
        run = run_check["runs"][0]
        assert "session_id" not in run
        assert "stream_id" not in run
        assert "workspace" not in run
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_run_registry_unregister_records_last_finished_time():
    """Guards need a grace window after the last real worker exits."""
    from api import config

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.LAST_RUN_FINISHED_AT = None
    config.register_stream_owner("stream-2", "session-2")

    config.register_active_run("stream-2", session_id="session-2", phase="starting")
    with config.ACTIVE_RUNS_LOCK:
        assert "stream-2" in config.ACTIVE_RUNS
    assert config.stream_owner_session_id("stream-2") == "session-2"

    config.unregister_active_run("stream-2")

    with config.ACTIVE_RUNS_LOCK:
        assert "stream-2" not in config.ACTIVE_RUNS
        assert isinstance(config.LAST_RUN_FINISHED_AT, float)
    assert config.stream_owner_session_id("stream-2") is None


def test_active_run_session_snapshot_is_bounded_sanitized_and_deduped():
    from api import config

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update({
            "one": {"session_id": "s1", "started_at": 30, "workspace": "/private"},
            "two": {"session_id": "s1", "started_at": 20, "phase": "secret"},
            "bad": {"session_id": "s2", "started_at": "nan"},
            "ownerless": {"started_at": 10},
            "malformed": "not-a-row",
        })
    try:
        assert config.active_run_session_snapshot() == {"s1": {"started_at": 20.0}}
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_active_run_snapshot_excludes_ephemeral_and_suppressed_helper_sessions():
    from api import config

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    config.register_active_run("ephemeral", session_id="btw-session", ephemeral=True, started_at=10)
    config.suppress_active_run_visibility("background", "parent-session")
    config.register_active_run("background", session_id="bg-session", started_at=11)
    try:
        assert config.active_run_session_snapshot() == {}
    finally:
        config.unregister_active_run("ephemeral")
        config.unregister_active_run("background")
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_session_list_runtime_overlay_adds_active_run_only_to_accepted_rows():
    from api import config, route_session_list_cache as cache

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update({
            "active": {"session_id": "accepted", "started_at": time.time() - 3},
            "archived": {"session_id": "closed", "started_at": time.time() - 2},
        })
    try:
        rows = cache._session_list_cache_overlay_runtime_rows([
            {"session_id": "accepted", "archived": False},
            {"session_id": "closed", "archived": True},
            {"session_id": "absent", "archived": False},
        ])
        by_id = {row["session_id"]: row for row in rows}
        assert set(by_id["accepted"]["active_run"]) == {"started_at", "age_seconds"}
        assert "active_run" not in by_id["closed"]
        assert "active_run" not in by_id["absent"]
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_active_run_annotation_preserves_profile_before_messaging_dedupe_and_rejects_archived():
    from api import config, route_session_list_cache as cache

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["run-a"] = {"session_id": "profile-a", "started_at": time.time() - 1}
    try:
        rows = cache._session_list_cache_overlay_runtime_rows([
            {"session_id": "profile-a", "profile": "a", "archived": False},
            {"session_id": "profile-b", "profile": "b", "archived": True},
        ])
        assert rows[0]["profile"] == "a"
        assert rows[0]["active_run"]["started_at"] > 0
        assert "active_run" not in rows[1]
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
