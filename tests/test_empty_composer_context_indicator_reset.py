"""Regression coverage for stale context usage leaking into an empty composer."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def test_empty_composer_reset_clears_transient_usage_and_hides_ring():
    start = UI_JS.find("function _resetCtxIndicatorForEmptyComposer()")
    assert start != -1
    end = UI_JS.find("// Context usage indicator in composer footer", start)
    assert end != -1
    block = UI_JS[start:end]

    assert "S.lastUsage={};" in block
    assert "_syncCtxIndicator({});" in block


def test_new_session_clears_context_indicator_before_network_create():
    start = SESSIONS_JS.find("async function newSession(")
    assert start != -1
    post = SESSIONS_JS.find("const data=await api('/api/session/new'", start)
    assert post != -1
    block = SESSIONS_JS[start:post]

    assert "_resetCtxIndicatorForEmptyComposer()" in block


def test_blank_boot_and_bfcache_paths_clear_context_indicator():
    no_saved = BOOT_JS.find("// no saved session - show empty state")
    assert no_saved != -1
    no_saved_end = BOOT_JS.find("S._bootReady=true;", no_saved)
    assert no_saved_end != -1
    assert "_resetCtxIndicatorForEmptyComposer()" in BOOT_JS[no_saved:no_saved_end]

    pageshow = BOOT_JS.find("window.addEventListener('pageshow'")
    assert pageshow != -1
    pageshow_end = BOOT_JS.find("// Re-synchronise layout chrome", pageshow)
    assert pageshow_end != -1
    block = BOOT_JS[pageshow:pageshow_end]
    assert "else if (typeof _resetCtxIndicatorForEmptyComposer === 'function')" in block
    assert "_resetCtxIndicatorForEmptyComposer();" in block


def test_deleting_active_session_clears_context_indicator_before_fallback():
    reset_call = (
        "if(typeof _resetCtxIndicatorForEmptyComposer==='function') "
        "_resetCtxIndicatorForEmptyComposer();"
    )
    assert SESSIONS_JS.count(reset_call) >= 3
