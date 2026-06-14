"""Regression coverage for manual session title regeneration controls (#3106)."""

from pathlib import Path
from unittest.mock import MagicMock

import api.streaming as streaming

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_session_action_menu_exposes_regenerate_title_control():
    assert "session_title_regenerate" in SESSIONS_JS
    assert "session_title_regenerate_desc" in SESSIONS_JS
    assert "ICONS.spark" in SESSIONS_JS
    assert "api('/api/session/title/regenerate'" in SESSIONS_JS
    assert "renderSessionListFromCache();" in SESSIONS_JS


def test_writable_imported_sessions_keep_regenerate_action_without_broadening_shared_gate():
    # The shared _isReadOnlySession() helper must stay scoped to read_only flags
    # so it does not silently disable rename/pin/archive/etc. for imported
    # sessions. Writable imported sessions should still expose regenerate.
    helper_idx = SESSIONS_JS.index("function _isReadOnlySession(session)")
    next_helper_idx = SESSIONS_JS.index("function _sourceKeyForSession", helper_idx)
    helper_block = SESSIONS_JS[helper_idx:next_helper_idx]
    assert "session.is_imported" not in helper_block, (
        "_isReadOnlySession must not include is_imported; writable imports need regenerate"
    )
    regen_idx = SESSIONS_JS.index("api('/api/session/title/regenerate'")
    guard_window = SESSIONS_JS[regen_idx - 600:regen_idx]
    assert "session.is_imported" not in guard_window


def test_regenerate_title_i18n_and_changelog_entries_exist():
    for key in [
        "session_title_regenerate",
        "session_title_regenerate_desc",
        "session_title_regenerating",
        "session_title_regenerated",
        "session_title_regenerate_failed",
    ]:
        assert key in I18N_JS
    assert "session action menu can regenerate conversation titles" in CHANGELOG
    assert "#3106" in CHANGELOG


def test_regenerate_endpoint_persists_generated_title_without_reordering_sidebar():
    endpoint_idx = ROUTES_PY.index('"/api/session/title/regenerate"')
    next_endpoint_idx = ROUTES_PY.index('"/api/personality/set"', endpoint_idx)
    block = ROUTES_PY[endpoint_idx:next_endpoint_idx]
    assert "generate_session_title_for_session" in block
    assert '_persist_generated_session_title(s, next_title, event_reason="session_title_regenerate")' in block
    assert "Read-only imported sessions cannot regenerate titles" in block


def test_regenerate_helper_persists_generated_title_and_publishes_sidebar_refresh():
    helper_idx = ROUTES_PY.index("def _persist_generated_session_title")
    queue_idx = ROUTES_PY.index("def _queue_generated_title_for_imported_session", helper_idx)
    helper_block = ROUTES_PY[helper_idx:queue_idx]
    assert "mark_session_title_generated(session)" in helper_block
    assert "session.save(touch_updated_at=False)" in helper_block
    assert "_sync_session_title_to_insights(session)" in helper_block
    assert '_publish_session_list_changed(event_reason, profile=getattr(session, "profile", None))' in helper_block


def test_regenerate_endpoint_syncs_title_to_state_db_when_enabled():
    helper_idx = ROUTES_PY.index("def _sync_session_title_to_insights")
    endpoint_idx = ROUTES_PY.index('"/api/session/title/regenerate"')
    helper_block = ROUTES_PY[helper_idx:endpoint_idx]
    assert 'load_settings().get("sync_to_insights")' in helper_block
    assert "sync_session_usage" in helper_block
    assert "title=session.title" in helper_block
    assert "message_count=len(messages)" in helper_block
    assert "profile=getattr(session, \"profile\", None)" in helper_block


def test_streaming_helper_generates_title_from_persisted_transcript(monkeypatch):
    session = MagicMock()
    session.messages = [
        {"role": "user", "content": "Please fix the stale sidebar title controls"},
        {"role": "assistant", "content": "I will add a regenerate-title action."},
    ]

    class _ProfileEnv:
        def __enter__(self):
            return None
        def __exit__(self, exc_type, exc, tb):
            return False

    import api.profiles as profiles_api
    monkeypatch.setattr(profiles_api, "profile_env_for_background_worker", lambda *args, **kwargs: _ProfileEnv())
    monkeypatch.setattr(
        streaming,
        "_generate_llm_session_title_via_aux",
        lambda user, assistant, agent=None: ("Sidebar title controls", "llm", "raw"),
    )

    title, status, raw = streaming.generate_session_title_for_session(session)
    assert title == "Sidebar title controls"
    assert status == "llm"
    assert raw == "raw"


def test_streaming_helper_has_local_fallback_when_llm_title_is_empty(monkeypatch):
    session = MagicMock()
    session.messages = [
        {"role": "user", "content": "Can you triage this GitHub issue and PR review?"},
        {"role": "assistant", "content": "Sure."},
    ]

    class _ProfileEnv:
        def __enter__(self):
            return None
        def __exit__(self, exc_type, exc, tb):
            return False

    import api.profiles as profiles_api
    monkeypatch.setattr(profiles_api, "profile_env_for_background_worker", lambda *args, **kwargs: _ProfileEnv())
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", lambda *args, **kwargs: (None, "llm_empty", ""))

    title, status, _raw = streaming.generate_session_title_for_session(session)
    assert title == "GitHub Issue Triage"
    assert status == "local_summary:llm_empty"
