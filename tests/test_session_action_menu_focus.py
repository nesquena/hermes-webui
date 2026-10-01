"""Browser regression coverage for the portaled conversation-actions menu."""
from pathlib import Path
import re

import pytest


SESSIONS_JS = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(
    encoding="utf-8"
)


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


def _fixture_script() -> str:
    """Run the production menu lifecycle in a small real-DOM fixture.

    The menu is deliberately portaled to body in production. This fixture stubs
    only positioning/animation helpers, so the browser verifies the real focus,
    ARIA, and keyboard lifecycle without needing a live agent session.
    """
    return "\n".join(
        [
            "let _sessionActionMenu = null;",
            "let _sessionActionAnchor = null;",
            "let _sessionActionSessionId = null;",
            "let _sessionActionPreviousFocus = null;",
            "const esc = value => String(value);",
            "function _positionSessionActionMenu(){}",
            "function _playSessionActionMenuEntrance(){}",
            _function_source("_focusSessionActionMenuRestoreTarget"),
            _function_source("closeSessionActionMenu"),
            _function_source("_buildSessionAction"),
            _function_source("_mountSessionActionMenu"),
            """
            window.__sessionActionMenuFocusResult = () => {
              const row = document.createElement('div');
              row.className = 'session-item';
              const trigger = document.createElement('button');
              trigger.className = 'session-actions-trigger';
              trigger.setAttribute('aria-haspopup', 'menu');
              trigger.setAttribute('aria-expanded', 'false');
              trigger.setAttribute('aria-label', 'Conversation actions');
              row.appendChild(trigger);
              document.body.appendChild(row);
              trigger.focus();

              const menu = document.createElement('div');
              menu.className = 'session-action-menu';
              menu.id = 'sessionActionMenu-browser-test';
              menu.setAttribute('role', 'menu');
              menu.setAttribute('aria-label', 'Conversation actions');
              menu.appendChild(_buildSessionAction('Copy conversation link', '', '', () => {}));
              menu.appendChild(_buildSessionAction('Rename conversation', '', '', () => {}));
              menu.appendChild(_buildSessionAction('Delete conversation', '', '', () => {}));
              _mountSessionActionMenu(menu, {session_id: 'browser-focus-test'}, trigger);

              const result = {
                expandedOnOpen: trigger.getAttribute('aria-expanded'),
                controlsOnOpen: trigger.getAttribute('aria-controls'),
                menuRole: menu.getAttribute('role'),
                firstActionFocused: document.activeElement === menu.querySelector('.session-action-opt'),
                firstActionRole: document.activeElement.getAttribute('role'),
              };
              menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
              result.arrowDownText = document.activeElement.textContent.trim();
              menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true}));
              result.endText = document.activeElement.textContent.trim();
              menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
              result.menuRemovedOnEscape = !document.querySelector('.session-action-menu');
              result.focusRestoredOnEscape = document.activeElement === trigger;
              result.expandedAfterEscape = trigger.getAttribute('aria-expanded');
              result.controlsAfterEscape = trigger.getAttribute('aria-controls');
              row.remove();
              return result;
            };

            window.__sessionActionMenuNonFocusableOpenerResult = () => {
              const priorFocus = document.createElement('button');
              priorFocus.textContent = 'Prior keyboard focus';
              const row = document.createElement('div');
              row.className = 'session-item';
              row.textContent = 'Non-focusable session row';
              document.body.append(priorFocus, row);
              priorFocus.focus();

              const menu = document.createElement('div');
              menu.className = 'session-action-menu';
              menu.id = 'sessionActionMenu-nonfocusable-opener-test';
              menu.setAttribute('role', 'menu');
              menu.setAttribute('aria-label', 'Conversation actions');
              menu.appendChild(_buildSessionAction('Rename conversation', '', '', () => {}));
              _mountSessionActionMenu(menu, {session_id: 'nonfocusable-opener-test'}, row);
              menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));

              const result = {
                menuRemovedOnEscape: !document.querySelector('.session-action-menu'),
                focusReturnedToPreviousControl: document.activeElement === priorFocus,
              };
              priorFocus.remove();
              row.remove();
              return result;
            };
            """,
        ]
    )


def _picker_handoff_fixture_script() -> str:
    """Exercise picker-to-menu handoff with real browser DOM replacement."""
    return "\n".join(
        [
            "let _sessionActionMenu = null;",
            "let _sessionActionAnchor = null;",
            "let _sessionActionSessionId = null;",
            "let _sessionActionPreviousFocus = null;",
            "let _sessionActionMenuId = 0;",
            "let _projectPickerTeardown = null;",
            "let _sessionListRepaintDeferredByPicker = false;",
            "const esc = value => String(value);",
            "const ICONS = new Proxy({}, {get: () => ''});",
            "const S = {session: null};",
            "const _allSessions = [{session_id: 'parent', _child_sessions: [{session_id: 'picker-child'}]}, {session_id: 'other-row', pinned: true}];",
            "const t = key => ({session_unpin: 'Unpin conversation', session_pin: 'Pin conversation'}[key] || key);",
            "const showToast = () => {};",
            "const setStatus = () => {};",
            "const syncTopbar = () => {};",
            "const renderSessionList = async () => {};",
            "const _isReadOnlySession = () => false;",
            "const _isMessagingSession = () => false;",
            "const _isCliSession = () => false;",
            "const _appendSessionCopyLinkAction = () => {};",
            "const _appendSessionShareActions = () => {};",
            "const _appendSessionDuplicateAction = () => {};",
            "const _appendSessionExportHtmlAction = () => {};",
            "const _sessionArchiveDescription = () => '';",
            "const _sessionDeleteDescription = () => '';",
            "const _manualTitleRegenerateTimeoutMs = async () => 0;",
            "const _showProjectPicker = () => {};",
            "const _archiveSession = async () => {};",
            "const deleteSession = async () => {};",
            "const removeWorktree = async () => {};",
            "const cancelSessionStream = async () => true;",
            "function _positionSessionActionMenu(){}",
            "function _playSessionActionMenuEntrance(){}",
            "let persistedPinned = null;",
            "const api = async (path, options) => { if(path === '/api/session/pin') persistedPinned = JSON.parse(options.body).pinned; return {}; };",
            "let repaintCount = 0;",
            "let currentOtherAnchor = null;",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const parent = document.createElement('div'); parent.className = 'session-item'; parent.dataset.sid = 'parent';",
            "  const child = document.createElement('div'); child.className = 'session-child-session'; child.dataset.sid = 'picker-child';",
            "  const picker = document.createElement('div'); picker.className = 'project-picker'; child.appendChild(picker); parent.appendChild(child);",
            "  const other = document.createElement('div'); other.className = 'session-item'; other.dataset.sid = 'other-row';",
            "  const trigger = document.createElement('button'); trigger.className = 'session-actions-trigger'; trigger.textContent = 'Actions'; trigger.setAttribute('aria-expanded', 'false'); other.appendChild(trigger);",
            "  host.replaceChildren(parent, other); currentOtherAnchor = trigger;",
            "  return {picker, trigger};",
            "}",
            "function renderSessionListFromCache(){ repaintCount += 1; paintRows(); }",
            _function_source("_focusSessionActionMenuRestoreTarget"),
            _function_source("closeSessionActionMenu"),
            _function_source("_buildSessionAction"),
            _function_source("_mountSessionActionMenu"),
            _function_source("_findSessionRenameRow"),
            _function_source("_projectPickerSessionActionHandoff"),
            _function_source("_openSessionActionMenu"),
            """
            window.__pickerMenuHandoffResult = async () => {
              const first = paintRows();
              const staleOtherSession = {session_id: 'other-row', pinned: false};
              first.trigger.focus();
              _projectPickerTeardown = () => first.picker.remove();
              _sessionListRepaintDeferredByPicker = true;
              _openSessionActionMenu(staleOtherSession, first.trigger);

              const menu = document.querySelector('.session-action-menu');
              const unpin = [...menu.querySelectorAll('.session-action-opt')]
                .find(button => button.textContent.includes('Unpin conversation'));
              const result = {
                repaintBeforeMenu: repaintCount === 1,
                staleAnchorReplaced: !first.trigger.isConnected && _sessionActionAnchor === currentOtherAnchor,
                currentStateAction: Boolean(unpin),
              };
              unpin.click();
              await new Promise(resolve => setTimeout(resolve, 0));
              result.persistedUnpin = persistedPinned === false;

              _allSessions[1].pinned = true;
              const second = paintRows();
              _projectPickerTeardown = () => second.picker.remove();
              _sessionListRepaintDeferredByPicker = true;
              _openSessionActionMenu(staleOtherSession, second.trigger);
              const replacementAnchor = _sessionActionAnchor;
              _sessionActionMenu.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
              result.escapeFocusesReplacement = document.activeElement === replacementAnchor && replacementAnchor.isConnected;
              result.parentAndNestedChildRemain = Boolean(document.querySelector('.session-item[data-sid="parent"] .session-child-session[data-sid="picker-child"]'));
              return result;
            };
            """,
        ]
    )


def test_session_action_menu_focus_lifecycle_in_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the session action menu browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(content=_fixture_script())
        result = page.evaluate("window.__sessionActionMenuFocusResult()")
        browser.close()

    assert result == {
        "expandedOnOpen": "true",
        "controlsOnOpen": "sessionActionMenu-browser-test",
        "menuRole": "menu",
        "firstActionFocused": True,
        "firstActionRole": "menuitem",
        "arrowDownText": "Rename conversation",
        "endText": "Delete conversation",
        "menuRemovedOnEscape": True,
        "focusRestoredOnEscape": True,
        "expandedAfterEscape": "false",
        "controlsAfterEscape": None,
    }


def test_session_action_menu_returns_to_prior_focus_for_nonfocusable_opener_in_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the session action menu browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(content=_fixture_script())
        result = page.evaluate("window.__sessionActionMenuNonFocusableOpenerResult()")
        browser.close()

    assert result == {
        "menuRemovedOnEscape": True,
        "focusReturnedToPreviousControl": True,
    }


def test_picker_handoff_rebuilds_other_row_menu_and_escape_focus_in_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the session action menu browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_content('<!doctype html><html><body><div id="sessionList"></div></body></html>')
        page.add_script_tag(content=_picker_handoff_fixture_script())
        result = page.evaluate("window.__pickerMenuHandoffResult()")
        browser.close()

    assert result == {
        "repaintBeforeMenu": True,
        "staleAnchorReplaced": True,
        "currentStateAction": True,
        "persistedUnpin": True,
        "escapeFocusesReplacement": True,
        "parentAndNestedChildRemain": True,
    }
