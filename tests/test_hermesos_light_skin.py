"""HermesOS light appearance preset.

HermesOS is a skin layered over the light/dark theme axis. The UI needs an
obvious one-click light version so users do not have to infer that "Light" +
"HermesOS" is the intended pairing.
"""

import re
from pathlib import Path

REPO = Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def test_hermesos_skin_defines_full_light_and_dark_palettes():
    """HermesOS must be a real light/dark skin, not only a dark theme."""
    assert ':root[data-skin="hermesos"]{' in CSS
    assert ':root.dark[data-skin="hermesos"]{' in CSS

    for token in ("--bg:#fdfcf9", "--sidebar:#faf9f6", "--accent:#c5a059"):
        assert token in CSS, f"HermesOS light token missing: {token}"
    for token in ("--bg:#0d0d0d", "--sidebar:#141414", "--accent:#d4af37"):
        assert token in CSS, f"HermesOS dark token missing: {token}"


def test_hermesos_light_is_a_visible_theme_picker_shortcut():
    """Appearance settings should expose HermesOS Light as a deliberate choice."""
    assert 'data-theme-val="hermesos-light"' in INDEX_HTML
    assert "_pickTheme('hermesos-light')" in INDEX_HTML
    assert "HermesOS Light" in INDEX_HTML


def test_hermesos_light_preset_maps_to_light_theme_and_hermesos_skin():
    """The shortcut should persist existing schema values: theme=light, skin=hermesos."""
    preset_pattern = re.compile(
        r"'hermesos-light'\s*:\s*\{\s*theme\s*:\s*'light'\s*,\s*skin\s*:\s*'hermesos'\s*\}"
    )
    assert preset_pattern.search(BOOT_JS), (
        "HermesOS Light must normalize to the existing light+hermesos appearance pair"
    )


def test_hermesos_light_shortcut_highlights_only_when_light_skin_pair_is_active():
    """The picker should not mark both generic Light and HermesOS Light active."""
    helper_idx = BOOT_JS.find("function _isThemePickerActive(")
    sync_idx = BOOT_JS.find("function _syncThemePicker(")
    assert helper_idx >= 0, "_isThemePickerActive function missing"
    assert sync_idx >= 0, "_syncThemePicker function missing"
    sync_block = BOOT_JS[helper_idx : sync_idx + 400]

    assert "hermesos-light" in sync_block
    assert "isHermesosLight" in sync_block
    assert "val==='light'?active==='light'&&!isHermesosLight" in sync_block.replace(" ", "")
    assert "_isThemePickerActive(btn.dataset.themeVal,active)" in sync_block


def test_theme_command_accepts_hermesos_light_shortcut():
    """The slash command should accept the same preset as the visible picker."""
    assert "Object.keys(_APPEARANCE_PRESETS||{})" in COMMANDS_JS
