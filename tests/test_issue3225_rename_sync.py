"""Regression coverage: WebUI session rename writes through to state.db (#3225).

The /api/session/rename handler must call _sync_session_title_to_insights(s, rename=True),
just like the sibling /api/session/title/regenerate handler does, so a rename
propagates the new title to the agent's state.db. ``rename=True`` marks it as an
explicit user rename: storage-interface Agents record or clear the title only
then (#9157). Without it the TUI and CLI
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
    assert "_sync_session_title_to_insights(s, rename=True)" in block, (
        "rename handler must call _sync_session_title_to_insights(s, rename=True) so the "
        "new title reaches state.db, matching the regenerate handler"
    )
    assert 'publish_session_list_changed(\n            "session_rename",' in block
    # Sync must run BEFORE the list-changed publish, matching the regenerate
    # handler, so SSE subscribers refresh after state.db holds the new title.
    sync_idx = block.index("_sync_session_title_to_insights(s, rename=True)")
    publish_idx = block.index('publish_session_list_changed(\n            "session_rename",')
    assert sync_idx < publish_idx, (
        "rename must sync to state.db before publishing the list-changed event"
    )
