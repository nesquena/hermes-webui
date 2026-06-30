"""Regression tests for searchable settings feature.

The Settings panel must have a search input and filtering capability to allow
users to find settings across all tabs without having to click through each section.

Issue: #3850 (Add search input at top of Settings panel)
"""
from pathlib import Path
import re

INDEX_HTML = (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (Path(__file__).parent.parent / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (Path(__file__).parent.parent / "static" / "style.css").read_text(encoding="utf-8")


class TestSettingsSearch:
    """Search input and filtering must be present and functional."""

    def test_index_html_has_search_input(self):
        """index.html must contain the search input element with correct id."""
        assert 'id="settingsSearch"' in INDEX_HTML, (
            "index.html must contain a search input with id='settingsSearch'"
        )
        assert 'filterSettings(this.value)' in INDEX_HTML, (
            "index.html search input must have oninput='filterSettings(this.value)'"
        )
        assert 'data-i18n-placeholder="settings_search_placeholder"' in INDEX_HTML, (
            "index.html search input must have data-i18n-placeholder attribute"
        )

    def test_index_html_has_search_results_dropdown(self):
        """index.html must contain the search results dropdown element."""
        assert 'id="settingsSearchResults"' in INDEX_HTML, (
            "index.html must contain a results dropdown with id='settingsSearchResults'"
        )
        assert 'class="settings-search-results"' in INDEX_HTML, (
            "index.html results dropdown must have class='settings-search-results'"
        )

    def test_panels_js_has_build_settings_index_function(self):
        """panels.js must contain _buildSettingsIndex function."""
        assert "function _buildSettingsIndex()" in PANELS_JS, (
            "panels.js must contain _buildSettingsIndex() function"
        )
        body = PANELS_JS[PANELS_JS.find("function _buildSettingsIndex()"):]
        assert "settingsPaneConversation" in body, (
            "_buildSettingsIndex must reference settingsPaneConversation pane"
        )
        assert "searchBlob" in body, (
            "_buildSettingsIndex must store a searchBlob for settings fields"
        )

    def test_panels_js_has_filter_settings_function(self):
        """panels.js must contain filterSettings function."""
        assert "function filterSettings(query)" in PANELS_JS, (
            "panels.js must contain filterSettings(query) function"
        )
        body = PANELS_JS[PANELS_JS.find("function filterSettings(query)"):]
        assert "(entry.searchBlob || entry.label).toLowerCase().includes(q)" in body, (
            "filterSettings must do case-insensitive substring matching over the search blob"
        )
        assert "esc(m.label)" in body, (
            "filterSettings must keep rendering the visible label text"
        )

    def test_panels_js_has_navigate_to_field_function(self):
        """panels.js must contain _navigateToSettingsField function."""
        assert "function _navigateToSettingsField(entry)" in PANELS_JS, (
            "panels.js must contain _navigateToSettingsField(entry) function"
        )
        assert "switchSettingsSection" in PANELS_JS[PANELS_JS.find("function _navigateToSettingsField"):], (
            "_navigateToSettingsField must call switchSettingsSection"
        )

    def test_panels_js_has_highlight_settings_field_function(self):
        """panels.js must contain _highlightSettingsField function."""
        assert "function _highlightSettingsField(el)" in PANELS_JS, (
            "panels.js must contain _highlightSettingsField(el) function"
        )
        assert "settings-field-highlight" in PANELS_JS[PANELS_JS.find("function _highlightSettingsField"):], (
            "_highlightSettingsField must toggle 'settings-field-highlight' class"
        )

    def test_panels_js_resets_index_on_panel_open(self):
        """panels.js _beginSettingsPanelSession must reset the settings index."""
        idx = PANELS_JS.find("function _beginSettingsPanelSession()")
        assert idx >= 0, "_beginSettingsPanelSession function not found"
        body = PANELS_JS[idx:idx + 300]
        assert "_settingsIndex = null" in body, (
            "_beginSettingsPanelSession must reset _settingsIndex to null"
        )

    def test_i18n_js_has_search_placeholder_key(self):
        """i18n.js must contain settings_search_placeholder key."""
        assert "settings_search_placeholder:" in I18N_JS, (
            "i18n.js must contain settings_search_placeholder key"
        )
        assert "Search settings" in I18N_JS, (
            "settings_search_placeholder must have an appropriate translation"
        )

    def test_i18n_js_has_no_results_key(self):
        """i18n.js must contain settings_search_no_results key."""
        assert "settings_search_no_results:" in I18N_JS, (
            "i18n.js must contain settings_search_no_results key"
        )
        assert "No settings found" in I18N_JS, (
            "settings_search_no_results must have an appropriate translation"
        )

    def test_style_css_has_search_results_style(self):
        """style.css must contain styles for the search results dropdown."""
        assert ".settings-search-results" in STYLE_CSS, (
            "style.css must contain .settings-search-results class"
        )
        assert ".settings-search-result" in STYLE_CSS, (
            "style.css must contain .settings-search-result class for individual items"
        )

    def test_style_css_has_field_highlight_style(self):
        """style.css must contain styles for field highlighting."""
        assert ".settings-field-highlight" in STYLE_CSS, (
            "style.css must contain .settings-field-highlight class"
        )
        assert "settings-field-pulse" in STYLE_CSS, (
            "style.css must contain settings-field-pulse animation"
        )

    def test_style_css_has_search_positioning(self):
        """style.css must position search results absolutely within the menu."""
        assert "position:relative" in STYLE_CSS and ".settings-search" in STYLE_CSS, (
            "style.css must make .settings-search relative positioned"
        )
        assert "position:absolute" in STYLE_CSS and ".settings-search-results" in STYLE_CSS, (
            "style.css must make .settings-search-results absolutely positioned"
        )

    def test_settings_menu_layout_ownership_contract(self):
        """Settings search should be anchored in the menu while scrolling is owned by the button list."""
        assert 'class="settings-menu-items"' in INDEX_HTML, (
            "index.html must keep the section buttons inside .settings-menu-items"
        )

        menu_match = re.search(
            r"(^|\n)\s*#settingsMenu\s*\{[^}]*\}",
            STYLE_CSS,
            re.MULTILINE,
        )
        assert menu_match is not None, "style.css must have a #settingsMenu rule"
        menu_rules = menu_match.group(0)
        assert "overflow: visible" in menu_rules, (
            "settings menu must not own vertical clipping overflow"
        )

        items_match = re.search(
            r"(^|\n)\s*#settingsMenu\s+\.settings-menu-items\s*\{[^}]*\}",
            STYLE_CSS,
            re.MULTILINE,
        )
        assert items_match is not None, "style.css must have a .settings-menu-items rule"
        items_rules = items_match.group(0)
        assert "overflow-y: auto" in items_rules, (
            "settings menu items wrapper must own vertical scrolling"
        )

    def test_panels_js_handles_providers_pane(self):
        """panels.js must handle the Providers pane in index building."""
        idx = PANELS_JS.find("function _buildSettingsIndex()")
        assert idx >= 0, "_buildSettingsIndex not found"
        body = PANELS_JS[idx:idx + 2000]
        assert "settingsPaneProviders" in body, (
            "_buildSettingsIndex must handle Providers pane"
        )

    def test_panels_js_handles_plugins_pane(self):
        """panels.js must handle the Plugins pane in index building."""
        idx = PANELS_JS.find("function _buildSettingsIndex()")
        assert idx >= 0, "_buildSettingsIndex not found"
        body = PANELS_JS[idx:idx + 2000]
        assert "settingsPanePlugins" in body, (
            "_buildSettingsIndex must handle Plugins pane"
        )

    def test_settings_index_includes_provider_cards(self):
        """Providers pane entries must index provider cards and API key fields."""
        idx = PANELS_JS.find("function _buildSettingsIndex()")
        assert idx >= 0, "_buildSettingsIndex not found"
        body = PANELS_JS[idx:idx + 3500]
        assert "pane.querySelectorAll('.provider-card')" in body, (
            "_buildSettingsIndex must scan provider cards so Providers search is not empty"
        )
        assert "card.querySelectorAll('.provider-card-field')" in body, (
            "_buildSettingsIndex must index provider card fields like API key controls"
        )

    def test_settings_index_includes_plugin_cards(self):
        """Plugins pane entries must index plugin cards by plugin name."""
        idx = PANELS_JS.find("function _buildSettingsIndex()")
        assert idx >= 0, "_buildSettingsIndex not found"
        body = PANELS_JS[idx:idx + 3500]
        assert "pane.querySelectorAll('.plugin-card')" in body, (
            "_buildSettingsIndex must scan plugin cards so Plugins search is not empty"
        )

    def test_resolve_settings_field_rehydrates_provider_plugin_cards(self):
        """Provider and plugin search entries must survive pane re-renders."""
        idx = PANELS_JS.find("function _resolveSettingsField(entry)")
        assert idx >= 0, "_resolveSettingsField not found"
        body = PANELS_JS[idx:idx + 2200]
        assert "entry.cardName && (entry.sectionKey === 'providers' || entry.sectionKey === 'plugins')" in body, (
            "_resolveSettingsField must re-find provider/plugin cards by name after lazy pane re-renders"
        )
        assert "card.querySelectorAll('.provider-card-field')" in body, (
            "_resolveSettingsField must be able to re-find provider card fields by label"
        )

    def test_filter_settings_caps_results(self):
        """filterSettings must cap results at 12 items."""
        idx = PANELS_JS.find("function filterSettings(query)")
        assert idx >= 0, "filterSettings function not found"
        body = PANELS_JS[idx:idx + 2000]
        assert ".slice(0, 12)" in body, (
            "filterSettings must cap results at 12 items with .slice(0, 12)"
        )

    def test_filter_settings_uses_escaping(self):
        """filterSettings must escape HTML in rendered text with esc()."""
        idx = PANELS_JS.find("function filterSettings(query)")
        assert idx >= 0, "filterSettings function not found"
        body = PANELS_JS[idx:idx + 2000]
        assert "esc(" in body, (
            "filterSettings must use esc() for HTML escaping of all rendered text"
        )


class TestSettingsSearchReviewFixes:
    """Regression coverage for the #4340 review fixes (Codex+Opus)."""

    def test_index_covers_toggle_label_shapes(self):
        """The field index must catch the common toggle shape
        <label><input><span data-i18n='...'></span></label>, not just
        label[data-i18n]. Otherwise most checkbox settings are unsearchable."""
        idx = PANELS_JS.find("function _buildSettingsIndex")
        assert idx >= 0, "_buildSettingsIndex not found"
        body = PANELS_JS[idx:idx + 1600]
        assert "label[data-i18n], label [data-i18n], label" in body, (
            "field index query must include 'label [data-i18n]' (span-in-label) "
            "and plain 'label' so toggle settings are searchable"
        )

    def test_resolver_finds_data_i18n_anywhere(self):
        """_resolveSettingsField must resolve a [data-i18n] node anywhere (not
        only on the <label>) back to its .settings-field."""
        idx = PANELS_JS.find("function _resolveSettingsField")
        assert idx >= 0, "_resolveSettingsField not found"
        body = PANELS_JS[idx:idx + 2200]
        assert "[data-i18n=" in body and "CSS.escape(entry.i18nKey)" in body, (
            "_resolveSettingsField must query any [data-i18n] node, not "
            "label[data-i18n] only"
        )
        assert "label[data-i18n=" not in body, (
            "_resolveSettingsField must NOT restrict the lookup to label[data-i18n]"
        )

    def test_panel_session_invalidates_stale_search(self):
        """_beginSettingsPanelSession must bump the search seq and clear the
        input + results so a stale in-flight render from a prior session can't
        paint into the dropdown."""
        idx = PANELS_JS.find("function _beginSettingsPanelSession")
        assert idx >= 0, "_beginSettingsPanelSession not found"
        body = PANELS_JS[idx:idx + 900]
        assert "++_settingsSearchSeq" in body, (
            "_beginSettingsPanelSession must bump _settingsSearchSeq to "
            "invalidate in-flight searches"
        )
        assert "settingsSearch" in body and "value = ''" in body, (
            "_beginSettingsPanelSession must clear the search input"
        )

    def test_dismiss_handler_invalidates_inflight_build(self):
        """The outside-click dismiss must also invalidate an in-flight first
        build so it can't resurrect the dismissed dropdown."""
        idx = PANELS_JS.find("_settingsSearchDismissListenerRegistered = true")
        assert idx >= 0, "dismiss listener registration not found"
        body = PANELS_JS[idx:idx + 500]
        assert "++_settingsSearchSeq" in body, (
            "the outside-click dismiss handler must bump _settingsSearchSeq"
        )
