from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


def test_single_available_credential_pool_is_summarized_without_duplicate_breakdown():
    assert "function _providerQuotaIsSingleAvailableCredentialPool" in PANELS_JS
    assert "pool.credentials.length!==1" in PANELS_JS
    assert "total===1&&available===1&&exhausted===0&&failed===0" in PANELS_JS
    assert "if(_providerQuotaIsSingleAvailableCredentialPool(pool)) return ''" in PANELS_JS


def test_single_available_credential_pool_hides_best_of_one_window_detail():
    assert "const singleAvailablePool=_providerQuotaIsSingleAvailableCredentialPool(accountLimits.pool);" in PANELS_JS
    assert "const detail=singleAvailablePool?'':((w&&w.detail)?String(w.detail).trim():'');" in PANELS_JS


def test_multi_credential_pool_still_has_breakdown_and_can_default_open():
    assert "return count>1&&count<=3;" in PANELS_JS
    assert "provider-quota-pool" in PANELS_JS
    assert "provider-quota-pool-rows" in PANELS_JS
    assert "provider_quota_pool_summary_available" in PANELS_JS
