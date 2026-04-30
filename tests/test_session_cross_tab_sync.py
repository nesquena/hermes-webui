"""Regression tests for cross-tab active session synchronization."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_sessions_js_listens_for_active_session_storage_changes():
    assert "addEventListener('storage'" in SESSIONS_JS or 'addEventListener("storage"' in SESSIONS_JS
    assert "hermes-webui-session" in SESSIONS_JS
    assert "_handleActiveSessionStorageEvent" in SESSIONS_JS


def test_storage_sync_does_not_switch_while_busy():
    marker = "if(S.busy)"
    assert marker in SESSIONS_JS, "cross-tab storage sync must not switch sessions during an active turn"
