"""Browser regression for the docked session batch-selection controls (#6928, #6929)."""
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    marker = f"function {name}"
    start = SESSIONS_JS.find(marker)
    assert start >= 0, f"{name} not found"
    signature_end = re.search(r"\)\s*\{", SESSIONS_JS[start:])
    assert signature_end, f"{name} signature did not close"
    brace = start + signature_end.end() - 1
    depth = 1
    index = brace + 1
    while depth and index < len(SESSIONS_JS):
        if SESSIONS_JS[index] == "{":
            depth += 1
        elif SESSIONS_JS[index] == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"{name} body did not close"
    return SESSIONS_JS[start:index]


def _chat_panel_html() -> str:
    match = re.search(
        r'(<div class="panel-view active" id="panelChat">.*?)(?=\s*<!-- Tasks \(cron\) panel -->)',
        INDEX_HTML,
        re.S,
    )
    assert match, "Chat panel markup not found"
    return match.group(1)


def _fixture_script() -> str:
    functions = [
        "_setActiveProjectFilter",
        "_toggleArchivedSessionsVisibility",
        "_focusSessionBatchControl",
        "_resetSessionSelectionForScopeChange",
        "_pruneSessionSelectionToCurrentScope",
        "toggleSessionSelectMode",
        "exitSessionSelectMode",
        "toggleSessionSelect",
        "setSessionSelected",
        "_selectableSessionIds",
        "selectAllSessions",
        "deselectAllSessions",
        "_updateBatchActionBar",
        "_renderBatchActionBar",
        "_showBatchProjectPicker",
    ]
    return "\n".join(
        [
            "const $ = id => document.getElementById(id);",
            "let _sessionSelectMode = false;",
            "const _selectedSessions = new Set();",
            "let _batchProjectPickerCleanup = null;",
            "let _showArchived = false;",
            "const SESSION_ARCHIVED_PAGE_SIZE = 50;",
            "let _archivedRowsLoadedLimit = 0;",
            "const NO_PROJECT_FILTER = '__none__';",
            "let _activeProject = 'project-1';",
            "let _sessionVisibleSidebarIds = ['session-1', 'session-2'];",
            "const t = (key, value) => ({",
            "  cancel: 'Cancel',",
            "  session_select_mode_desc: 'Conversation selection',",
            "  session_select_all: 'Select all',",
            "  session_deselect_all: 'Deselect all',",
            "  session_selected_count: `${value} selected`,",
            "  session_batch_archive: 'Archive',",
            "  session_batch_move: 'Move',",
            "  session_batch_delete: 'Delete',",
            "}[key] || key);",
            "function renderSessionListFromCache(){ _renderBatchActionBar(); }",
            "function _worktreeSessionCount(){ return 0; }",
            "function _sessionSnapshotById(){ return null; }",
            "function _worktreeResponseCount(){ return 0; }",
            "function showConfirmDialog(){ return Promise.resolve(false); }",
            "let _apiShouldFail = false;",
            "function api(){ return _apiShouldFail ? Promise.reject(new Error('fixture move failed')) : Promise.resolve({}); }",
            "function showToast(){}",
            "function renderSessionList(){",
            "  if(!_showArchived) document.querySelectorAll('[data-archived=true]').forEach(row => row.remove());",
            "  _renderBatchActionBar();",
            "  return Promise.resolve();",
            "}",
            "function _clearHandoffStorageForSession(){}",
            "function _isReadOnlySession(session){ return Boolean(session && session.read_only); }",
            "const _allProjects = Array.from({length: 20}, (_, index) => ({project_id: `project-${index + 1}`, name: `Project ${index + 1}`, color: '#f5c400'}));",
            "const S = {session: null, messages: [], entries: []};",
            *(_function_source(name) for name in functions),
            """
            window.__prepareSessionRows = () => {
              const list = $('sessionList');
              for (let index = 0; index < 32; index += 1) {
                const row = document.createElement('div');
                row.className = 'session-item';
                row.dataset.sid = `session-${index + 1}`;
                row.style.height = '48px';
                row.textContent = `Conversation ${index + 1}`;
                if (index < 2) {
                  const checkbox = document.createElement('input');
                  checkbox.type = 'checkbox';
                  checkbox.className = 'session-select-cb';
                  checkbox.dataset.sid = row.dataset.sid;
                  checkbox.onchange = event => setSessionSelected(row.dataset.sid, event.currentTarget.checked);
                  row.prepend(checkbox);
                }
                list.appendChild(row);
              }
              _renderBatchActionBar();
            };
            window.__sessionSelectionState = () => ({
              mode: _sessionSelectMode,
              selected: [..._selectedSessions],
            });
            window.__setApiFailure = value => { _apiShouldFail = Boolean(value); };
            window.__prepareArchivedSelection = sessionId => {
              const row = document.querySelector(`.session-item[data-sid="${sessionId}"]`);
              row.dataset.archived = 'true';
              _showArchived = true;
            };
            """,
        ]
    )


def _browser_page(viewport: dict[str, int]):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the session batch dock browser test")

    manager = sync_playwright().start()
    try:
        browser = manager.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception:
        manager.stop()
        pytest.skip("playwright chromium is unavailable; run `playwright install chromium`")

    page = browser.new_page(viewport=viewport)
    page.set_content(
        "<!doctype html><html><head><style>"
        + STYLE_CSS
        + "\nhtml,body{margin:0;width:100%;height:100%;}"
        + "\n.sidebar{position:relative!important;left:0!important;width:100%!important;height:100%!important;}"
        + "\n#panelChat{margin-left:0!important;height:100%!important;}"
        + "</style></head><body><aside class=\"sidebar mobile-open\">"
        + _chat_panel_html()
        + "</aside></body></html>"
    )
    page.add_script_tag(content=_fixture_script())
    page.evaluate("window.__prepareSessionRows()")
    return manager, browser, page


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 300, "height": 640},
        {"width": 390, "height": 844},
    ],
)
def test_session_select_controls_stay_in_a_bottom_dock(viewport):
    manager, browser, page = _browser_page(viewport)
    try:
        dock = page.locator("#sessionBatchDock")
        session_list = page.locator("#sessionList")
        select_button = page.get_by_role("button", name="Select", exact=True)

        assert dock.count() == 1
        assert select_button.is_visible()
        assert page.locator("#sessionList #sessionBatchDock").count() == 0

        before = dock.bounding_box()
        assert before
        session_list.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        page.wait_for_timeout(50)
        after = dock.bounding_box()
        assert after
        assert abs(after["y"] - before["y"]) < 1
        assert abs((after["y"] + after["height"]) - viewport["height"]) < 1
        assert session_list.evaluate("el => el.scrollHeight > el.clientHeight")

        select_button.click()
        toolbar = page.get_by_role("toolbar", name="Conversation selection")
        assert toolbar.is_visible()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "batchSelectAllBtn"
        assert not select_button.is_visible()
        assert page.get_by_text("0 selected", exact=True).is_visible()
        assert page.get_by_role("button", name="Archive", exact=True).is_disabled()
        assert page.get_by_role("button", name="Move", exact=True).is_disabled()
        assert page.get_by_role("button", name="Delete", exact=True).is_disabled()

        toolbar_box = toolbar.bounding_box()
        assert toolbar_box
        assert toolbar_box["x"] >= 0
        assert toolbar_box["x"] + toolbar_box["width"] <= viewport["width"] + 1
        assert page.locator("#sessionBatchDock").evaluate(
            "el => el.scrollWidth <= el.clientWidth"
        )
    finally:
        browser.close()
        manager.stop()


def test_session_selection_toolbar_remains_visible_at_zero_and_updates_actions():
    manager, browser, page = _browser_page({"width": 300, "height": 640})
    try:
        page.get_by_role("button", name="Select", exact=True).click()
        toolbar = page.get_by_role("toolbar", name="Conversation selection")
        select_all = page.get_by_role("button", name="Select all", exact=True)
        assert toolbar.is_visible()
        assert select_all.is_enabled()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "batchSelectAllBtn"

        page.locator('.session-select-cb[data-sid="session-1"]').check()
        assert page.get_by_text("1 selected", exact=True).is_visible()
        assert page.get_by_role("button", name="Archive", exact=True).is_enabled()

        select_all.click()
        assert page.get_by_text("2 selected", exact=True).is_visible()
        assert page.get_by_role("button", name="Archive", exact=True).is_enabled()
        assert page.get_by_role("button", name="Move", exact=True).is_enabled()
        assert page.get_by_role("button", name="Delete", exact=True).is_enabled()
        assert page.get_by_role("button", name="Deselect all", exact=True).is_visible()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "batchSelectAllBtn"

        page.get_by_role("button", name="Deselect all", exact=True).click()
        assert toolbar.is_visible()
        assert page.get_by_text("0 selected", exact=True).is_visible()
        assert page.get_by_role("button", name="Archive", exact=True).is_disabled()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "batchSelectAllBtn"

        page.get_by_role("button", name="Cancel", exact=True).click()
        assert not toolbar.is_visible()
        assert page.get_by_role("button", name="Select", exact=True).is_visible()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "sessionSelectToggle"
        assert page.evaluate("window.__sessionSelectionState()") == {
            "mode": False,
            "selected": [],
        }
    finally:
        browser.close()
        manager.stop()


def test_scope_replacement_releases_selection_state():
    manager, browser, page = _browser_page({"width": 300, "height": 640})
    try:
        page.get_by_role("button", name="Select", exact=True).click()
        page.locator('.session-select-cb[data-sid="session-1"]').check()
        page.evaluate("_resetSessionSelectionForScopeChange()")

        assert page.get_by_role("button", name="Select", exact=True).is_visible()
        assert not page.get_by_role("toolbar", name="Conversation selection").is_visible()
        assert page.evaluate("window.__sessionSelectionState()") == {
            "mode": False,
            "selected": [],
        }
    finally:
        browser.close()
        manager.stop()


def test_project_filter_change_releases_hidden_selection_state():
    manager, browser, page = _browser_page({"width": 300, "height": 640})
    try:
        page.get_by_role("button", name="Select", exact=True).click()
        page.locator('.session-select-cb[data-sid="session-1"]').check()

        page.evaluate("_setActiveProjectFilter('project-2')")

        assert page.get_by_role("button", name="Select", exact=True).is_visible()
        assert not page.get_by_role("toolbar", name="Conversation selection").is_visible()
        assert page.get_by_role("button", name="Archive", exact=True).count() == 0
        assert page.get_by_role("button", name="Move", exact=True).count() == 0
        assert page.get_by_role("button", name="Delete", exact=True).count() == 0
        assert page.evaluate("window.__sessionSelectionState()") == {
            "mode": False,
            "selected": [],
        }
    finally:
        browser.close()
        manager.stop()


def test_hiding_archived_releases_hidden_selection_state():
    manager, browser, page = _browser_page({"width": 300, "height": 640})
    try:
        page.evaluate("window.__prepareArchivedSelection('session-1')")
        page.get_by_role("button", name="Select", exact=True).click()
        page.locator('.session-select-cb[data-sid="session-1"]').check()

        page.evaluate("_toggleArchivedSessionsVisibility()")

        assert page.locator('.session-item[data-sid="session-1"]').count() == 0
        assert page.get_by_role("button", name="Select", exact=True).is_visible()
        assert not page.get_by_role("toolbar", name="Conversation selection").is_visible()
        assert page.get_by_role("button", name="Archive", exact=True).count() == 0
        assert page.get_by_role("button", name="Move", exact=True).count() == 0
        assert page.get_by_role("button", name="Delete", exact=True).count() == 0
        assert page.evaluate("window.__sessionSelectionState()") == {
            "mode": False,
            "selected": [],
        }
    finally:
        browser.close()
        manager.stop()


def test_authoritative_refresh_prunes_removed_session_ids():
    manager, browser, page = _browser_page({"width": 300, "height": 640})
    try:
        page.get_by_role("button", name="Select", exact=True).click()
        page.locator('.session-select-cb[data-sid="session-1"]').check()
        page.locator('.session-select-cb[data-sid="session-2"]').check()

        changed = page.evaluate(
            "_pruneSessionSelectionToCurrentScope([{session_id:'session-2'}], [])"
        )
        assert changed is True
        assert page.evaluate("window.__sessionSelectionState()") == {
            "mode": True,
            "selected": ["session-2"],
        }
        assert page.get_by_text("1 selected", exact=True).is_visible()

        removed_read_only = page.evaluate(
            "_selectedSessions.add('read-only'); "
            "_pruneSessionSelectionToCurrentScope("
            "[{session_id:'session-2'},{session_id:'read-only',read_only:true}], [])"
        )
        assert removed_read_only is True
        assert page.evaluate("window.__sessionSelectionState().selected") == ["session-2"]
    finally:
        browser.close()
        manager.stop()


def test_batch_move_picker_is_bounded_and_keyboard_accessible():
    manager, browser, page = _browser_page({"width": 300, "height": 640})
    try:
        page.get_by_role("button", name="Select", exact=True).click()
        page.locator('.session-select-cb[data-sid="session-1"]').check()
        move_button = page.get_by_role("button", name="Move", exact=True)
        move_button.click()

        picker = page.get_by_role("group", name="Move")
        assert picker.is_visible()
        assert move_button.get_attribute("aria-expanded") == "true"
        page.wait_for_function(
            "document.activeElement && document.activeElement.textContent === 'No project'"
        )
        assert picker.evaluate("el => el.scrollHeight > el.clientHeight")

        page.evaluate("_renderBatchActionBar()")
        page.wait_for_timeout(100)
        assert picker.is_visible()
        assert page.evaluate("document.activeElement && document.activeElement.textContent") == "No project"

        picker_box = picker.bounding_box()
        viewport_height = page.evaluate("window.innerHeight")
        assert picker_box
        assert picker_box["y"] >= 0
        assert picker_box["y"] + picker_box["height"] <= viewport_height + 1

        page.keyboard.press("ArrowDown")
        assert page.evaluate("document.activeElement && document.activeElement.textContent.trim()") == "Project 1"
        page.keyboard.press("Escape")
        assert not picker.is_visible()
        assert move_button.get_attribute("aria-expanded") == "false"
        assert page.get_by_role("toolbar", name="Conversation selection").is_visible()
        assert page.evaluate("window.__sessionSelectionState().mode") is True
        assert page.evaluate("document.activeElement && document.activeElement.id") == "batchMoveBtn"

        page.evaluate("window.__setApiFailure(true)")
        move_button.click()
        page.get_by_role("button", name="No project", exact=True).click()
        page.wait_for_timeout(100)
        assert page.get_by_role("toolbar", name="Conversation selection").is_visible()
        assert page.evaluate("window.__sessionSelectionState().mode") is True
        assert page.evaluate("document.activeElement && document.activeElement.id") == "batchMoveBtn"
    finally:
        browser.close()
        manager.stop()
