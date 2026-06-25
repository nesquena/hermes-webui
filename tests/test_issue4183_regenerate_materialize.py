"""Issue #4183: regenerate-title must materialize CLI/TUI sessions from state.db.

The /api/session/title/regenerate endpoint must use _get_or_materialize_session
so sessions that only exist in state.db (no sidecar JSON) can have their titles
regenerated instead of returning "Session not found".
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_regenerate_endpoint_uses_materialize_fallback():
    start = ROUTES_PY.index('"/api/session/title/regenerate"')
    end = ROUTES_PY.index('"/api/personality/set"', start)
    block = ROUTES_PY[start:end]
    assert "_get_or_materialize_session(sid)" in block, (
        "regenerate handler must use _get_or_materialize_session to find "
        "sessions that only exist in state.db (CLI/TUI sessions)"
    )
    assert "get_session(sid)" not in block, (
        "regenerate handler must not use bare get_session — it misses "
        "CLI/TUI sessions that lack a sidecar JSON file"
    )


def test_regenerate_endpoint_catches_permission_error():
    start = ROUTES_PY.index('"/api/session/title/regenerate"')
    end = ROUTES_PY.index('"/api/personality/set"', start)
    block = ROUTES_PY[start:end]
    assert "except PermissionError:" in block, (
        "regenerate handler must catch PermissionError from "
        "_get_or_materialize_session for read-only imported sessions"
    )
