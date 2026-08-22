"""Regression coverage for the persistent sidebar multiselect dock."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_batch_dock_is_a_sibling_of_the_scrolling_session_list():
    html = _read("static/index.html")

    list_start = html.index('<div class="session-list" id="sessionList">')
    dock_start = html.index('id="sessionBatchDock"', list_start)
    assert html.index("</div>", list_start) < dock_start
    assert 'class="session-batch-dock"' in html


def test_batch_toolbar_is_rendered_only_in_selection_mode():
    js = _read("static/sessions.js")

    assert "function _renderSessionBatchDock()" in js
    assert "const dock=$('sessionBatchDock')" in js
    assert "if(!_sessionSelectMode){" in js
    assert "dock.style.display='none';" in js
    assert "dock.appendChild(batchBar)" in js
    assert "bar.style.display='flex'" in js
    assert "archiveBtn.disabled=!count" in js
    assert "moveBtn.disabled=!count" in js
    assert "deleteBtn.disabled=!writableSelectionIds.length" in js


def test_one_selection_predicate_drives_checkbox_select_all_and_pruning():
    js = _read("static/sessions.js")

    assert "function _isSessionSelectable(session)" in js
    assert "_isOrganizableReadOnlySession(session)" in js
    assert "_sessionSelectMode&&_isSessionSelectable(s)" in js
    assert "_isSessionSelectable(child)" in js
    assert "_sessionSelectionIds()" in js
    assert "_pruneSelectedSessions" in js


def test_batch_move_keeps_existing_endpoint_and_uses_selected_ids():
    js = _read("static/sessions.js")

    assert "api('/api/session/move'" in js
    assert "const ids=[..._selectedSessions]" in js
    assert "session_batch_move" in js


def test_dock_has_responsive_safe_area_styles():
    css = _read("static/style.css")

    assert ".session-batch-dock" in css
    assert "env(safe-area-inset-bottom)" in css
    assert ".session-batch-dock .batch-action-bar" in css


def test_date_group_select_trigger_is_the_selection_mode_entry_point():
    js = _read("static/sessions.js")

    assert "session-group-select-trigger" in js
    assert "Select conversations" in js
    assert "_enableSessionSelectMode()" in js
    assert "if(!g.isPinned)" in js
