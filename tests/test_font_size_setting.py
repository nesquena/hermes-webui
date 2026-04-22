"""Tests for font size setting (#833) — 3-toggle Small/Default/Large in Appearance."""
import os
import re

_SRC = os.path.join(os.path.dirname(__file__), "..")

def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


class TestFontSizeCssModifiers:
    """CSS must define font-size overrides for small and large via data attribute."""

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

    def test_small_is_smaller_than_default(self):
        css = _read("static/style.css")
        m_small = re.search(r':root\[data-font-size="small"\]\{font-size:(\d+)px', css)
        m_large = re.search(r':root\[data-font-size="large"\]\{font-size:(\d+)px', css)
        assert m_small and m_large, "Both small and large font-size rules must set px values"
        assert int(m_small.group(1)) < 14, "Small font size must be < 14px (default)"
        assert int(m_large.group(1)) > 14, "Large font size must be > 14px (default)"


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

    def test_three_font_size_values_present(self):
        html = _read("static/index.html")
        assert 'data-font-size-val="small"' in html, "Small button must exist"
        assert 'data-font-size-val="default"' in html, "Default button must exist"
        assert 'data-font-size-val="large"' in html, "Large button must exist"

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

    REQUIRED_KEYS = {"settings_label_font_size", "font_size_small", "font_size_default", "font_size_large"}

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
