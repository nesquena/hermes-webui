"""Regression tests for #6728: active cron sessions wrongly appear as completed.

The sidebar polls /api/sessions (never /api/crons/status), so cron liveness
must be stamped onto the session-list rows. A still-running cron job's row
must carry ``cron_running=True`` so the client defers its completion/unread
transition; a finished (or non-cron) row must not. Because cron session ids
are ``cron_{job_id}_{run_timestamp}``, only the run whose ``created_at`` is at
or after the tracked start belongs to the live execution — older runs of the
same job must stay completed.
"""


def _cron_row(sid, created_at, **overrides):
    row = {
        "session_id": sid,
        "title": "Cron Session",
        "profile": "default",
        "created_at": created_at,
        "updated_at": created_at,
        "last_message_at": created_at,
        "message_count": 1,
        "user_message_count": 1,
        "archived": False,
        "project_id": "cron-project",
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "is_cli_session": False,
    }
    row.update(overrides)
    return row


def test_overlay_marks_current_running_cron_row(monkeypatch):
    import api.routes as routes
    import api.route_session_list_cache as slc

    # job6728 started at epoch 1000; the current run's session was created at
    # 1100 (>= start) and carries the matching cron_{job_id}_ prefix.
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"job6728": 1000.0})
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())

    rows = slc._session_list_cache_overlay_runtime_rows(
        [_cron_row("cron_job6728_20260803_100000", created_at=1100)]
    )
    assert len(rows) == 1
    assert rows[0]["cron_running"] is True


def test_overlay_does_not_mark_finished_or_other_cron_rows(monkeypatch):
    import api.routes as routes
    import api.route_session_list_cache as slc

    # job6728 is running (started at 1000) but:
    # - cron_job9999... belongs to a different job (not tracked)
    # - cron_job6728_20260701... is an OLD run of the same job (created < start)
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"job6728": 1000.0})
    monkeypatch.setattr(slc, "_session_list_cache_active_stream_ids", lambda: set())

    rows = slc._session_list_cache_overlay_runtime_rows(
        [
            _cron_row("cron_job9999_20260803_100000", created_at=1100),
            _cron_row("cron_job6728_20260701_050000", created_at=500),
            {"session_id": "regular-chat", "title": "Chat", "is_cli_session": False},
        ]
    )
    by_sid = {row["session_id"]: row for row in rows}
    assert by_sid["cron_job9999_20260803_100000"]["cron_running"] is False
    assert by_sid["cron_job6728_20260701_050000"]["cron_running"] is False
    assert by_sid["regular-chat"]["cron_running"] is False


def test_overlay_fails_closed_when_routes_unavailable(monkeypatch):
    import api.route_session_list_cache as slc

    monkeypatch.setattr(slc, "_session_list_cache_running_cron_jobs", lambda: {})

    rows = slc._session_list_cache_overlay_runtime_rows(
        [_cron_row("cron_job6728_20260803_100000", created_at=1100)]
    )
    assert rows[0]["cron_running"] is False


def test_sidebar_response_preserves_cron_running(monkeypatch):
    import api.routes as routes

    row = _cron_row("cron_job6728_20260803_100000", created_at=1100)
    row["cron_running"] = True
    item = routes._sidebar_session_response_item(row)
    assert item.get("cron_running") is True


def test_payload_response_roundtrip_stamps_running_cron(monkeypatch):
    """End-to-end: /api/sessions response rows carry cron_running for live jobs."""
    import api.routes as routes

    raw_cron_row = _cron_row("cron_job6728_20260803_100000", created_at=1100)
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(
        routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [raw_cron_row]
    )
    monkeypatch.setattr(
        routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False
    )
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {"job6728": 1000.0})

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=True,
    )
    response = routes._session_list_payload_to_response(payload)
    rows = response["sessions"]
    matching = [row for row in rows if row["session_id"] == "cron_job6728_20260803_100000"]
    assert len(matching) == 1
    assert matching[0]["cron_running"] is True
