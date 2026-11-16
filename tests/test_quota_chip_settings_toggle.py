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


def test_quota_chip_render_short_circuits_when_disabled():
    """Both renderProviderQuotaIndicator and refreshProviderQuotaIndicator must
    hide the chip when window._showQuotaChip !== true. Specifically renderer
    must hide BEFORE any other render logic, and refresher must skip the fetch
    entirely so we don't burn quota API calls for chip-disabled users."""
    js = UI_JS.read_text(encoding="utf-8")

    # Renderer must early-hide when disabled
    render_start = js.index("function renderProviderQuotaIndicator(status){")
    render_end = js.index("\nasync function refreshProviderQuotaIndicator", render_start)
    render_body = js[render_start:render_end]
    assert "window._showQuotaChip!==true" in render_body, (
        "renderProviderQuotaIndicator must check window._showQuotaChip before rendering"
    )
    # Guard must come BEFORE the existing _providerQuotaIndicatorText(status) call
    guard_idx = render_body.index("window._showQuotaChip!==true")
    text_call_idx = render_body.index("_providerQuotaIndicatorText(status)")
    assert guard_idx < text_call_idx, (
        "Disabled-chip guard must run before the indicator-text computation"
    )

    # Refresher must short-circuit fetch when disabled
    refresh_start = js.index("async function refreshProviderQuotaIndicator(providerId){")
    # Find the closing brace of the function — first 'try{' line marks the live body
    # Just check the entire snippet ahead of try{
    refresh_head = js[refresh_start:js.index("try{", refresh_start)]
    assert "window._showQuotaChip!==true" in refresh_head, (
        "refreshProviderQuotaIndicator must skip the fetch when chip is disabled"
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
    # Live refresh uses the active session provider (immediate visual feedback without
    # falling back to a different configured default during a provider-specific chat).
    assert "refreshProviderQuotaIndicator(provider)" in js
    assert "S.session.model_provider" in js


def test_quota_chip_localized_in_all_locales():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_quota_chip:") == 15, "12 locales expected"
    assert js.count("settings_desc_quota_chip:") == 15, "12 locales expected"


def test_quota_chip_window_description_names_the_real_surfaces_and_the_fallback():
    """The description points to the hover and Settings → Providers (there is no
    separate quota panel) and says what happens when lengths aren't reported."""
    expected = (
        "For providers with several limits (e.g. 5-hour, weekly), which one the chip shows. "
        "Hover the chip or open Settings → Providers to see all of them. "
        "If a provider doesn't report limit lengths, the chip shows the lowest remaining."
    )
    js = I18N.read_text(encoding="utf-8")
    entry = "settings_desc_quota_chip_window: '" + expected.replace("'", "\\'") + "',"
    assert js.count("settings_desc_quota_chip_window:") == 15
    assert js.count(entry) == 15
    html = INDEX.read_text(encoding="utf-8")
    assert 'data-i18n="settings_desc_quota_chip_window">' + expected + "</div>" in html
    assert "quota panel" not in js and "quota panel" not in html


def test_quota_chip_window_label_reads_quota_chip_shows():
    """#8548: the select's label says what it picks ("Quota chip shows"), not the
    ambiguous "Quota chip window". Copy only: the i18n key and select id stay."""
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_quota_chip_window:") == 15
    assert js.count("settings_label_quota_chip_window: 'Quota chip shows',") == 15
    assert "Quota chip window" not in js
    html = INDEX.read_text(encoding="utf-8")
    assert (
        '<label for="settingsQuotaChipWindow" data-i18n="settings_label_quota_chip_window">'
        "Quota chip shows</label>"
    ) in html
    assert "Quota chip window" not in html
