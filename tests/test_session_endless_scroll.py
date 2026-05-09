from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def test_endless_scroll_is_opt_in_setting():
    assert '"session_endless_scroll": False' in CONFIG_PY
    assert '"session_endless_scroll"' in CONFIG_PY
    assert 'id="settingsSessionEndlessScroll"' in INDEX_HTML
    assert 'data-i18n="settings_label_session_endless_scroll"' in INDEX_HTML
    assert 'data-i18n="settings_desc_session_endless_scroll"' in INDEX_HTML
    assert "session_endless_scroll: !!($('settingsSessionEndlessScroll')||{}).checked" in PANELS_JS
    assert "window._sessionEndlessScrollEnabled=!!s.session_endless_scroll" in BOOT_JS
    assert "window._sessionEndlessScrollEnabled=false" in BOOT_JS


def test_scroll_listener_prefetches_older_messages_only_when_enabled():
    assert "function _isSessionEndlessScrollEnabled" in UI_JS
    assert "const olderPrefetchPx=Math.max(600,el.clientHeight*1.5)" in UI_JS
    assert "_isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx" in UI_JS
    assert "el.scrollTop<80 && typeof _messagesTruncated" not in UI_JS


def test_endless_scroll_i18n_keys_exist_for_each_locale():
    assert I18N_JS.count("settings_label_session_endless_scroll") == I18N_JS.count("settings_label_workspace_panel_open")
    assert I18N_JS.count("settings_desc_session_endless_scroll") == I18N_JS.count("settings_desc_workspace_panel_open")


def test_full_history_load_serializes_with_endless_scroll_prefetch():
    load_older = _function_body(SESSIONS_JS, "async function _loadOlderMessages")
    load_all = _function_body(SESSIONS_JS, "async function _ensureAllMessagesLoaded")

    assert "function _waitForHistoryLoadIdle" in SESSIONS_JS
    assert "if (_loadingOlder || !_messagesTruncated) return;" in load_older
    assert "if (_loadingOlder) await _waitForHistoryLoadIdle(sid);" in load_all

    lock_idx = load_all.find("_loadingOlder = true")
    fetch_idx = load_all.find("api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=1&resolve_model=0`")
    mutate_idx = load_all.find("S.messages = msgs")
    unlock_idx = load_all.rfind("_loadingOlder = false")

    assert lock_idx != -1 and fetch_idx != -1 and mutate_idx != -1 and unlock_idx != -1
    assert lock_idx < fetch_idx < mutate_idx < unlock_idx
    assert "finally" in load_all
    assert "S.session.session_id !== sid" in load_all
