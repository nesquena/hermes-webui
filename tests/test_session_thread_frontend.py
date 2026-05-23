from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_frontend_has_continue_in_linked_chat_action():
    assert "session_thread_continue" in SESSIONS_JS
    assert "Continue in linked chat" in I18N_JS
    assert "link_to_prev" in SESSIONS_JS
    assert "Started a fresh linked chat. Nothing was carried over automatically." in I18N_JS
    assert "/api/session/new" in SESSIONS_JS


def test_frontend_has_retroactive_link_action_and_thread_fields():
    assert "session_link_to_thread" in SESSIONS_JS
    assert "/api/thread/link-session" in SESSIONS_JS
    assert "/api/thread/create" in SESSIONS_JS
    assert "thread_id" in SESSIONS_JS
    assert "thread_sequence" in SESSIONS_JS
    assert "parent_session_id" not in _continue_linked_chat_function_source()


def test_frontend_has_thread_badge_and_thread_summary_cache():
    assert "/api/threads" in SESSIONS_JS
    assert "_threadSummariesById" in SESSIONS_JS
    assert "session-thread-indicator" in SESSIONS_JS
    assert "session-thread-meta" in SESSIONS_JS
    assert "Part" in I18N_JS or "session_thread_part" in I18N_JS
    assert ".session-thread-indicator" in STYLE_CSS


def test_threads_view_wiring_and_manifest_export():
    assert "data-panel=\"threads\"" in INDEX_HTML
    assert "panelThreads" in INDEX_HTML
    assert "tab_threads" in I18N_JS
    assert "threads" in PANELS_JS
    assert "renderThreadsPanel" in PANELS_JS
    assert "/api/threads" in PANELS_JS
    assert "/api/thread?" in PANELS_JS
    assert "/api/thread/export" in PANELS_JS
    assert "/api/thread/unlink-session" in PANELS_JS
    assert "include_messages=1" in PANELS_JS
    assert "manifest" in PANELS_JS.lower()
    assert ".thread-card" in STYLE_CSS


def _continue_linked_chat_function_source():
    marker = "async function continueInLinkedChat"
    start = SESSIONS_JS.find(marker)
    if start == -1:
        return ""
    next_fn = SESSIONS_JS.find("\nfunction ", start + len(marker))
    next_async = SESSIONS_JS.find("\nasync function ", start + len(marker))
    candidates = [idx for idx in (next_fn, next_async) if idx != -1]
    end = min(candidates) if candidates else len(SESSIONS_JS)
    return SESSIONS_JS[start:end]
