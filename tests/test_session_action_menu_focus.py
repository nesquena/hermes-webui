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
    """Fork long-press release while a picker-deferred refresh is pending.

    The long-press completion (as `_scheduleForkLongPressMenu` performs it)
    arms `_skipNextChildOpen` on the pressed fork row and opens its action
    menu while the refresh a peer message triggered is still unpainted.
    Chromium dispatches the release click at the physical release point, so
    the pending repaint must be postponed: the live row keeps its suppression
    through the release, the menu survives, and the deferred repaint paints
    once the menu closes. The click handler below is the production one.
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
            "let peerLayout = false;",
            "let pressedRow = null;",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const parent = document.createElement('div'); parent.className = 'session-item'; parent.dataset.sid = 'parent';",
            "  parent.style.cssText = 'position:fixed;top:' + (window.innerHeight - 260) + 'px;left:16px;width:340px;height:200px';",
            "  const row = document.createElement('div'); row.className = 'session-child-session session-child-session-fork'; row.dataset.sid = 'fork-child';",
            "  const rowTop = peerLayout ? (window.innerHeight - 186) : (window.innerHeight - 140);",
            "  row.style.cssText = 'position:fixed;top:' + rowTop + 'px;left:16px;width:340px;height:48px';",
            "  const child = {session_id: 'fork-child'};",
            "  const mainBtn = document.createElement('button'); mainBtn.type = 'button'; mainBtn.className = 'session-child-session-main'; mainBtn.textContent = '-> Forked child';",
            _fork_open_handler_snippet(),
            "  row.appendChild(mainBtn);",
            "  const actions = document.createElement('div'); actions.className = 'session-actions';",
            "  const trigger = document.createElement('button'); trigger.className = 'session-actions-trigger'; trigger.textContent = 'Actions';",
            "  actions.appendChild(trigger); row.appendChild(actions);",
            "  parent.appendChild(row);",
            "  host.replaceChildren(parent);",
            "  return {row, mainBtn};",
            "}",
            "function renderSessionListFromCache(){",
            "  // Mirror the shipped guard: while the picker owns its row, repaints defer.",
            "  if(_projectPickerTeardown !== null){ _sessionListRepaintDeferredByPicker = true; return; }",
            "  repaintCount += 1;",
            "  paintRows();",
            "}",
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
              const picker = document.createElement('div'); picker.className = 'project-picker'; document.body.appendChild(picker);
              _projectPickerTeardown = () => picker.remove();
              const btn = first.row.querySelector('.session-child-session-main');
              const rect = btn.getBoundingClientRect();
              window.__pickerForkTap = {tapX: rect.x + rect.width / 2, tapY: rect.y + rect.height / 2};
              return {
                tapX: window.__pickerForkTap.tapX,
                tapY: window.__pickerForkTap.tapY,
                pressOnRowButton: document.elementFromPoint(window.__pickerForkTap.tapX, window.__pickerForkTap.tapY) === btn,
              };
            };
            window.__pickerForkPeerRefresh = () => {
              // A peer message arrives and the normal list refresh is accepted
              // while the picker holds the old layout, so its repaint defers.
              peerLayout = true;
              renderSessionListFromCache();
              return {deferred: _sessionListRepaintDeferredByPicker === true, repaintCount};
            };
            window.__pickerForkLongPress = () => {
              // Exactly what _scheduleForkLongPressMenu() does when the
              // long-press fires: arm the one-click suppression, open the menu.
              pressedRow = document.querySelector('.session-child-session[data-sid="fork-child"]');
              pressedRow._skipNextChildOpen = true;
              _openSessionActionMenu({session_id: 'fork-child'}, pressedRow);
              const current = document.querySelector('.session-child-session[data-sid="fork-child"]');
              const btn = current.querySelector('.session-child-session-main');
              const tap = window.__pickerForkTap;
              return {
                repaintDuringGesture: repaintCount,
                rowNotReplaced: current === pressedRow && pressedRow.isConnected,
                menuOpened: Boolean(document.querySelector('.session-action-menu')),
                anchorIsPressedRow: _sessionActionAnchor === pressedRow,
                suppressionArmedThroughRelease: pressedRow._skipNextChildOpen === true,
                releaseTargetIsRow: document.elementFromPoint(tap.tapX, tap.tapY) === btn,
              };
            };
            window.__pickerForkAfterRelease = () => ({
              releaseClickDidNotOpenChild: openedChildren.length === 0,
              menuSurvivedRelease: Boolean(document.querySelector('.session-action-menu')),
              suppressionConsumed: pressedRow._skipNextChildOpen === false,
            });
            window.__pickerForkAfterSecondTap = () => ({
              nextTapOpensFork: openedChildren.length === 1 && openedChildren[0] === 'fork-child',
            });
            window.__pickerForkCloseMenu = () => {
              closeSessionActionMenu();
            };
            window.__pickerForkDrained = () => {
              const current = document.querySelector('.session-child-session[data-sid="fork-child"]');
              return {
                repaintsAfterMenuClose: repaintCount,
                flagCleared: _sessionListRepaintDeferredByPicker === false,
                rowPaintedAtRefreshedPosition: Boolean(current) && Math.abs(current.getBoundingClientRect().top - (window.innerHeight - 186)) <= 1,
              };
            };
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
def test_picker_deferred_fork_release_survives_a_moving_row_in_browser(width, height):
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
        refresh = page.evaluate("window.__pickerForkPeerRefresh()")
        # Start the touch on the row, hold it through the long-press handoff,
        # then release it: the deferred repaint must not have moved the row,
        # so the browser's compatibility click still lands on it.
        cdp = page.context.new_cdp_session(page)
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": press["tapX"], "y": press["tapY"]}]},
        )
        setup = page.evaluate("window.__pickerForkLongPress()")
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(50)
        after_release = page.evaluate("window.__pickerForkAfterRelease()")
        page.touchscreen.tap(press["tapX"], press["tapY"])
        page.wait_for_timeout(50)
        after_second = page.evaluate("window.__pickerForkAfterSecondTap()")
        page.evaluate("window.__pickerForkCloseMenu()")
        page.wait_for_timeout(50)
        drained = page.evaluate("window.__pickerForkDrained()")
        browser.close()

    assert press["pressOnRowButton"] is True
    assert refresh == {"deferred": True, "repaintCount": 0}
    assert setup == {
        "repaintDuringGesture": 0,
        "rowNotReplaced": True,
        "menuOpened": True,
        "anchorIsPressedRow": True,
        "suppressionArmedThroughRelease": True,
        "releaseTargetIsRow": True,
    }
    assert after_release == {
        "releaseClickDidNotOpenChild": True,
        "menuSurvivedRelease": True,
        "suppressionConsumed": True,
    }
    assert after_second == {"nextTapOpensFork": True}
    assert drained == {
        "repaintsAfterMenuClose": 1,
        "flagCleared": True,
        "rowPaintedAtRefreshedPosition": True,
    }


def _picker_deferred_search_hit_script() -> str:
    """Exercise the picker-to-menu handoff for a content-search-only session.

    The session is returned only by the content search, and its row renders
    from `_contentSearchResults` through the same merge the sidebar uses. A
    deferred repaint replaces that row, and the first click on its trigger
    must still resolve the owner and open the menu.
    """
    return "\n".join(
        _picker_handoff_common_stubs()
        + [
            "const S = {session: null};",
            "const _allSessions = [{session_id: 'parent', _child_sessions: []}, {session_id: 'webui-telegram-notes', title: 'Telegram notes'}];",
            "const _contentSearchResults = [{session_id: 'tg-search-hit', title: 'Older Telegram chat', match_type: 'content', match_preview: 'older telegram match'}];",
            "let repaintCount = 0;",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const merged = _sessionSearchMergeMatches(_allSessions, 'telegram', _contentSearchResults);",
            "  const entries = merged.map(entry => {",
            "    const row = document.createElement('div'); row.className = 'session-item'; row.dataset.sid = entry.session_id;",
            "    const actions = document.createElement('div'); actions.className = 'session-actions';",
            "    const trigger = document.createElement('button'); trigger.className = 'session-actions-trigger'; trigger.textContent = 'Actions'; trigger.setAttribute('aria-expanded', 'false');",
            "    actions.appendChild(trigger); row.appendChild(actions);",
            "    return {row, trigger};",
            "  });",
            "  host.replaceChildren(...entries.map(entry => entry.row));",
            "  return entries;",
            "}",
            "function renderSessionListFromCache(){",
            "  // Mirror the shipped guard: while the picker owns its row, repaints defer.",
            "  if(_projectPickerTeardown !== null){ _sessionListRepaintDeferredByPicker = true; return; }",
            "  repaintCount += 1;",
            "  paintRows();",
            "}",
            _function_source("_sessionSearchMergeMatches"),
            _function_source("_sessionSearchDirectAndTitleMatches"),
            _function_source("_sessionDisplayTitle"),
            _function_source("_sessionSearchDirectSessionMatches"),
            _function_source("_sessionSearchSessionIdCandidates"),
            _function_source("_sessionSearchAddIdCandidate"),
            _function_source("_sessionSearchCleanUrlToken"),
            _function_source("_positionSessionActionMenu"),
            _function_source("_focusSessionActionMenuRestoreTarget"),
            _function_source("closeSessionActionMenu"),
            _function_source("_buildSessionAction"),
            _function_source("_mountSessionActionMenu"),
            _function_source("_findSessionRenameRow"),
            _function_source("_projectPickerSessionActionHandoff"),
            _function_source("_openSessionActionMenu"),
            """
            window.__pickerDeferredSearchHitResult = () => {
              const first = paintRows();
              const searchEntry = first.find(entry => entry.row.dataset.sid === 'tg-search-hit');
              const trigger = searchEntry.trigger;
              trigger.focus();
              const picker = document.createElement('div'); picker.className = 'project-picker'; document.body.appendChild(picker);
              _projectPickerTeardown = () => picker.remove();
              renderSessionListFromCache();   // a background refresh while the picker is open
              const deferredBeforeClick = _sessionListRepaintDeferredByPicker === true && repaintCount === 0;
              _openSessionActionMenu(_contentSearchResults[0], trigger);
              const replacement = document.querySelector('.session-item[data-sid="tg-search-hit"] .session-actions-trigger');
              return {
                deferredBeforeClick,
                renderedWhileAbsentFromCache: !_allSessions.some(entry => entry.session_id === 'tg-search-hit'),
                repaintReplacedRow: repaintCount === 1 && !trigger.isConnected && Boolean(replacement),
                menuOpenedOnFirstClick: Boolean(document.querySelector('.session-action-menu')),
                menuSessionId: _sessionActionSessionId,
                anchorIsReplacementTrigger: _sessionActionAnchor === replacement,
                anchorConnected: Boolean(_sessionActionAnchor && _sessionActionAnchor.isConnected),
              };
            };
            """,
        ]
    )


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_picker_handoff_opens_menu_for_content_search_only_session_in_browser(width, height):
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
        page.add_script_tag(content=_picker_deferred_search_hit_script())
        result = page.evaluate("window.__pickerDeferredSearchHitResult()")
        browser.close()

    assert result == {
        "deferredBeforeClick": True,
        "renderedWhileAbsentFromCache": True,
        "repaintReplacedRow": True,
        "menuOpenedOnFirstClick": True,
        "menuSessionId": "tg-search-hit",
        "anchorIsReplacementTrigger": True,
        "anchorConnected": True,
    }


def _explicit_control_common_stubs() -> list:
    """Shared stubs for the explicit-control fixtures below.

    renderSessionListFromCache mirrors the shipped guard: while the picker owns
    its row, a repaint defers (static/sessions.js renderSessionListFromCache).
    """
    return [
        "let _sessionActionMenu = null;",
        "let _sessionActionAnchor = null;",
        "let _sessionActionSessionId = null;",
        "let _sessionActionPreviousFocus = null;",
        "let _sessionActionMenuId = 0;",
        "let _projectPickerTeardown = null;",
        "let _sessionListRepaintDeferredByPicker = false;",
        "let repaints = 0;",
        "const showToast = () => {};",
        "const t = (key, count) => count === undefined ? key : key + ':' + count;",
        "const $ = id => document.getElementById(id);",
        "function renderSessionListFromCache(){",
        "  if(_projectPickerTeardown !== null){ _sessionListRepaintDeferredByPicker = true; return; }",
        "  repaints += 1;",
        "  paintRows();",
        "}",
    ]


def _child_count_toggle_snippet() -> str:
    """Extract the production child-count control, including its toggle handler."""
    start = SESSIONS_JS.index("const childCount=typeof s._child_session_count==='number'")
    end = SESSIONS_JS.index("titleRow.appendChild(childCountEl);", start) + len("titleRow.appendChild(childCountEl);")
    snippet = SESSIONS_JS[start:end]
    assert "_retireProjectPickerForExplicitRepaint" in snippet
    return snippet + "\n    }"


def _tag_filter_snippet() -> str:
    """Extract the production tag chip, including its filter handler."""
    start = SESSIONS_JS.index("for(const tag of tags){")
    end = SESSIONS_JS.index("title.appendChild(chip);", start) + len("title.appendChild(chip);")
    snippet = SESSIONS_JS[start:end]
    assert "_retireProjectPickerForExplicitRepaint" in snippet
    return snippet + "\n    }"


def _explicit_child_count_script() -> str:
    """Child-count toggle while the picker is open (gate finding N2)."""
    return "\n".join(
        _explicit_control_common_stubs()
        + [
            "const _expandedChildSessionKeys = new Set();",
            "const _sidebarLineageKeyForRow = (s) => 'lineage-' + s.session_id;",
            "const _sessionChildBadgeTooltip = (label) => label;",
            "window.__parentSession = {session_id: 'parent', _child_sessions: Array.from({length: 20}, (_, i) => ({session_id: 'kid-' + i, session_source: 'fork'}))};",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const parent = document.createElement('div'); parent.className = 'session-item'; parent.dataset.sid = 'parent';",
            "  const titleRow = document.createElement('div');",
            "  const s = window.__parentSession;",
            _child_count_toggle_snippet(),
            "  parent.appendChild(titleRow);",
            "  if(_expandedChildSessionKeys.has('lineage-parent')){",
            "    for(let i = 0; i < 20; i += 1){ const kid = document.createElement('div'); kid.className = 'session-child-session'; kid.dataset.sid = 'kid-' + i; parent.appendChild(kid); }",
            "  }",
            "  host.replaceChildren(parent);",
            "}",
            _function_source("_retireProjectPickerForExplicitRepaint"),
            """
            window.__explicitChildCountCase = () => {
              const host = document.getElementById('sessionList');
              const countForks = () => host.querySelectorAll('.session-child-session').length;
              paintRows();
              const before = {forks: countForks(), repaints};
              const picker = document.createElement('div'); picker.className = 'project-picker'; document.body.appendChild(picker);
              _projectPickerTeardown = () => picker.remove();
              renderSessionListFromCache();   // a background refresh while the picker is open
              const deferred = {flag: _sessionListRepaintDeferredByPicker, repaints};
              document.querySelector('.session-child-count').click();
              return {
                before,
                deferred,
                after: {
                  forks: countForks(),
                  repaints,
                  pickerRemoved: picker.isConnected === false,
                  pickerTeardownCleared: _projectPickerTeardown === null,
                  flagCleared: _sessionListRepaintDeferredByPicker === false,
                  expanded: _expandedChildSessionKeys.has('lineage-parent'),
                  controlLabel: document.querySelector('.session-child-count').textContent,
                },
              };
            };
            """,
        ]
    )


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_child_count_toggle_retires_picker_and_expands_in_browser(width, height):
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
        page.add_script_tag(content=_explicit_child_count_script())
        result = page.evaluate("window.__explicitChildCountCase()")
        browser.close()

    assert result == {
        "before": {"forks": 0, "repaints": 0},
        "deferred": {"flag": True, "repaints": 0},
        "after": {
            "forks": 20,
            "repaints": 1,
            "pickerRemoved": True,
            "pickerTeardownCleared": True,
            "flagCleared": True,
            "expanded": True,
            "controlLabel": "session_meta_children:20",
        },
    }


def _explicit_tag_filter_script() -> str:
    """Tag-chip filter while the picker is open (gate finding N3)."""
    return "\n".join(
        _explicit_control_common_stubs()
        + [
            "function filterSessions(){ renderSessionListFromCache(); }",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const parent = document.createElement('div'); parent.className = 'session-item'; parent.dataset.sid = 'parent';",
            "  const title = document.createElement('div');",
            "  const tags = ['#triage'];",
            _tag_filter_snippet(),
            "  parent.appendChild(title);",
            "  const query = ($('sessionSearch') && $('sessionSearch').value || '').trim();",
            "  const rowCount = query === '#triage' ? 1 : 15;",
            "  for(let i = 0; i < rowCount; i += 1){ const row = document.createElement('div'); row.className = 'session-row'; row.dataset.sid = 'row-' + i; parent.appendChild(row); }",
            "  host.replaceChildren(parent);",
            "}",
            _function_source("_retireProjectPickerForExplicitRepaint"),
            """
            window.__explicitTagCase = () => {
              const host = document.getElementById('sessionList');
              const countRows = () => host.querySelectorAll('.session-row').length;
              paintRows();
              const before = {rows: countRows(), repaints};
              const picker = document.createElement('div'); picker.className = 'project-picker'; document.body.appendChild(picker);
              _projectPickerTeardown = () => picker.remove();
              renderSessionListFromCache();   // a background refresh while the picker is open
              const deferred = {flag: _sessionListRepaintDeferredByPicker, repaints};
              document.querySelector('.session-tag').click();
              return {
                before,
                deferred,
                after: {
                  rows: countRows(),
                  repaints,
                  searchValue: $('sessionSearch').value,
                  pickerRemoved: picker.isConnected === false,
                  flagCleared: _sessionListRepaintDeferredByPicker === false,
                },
              };
            };
            """,
        ]
    )


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_tag_filter_retires_picker_and_filters_in_browser(width, height):
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
        page.set_content('<!doctype html><html><body><div id="sessionList"></div><input id="sessionSearch" value=""></body></html>')
        page.add_script_tag(content=_explicit_tag_filter_script())
        result = page.evaluate("window.__explicitTagCase()")
        browser.close()

    assert result == {
        "before": {"rows": 15, "repaints": 0},
        "deferred": {"flag": True, "repaints": 0},
        "after": {
            "rows": 1,
            "repaints": 1,
            "searchValue": "#triage",
            "pickerRemoved": True,
            "flagCleared": True,
        },
    }


def _explicit_project_create_script() -> str:
    """Inline project create while the picker is open (gate finding N4)."""
    return "\n".join(
        _explicit_control_common_stubs()
        + [
            "const PROJECT_COLORS = ['#7cb9ff', '#f5c542'];",
            "let _allProjects = [{project_id: 'p0', name: 'Existing'}];",
            "const _markNonCredentialInput = () => {};",
            "window.__createdProjects = [];",
            "async function api(path, options){",
            "  if(path === '/api/projects/create'){",
            "    const body = JSON.parse(options.body);",
            "    const project = {project_id: 'proj-new', name: body.name, color: body.color};",
            "    window.__createdProjects.push(project);",
            "    return {project};",
            "  }",
            "  return {};",
            "}",
            "async function renderSessionList(){",
            "  // The refetch carries the created project back; the repaint builds the chip.",
            "  for(const project of window.__createdProjects){ if(!_allProjects.includes(project)) _allProjects.push(project); }",
            "  renderSessionListFromCache();",
            "}",
            "function paintRows(){",
            "  const host = document.getElementById('sessionList');",
            "  const bar = document.createElement('div'); bar.className = 'project-bar';",
            "  for(const project of _allProjects){ const chip = document.createElement('span'); chip.className = 'project-chip'; chip.textContent = project.name; bar.appendChild(chip); }",
            "  const addBtn = document.createElement('button'); addBtn.className = 'project-create-btn'; addBtn.textContent = '+';",
            "  addBtn.onclick = (e) => { e.stopPropagation(); _startProjectCreate(bar, addBtn); };",
            "  bar.appendChild(addBtn);",
            "  window.__projectBar = bar; window.__projectAddBtn = addBtn;",
            "  host.replaceChildren(bar);",
            "}",
            _function_source("_retireProjectPickerForExplicitRepaint"),
            _function_source("_resizeProjectInput"),
            _function_source("_startProjectCreate"),
            """
            window.__explicitProjectCreateCase = async () => {
              paintRows();
              const picker = document.createElement('div'); picker.className = 'project-picker'; document.body.appendChild(picker);
              _projectPickerTeardown = () => picker.remove();
              renderSessionListFromCache();   // a background refresh while the picker is open
              const deferred = {flag: _sessionListRepaintDeferredByPicker, repaints};
              window.__projectAddBtn.click();
              const inp = document.querySelector('.project-create-input');
              const editorStarted = Boolean(inp);
              inp.value = 'Roadmap';
              inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
              await new Promise(resolve => setTimeout(resolve, 30));
              const bar = document.querySelector('.project-bar');
              return {
                deferred,
                editorStarted,
                apiCalled: window.__createdProjects.map(project => project.name),
                editorReplaced: !document.querySelector('.project-create-input'),
                chips: [...bar.querySelectorAll('.project-chip')].map(chip => chip.textContent),
                pickerRemoved: picker.isConnected === false,
                flagCleared: _sessionListRepaintDeferredByPicker === false,
                repaints,
              };
            };
            """,
        ]
    )


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_inline_project_create_retires_picker_and_completes_in_browser(width, height):
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
        page.add_script_tag(content=_explicit_project_create_script())
        result = page.evaluate("window.__explicitProjectCreateCase()")
        browser.close()

    assert result == {
        "deferred": {"flag": True, "repaints": 0},
        "editorStarted": True,
        "apiCalled": ["Roadmap"],
        "editorReplaced": True,
        "chips": ["Existing", "Roadmap"],
        "pickerRemoved": True,
        "flagCleared": True,
        "repaints": 1,
    }
