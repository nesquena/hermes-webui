"""Regression coverage for #2518 new-conversation cold-start dedupe."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_new_session_reuses_inflight_request_before_posting_again():
    src = _source("static/sessions.js")
    assert "let _newSessionInFlight=null" in src
    assert "if(_newSessionInFlight){" in src
    assert "return _newSessionInFlight;" in src, (
        "newSession() must return the existing create promise so rapid clicks do "
        "not enqueue multiple /api/session/new requests"
    )
    assert "_newSessionInFlight=(async()=>" in src
    assert "_newSessionInFlight=null;" in src


def test_new_session_sets_visible_pending_state_for_cold_catalog_wait():
    src = _source("static/sessions.js")
    assert "function _setNewSessionPending(pending)" in src
    assert "btn.disabled=!!pending" in src
    assert "btn.setAttribute('aria-busy',pending?'true':'false')" in src
    assert "setComposerStatus(pendingText)" in src
    assert "t('new_session_creating')" in src


def test_new_session_pending_button_style_and_copy_exist():
    css = _source("static/style.css")
    i18n = _source("static/i18n.js")
    assert '.panel-head-btn:disabled,.panel-head-btn[aria-busy="true"]' in css
    assert "cursor:wait" in css
    assert "new_session_creating: 'Creating new conversation…'" in i18n
