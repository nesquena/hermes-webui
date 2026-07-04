"""Regression test for #show-quota-chip-toggle — Settings toggle to opt into the ambient quota chip.

Quota chip default state is now OFF (per Nathan's directive 2026-05-16, immediately
after the stage-371 release of #2082). Users opt in via Settings → Preferences.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
PANELS = REPO_ROOT / "static" / "panels.js"
UI_JS = REPO_ROOT / "static" / "ui.js"
BOOT = REPO_ROOT / "static" / "boot.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"


def test_quota_chip_settings_field_present():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsShowQuotaChip"' in html
    assert 'data-i18n="settings_label_quota_chip"' in html
    assert 'data-i18n="settings_desc_quota_chip"' in html


def test_quota_chip_default_off_in_config_defaults():
    src = CONFIG.read_text(encoding="utf-8")
    assert '"show_quota_chip": False' in src, "show_quota_chip must default to False (opt-in)"
    # Must be in the writable settings allow-list (bool keys)
    assert '"show_quota_chip",' in src, "show_quota_chip must be in _SETTINGS_BOOL_KEYS"


def test_quota_chip_render_hides_ambient_chip_but_keeps_dropdown_cache_when_disabled():
    """show_quota_chip controls only the ambient composer chip.

    Compact composer layouts hide the ambient chip, so Codex quota still needs to
    be fetched/cached for the model picker row even when the opt-in chip is off.
    """
    js = UI_JS.read_text(encoding="utf-8")

    render_start = js.index("function renderProviderQuotaIndicator(status){")
    render_end = js.index("\nasync function refreshProviderQuotaIndicator", render_start)
    render_body = js[render_start:render_end]
    assert "window._showQuotaChip!==true" in render_body, (
        "renderProviderQuotaIndicator must check window._showQuotaChip before showing the ambient chip"
    )
    cache_idx = render_body.index("_providerQuotaLastText=text||null")
    guard_idx = render_body.index("window._showQuotaChip!==true")
    assert cache_idx < guard_idx, (
        "Quota text must be cached before hiding the ambient chip so the model picker can show it"
    )

    refresh_start = js.index("async function refreshProviderQuotaIndicator(){")
    refresh_end = js.index("\nwindow.addEventListener('visibilitychange'", refresh_start)
    refresh_body = js[refresh_start:refresh_end]
    assert "api(_providerQuotaIndicatorUrl())" in refresh_body
    assert "window._showQuotaChip!==true" not in refresh_body, (
        "refreshProviderQuotaIndicator must still fetch for model-picker quota badges"
    )


def test_quota_chip_boot_initializes_default_off():
    js = BOOT.read_text(encoding="utf-8")
    # Both success path (reads from settings) and failure path (defaults block)
    # must set window._showQuotaChip
    assert "window._showQuotaChip=s.show_quota_chip===true" in js, (
        "Boot must initialize _showQuotaChip from settings.show_quota_chip"
    )
    assert "window._showQuotaChip=false" in js, (
        "Boot must default _showQuotaChip to false in the settings-fetch-failed branch"
    )


def test_quota_chip_panels_round_trip():
    js = PANELS.read_text(encoding="utf-8")
    # Payload read
    assert "const showQuotaChipCb=$('settingsShowQuotaChip');" in js
    assert "payload.show_quota_chip=showQuotaChipCb.checked;" in js
    # Body assignment
    assert "body.show_quota_chip=showQuotaChip===true;" in js
    # Settings panel load — checkbox is initialized from saved settings
    assert "showQuotaChipCb.checked=settings.show_quota_chip===true;" in js
    # Window-state propagation
    assert "window._showQuotaChip=showQuotaChip===true;" in js
    # Live refresh on toggle (immediate visual feedback)
    assert "if(typeof refreshProviderQuotaIndicator==='function') refreshProviderQuotaIndicator();" in js


def test_quota_chip_localized_in_all_locales():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_quota_chip:") == 14, "12 locales expected"
    assert js.count("settings_desc_quota_chip:") == 14, "12 locales expected"
