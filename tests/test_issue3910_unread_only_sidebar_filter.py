"""Regression coverage for issue #3910 unread-only sidebar filtering."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
STYLE_CSS = ROOT / "static" / "style.css"


def _js() -> str:
    return SESSIONS_JS.read_text(encoding="utf-8")


def _css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


def test_unread_only_filter_state_persists_in_local_storage():
    js = _js()
    assert "let _sessionUnreadOnlyFilter = false;" in js
    assert "_restoreSessionUnreadOnlyFilter();" in js
    assert "_activeProject = null;" in js
    assert "localStorage.setItem('hermes-session-unread-only-filter', next ? '1' : '0')" in js
    assert "const raw = localStorage.getItem('hermes-session-unread-only-filter');" in js
    assert "if (raw === '1' || raw === '0') _sessionUnreadOnlyFilter = raw === '1';" in js


def test_unread_only_toggle_uses_sidebar_filter_chip_pattern():
    js = _js()
    css = _css()
    assert "sourceTabs.className='session-source-tabs';" in js
    assert "unreadBtn.className='session-source-tab session-source-toggle'+(_sessionUnreadOnlyFilter?' active':'');" in js
    assert "unreadBtn.textContent=_sessionUnreadOnlyLabel(unreadCount);" in js
    assert "unreadBtn.onclick=()=>_setSessionUnreadOnlyFilter(!_sessionUnreadOnlyFilter);" in js
    assert ".session-source-toggle{flex:0 0 auto;padding-inline:10px;}" in css


def test_unread_only_filter_runs_inside_partition_pipeline():
    js = _js()
    assert "function _sessionHasUnreadForSidebar(s, viewedCounts=null)" in js
    assert "const viewedCounts=_getSessionViewedCounts();" in js
    assert "let unreadCount=0;" in js
    assert "const unreadById=new Map();" in js
    assert "const hasUnread=_sessionHasUnreadForSidebar(s, viewedCounts);" in js
    assert "unreadById.set(s.session_id, hasUnread);" in js
    assert "if(hasUnread) unreadCount++;" in js
    assert "if(_sessionUnreadOnlyFilter&&!hasUnread) continue;" in js
    assert "unreadCount," in js
    assert "unreadById," in js


def test_unread_only_composes_with_source_filter_before_row_rendering():
    js = _js()
    assert "const showCliOnly=_sessionSourceFilter==='cli';" in js
    assert "if(showCliOnly ? !isCli : isCli) continue;" in js
    assert "if(_sessionSourceFilter==='cli' && !window._showCliSessions && cliSessionCount===0 && !_sessionUnreadOnlyFilter){" in js
    source_gate = js.index("if(showCliOnly ? !isCli : isCli) continue;")
    unread_gate = js.index("if(_sessionUnreadOnlyFilter&&!hasUnread) continue;")
    assert source_gate < unread_gate, (
        "The unread-only predicate must run after the source filter inside "
        "_partitionSidebarSessionRows so the filters compose in one pipeline."
    )
    assert "const hasUnread=Boolean(unreadById.get(s.session_id))&&!isActive;" in js


def test_unread_only_empty_state_is_specific():
    js = _js()
    assert "if(_sessionUnreadOnlyFilter&&sessions.length===0){" in js
    assert "? 'Enable Show agent sessions in Settings to list unread CLI sessions here.'" in js
    assert ": 'No unread sessions match the current filters.'" in js
