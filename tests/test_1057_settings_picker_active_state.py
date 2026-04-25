"""Regression tests for settings picker active-state highlighting.

The theme, skin, and font-size pickers in the Appearance settings tab must show
the currently-selected option with a visible accent border. This was broken because
the CSS rule used !important on border-color:var(--border) which overrode the inline
style that _syncThemePicker/etc. set. Fixed by moving to .active CSS class + !important
override on the active state.

Issue: #1057
"""
from pathlib import Path

BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (Path(__file__).parent.parent / "static" / "style.css").read_text(encoding="utf-8")


class TestSettingsPickerActiveState:
    """The selected picker card must be visually distinct via the .active class."""

    def test_theme_picker_uses_active_class(self):
        """_syncThemePicker must toggle .active class, not set inline borderColor."""
        idx = BOOT_JS.find("function _syncThemePicker(")
        assert idx >= 0, "_syncThemePicker function not found in boot.js"
        body = BOOT_JS[idx:idx + 300]
        assert "classList.toggle" in body, (
            "_syncThemePicker must use classList.toggle('active', ...) — "
            "inline style.borderColor is overridden by !important CSS rules"
        )
        # Confirm no accent/border2 color values set inline (clearing with '' is OK)
        assert "var(--accent)" not in body and "var(--border2)" not in body, (
            "_syncThemePicker must not set var(--accent) or var(--border2) inline — "
            "those are overridden by !important CSS rules"
        )

    def test_font_size_picker_uses_active_class(self):
        """_syncFontSizePicker must toggle .active class."""
        idx = BOOT_JS.find("function _syncFontSizePicker(")
        assert idx >= 0, "_syncFontSizePicker function not found in boot.js"
        body = BOOT_JS[idx:idx + 300]
        assert "classList.toggle" in body, (
            "_syncFontSizePicker must use classList.toggle('active', ...)"
        )
        assert "var(--accent)" not in body and "var(--border2)" not in body, (
            "_syncFontSizePicker must not set var(--accent) or var(--border2) inline"
        )

    def test_skin_picker_uses_active_class(self):
        """_syncSkinPicker must toggle .active class."""
        idx = BOOT_JS.find("function _syncSkinPicker(")
        assert idx >= 0, "_syncSkinPicker function not found in boot.js"
        body = BOOT_JS[idx:idx + 300]
        assert "classList.toggle" in body, (
            "_syncSkinPicker must use classList.toggle('active', ...)"
        )
        assert "var(--accent)" not in body and "var(--border2)" not in body, (
            "_syncSkinPicker must not set var(--accent) or var(--border2) inline"
        )

    def test_css_active_rule_beats_base_rule(self):
        """CSS must have a .active rule with !important that overrides the base border-color rule."""
        assert ".theme-pick-btn.active" in STYLE_CSS, (
            "style.css must have a .theme-pick-btn.active rule"
        )
        assert ".font-size-pick-btn.active" in STYLE_CSS, (
            "style.css must have a .font-size-pick-btn.active rule"
        )
        assert ".skin-pick-btn.active" in STYLE_CSS, (
            "style.css must have a .skin-pick-btn.active rule"
        )
        # The active rule must use !important to beat the base !important rule
        idx = STYLE_CSS.find(".theme-pick-btn.active")
        rule = STYLE_CSS[idx:idx + 200]
        assert "!important" in rule, (
            ".theme-pick-btn.active must use !important to override "
            "the base border-color:var(--border)!important rule"
        )

    def test_active_rule_uses_accent_color(self):
        """The .active rule must apply the accent color to make selection visible."""
        idx = STYLE_CSS.find(".theme-pick-btn.active")
        rule = STYLE_CSS[idx:idx + 200]
        assert "var(--accent)" in rule, (
            ".theme-pick-btn.active must set border-color to var(--accent)"
        )
