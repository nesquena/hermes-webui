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


def test_active_run_visibility_snapshot_scopes_dedupes_and_uses_metadata_only_lookup(monkeypatch):
    from api import config, routes

    class _FakeSession:
        def __init__(self, row):
            self._row = dict(row)

        def compact(self):
            return dict(self._row)

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "all_sessions", lambda: (_ for _ in ()).throw(AssertionError("all_sessions should not be called")))
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": True,
        "show_claude_code_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(routes, "_session_list_cache_get", lambda key, allow_stale=False: (None, False))
    seen = []
    sessions = {
        "session-1": {"session_id": "session-1", "profile": "default", "raw_source": "webui", "project_id": "p1"},
        "session-2": {"session_id": "session-2", "profile": "default", "raw_source": "cli", "project_id": "p1"},
        "other": {"session_id": "other", "profile": "other", "raw_source": "webui", "project_id": "p1"},
    }

    def _fake_get_session(sid, metadata_only=False):
        seen.append((sid, metadata_only))
        row = sessions.get(sid)
        return _FakeSession(row) if row else None

    monkeypatch.setattr(routes, "get_session", _fake_get_session)
    now = time.time()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update({
            "stream-1": {"stream_id": "stream-1", "session_id": "session-1", "started_at": now - 10, "phase": "running", "workspace": "/secret"},
            "stream-2": {"stream_id": "stream-2", "session_id": "session-1", "started_at": now - 20, "phase": "thinking", "run_id": "secret"},
            "stream-3": {"stream_id": "stream-3", "session_id": "session-2", "started_at": now - 30, "phase": "running"},
            "stream-4": {"stream_id": "stream-4", "session_id": "other", "started_at": now - 40, "phase": "running"},
        })
    try:
        payload = routes._active_run_visibility_snapshot(sidebar_source="webui", project_id="p1")
        assert payload["active_runs"] == 1
        assert payload["runs"][0]["session_id"] == "session-1"
        assert payload["runs"][0]["phase"] == "thinking"
        assert set(payload["runs"][0]) == {"session_id", "phase", "started_at", "age_seconds"}
        assert routes._active_run_visibility_snapshot(project_id="p1")["active_runs"] == 1
        assert routes._active_run_visibility_snapshot(sidebar_source="bogus", project_id="p1")["active_runs"] == 1
        assert routes._active_run_visibility_snapshot(sidebar_source="cli")["active_runs"] == 1
        assert seen
        assert all(metadata_only is True for _sid, metadata_only in seen)
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_active_run_visibility_snapshot_uses_sidebar_cache_when_available(monkeypatch):
    from api import config, routes

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": True,
        "show_claude_code_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(
        routes,
        "_session_list_cache_get",
        lambda key, allow_stale=False: ({
            "sessions": [
                {"session_id": "session-1", "project_id": "p1"},
            ]
        }, False),
    )
    monkeypatch.setattr(routes, "get_session", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("get_session should not be called on cache hits")))
    monkeypatch.setattr(routes, "all_sessions", lambda: (_ for _ in ()).throw(AssertionError("all_sessions should not be called")))
    now = time.time()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update({
            "stream-1": {"stream_id": "stream-1", "session_id": "session-1", "started_at": now - 20, "phase": "thinking"},
            "stream-2": {"stream_id": "stream-2", "session_id": "session-2", "started_at": now - 10, "phase": "running"},
        })
    try:
        payload = routes._active_run_visibility_snapshot(sidebar_source="webui", project_id="p1")
        assert payload["active_runs"] == 1
        assert payload["runs"][0]["session_id"] == "session-1"
        assert payload["runs"][0]["phase"] == "thinking"
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_active_run_visibility_snapshot_short_circuits_empty_and_caps_metadata_lookups(monkeypatch):
    from api import config, routes

    monkeypatch.setattr(routes, "load_settings", lambda: (_ for _ in ()).throw(AssertionError("load_settings should not run when there are no active runs")))
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    assert routes._active_run_visibility_snapshot() == {
        "active_runs": 0,
        "runs": [],
        "oldest_run_age_seconds": None,
    }

    class _FakeSession:
        def __init__(self, sid):
            self._sid = sid

        def compact(self):
            return {
                "session_id": self._sid,
                "profile": "default",
                "raw_source": "webui",
                "project_id": "p1",
            }

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": True,
        "show_claude_code_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(routes, "_session_list_cache_get", lambda key, allow_stale=False: (None, False))
    monkeypatch.setattr(routes, "all_sessions", lambda: (_ for _ in ()).throw(AssertionError("all_sessions should not be called")))
    seen = []

    def _fake_get_session(sid, metadata_only=False):
        seen.append((sid, metadata_only))
        return _FakeSession(sid)

    monkeypatch.setattr(routes, "get_session", _fake_get_session)
    now = time.time()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        for idx in range(101):
            sid = f"session-{idx:03d}"
            config.ACTIVE_RUNS[f"stream-{idx:03d}"] = {
                "stream_id": f"stream-{idx:03d}",
                "session_id": sid,
                "started_at": now - idx,
                "phase": "running",
            }
    try:
        payload = routes._active_run_visibility_snapshot(project_id="p1")
        assert payload["active_runs"] == 100
        assert len(payload["runs"]) == 100
        assert len(seen) == 100
        assert all(metadata_only is True for _sid, metadata_only in seen)
        assert [run["session_id"] for run in payload["runs"]] == [sid for sid, _metadata_only in seen]
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
