from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INDEX = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_quota_indicator_is_next_to_send_button_in_composer_chrome():
    quota_idx = INDEX.find('id="providerQuotaChip"')
    send_idx = INDEX.find('id="btnSend"')

    assert quota_idx != -1, "provider quota chip must exist"
    assert send_idx != -1, "send button must exist"
    assert INDEX.find('class="composer-right"') < quota_idx < send_idx, (
        "quota chip should sit in composer-right immediately before the send button"
    )
    assert 'class="provider-quota-chip"' in INDEX
    assert 'id="providerQuotaRingValue"' in INDEX
    assert 'id="providerQuotaPopover"' in INDEX
    assert 'toggleProviderQuotaPopover(event)' in INDEX
    assert 'hidden' in INDEX[quota_idx - 200 : quota_idx + 900]


def test_quota_indicator_fetches_provider_quota_on_boot():
    assert "function refreshProviderQuotaIndicator" in UI_JS
    assert "api('/api/provider/quota')" in UI_JS
    assert "refreshProviderQuotaIndicator" in BOOT_JS


def test_quota_indicator_hides_unsupported_or_failed_statuses():
    render_idx = UI_JS.find("function renderProviderQuotaIndicator")
    assert render_idx != -1, "renderProviderQuotaIndicator helper must exist"
    render_block = UI_JS[render_idx : UI_JS.find("function ", render_idx + 1)]

    assert "providerQuotaChip" in render_block
    assert "chip.hidden=true" in render_block
    assert "status.status!=='available'" in render_block
    assert "!status.quota" in render_block
    assert "unsupported" not in render_block.lower(), "ambient chip should disappear instead of showing noisy unsupported text"


def test_quota_indicator_formats_openrouter_and_account_limit_shapes_minimally():
    assert "function _providerQuotaIndicatorText" in UI_JS
    assert "limit_remaining" in UI_JS
    assert "account_limits" in UI_JS
    assert "remaining_percent" in UI_JS
    assert "label:remaining" in UI_JS, "quota helpers should still compute compact remaining text for titles/footer"
    assert "label.textContent=''" in UI_JS, "composer quota circle should not show an in-ring percentage label"
    assert "provider+' '+remaining" not in UI_JS, "composer chip should not include provider name in the visible label"
    assert "_providerQuotaWindowModePreference='five_hour'" in UI_JS
    assert "_providerQuotaAccountWindow(status,_providerQuotaWindowModePreference)" in UI_JS
    assert "return '5 hour'" in UI_JS
    assert "return 'Weekly'" in UI_JS
    assert "_providerQuotaRemainingPercent" in UI_JS
    assert "function _formatQuotaResetDate" in UI_JS
    assert "`${mm}/${dd}/${yy}`" in UI_JS
    assert "_formatQuotaResetDate(w.reset_at)" in UI_JS
    assert "providerQuotaRingValue" in UI_JS
    assert "strokeDashoffset" in UI_JS
    assert "toggleProviderQuotaPopover" in UI_JS
    assert "renderProviderQuotaPopover" in UI_JS
    assert "provider-quota-popover-window-option" in UI_JS
    assert "aria-pressed" in UI_JS
    assert "quota-low" in UI_JS and "quota-mid" in UI_JS
    assert "remainingPct<20" in UI_JS
    assert "remainingPct>=20&&remainingPct<60" in UI_JS
    assert "provider-quota-chip" in CSS
    assert ".provider-quota-chip-dot{display:none;}" in CSS
    assert ".provider-quota-chip-label{display:none;}" in CSS
    assert ".provider-quota-ring-value" in CSS
    assert ".provider-quota-popover" in CSS
    assert ".provider-quota-popover-window-option" in CSS
    assert ".provider-quota-chip.quota-mid{color:var(--warning);}" in CSS
    assert ".provider-quota-chip.quota-low{color:var(--error);}" in CSS
    assert "@media (max-width:1399.98px)" in CSS
    assert ".provider-quota-chip{display:none!important;}" in CSS


def test_chat_turn_footer_shows_remaining_provider_quota_after_done():
    assert "function _providerQuotaChatText" in UI_JS
    assert "function attachProviderQuotaToLastAssistant" in UI_JS
    assert "api('/api/provider/quota'" in UI_JS
    assert "api('/api/provider/quota?refresh=1'" not in UI_JS
    assert "_providerQuota" in UI_JS
    assert "msg-provider-quota-inline" in UI_JS
    assert "msg-provider-quota-inline" in CSS
    assert "attachProviderQuotaToLastAssistant(completedSid)" in (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    assert "'_providerQuota'" in (REPO / "static" / "messages.js").read_text(encoding="utf-8")
