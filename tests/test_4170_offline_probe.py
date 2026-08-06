"""Regression coverage for unreliable navigator.onLine offline reports."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//.*", "", src)


def _fn_body(src: str, marker: str) -> str:
    idx = src.find(marker)
    assert idx != -1, f"{marker!r} not found"
    brace = src.find("{", idx)
    assert brace != -1, f"{marker!r} has no body"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{marker!r} body did not close"
    return src[brace + 1 : i - 1]


def test_recovery_loop_probes_even_when_browser_reports_offline():
    body = _strip_comments(_fn_body(UI_JS, "async function checkOfflineRecoveryNow("))
    assert "_probeOfflineRecovery()" in body
    assert "if(!_browserReportsOnline()){showOfflineBanner('browser');return false;}" not in body
    assert body.find("_probeOfflineRecovery()") < body.find("showOfflineBanner(")
    assert "if(ok){_offlineFetchProbeFailures=0;if(!_offlineVisible)return true;_stopOfflineProbeTimer();await _recoverFromOfflineSoftly();return true;}" in body
    assert "showOfflineBanner(_browserReportsOnline()?'network':'browser')" in body


def test_probe_backed_browser_banner_helper_uses_health_as_source_of_truth():
    body = _strip_comments(_fn_body(UI_JS, "async function _showOfflineBannerIfProbeFails("))
    assert "opts=opts||{};" in body
    assert "const visibleAtStart=_offlineVisible;" in body
    assert "const requireConsecutiveFailures=opts.requireConsecutiveFailures!==false;" in body
    assert "if(visibleAtStart)_setOfflineChecking(true);" in body
    assert "const ok=await _probeOfflineRecovery();" in body
    assert "if(visibleAtStart)_setOfflineChecking(false);" in body
    assert "if(ok)" in body
    assert "_offlineFetchProbeFailures<OFFLINE_FETCH_FAILURES_BEFORE_BANNER" in body
    assert "showOfflineBanner(reason||(_browserReportsOnline()?'network':'browser'))" in body
    assert body.find("_probeOfflineRecovery()") < body.find("showOfflineBanner(")


def test_offline_event_and_startup_false_online_probe_before_browser_banner():
    body = _strip_comments(_fn_body(UI_JS, "function initOfflineMonitor("))
    assert "window.addEventListener('offline',()=>showOfflineBanner('browser'))" not in body
    assert "if(!_browserReportsOnline())showOfflineBanner('browser');" not in body
    assert "window.addEventListener('offline',()=>{void _showOfflineBannerIfProbeFails('browser',{requireConsecutiveFailures:false});});" in body
    assert "if(!_browserReportsOnline())void _showOfflineBannerIfProbeFails('browser',{requireConsecutiveFailures:false});" in body


def test_fetch_catch_probes_before_browser_banner_and_rethrows():
    body = _strip_comments(_fn_body(UI_JS, "function _patchOfflineFetch("))
    fetch_body = body.split("window.fetch=async function(...args){", 1)[1]
    assert "if(!_browserReportsOnline())showOfflineBanner('browser');" not in fetch_body
    assert "void _showOfflineBannerIfProbeFails(_browserReportsOnline()?'network':'browser');" in fetch_body
    assert "throw e;" in fetch_body
    assert fetch_body.find("_showOfflineBannerIfProbeFails(") < fetch_body.find("throw e;")
