"""Regression test for #1468: new conversations in filtered project sidebar."""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).parent.parent
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _new_session_block() -> str:
    """Return the source block for `newSession()` only."""
    start = SESSIONS_JS.find("async function newSession(")
    assert start >= 0, "newSession() not found in sessions.js"
    next_fn = SESSIONS_JS.find("async function ", start + 10)
    return SESSIONS_JS[start:next_fn if next_fn >= 0 else None]


def test_new_session_post_payload_includes_project_id_with_sentinel_safe_fallback():
    """newSession() must send project_id and map NO_PROJECT_FILTER to null."""
    block = _new_session_block()
    assert "activeProjectId=_activeProject===NO_PROJECT_FILTER ? null : _activeProject" in block, (
        "newSession() should normalize NO_PROJECT_FILTER to null before building request body"
    )
    assert re.search(r"project_id:\s*activeProjectId\s*\|\|\s*null", block), (
        "newSession() should always include project_id in the request and pass null for the "
        "all-projects/unassigned cases"
    )
    assert "_showArchived?null" not in block, (
        "archived-only visibility should not override an active project filter during creation"
    )
    # Guard against the old accidental behavior of setting only when truthy.
    assert "if(_activeProject&&_activeProject!==NO_PROJECT_FILTER) reqBody.project_id" not in block, (
        "newSession() should no longer guard project_id behind a truthy check"
    )
