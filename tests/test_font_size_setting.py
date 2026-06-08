"""Tests for font size setting (#833) — Small/Default/Large/Extra Large in Appearance."""
import os
import re

_SRC = os.path.join(os.path.dirname(__file__), "..")

def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


class TestFontSizeCssModifiers:
    """CSS must define font-size overrides via data attribute."""

    def test_small_font_size_rule_exists(self):
        css = _read("static/style.css")
        assert 'data-font-size="small"' in css, (
            "style.css must have :root[data-font-size=\"small\"] font-size rule"
        )

    def test_large_font_size_rule_exists(self):
        css = _read("static/style.css")
        assert 'data-font-size="large"' in css, (
            "style.css must have :root[data-font-size=\"large\"] font-size rule"
        )

    def test_extra_large_font_size_rule_exists(self):
        css = _read("static/style.css")
        assert 'data-font-size="xlarge"' in css, (
            "style.css must have :root[data-font-size=\"xlarge\"] font-size rule"
        )

    def test_small_large_and_xlarge_scale_from_default(self):
        css = _read("static/style.css")
        # Match both compact {font-size:12px} and spaced { font-size: 12px; } formats
        m_small = re.search(r':root\[data-font-size="small"\][^{]*\{[^}]*font-size:\s*(\d+)px', css)
        m_large = re.search(r':root\[data-font-size="large"\][^{]*\{[^}]*font-size:\s*(\d+)px', css)
        m_xlarge = re.search(r':root\[data-font-size="xlarge"\][^{]*\{[^}]*font-size:\s*(\d+)px', css)
        assert m_small and m_large and m_xlarge, (
            "Small, large, and extra-large font-size rules must set px values"
        )
        assert int(m_small.group(1)) < 14, "Small font size must be < 14px (default)"
        assert int(m_large.group(1)) > 14, "Large font size must be > 14px (default)"
        assert int(m_xlarge.group(1)) > int(m_large.group(1)), (
            "Extra Large font size must be larger than Large"
        )


class TestFontSizeBootScript:
    """The boot script must apply font size from localStorage before page renders."""

    def test_boot_script_reads_hermes_font_size(self):
        html = _read("static/index.html")
        assert "hermes-font-size" in html, (
            "index.html boot script must read 'hermes-font-size' from localStorage"
        )
        assert "data-font-size" in html, (
            "boot script must set document.documentElement.dataset.fontSize"
        )

    def test_font_size_picker_html_present(self):
        html = _read("static/index.html")
        assert "fontSizePickerGrid" in html, (
            "Appearance pane must contain a fontSizePickerGrid element"
        )
        assert "settingsFontSize" in html, (
            "Appearance pane must contain a hidden #settingsFontSize input"
        )
        assert "font-size-pick-btn" in html, (
            "Font size picker buttons must have font-size-pick-btn class"
        )

    def test_font_size_values_present(self):
        html = _read("static/index.html")
        assert 'data-font-size-val="small"' in html, "Small button must exist"
        assert 'data-font-size-val="default"' in html, "Default button must exist"
        assert 'data-font-size-val="large"' in html, "Large button must exist"
        assert 'data-font-size-val="xlarge"' in html, "Extra Large button must exist"
        assert 'data-i18n="font_size_xlarge"' in html, (
            "Extra Large button must use the font_size_xlarge i18n key"
        )

    def test_font_size_picker_not_duplicated(self):
        """Regression guard: the font size picker grid must appear exactly once
        in index.html. Earlier versions of this PR accidentally injected the
        block into both settingsPaneAppearance (correct) and
        settingsPanePreferences (copy-paste duplicate), creating duplicate IDs
        that break _syncFontSizePicker visual sync on one of the grids."""
        html = _read("static/index.html")
        assert html.count('id="fontSizePickerGrid"') == 1, (
            "fontSizePickerGrid must appear exactly once — duplicate IDs "
            "violate HTML spec and break querySelectorAll-based sync."
        )
        assert html.count('id="settingsFontSize"') == 1, (
            "settingsFontSize hidden input must appear exactly once"
        )

    def test_font_size_picker_lives_in_appearance_pane(self):
        """The font size picker must be under settingsPaneAppearance,
        not Preferences/System/Conversation."""
        html = _read("static/index.html")
        appearance_start = html.find('id="settingsPaneAppearance"')
        next_pane_markers = [
            'id="settingsPanePreferences"',
            'id="settingsPaneSystem"',
            'id="settingsPaneConversation"',
        ]
        next_pane_starts = [
            html.find(m, appearance_start + 1) for m in next_pane_markers
        ]
        after_appearance = min(
            [p for p in next_pane_starts if p != -1] or [len(html)]
        )
        picker_pos = html.find('id="fontSizePickerGrid"')
        assert appearance_start != -1, "settingsPaneAppearance not found"
        assert picker_pos != -1, "fontSizePickerGrid not found"
        assert appearance_start < picker_pos < after_appearance, (
            "Font size picker must live inside settingsPaneAppearance "
            "(same section as Theme and Skin)"
        )


class TestFontSizeJsFunctions:
    """JS must expose _pickFontSize, _applyFontSize, and _syncFontSizePicker."""

    def test_pick_font_size_function_exists(self):
        boot = _read("static/boot.js")
        assert "function _pickFontSize(" in boot, (
            "boot.js must define _pickFontSize()"
        )

    def test_apply_font_size_function_exists(self):
        boot = _read("static/boot.js")
        assert "function _applyFontSize(" in boot, (
            "boot.js must define _applyFontSize()"
        )

    def test_sync_font_size_picker_function_exists(self):
        boot = _read("static/boot.js")
        assert "function _syncFontSizePicker(" in boot, (
            "boot.js must define _syncFontSizePicker()"
        )

    def test_pick_font_size_persists_to_localstorage(self):
        boot = _read("static/boot.js")
        idx = boot.find("function _pickFontSize(")
        block = boot[idx:idx+400]
        assert "localStorage.setItem('hermes-font-size'" in block, (
            "_pickFontSize must persist choice to localStorage"
        )

    def test_apply_font_size_sets_data_attribute(self):
        boot = _read("static/boot.js")
        idx = boot.find("function _applyFontSize(")
        block = boot[idx:idx+300]
        assert "dataset.fontSize" in block, (
            "_applyFontSize must set document.documentElement.dataset.fontSize"
        )


class TestFontSizeI18nCoverage:
    """All locales must include the font size i18n keys."""

    def _get_locale_keys(self, src, locale_marker_after, stop_marker):
        """Extract keys from a locale block."""
        start = src.find(locale_marker_after)
        if start < 0:
            return set()
        end = src.find(stop_marker, start)
        block = src[start:end if end > 0 else start + 20000]
        return set(re.findall(r"(\w[\w_]+):", block))

    REQUIRED_KEYS = {
        "settings_label_font_size",
        "font_size_small",
        "font_size_default",
        "font_size_large",
        "font_size_xlarge",
    }

    def test_all_locales_have_font_size_keys(self):
        src = _read("static/i18n.js")
        count = src.count("settings_label_font_size")
        # 6 locales: en, ru, es, de, zh, zh-Hant
        assert count >= 6, (
            f"settings_label_font_size must appear in all 6 locales, found {count}"
        )

    def test_font_size_small_key_in_all_locales(self):
        src = _read("static/i18n.js")
        count = src.count("font_size_small")
        assert count >= 6, f"font_size_small must appear in all 6 locales, found {count}"

    def test_font_size_large_key_in_all_locales(self):
        src = _read("static/i18n.js")
        count = src.count("font_size_large")
        assert count >= 6, f"font_size_large must appear in all 6 locales, found {count}"

    def test_font_size_extra_large_key_in_all_locales(self):
        src = _read("static/i18n.js")
        count = src.count("font_size_xlarge")
        assert count >= 6, f"font_size_xlarge must appear in all locales, found {count}"


class TestFontSizeSettingsValidation:
    """The backend settings contract must accept the persisted xlarge value."""

    def test_config_allows_extra_large_font_size(self):
        config = _read("api/config.py")
        assert '"font_size": {"small", "default", "large", "xlarge"}' in config, (
            "api/config.py must accept xlarge as a persisted font_size value"
        )


class TestFontSizeCssTargetedOverrides:
    """CSS must override px-unit text in key UI elements, not just :root font-size.

    The original PR only set :root font-size, but the stylesheet uses hardcoded px
    values throughout — changing :root has no effect on those. This test class locks
    in the targeted overrides for the most visible UI surfaces.
    """

    def test_msg_body_overridden_for_small(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="small"] .msg-body' in css, \
            "Chat message text must be explicitly scaled for small"

    def test_msg_body_overridden_for_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="large"] .msg-body' in css, \
            "Chat message text must be explicitly scaled for large"

    def test_msg_body_overridden_for_extra_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="xlarge"] .msg-body' in css, \
            "Chat message text must be explicitly scaled for extra large"

    def test_session_item_overridden_for_small(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="small"] .session-item' in css, \
            "Sidebar session list text must be explicitly scaled for small"

    def test_session_item_overridden_for_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="large"] .session-item' in css, \
            "Sidebar session list text must be explicitly scaled for large"

    def test_session_item_overridden_for_extra_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="xlarge"] .session-item' in css, \
            "Sidebar session list text must be explicitly scaled for extra large"

    def test_composer_overridden_for_small(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="small"] #msg' in css, \
            "Composer textarea must be explicitly scaled for small"

    def test_composer_overridden_for_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="large"] #msg' in css, \
            "Composer textarea must be explicitly scaled for large"
        # Large composer must not equal the default 16px — that's a no-op
        import re
        m = re.search(r':root\[data-font-size="large"\] #msg \{ font-size: (\d+)px', css)
        assert m and int(m.group(1)) != 16, \
            "Large composer font-size must differ from default (16px) to have visible effect"

    def test_composer_overridden_for_extra_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="xlarge"] #msg' in css, \
            "Composer textarea must be explicitly scaled for extra large"
        m_large = re.search(r':root\[data-font-size="large"\] #msg \{ font-size: (\d+)px', css)
        m_xlarge = re.search(r':root\[data-font-size="xlarge"\] #msg \{ font-size: (\d+)px', css)
        assert m_large and m_xlarge and int(m_xlarge.group(1)) > int(m_large.group(1)), \
            "Extra Large composer font-size must be larger than Large"

    def test_file_item_overridden_for_small(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="small"] .file-item' in css, \
            "Workspace file tree text must be explicitly scaled for small"

    def test_file_item_overridden_for_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="large"] .file-item' in css, \
            "Workspace file tree text must be explicitly scaled for large"

    def test_file_item_overridden_for_extra_large(self):
        css = _read("static/style.css")
        assert ':root[data-font-size="xlarge"] .file-item' in css, \
            "Workspace file tree text must be explicitly scaled for extra large"
