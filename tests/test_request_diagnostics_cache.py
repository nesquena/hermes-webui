from pathlib import Path

from api.request_diagnostics import RequestDiagnostics


def test_request_diagnostics_maybe_start_still_covers_sessions_route():
    assert RequestDiagnostics.maybe_start("GET", "/api/sessions") is not None


def test_sessions_route_emits_cache_diagnostic_stage_names():
    src = Path("api/routes.py").read_text(encoding="utf-8")

    for stage in (
        "session_list_cache_lookup",
        "session_list_cache_hit",
        "session_list_cache_wait",
        "session_list_cache_stored",
        "session_list_cache_invalidated_during_rebuild",
    ):
        assert stage in src
