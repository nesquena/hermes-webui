from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS  = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS    = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

LOCALE_COUNT = 13  # en, it, ja, ru, es, de, zh, zh-TW, pt, ko, fr, tr, pl


def test_help_nav_button_present():
    assert 'data-settings-section="help"' in INDEX_HTML
    assert "switchSettingsSection('help')" in INDEX_HTML
    assert 'data-i18n="settings_tab_help"' in INDEX_HTML


def test_help_pane_present():
    assert 'id="settingsPaneHelp"' in INDEX_HTML
    assert 'href="https://get-hermes.ai/"' in INDEX_HTML
    assert 'href="https://github.com/nesquena/hermes-webui/issues"' in INDEX_HTML


def test_help_pane_links_are_outbound():
    assert 'target="_blank"' in INDEX_HTML
    assert 'rel="noopener noreferrer"' in INDEX_HTML


def test_panels_js_allowlist_includes_help():
    assert "name==='help'" in PANELS_JS


def test_panels_js_map_includes_help():
    assert "help:'Help'" in PANELS_JS


def test_panels_js_foreach_includes_help():
    assert "'help'" in PANELS_JS
    assert "settingsPaneHelp" not in PANELS_JS or 'settingsPane'+'{map[key]}' not in PANELS_JS
    # Simpler: confirm the forEach array string contains help
    assert ",'help'," in PANELS_JS or ",'help']" in PANELS_JS


def test_i18n_help_keys_present_in_all_locales():
    assert I18N_JS.count("settings_tab_help") == LOCALE_COUNT
    assert I18N_JS.count("settings_help_docs_label") == LOCALE_COUNT
    assert I18N_JS.count("settings_help_issue_label") == LOCALE_COUNT
    assert I18N_JS.count("settings_help_docs_link") == LOCALE_COUNT
    assert I18N_JS.count("settings_help_issue_link") == LOCALE_COUNT
