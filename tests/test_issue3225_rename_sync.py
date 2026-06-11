"""Regression coverage: WebUI session rename writes through to state.db (#3225).

The /api/session/rename handler must call _sync_session_title_to_insights(s),
just like the sibling /api/session/title/regenerate handler does, so a rename
propagates the new title to the agent's state.db. Without it the TUI and CLI
keep showing the stale name. Static source-text assertion that mirrors
test_regenerate_endpoint_syncs_title_to_state_db_when_enabled.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_rename_endpoint_syncs_title_to_state_db():
    start_idx = ROUTES_PY.index('"/api/session/rename"')
    end_idx = ROUTES_PY.index('"/api/session/title/regenerate"', start_idx)
    block = ROUTES_PY[start_idx:end_idx]
    assert "_sync_session_title_to_insights(s)" in block, (
        "rename handler must call _sync_session_title_to_insights(s) so the "
        "new title reaches state.db, matching the regenerate handler"
    )
    assert 'publish_session_list_changed("session_rename", profile=getattr(s, "profile", None))' in block
    # Sync must run BEFORE the list-changed publish, matching the regenerate
    # handler, so SSE subscribers refresh after state.db holds the new title.
    sync_idx = block.index("_sync_session_title_to_insights(s)")
    publish_idx = block.index('publish_session_list_changed("session_rename", profile=getattr(s, "profile", None))')
    assert sync_idx < publish_idx, (
        "rename must sync to state.db before publishing the list-changed event"
    )
