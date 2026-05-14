from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_endless_scroll_is_opt_in_setting():
    assert '"session_endless_scroll": False' in CONFIG_PY
    assert '"session_endless_scroll"' in CONFIG_PY
    assert 'id="settingsSessionEndlessScroll"' in INDEX_HTML
    assert 'data-i18n="settings_label_session_endless_scroll"' in INDEX_HTML
    assert 'data-i18n="settings_desc_session_endless_scroll"' in INDEX_HTML
    assert "session_endless_scroll: !!($('settingsSessionEndlessScroll')||{}).checked" in PANELS_JS
    assert "window._sessionEndlessScrollEnabled=!!s.session_endless_scroll" in BOOT_JS
    assert "window._sessionEndlessScrollEnabled=false" in BOOT_JS


def test_endless_scroll_setting_has_its_own_label():
    match = re.search(
        r"<label[^>]*>\s*"
        r'<input type="checkbox" id="settingsSessionEndlessScroll"[^>]*>\s*'
        r'<span data-i18n="settings_label_session_endless_scroll">[^<]+</span>\s*'
        r"</label>\s*"
        r'<div[^>]*data-i18n="settings_desc_session_endless_scroll"',
        INDEX_HTML,
    )
    assert match, "endless-scroll checkbox must be wrapped by its own label before its description"


def test_session_jump_and_endless_scroll_labels_are_separate():
    jump_label_end = INDEX_HTML.index('data-i18n="settings_desc_session_jump_buttons"')
    endless_label_start = INDEX_HTML.index('id="settingsSessionEndlessScroll"')
    between = INDEX_HTML[jump_label_end:endless_label_start]
    assert "<label" in between, "endless-scroll checkbox must not share the session-jump label"


def test_scroll_listener_prefetches_older_messages_only_when_enabled():
    assert "function _isSessionEndlessScrollEnabled" in UI_JS
    assert "const olderPrefetchPx=Math.max(600,el.clientHeight*1.5)" in UI_JS
    assert "_isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx" in UI_JS
    assert "el.scrollTop<80 && typeof _messagesTruncated" not in UI_JS


def test_endless_scroll_i18n_keys_exist_for_each_locale():
    assert I18N_JS.count("settings_label_session_endless_scroll") == I18N_JS.count("settings_label_workspace_panel_open")
    assert I18N_JS.count("settings_desc_session_endless_scroll") == I18N_JS.count("settings_desc_workspace_panel_open")
