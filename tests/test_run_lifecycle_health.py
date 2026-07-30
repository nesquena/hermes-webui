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
