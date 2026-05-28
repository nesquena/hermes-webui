"""Unit tests for PR #3019, #3020, #2973 review feedback.

Static-analysis tests that verify source-level invariants without
spinning up the test server.

#3019 — _hide_from_default_sidebar respects cron project_id.
#3020 — _setSessionViewedCount clears completion-unread marker.
#2973 — Compression elapsed-timer clear is guarded by active-session check.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODELS_PY = (REPO / "api" / "models.py").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


# ── #3019: Cron session with project_id should not be hidden ──

def test_hide_from_default_sidebar_checks_project_id():
    """_hide_from_default_sidebar must allow cron sessions with project_id."""
    idx = MODELS_PY.find("def _hide_from_default_sidebar(")
    assert idx != -1
    block = MODELS_PY[idx : idx + 500]
    assert "project_id" in block


def test_cron_project_created_with_system_flag():
    """ensure_cron_project must set system=True on the project."""
    idx = MODELS_PY.find("def ensure_cron_project(")
    assert idx != -1
    block = MODELS_PY[idx : idx + 1500]
    assert "system" in block and "True" in block


# ── #3020: _setSessionViewedCount clears completion-unread ──

def test_set_viewed_count_clears_completion_unread():
    """_setSessionViewedCount must call _clearSessionCompletionUnread."""
    idx = SESSIONS_JS.find("function _setSessionViewedCount(")
    assert idx != -1
    # Function is ~450 chars; use 600 to be safe
    block = SESSIONS_JS[idx : idx + 600]
    assert "_clearSessionCompletionUnread" in block


def test_has_unread_checks_completion_before_count():
    """_hasUnreadForSession must check completion-unread marker first."""
    marker_idx = SESSIONS_JS.find("_hasSessionCompletionUnread(s.session_id)")
    count_idx = SESSIONS_JS.find("s.message_count > Number")
    assert marker_idx != -1
    assert count_idx != -1
    assert marker_idx < count_idx


# ── #2973: Compression timer clear is guarded ──

def test_timer_clear_uses_active_session_guard():
    """appendLiveCompressionCard must guard timer clear with active-session check."""
    idx = UI_JS.find("function appendLiveCompressionCard(")
    assert idx != -1
    branch_idx = UI_JS.find("removeAttribute('data-compression-started-at')", idx)
    assert branch_idx != -1
    block = UI_JS[branch_idx : branch_idx + 500]
    assert "_compressionStateForCurrentSession()" in block
