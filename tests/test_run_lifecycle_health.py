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
            for key, value in row.items():
                setattr(self, key, value)
            self.message_count = 1

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
    monkeypatch.setattr(routes, "_session_list_cache_get_with_reason", lambda key, allow_stale=False: (None, False, None))
    seen = []
    sessions = {
        "session-1": {"session_id": "session-1", "profile": "default", "raw_source": "webui", "project_id": "p1"},
        "session-2": {"session_id": "session-2", "profile": "default", "raw_source": "cli", "project_id": "p1", "actual_message_count": 1, "actual_user_message_count": 2},
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
        "show_cli_sessions": False,
        "show_claude_code_sessions": False,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(
        routes,
        "_session_list_cache_get_with_reason",
        lambda key, allow_stale=False: ({
            "sessions": [
                {"session_id": "session-1", "project_id": "p1"},
            ]
        }, False, "age"),
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
            self.session_id = sid
            self.profile = "default"
            self.raw_source = "webui"
            self.project_id = "p1"
            self.message_count = 1

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": False,
        "show_claude_code_sessions": False,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(routes, "_session_list_cache_get_with_reason", lambda key, allow_stale=False: (None, False, None))
    monkeypatch.setattr(routes, "all_sessions", lambda: (_ for _ in ()).throw(AssertionError("all_sessions should not be called")))
    seen = []

    def _fake_get_session(sid, metadata_only=False):
        seen.append((sid, metadata_only))
        idx = int(sid.rsplit('-', 1)[-1])
        mode = idx % 9
        if mode == 0:
            return None
        session = _FakeSession(sid)
        if mode == 1:
            session.profile = "other"
        elif mode == 2:
            session.raw_source = "cli"
            session.is_cli_session = True
        elif mode == 3:
            session.project_id = "p2"
        elif mode == 4:
            session.default_hidden = True
        elif mode == 5:
            session.message_count = 0
            session.actual_message_count = 0
            session.user_message_count = 0
            session.actual_user_message_count = 0
            session.attention = None
            session.is_streaming = False
            session.active_stream_id = None
            session.pending_user_message = False
            session.has_pending_user_message = False
        elif mode == 6:
            session.raw_source = "cron"
            session.source = "cron"
            session.message_count = 0
        elif mode == 7:
            session.raw_source = "webhook"
            session.source = "webhook"
            session.message_count = 0
        else:
            session.raw_source = "claude_code"
            session.source = "claude_code"
            session.is_cli_session = True
        return session

    monkeypatch.setattr(routes, "get_session", _fake_get_session)
    now = time.time()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        for idx in range(101):
            sid = f"session-{idx:03d}"
            config.ACTIVE_RUNS[f"stream-{idx:03d}"] = {
                "stream_id": f"stream-{idx:03d}",
                "session_id": sid,
                "started_at": now - (100 - idx),
                "phase": "running",
            }
    try:
        payload = routes._active_run_visibility_snapshot(
            sidebar_source="webui",
            project_id="p1",
            exclude_hidden=True,
        )
        assert payload["active_runs"] == 0
        assert len(payload["runs"]) == 0
        assert len(seen) == 100
        assert all(metadata_only is True for _sid, metadata_only in seen)
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_session_list_cache_get_with_reason_reports_source_staleness_atomically(monkeypatch):
    from api import route_session_list_cache as cache

    key = ("active-run-key",)
    with cache._SESSIONS_CACHE_LOCK:
        cache._SESSIONS_CACHE.clear()
        cache._SESSIONS_CACHE[key] = (
            time.monotonic(),
            "old-stamp",
            {"sessions": [{"session_id": "stale"}]},
        )
    monkeypatch.setattr(cache, "_session_list_cache_resolved_source_stamp", lambda _key: "new-stamp")
    try:
        payload, is_fresh, stale_reason = cache._session_list_cache_get_with_reason(
            key,
            allow_stale=True,
        )
        assert payload == {"sessions": [{"session_id": "stale"}]}
        assert is_fresh is False
        assert stale_reason == "source"
    finally:
        with cache._SESSIONS_CACHE_LOCK:
            cache._SESSIONS_CACHE.clear()


def test_active_run_visibility_ignores_source_invalidated_cache_membership(monkeypatch):
    from api import config, routes

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": False,
        "show_claude_code_sessions": False,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(routes, "_session_list_cache_get_with_reason", lambda key, allow_stale=False: (
        {"sessions": [{"session_id": "wrong", "project_id": "p1"}]}, False, "source"
    ))
    seen = []

    class _Session:
        profile = "default"
        raw_source = "webui"
        project_id = "p1"
        message_count = 1
        session_id = "fresh"

    def _get_session(sid, metadata_only=False):
        seen.append((sid, metadata_only))
        return _Session() if sid == "fresh" else None

    monkeypatch.setattr(routes, "get_session", _get_session)
    now = time.time()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update({
            "stream-old": {"session_id": "wrong", "started_at": now - 20, "phase": "running"},
            "stream-fresh": {"session_id": "fresh", "started_at": now - 10, "phase": "thinking"},
        })
    try:
        payload = routes._active_run_visibility_snapshot(project_id="p1")
        assert [run["session_id"] for run in payload["runs"]] == ["fresh"]
        assert seen == [("wrong", True), ("fresh", True)]
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_active_run_visibility_snapshot_hides_cli_rows_when_cli_sidebar_is_disabled(monkeypatch):
    from api import config, routes

    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            self.profile = "default"
            self.raw_source = "cli"
            self.source = "cli"
            self.project_id = "p1"
            self.is_cli_session = True
            self.message_count = 1

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": False,
        "show_claude_code_sessions": False,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_webhook_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(routes, "_session_list_cache_get_with_reason", lambda key, allow_stale=False: (None, False, None))
    monkeypatch.setattr(routes, "all_sessions", lambda: (_ for _ in ()).throw(AssertionError("all_sessions should not be called")))
    seen = []

    def _fake_get_session(sid, metadata_only=False):
        seen.append((sid, metadata_only))
        return _FakeSession(sid)

    monkeypatch.setattr(routes, "get_session", _fake_get_session)
    now = time.time()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stream-cli"] = {
            "stream_id": "stream-cli",
            "session_id": "session-cli",
            "started_at": now - 15,
            "phase": "running",
        }
    try:
        payload = routes._active_run_visibility_snapshot(sidebar_source="cli", project_id="p1")
        assert payload["active_runs"] == 0
        assert payload["runs"] == []
        assert seen == [("session-cli", True)]
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
