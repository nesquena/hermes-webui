from pathlib import Path


SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")


def test_streaming_sidebar_poll_is_not_5s_anymore():
    assert "const _streamingPollMs = 15000;" in SESSIONS_JS
    assert "const _streamingPollMs = 5000;" not in SESSIONS_JS


def test_active_session_external_refresh_is_not_5s_anymore():
    assert "const _activeSessionExternalRefreshMs = 20000;" in SESSIONS_JS
    assert "const _activeSessionExternalRefreshMs = 5000;" not in SESSIONS_JS
