from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INDEX = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_quota_indicator_is_near_model_picker_in_composer_chrome():
    model_idx = INDEX.find('id="composerModelChip"')
    quota_idx = INDEX.find('id="providerQuotaChip"')

    assert model_idx != -1, "composer model chip must exist"
    assert quota_idx != -1, "provider quota chip must exist"
    assert model_idx < quota_idx < INDEX.find('id="composerReasoningWrap"'), (
        "quota chip should sit next to the model picker, before reasoning/toolset chrome"
    )
    assert 'class="provider-quota-chip"' in INDEX
    assert 'hidden' in INDEX[quota_idx - 200 : quota_idx + 400]


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


def test_quota_indicator_formats_openrouter_and_account_limit_shapes():
    assert "function _providerQuotaIndicatorText" in UI_JS
    assert "limit_remaining" in UI_JS
    assert "account_limits" in UI_JS
    assert "remaining_percent" in UI_JS
    assert "provider-quota-chip" in CSS
