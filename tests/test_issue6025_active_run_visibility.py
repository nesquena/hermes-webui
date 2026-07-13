from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static/ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")


def test_activity_ui_uses_dedicated_endpoint_and_known_session_labels():
    assert "/api/activity/active-runs" in UI_JS
    assert "/health" not in UI_JS[UI_JS.index("refreshActiveRunVisibility"):UI_JS.index("refreshActiveRunVisibility") + 1800]
    assert "_allSessions" in UI_JS and "loadSession(run.session_id)" in UI_JS
    assert "_activeRunSnapshotRequestSeq" in UI_JS
    assert "_activeRunSnapshotInflight" in UI_JS
    assert "_activeRunSnapshotRefreshQueued" in UI_JS
    assert "if (_activeRunSnapshotInflight)" in UI_JS
    assert "requestSeq !== _activeRunSnapshotRequestSeq" in UI_JS
    assert "ACTIVE_RUN_SNAPSHOT_STALE_MS" in UI_JS
    assert "_activeRunSnapshotFreshAt = 0;" in UI_JS
    assert "activeRunPill" in INDEX_HTML and "activeRunTray" in INDEX_HTML


def test_activity_overlay_reuses_existing_sidebar_ring_and_clears_idle():
    assert "_activeRunSessionIds.has(s.session_id)" in SESSIONS_JS
    assert "session-state-indicator" in SESSIONS_JS
    assert "_reconcileActiveSessionIdleStateFromList" in SESSIONS_JS
    assert "_activeRunSessionIds.clear()" in UI_JS
