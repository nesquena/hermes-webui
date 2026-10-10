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


def _picker_handoff_fixture_script(anchor_kind="trigger") -> str:
    """Exercise picker-to-menu handoff with real browser DOM replacement."""
    return "\n".join(
        [
            f"const handoffAnchorKind = '{anchor_kind}';",
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
            _function_source("_positionSessionActionMenu"),
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
            "  other.tabIndex = -1; other.style.cssText = 'position:fixed;top:300px;left:16px;width:340px;height:48px';",
            "  const fork = document.createElement('div'); fork.className = 'session-child-session session-child-session-fork';",
            "  const childActions = document.createElement('div'); childActions.className = 'session-actions';",
            "  const childTrigger = document.createElement('button'); childTrigger.className = 'session-actions-trigger'; childTrigger.dataset.sid = 'child-39'; childActions.appendChild(childTrigger); fork.appendChild(childActions); other.appendChild(fork);",
            "  const actions = document.createElement('div'); actions.className = 'session-actions'; other.appendChild(actions);",
            "  const trigger = document.createElement('button'); trigger.className = 'session-actions-trigger'; trigger.textContent = 'Actions'; trigger.setAttribute('aria-expanded', 'false'); trigger.dataset.sid = 'other-row'; actions.appendChild(trigger);",
            "  if(handoffAnchorKind === 'row') actions.style.display = 'none';",
            "  host.replaceChildren(parent, other); currentOtherAnchor = handoffAnchorKind === 'row' ? other : trigger;",
            "  const opener = handoffAnchorKind === 'row' ? other : trigger;",
            "  return {picker, trigger:opener};",
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
                childNotActive: !document.querySelector('[data-sid="child-39"]').classList.contains('active'),
                ownAria: handoffAnchorKind === 'row' || currentOtherAnchor.getAttribute('aria-expanded') === 'true',
                touchAnchorVisible: handoffAnchorKind !== 'row' || _sessionActionAnchor.getBoundingClientRect().height > 0,
              };
              currentOtherAnchor.dispatchEvent(new Event('touchend', {bubbles:true}));
              result.menuSurvivesRelease = Boolean(document.querySelector('.session-action-menu'));
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


@pytest.mark.parametrize("anchor_kind", ["trigger", "row"])
@pytest.mark.parametrize("width,height", [(1440, 900), (390, 620)])
def test_picker_handoff_rebuilds_other_row_menu_and_escape_focus_in_browser(anchor_kind, width, height):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the session action menu browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": width, "height": height}, has_touch=anchor_kind == "row")
        page.set_content('<!doctype html><html><body><div id="sessionList"></div></body></html>')
        page.add_script_tag(content=_picker_handoff_fixture_script(anchor_kind))
        result = page.evaluate("window.__pickerMenuHandoffResult()")
        browser.close()

    assert result == {
        "repaintBeforeMenu": True,
        "staleAnchorReplaced": True,
        "currentStateAction": True,
        "childNotActive": True,
        "ownAria": True,
        "touchAnchorVisible": True,
        "menuSurvivesRelease": True,
        "persistedUnpin": True,
        "escapeFocusesReplacement": True,
        "parentAndNestedChildRemain": True,
    }


def _picker_handoff_common_stubs():
    """Shared stubs for the picker-deferred handoff fixtures below."""
    return [
        "let _sessionActionMenu = null;",
        "let _sessionActionAnchor = null;",
        "let _sessionActionSessionId = null;",
        "let _sessionActionPreviousFocus = null;",
        "let _sessionActionMenuId = 0;",
        "let _projectPickerTeardown = null;",
        "let _sessionListRepaintDeferredByPicker = false;",
        "const esc = value => String(value);",
        "const ICONS = new Proxy({}, {get: () => ''});",
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
        "const api = async () => ({});",
        "function _playSessionActionMenuEntrance(){}",
    ]


def _fork_open_handler_snippet() -> str:
    """Extract the production one-click open handler of a fork child row.

    The deferred-repaint fixture below runs this exact handler body, including
    its `_skipNextChildOpen` consumption, so the regression binds to the
    shipped behavior instead of a re-implementation.
    """
    start = SESSIONS_JS.index("mainBtn.onclick=async(e)=>{")
    end = SESSIONS_JS.index("await openChildSession(child);", start)
    end = SESSIONS_JS.index("};", end) + 2
    snippet = SESSIONS_JS[start:end]
    assert "_skipNextChildOpen" in snippet
    return snippet


def _picker_deferred_new_chat_script() -> str:
    """Exercise the picker-to-menu handoff for a visible, uncached New Chat.

    The active New Chat has no messages yet, so it lives only in S.session,
    not in _allSessions. After the deferred repaint replaced its row, the
    handoff must still resolve the session and open its menu on the click.
    """
    return "\n".join(
        _picker_handoff_common_stubs()
        + [
            "const S = {session: {session_id: 'new-chat'}};",
            "const _allSessions = [{session_id: 'parent', _child_sessions: [{session_id: 'picker-child'}]}, {session_id: 'other-row', pinned: true}];",
            "let repaintCount = 0;",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const parent = document.createElement('div'); parent.className = 'session-item'; parent.dataset.sid = 'parent';",
            "  const newChat = document.createElement('div'); newChat.className = 'session-item active'; newChat.dataset.sid = 'new-chat';",
            "  newChat.tabIndex = -1; newChat.style.cssText = 'position:fixed;top:120px;left:16px;width:340px;height:48px';",
            "  const actions = document.createElement('div'); actions.className = 'session-actions';",
            "  const trigger = document.createElement('button'); trigger.className = 'session-actions-trigger'; trigger.textContent = 'Actions'; trigger.setAttribute('aria-expanded', 'false');",
            "  actions.appendChild(trigger); newChat.appendChild(actions);",
            "  const picker = document.createElement('div'); picker.className = 'project-picker'; newChat.appendChild(picker);",
            "  host.replaceChildren(parent, newChat);",
            "  return {picker, trigger};",
            "}",
            "function renderSessionListFromCache(){ repaintCount += 1; paintRows(); }",
            _function_source("_positionSessionActionMenu"),
            _function_source("_focusSessionActionMenuRestoreTarget"),
            _function_source("closeSessionActionMenu"),
            _function_source("_buildSessionAction"),
            _function_source("_mountSessionActionMenu"),
            _function_source("_findSessionRenameRow"),
            _function_source("_projectPickerSessionActionHandoff"),
            _function_source("_openSessionActionMenu"),
            """
            window.__pickerDeferredNewChatResult = () => {
              const first = paintRows();
              first.trigger.focus();
              _projectPickerTeardown = () => first.picker.remove();
              _sessionListRepaintDeferredByPicker = true;
              _openSessionActionMenu({session_id: 'new-chat'}, first.trigger);
              const replacement = document.querySelector('.session-item[data-sid="new-chat"] .session-actions-trigger');
              return {
                repaintReplacedRow: repaintCount === 1 && !first.trigger.isConnected && Boolean(replacement),
                menuOpenedOnFirstClick: Boolean(document.querySelector('.session-action-menu')),
                menuSessionId: _sessionActionSessionId,
                anchorIsReplacementTrigger: _sessionActionAnchor === replacement,
                anchorConnected: Boolean(_sessionActionAnchor && _sessionActionAnchor.isConnected),
              };
            };
            """,
        ]
    )


def _picker_deferred_fork_release_script() -> str:
    """Exercise the fork long-press release after a picker-deferred repaint.

    The long-press completion (as `_scheduleForkLongPressMenu` performs it)
    arms `_skipNextChildOpen` on the pressed fork row and opens its action
    menu; the deferred repaint replaces that row while the finger is still
    down, so the pending suppression must reach the replacement before the
    browser's release click lands on its main button. The fixture paints the
    row first, so the caller can hold a real touch from before the handoff
    through the repaint and release it after. The click handler below is the
    production one.
    """
    return "\n".join(
        _picker_handoff_common_stubs()
        + [
            "const S = {session: {session_id: 'parent'}};",
            "const _allSessions = [{session_id: 'parent', _child_sessions: [{session_id: 'fork-child', session_source: 'fork'}]}, {session_id: 'other-row', pinned: true}];",
            "const openedChildren = [];",
            "const openChildSession = async (child) => { openedChildren.push(child.session_id); };",
            "const _consumeSessionNewTabClick = () => false;",
            "let repaintCount = 0;",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const parent = document.createElement('div'); parent.className = 'session-item'; parent.dataset.sid = 'parent';",
            "  const row = document.createElement('div'); row.className = 'session-child-session session-child-session-fork'; row.dataset.sid = 'fork-child';",
            "  row.style.cssText = 'position:fixed;top:' + Math.max(120, window.innerHeight - 140) + 'px;left:16px;width:340px;height:48px';",
            "  const child = {session_id: 'fork-child'};",
            "  const mainBtn = document.createElement('button'); mainBtn.type = 'button'; mainBtn.className = 'session-child-session-main'; mainBtn.textContent = '-> Forked child';",
            _fork_open_handler_snippet(),
            "  row.appendChild(mainBtn);",
            "  const actions = document.createElement('div'); actions.className = 'session-actions';",
            "  const trigger = document.createElement('button'); trigger.className = 'session-actions-trigger'; trigger.textContent = 'Actions';",
            "  actions.appendChild(trigger); row.appendChild(actions);",
            "  const picker = document.createElement('div'); picker.className = 'project-picker'; row.appendChild(picker);",
            "  parent.appendChild(row);",
            "  host.replaceChildren(parent);",
            "  return {picker, row, mainBtn};",
            "}",
            "function renderSessionListFromCache(){ repaintCount += 1; paintRows(); }",
            _function_source("_positionSessionActionMenu"),
            _function_source("_focusSessionActionMenuRestoreTarget"),
            _function_source("closeSessionActionMenu"),
            _function_source("_buildSessionAction"),
            _function_source("_mountSessionActionMenu"),
            _function_source("_findSessionRenameRow"),
            _function_source("_projectPickerSessionActionHandoff"),
            _function_source("_openSessionActionMenu"),
            """
            window.__pickerForkPaint = () => {
              const first = paintRows();
              window.__pickerForkPressedPicker = first.picker;
              const btn = first.row.querySelector('.session-child-session-main');
              const rect = btn.getBoundingClientRect();
              const tapX = rect.x + rect.width / 2;
              const tapY = rect.y + rect.height / 2;
              return {
                tapX,
                tapY,
                pressOnRowButton: document.elementFromPoint(tapX, tapY) === btn,
              };
            };
            window.__pickerForkLongPress = () => {
              // Exactly what _scheduleForkLongPressMenu() does when the
              // long-press fires: arm the one-click suppression, open the menu.
              const pressed = document.querySelector('.session-child-session[data-sid="fork-child"]');
              pressed._skipNextChildOpen = true;
              _projectPickerTeardown = () => window.__pickerForkPressedPicker.remove();
              _sessionListRepaintDeferredByPicker = true;
              _openSessionActionMenu({session_id: 'fork-child'}, pressed);
              const replacement = document.querySelector('.session-child-session[data-sid="fork-child"]');
              const btn = replacement && replacement.querySelector('.session-child-session-main');
              const rect = btn ? btn.getBoundingClientRect() : {x: 0, y: 0, width: 0, height: 0};
              return {
                repaintReplacedRow: repaintCount === 1 && !pressed.isConnected && replacement !== pressed,
                menuOpened: Boolean(document.querySelector('.session-action-menu')),
                anchorIsPressedRowReplacement: _sessionActionAnchor === replacement,
                suppressionCarried: Boolean(replacement && replacement._skipNextChildOpen),
                tapX: rect.x + rect.width / 2,
                tapY: rect.y + rect.height / 2,
              };
            };
            window.__pickerForkAfterRelease = () => ({
              releaseClickDidNotOpenChild: openedChildren.length === 0,
            });
            window.__pickerForkAfterSecondTap = () => ({
              nextTapOpensFork: openedChildren.length === 1 && openedChildren[0] === 'fork-child',
            });
            """,
        ]
    )


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_picker_handoff_opens_menu_for_uncached_new_chat_in_browser(width, height):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the session action menu browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_content('<!doctype html><html><body><div id="sessionList"></div></body></html>')
        page.add_script_tag(content=_picker_deferred_new_chat_script())
        result = page.evaluate("window.__pickerDeferredNewChatResult()")
        browser.close()

    assert result == {
        "repaintReplacedRow": True,
        "menuOpenedOnFirstClick": True,
        "menuSessionId": "new-chat",
        "anchorIsReplacementTrigger": True,
        "anchorConnected": True,
    }


@pytest.mark.parametrize("width,height", [(390, 844), (768, 1024)])
def test_picker_deferred_fork_release_keeps_parent_session_in_browser(width, height):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the session action menu browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": width, "height": height}, has_touch=True)
        page.set_content('<!doctype html><html><body><div id="sessionList"></div></body></html>')
        page.add_script_tag(content=_picker_deferred_fork_release_script())
        press = page.evaluate("window.__pickerForkPaint()")
        # Start the touch on the row, hold it while the long-press handoff
        # repaints that row out of the DOM, then release it. The browser
        # synthesizes the release click on whatever replaced the pressed row.
        cdp = page.context.new_cdp_session(page)
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": press["tapX"], "y": press["tapY"]}]},
        )
        setup = page.evaluate("window.__pickerForkLongPress()")
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(50)
        after_release = page.evaluate("window.__pickerForkAfterRelease()")
        page.touchscreen.tap(setup["tapX"], setup["tapY"])
        page.wait_for_timeout(50)
        after_second = page.evaluate("window.__pickerForkAfterSecondTap()")
        browser.close()

    assert press["pressOnRowButton"] is True
    assert after_release == {
        "releaseClickDidNotOpenChild": True,
    }
    assert after_second == {
        "nextTapOpensFork": True,
    }
    assert {key: setup[key] for key in (
        "repaintReplacedRow",
        "menuOpened",
        "anchorIsPressedRowReplacement",
        "suppressionCarried",
    )} == {
        "repaintReplacedRow": True,
        "menuOpened": True,
        "anchorIsPressedRowReplacement": True,
        "suppressionCarried": True,
    }
