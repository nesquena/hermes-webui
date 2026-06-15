import api.routes as routes
from api.request_diagnostics import RequestDiagnostics


def test_request_diagnostics_maybe_start_still_covers_sessions_route():
    assert RequestDiagnostics.maybe_start("GET", "/api/sessions") is not None


def _session_cache_diag_stage_names(diag: RequestDiagnostics) -> list[str]:
    return [stage["name"] for stage in diag._stages] + [diag._current_stage]


def _session_cache_key() -> tuple:
    return routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )


def test_sessions_route_emits_cache_hit_diagnostic_stages():
    key = _session_cache_key()
    routes._session_list_cache_clear()
    routes._session_list_cache_set(key, {"sessions": [], "cli_count": 0})

    diag = RequestDiagnostics("GET", "/api/sessions", auto_start=False)
    payload = routes._get_cached_session_list_payload(
        key=key,
        builder=lambda: {"sessions": ["unexpected"]},
        diag=diag,
    )

    assert payload == {"sessions": [], "cli_count": 0}
    assert _session_cache_diag_stage_names(diag) == [
        "start",
        "session_list_cache_lookup",
        "session_list_cache_hit",
    ]


def test_sessions_route_emits_cache_store_diagnostic_stages():
    key = _session_cache_key()
    routes._session_list_cache_clear()

    diag = RequestDiagnostics("GET", "/api/sessions", auto_start=False)
    payload = routes._get_cached_session_list_payload(
        key=key,
        builder=lambda: {"sessions": ["rebuilt"], "cli_count": 0},
        diag=diag,
    )

    assert payload == {"sessions": ["rebuilt"], "cli_count": 0}
    assert _session_cache_diag_stage_names(diag) == [
        "start",
        "session_list_cache_lookup",
        "session_list_cache_rebuild_owner",
        "session_list_cache_stored",
    ]


def test_sessions_route_emits_invalidation_retry_diagnostic_stage():
    key = _session_cache_key()
    routes._session_list_cache_clear()
    calls = {"count": 0}

    def _builder():
        calls["count"] += 1
        if calls["count"] == 1:
            routes._session_list_cache_clear()
        return {"sessions": [calls["count"]], "cli_count": 0}

    diag = RequestDiagnostics("GET", "/api/sessions", auto_start=False)
    payload = routes._get_cached_session_list_payload(
        key=key,
        builder=_builder,
        diag=diag,
    )

    assert payload == {"sessions": [2], "cli_count": 0}
    assert calls["count"] == 2
    assert _session_cache_diag_stage_names(diag) == [
        "start",
        "session_list_cache_lookup",
        "session_list_cache_rebuild_owner",
        "session_list_cache_invalidated_during_rebuild",
        "session_list_cache_stored",
    ]
