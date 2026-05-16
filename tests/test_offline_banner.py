"""Regression coverage for the browser-offline banner and auto-refresh loop."""

from __future__ import annotations

import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_offline_banner_markup_styles_and_copy_exist():
    assert 'id="offlineBanner"' in INDEX_HTML
    assert 'role="status"' in INDEX_HTML
    assert 'aria-live="assertive"' in INDEX_HTML
    assert 'onclick="checkOfflineRecoveryNow()"' in INDEX_HTML
    assert ".offline-banner" in STYLE_CSS
    assert ".offline-banner.visible" in STYLE_CSS
    assert ".offline-action[disabled]" in STYLE_CSS
    for key in (
        "offline_title",
        "offline_browser_detail",
        "offline_network_detail",
        "offline_autorefresh",
        "offline_check_now",
        "offline_checking",
        "offline_stream_waiting",
    ):
        assert key in I18N_JS


def test_offline_monitor_patches_fetch_and_auto_reloads_after_health_probe():
    assert "const OFFLINE_RECHECK_MS=2500" in UI_JS
    assert "window.fetch=async function(...args)" in UI_JS
    assert "window.addEventListener('offline',()=>showOfflineBanner('browser'))" in UI_JS
    assert "window.addEventListener('online',()=>{if(_offlineVisible)checkOfflineRecoveryNow();})" in UI_JS
    assert "setInterval(()=>{checkOfflineRecoveryNow();},OFFLINE_RECHECK_MS)" in UI_JS
    assert "new URL('health',document.baseURI||location.href)" in UI_JS
    assert "window.location.reload()" in UI_JS


def test_offline_recovery_probe_is_serialized_and_stops_timer_before_reload():
    assert "let _offlineProbePromise=null" in UI_JS
    assert "let _offlineHealthProbePromise=null" in UI_JS
    assert "if(!_offlineVisible)return false;" in UI_JS
    assert "if(!_offlineVisible&&!_offlineFetchPatched)return false;" not in UI_JS
    assert "finally{_offlineProbePromise=null;}" in UI_JS
    assert "finally{_offlineHealthProbePromise=null;}" in UI_JS
    reload_idx = UI_JS.find("window.location.reload()")
    assert reload_idx != -1
    assert UI_JS.rfind("_stopOfflineProbeTimer();", 0, reload_idx) != -1


def test_fetch_typeerror_is_gated_by_health_probe_not_blind_banner():
    fetch_patch = UI_JS.split("window.fetch=async function(...args){", 1)[1].split("function initOfflineMonitor", 1)[0]
    assert "function _isAbortError(e)" in UI_JS
    assert "e instanceof TypeError&&!_isAbortError(e)" in fetch_patch
    assert "void _probeOfflineRecovery().then(ok=>{if(!ok)showOfflineBanner('network');})" in fetch_patch
    assert "if(!_browserReportsOnline())showOfflineBanner('browser');" in fetch_patch
    assert "e instanceof TypeError||!_browserReportsOnline()" not in fetch_patch


def test_sse_network_error_defers_to_offline_banner_instead_of_inline_error():
    assert "function _deferStreamErrorIfOffline()" in MESSAGES_JS
    assert "t('offline_stream_waiting')" in MESSAGES_JS
    assert "if(_deferStreamErrorIfOffline()) return;" in MESSAGES_JS
    error_handler = MESSAGES_JS.split("source.addEventListener('error',async e=>{", 1)[1].split("source.addEventListener('cancel'", 1)[0]
    assert error_handler.find("_deferStreamErrorIfOffline()") < error_handler.rfind("_handleStreamError()")


def test_sse_error_defers_while_page_hidden_until_tab_returns():
    assert "function _deferStreamErrorIfPageHidden()" in MESSAGES_JS
    assert "document.visibilityState==='hidden'" in MESSAGES_JS
    assert "document.wasDiscarded===true" in MESSAGES_JS
    assert "Connection paused. Reconnecting when this tab returns…" in MESSAGES_JS
    assert "document.addEventListener('visibilitychange',resume)" in MESSAGES_JS
    assert "window.addEventListener('pageshow',resume)" in MESSAGES_JS
    error_handler = MESSAGES_JS.split("source.addEventListener('error',async e=>{", 1)[1].split("source.addEventListener('cancel'", 1)[0]
    assert "if(_deferStreamErrorIfPageHidden()) return;" in error_handler
    assert error_handler.find("_deferStreamErrorIfPageHidden()") < error_handler.rfind("_handleStreamError()")


def test_deferred_hidden_stream_error_reattaches_or_restores_before_inline_error():
    recovery_block = MESSAGES_JS.split("function _reattachOrRestoreAfterDeferredStreamError(){", 1)[1].split("function _deferStreamErrorIfPageHidden()", 1)[0]
    assert "api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`)" in recovery_block
    assert "if(st.active)" in recovery_block
    assert "_wireSSE(new EventSource" in recovery_block
    assert "if(await _restoreSettledSession()) return;" in recovery_block
    assert recovery_block.find("if(await _restoreSettledSession()) return;") < recovery_block.rfind("_handleStreamError()")
