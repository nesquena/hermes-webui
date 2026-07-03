"""Regression coverage for busy-input settings search terms."""
from pathlib import Path

INDEX_HTML = (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (Path(__file__).parent.parent / "static" / "panels.js").read_text(encoding="utf-8")


class TestBusyInputSettingsSearchTerms:
    """Busy-input settings search must cover option, descriptor, and supplemental text."""

    def test_busy_input_field_has_supplemental_search_terms(self):
        """The busy-input field must expose the supplemental terms from HTML."""
        assert 'data-settings-search="busy input mode message default while running queue interrupt steer"' in INDEX_HTML, (
            "busy-input field must expose supplemental search terms in data-settings-search"
        )
        assert 'data-i18n="settings_label_default_message_mode"' in INDEX_HTML, (
            "busy-input field label must stay wired to the renamed i18n key in the HTML"
        )

    def test_settings_index_uses_field_text_for_busy_input(self):
        """The index builder must include option and descriptor text in searchBlob."""
        idx = PANELS_JS.find("function _buildSettingsIndex()")
        assert idx >= 0, "_buildSettingsIndex not found"
        body = PANELS_JS[idx:]
        assert "searchBlob" in body, "_buildSettingsIndex must build a searchBlob"
        assert "field.textContent" in body, "_buildSettingsIndex must include field text in searchBlob"
        assert "settingsSearch" in body, (
            "_buildSettingsIndex must include supplemental data-settings-search terms in searchBlob"
        )

    def test_filter_settings_uses_search_blob_and_keeps_label_rendering(self):
        """filterSettings must rank by source and keep visible labels."""
        idx = PANELS_JS.find("function filterSettings(query)")
        assert idx >= 0, "filterSettings not found"
        body = PANELS_JS[idx:]
        assert "esc(m.label)" in body, (
            "filterSettings must keep rendering the visible label"
        )
        assert ".slice(0, 12)" in body, (
            "filterSettings must keep the existing 12-result cap"
        )
        assert ".sort(" in body, (
            "filterSettings must apply deterministic ranking"
        )
        assert "_scoreSettingsSearchMatch" in body, (
            "filterSettings must score ranked matches by source"
        )
