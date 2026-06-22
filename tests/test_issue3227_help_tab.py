from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS  = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS    = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS  = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

LOCALE_COUNT = 13  # en, it, ja, ru, es, de, zh, zh-TW, pt, ko, fr, tr, pl


def test_help_nav_button_present():
    assert 'data-settings-section="help"' in INDEX_HTML
    assert "switchSettingsSection('help',{fromSidebarItem:true})" in INDEX_HTML
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


def test_help_card_link_hover_is_contrast_safe():
    """The hover fill is var(--accent); the text color must NOT be var(--accent-text).

    In most themes --accent-text equals (or nearly equals) --accent — it is an
    accent-on-background link color, not a contrasting color for text sitting ON
    an accent fill. Using it on the accent-filled hover state produced same-color
    text on the fill (invisible) across nearly every theme (slate/gold/ares/mono/
    sisyphus/catppuccin dark all had accent == accent-text). The fix uses
    var(--bg) — the page background — which --accent is designed to contrast
    against, so the text stays readable in every theme.
    """
    import re
    m = re.search(r"\.help-card-link:hover\s*\{([^}]*)\}", STYLE_CSS)
    assert m, "expected a .help-card-link:hover rule in style.css"
    rule = m.group(1)
    assert "background:var(--accent)" in rule.replace(" ", "")
    # The bug: accent-text on accent fill. Must not reappear.
    assert "--accent-text" not in rule, (
        "hover text uses var(--accent-text) on an accent fill — invisible in most "
        "themes; use var(--bg) for theme-agnostic contrast"
    )
    assert "color:var(--bg)" in rule.replace(" ", "")
