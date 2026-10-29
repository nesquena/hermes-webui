#!/usr/bin/env python3
"""
Headless browser gate: a sidebar conversation row opens from the keyboard and
from a screen reader's activate command, not only from a pointer press.

WHY THIS EXISTS
  A session row opened only from its pointerdown/pointerup (or touchstart/
  touchend) gesture handlers, so swipe, long-press and double-tap rename could
  share one state machine. The row had no role, no tabindex and no click
  handler. NVDA in Firefox activates an element by dispatching mousedown,
  mouseup and click with no pointer events before them, and Enter on a
  focusable element dispatches a bare click, so neither could open a
  conversation; VoiceOver in Safari happened to work because it synthesizes
  pointer events as well.

WHAT IT CHECKS (Chromium, plus Firefox and WebKit when their Playwright
builds are installed and launch; WebKit needs its system libraries)
  - Tab from the conversation filter reaches a conversation title;
  - every row title is a native button; the open conversation's title carries
    aria-current="page", the others do not;
  - mouse events without pointer events (how Firefox performs an assistive
    technology's activate action) open the conversation, once;
  - a bare element.click() opens the conversation;
  - Tab focus on a title plus Enter opens it; a held (auto-repeated) Enter
    does not open again; with a tool approval card visible, that Enter does
    not answer the approval (document-level Enter shortcut);
  - a right-click on a row does not swallow the next keyboard activation;
  - a sidebar repaint restores the same focused title and never steals focus
    from a control outside the list;
  - a real mouse click still opens the conversation exactly once;
  - Enter on a row's actions trigger opens its menu and does not open the
    conversation;
  - in selection mode the title is not a button (the row checkbox is the control);
  - clicking another title during a rename commits and ends the rename, and
    clicking a title during a project rename saves the project name.
  touch (Chromium, 390x844, drawer open)
  - a tap still opens the conversation exactly once;
  - tapping another title during a rename commits and ends the rename;
  - a tap on a title moves focus out of the search box (1440x1000 only);
  - a long press opens the action menu and leaves focus in it.

  --screenshots DIR writes the sidebar at 1440x900 and 390x844 (drawer open)
  after pressing Tab from the conversation filter until focus reaches the
  second conversation's title (or 25 presses), showing where keyboard focus
  lands.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
        return False


# An ephemeral port by default, so the gate never talks to another server
# that already holds a fixed one; SESSION_ROW_ACTIVATION_PORT pins it.
PORT = int(os.getenv("SESSION_ROW_ACTIVATION_PORT") or _free_port())
BASE = f"http://127.0.0.1:{PORT}"
WAIT_MS = 5000
# Opening a conversation repaints the sidebar again shortly after; wait it out
# before counting opens or reading focus.
SETTLE_MS = 800


def _wait_for_health(proc, timeout=30):
    """True once this gate's own server answers /health. A server that exited
    (for example because the port was taken) fails at once, so a different
    process answering on the port is never mistaken for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return proc.poll() is None
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _wait_until(page, expression, arg=None, timeout_ms=WAIT_MS):
    """Poll ``expression`` with page.evaluate. The app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression, arg):
            return True
        page.wait_for_timeout(50)
    return False


SEED_JS = """async () => {
  const post = async (path, body) => {
    const response = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(path + ' failed: ' + JSON.stringify(data));
    return data;
  };
  const messages = [
    {role: 'user', content: 'hello'},
    {role: 'assistant', content: 'hello back'},
  ];
  const sids = [];
  for (const title of ['Alpha conversation', 'Beta conversation', 'Gamma conversation', 'Delta conversation #short']) {
    const data = await post('/api/session/import', {title, messages});
    sids.push(data.session.session_id);
  }
  await renderSessionList({deferWhileInteracting: false});
  return {alpha: sids[0], beta: sids[1], gamma: sids[2], delta: sids[3]};
}"""

# Count every sidebar open. _openSidebarSession is a global function the row
# handlers call by name, so replacing the global binding sees all of them.
COUNT_OPENS_JS = """() => {
  if (!window.__sidebarOpens) {
    window.__sidebarOpens = [];
    const original = _openSidebarSession;
    window._openSidebarSession = async (session, opts) => {
      window.__sidebarOpens.push(session && session.session_id);
      return original(session, opts);
    };
  }
  window.__sidebarOpens.length = 0;
}"""

ACTIVE_JS = "(sid) => !!(S.session && S.session.session_id === sid)"

TITLE_SELECTOR_JS = """(sid) => {
  const row = Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
    .find(node => node.dataset.sid === sid);
  return row ? row.querySelector('.session-title') : null;
}"""

TITLES_JS = """() => Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]')).map(row => {
  const title = row.querySelector('.session-title');
  return {
    sid: row.dataset.sid,
    tag: title && title.tagName,
    role: title && title.getAttribute('role'),
    tabindex: title && title.tabIndex,
    current: title && title.getAttribute('aria-current'),
  };
})"""

FOCUSED_SID_JS = """() => {
  const active = document.activeElement;
  if (!active || !active.classList.contains('session-title') || !active.isConnected) return null;
  const row = active.closest('.session-item');
  return row ? row.dataset.sid : null;
}"""


ACTIONS_TRIGGER_JS = """(sid) => {
  const row = Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
    .find(node => node.dataset.sid === sid);
  return row ? row.querySelector('.session-actions-trigger') : null;
}"""

RENAME_ENDED_JS = "() => !_renamingSid && !document.querySelector('#sessionList .session-title-input')"

# The seed is shared by every browser run, so each rename case types its own
# suffix and checks for that one.
RENAMED_JS = """([sid, suffix]) => Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
  .some(row => row.dataset.sid === sid && ((row.querySelector('.session-title') || {}).textContent || '').endsWith(suffix))"""


def _rename_suffix():
    return f" renamed-{time.monotonic_ns() % 10**9}"

START_RENAME_JS = """(sid) => {
  const row = Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
    .find(node => node.dataset.sid === sid);
  if (row && typeof row._startRename === 'function') row._startRename();
}"""

# Start renaming a project chip, creating the project first if needed.
START_PROJECT_RENAME_JS = """async () => {
  const find = () => (_allProjects || []).find(p => p.name.startsWith('Gate project'));
  if (!find()) {
    await api('/api/projects/create', {method: 'POST', body: JSON.stringify({name: 'Gate project', color: '#888888'})});
    await renderSessionList({deferWhileInteracting: false});
  }
  const proj = find();
  const chip = proj && Array.from(document.querySelectorAll('.project-chip')).find(c => c.textContent.includes(proj.name));
  if (!chip) return 'no project chip';
  _startProjectRename(proj, chip);
  return proj.project_id;
}"""

DELETE_GATE_PROJECT_JS = """async (projectId) => {
  await api('/api/projects/delete', {method: 'POST', body: JSON.stringify({project_id: projectId})});
  await renderSessionList({deferWhileInteracting: false});
}"""

PROJECT_INPUT_FOCUSED_JS = "() => !!(document.activeElement && document.activeElement.classList.contains('project-create-input'))"

PROJECT_RENAMED_JS = """async ([projectId, suffix]) => {
  const data = await api('/api/projects', {timeoutToast: false});
  const project = (data.projects || []).find(p => p.project_id === projectId);
  return !!(project && project.name.endsWith(suffix));
}"""

FOCUS_IN_LIST_JS = "() => !!document.querySelector('#sessionList:focus-within')"

CLEAR_SEARCH_JS = """() => {
  const box = document.getElementById('sessionSearch');
  if (box && box.value) { box.value = ''; if (typeof filterSessions === 'function') filterSessions(); }
}"""


def _title(page, sid):
    handle = page.evaluate_handle(TITLE_SELECTOR_JS, sid)
    return handle.as_element()


def _focus_title(page, sid):
    """Find and focus a row title in one page step: a repaint between a
    separate lookup and focus() would leave a detached element."""
    page.evaluate("""(sid) => {
      const row = Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
        .find(node => node.dataset.sid === sid);
      const title = row && row.querySelector('.session-title');
      if (title) title.focus();
    }""", sid)


RENAME_INPUT_FOCUSED_JS = "() => !!(document.activeElement && document.activeElement.matches('#sessionList .session-title-input'))"


def _dblclick_rename(page, sid):
    """Double-click a title to start its rename. One retry: a sidebar repaint
    between the two clicks can replace the row, so the browser sees two single
    clicks; a broken rename still fails both attempts."""
    for _ in range(2):
        title = _title(page, sid)
        if title is not None:
            title.dblclick()
            if _wait_until(page, RENAME_INPUT_FOCUSED_JS, timeout_ms=2000):
                return True
        page.wait_for_timeout(SETTLE_MS)
    return False


def _open(page, sid):
    page.evaluate("(sid) => loadSession(sid)", sid)
    _wait_until(page, ACTIVE_JS, sid)
    page.wait_for_timeout(SETTLE_MS)


def _opens(page):
    return page.evaluate("() => window.__sidebarOpens.slice()")


def _new_page(browser, **context_args):
    ctx = browser.new_context(base_url=BASE, **context_args)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    ready = "() => typeof S !== 'undefined' && typeof renderSessionList === 'function'"
    if not _wait_until(page, ready, timeout_ms=15000):
        ctx.close()
        return None, None, ["the app did not initialize"]
    page.wait_for_timeout(1000)
    return ctx, page, errors


def _check_desktop(page, seed, name):
    failures = []
    alpha, beta, gamma = seed["alpha"], seed["beta"], seed["gamma"]

    def fail(message):
        failures.append(f"  [{name}] {message}")

    _open(page, alpha)
    page.evaluate(COUNT_OPENS_JS)

    # A keyboard user with no screen reader: Tab from the conversation filter
    # must reach a conversation title. Let the sidebar show the open
    # conversation first: while focus is inside the list, deferred list
    # refreshes wait until it leaves (by design), so blur afterwards too.
    _wait_until(page, """(sid) => Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
      .some(row => row.dataset.sid === sid && row.querySelector('.session-title[aria-current="page"]'))""", alpha)
    page.focus("#sessionSearch")
    reached = None
    for _ in range(25):
        page.keyboard.press("Tab")
        reached = page.evaluate(FOCUSED_SID_JS)
        if reached:
            break
    if not reached:
        fail("Tab from the conversation filter never reached a conversation title (25 presses)")
    page.evaluate("() => document.activeElement && document.activeElement.blur()")

    titles = {t["sid"]: t for t in page.evaluate(TITLES_JS)}
    for sid in (alpha, beta):
        t = titles.get(sid)
        if not t:
            fail(f"row {sid} missing from the sidebar")
            return failures
        if t["tag"] != "BUTTON" or t["role"] is not None or t["tabindex"] != 0:
            fail(f"title of {sid} is not a native focusable button: {t}")
    if titles[alpha]["current"] != "page":
        fail(f"open conversation's title lacks aria-current=page: {titles[alpha]}")
    if titles[beta]["current"] is not None:
        fail(f"closed conversation's title carries aria-current: {titles[beta]}")

    # How Firefox performs an assistive technology's activate action: mouse
    # events, no pointer events.
    page.evaluate("""(el) => {
      for (const type of ['mousedown', 'mouseup', 'click']) {
        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window, detail: 1, button: 0}));
      }
    }""", _title(page, beta))
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("mouse events without pointer events did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta]:
        fail(f"assistive activation opened {_opens(page)}, expected exactly [beta]")

    _open(page, beta)
    page.evaluate(COUNT_OPENS_JS)
    page.evaluate("(el) => el.click()", _title(page, alpha))
    if not _wait_until(page, ACTIVE_JS, alpha):
        fail("a bare click() did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [alpha]:
        fail(f"a bare click() opened {_opens(page)}, expected exactly [alpha]")

    _open(page, alpha)
    page.evaluate(COUNT_OPENS_JS)
    _title(page, beta).focus()
    page.keyboard.press("Enter")
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("Enter on a focused title did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta]:
        fail(f"Enter opened {_opens(page)}, expected exactly [beta]")
    current = {t["sid"]: t["current"] for t in page.evaluate(TITLES_JS)}
    if current.get(beta) != "page" or current.get(alpha) is not None:
        fail(f"aria-current did not follow the open conversation: {current}")
    # Opening rebuilds the list; keyboard focus must land on the opened row's
    # rebuilt title (not <body>), so the next Tab continues from that row.
    if page.evaluate(FOCUSED_SID_JS) != beta:
        fail("after Enter opened a conversation, focus was not on its title in the sidebar")
    page.keyboard.press("Tab")
    if not page.evaluate("(sid) => document.activeElement === (" + ACTIONS_TRIGGER_JS + ")(sid)", beta):
        fail("Tab after Enter did not continue to the opened row's actions button")

    # Enter on A, then on B before A has loaded: A's late completion rebuilds
    # the list after B's focus return. Focus must end on B (the conversation
    # that is open), never back on A, or the next Enter would reopen A.
    _open(page, gamma)
    page.evaluate(COUNT_OPENS_JS)
    held = []

    def hold_alpha(route):
        if not held and f"session_id={alpha}" in route.request.url:
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/session?*", hold_alpha)
    _focus_title(page, alpha)
    page.keyboard.press("Enter")
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    if not held:
        fail("could not hold the first conversation's load (race setup)")
    _focus_title(page, beta)
    page.keyboard.press("Enter")
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("Enter on a second title while the first was loading did not open it")
    page.wait_for_timeout(SETTLE_MS)
    for route in held:
        route.continue_()
    page.unroute("**/api/session?*", hold_alpha)
    page.wait_for_timeout(SETTLE_MS)
    if not page.evaluate(ACTIVE_JS, beta):
        fail("the first conversation's late load replaced the second one")
    if page.evaluate(FOCUSED_SID_JS) != beta:
        fail(f"after a superseded open finished late, focus was on {page.evaluate(FOCUSED_SID_JS)!r}, expected the open conversation's title")

    # A held Enter auto-repeats; only the first keydown may open.
    page.evaluate(COUNT_OPENS_JS)
    page.evaluate("""(el) => {
      el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', repeat: true, bubbles: true, cancelable: true}));
    }""", _title(page, alpha))
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page):
        fail(f"an auto-repeated Enter opened {_opens(page)}")

    # With a tool approval card visible, a bare Enter elsewhere means
    # "Allow once" (document-level shortcut). Enter on a title must not reach it.
    page.evaluate("""() => {
      window.__approvals = [];
      window.respondApproval = (choice) => { window.__approvals.push(choice); };
      const card = document.getElementById('approvalCard');
      if (card) card.classList.add('visible');
    }""")
    page.evaluate(COUNT_OPENS_JS)
    _title(page, alpha).focus()
    page.keyboard.press("Enter")
    if not _wait_until(page, ACTIVE_JS, alpha):
        fail("Enter on a title did not open the conversation with an approval card visible")
    page.wait_for_timeout(SETTLE_MS)
    approvals = page.evaluate("() => window.__approvals")
    if approvals:
        fail(f"Enter on a conversation title answered the pending approval: {approvals}")
    page.evaluate("""() => {
      const card = document.getElementById('approvalCard');
      if (card) card.classList.remove('visible');
    }""")

    # A right-click (no click event follows) must not leave a marker that
    # swallows the next keyboard activation.
    _open(page, alpha)
    page.evaluate(COUNT_OPENS_JS)
    _title(page, beta).click(button="right")
    page.keyboard.press("Escape")
    page.evaluate("() => typeof closeSessionActionMenu === 'function' && closeSessionActionMenu()")
    page.wait_for_timeout(300)
    _title(page, beta).focus()
    page.keyboard.press("Enter")
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("Enter right after a right-click on the row did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta]:
        fail(f"Enter after a right-click opened {_opens(page)}, expected exactly [beta]")

    # Preserve the original PR's focus identity across a destructive repaint.
    _title(page, alpha).focus()
    page.evaluate("""() => {
      window.__titleFocusEvents = 0;
      if (!window.__titleFocusListener) {
        window.__titleFocusListener = true;
        document.addEventListener('focusin', (e) => {
          if (e.target && e.target.classList && e.target.classList.contains('session-title')) window.__titleFocusEvents++;
        }, true);
      }
    }""")
    page.evaluate("() => { renderSessionListFromCache(); renderSessionListFromCache(); }")
    page.wait_for_timeout(SETTLE_MS)
    if page.evaluate(FOCUSED_SID_JS) != alpha:
        fail("a sidebar repaint did not restore the same focused title")
    page.focus('#msg')
    page.evaluate("() => { renderSessionListFromCache(); renderSessionListFromCache(); }")
    if not page.evaluate("() => document.activeElement === document.getElementById('msg')"):
        fail("a sidebar repaint stole focus from the composer")

    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    _open(page, beta)
    page.mouse.move(0, 0)
    page.evaluate(COUNT_OPENS_JS)
    _title(page, alpha).click()
    if not _wait_until(page, ACTIVE_JS, alpha):
        fail("a mouse click no longer opens the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [alpha]:
        fail(f"a mouse click opened {_opens(page)}, expected exactly [alpha]")

    # A mouse click, then Enter on another title as soon as the clicked
    # conversation is open: each opens exactly once (the click leaves nothing
    # behind that could swallow the Enter).
    page.evaluate(COUNT_OPENS_JS)
    _title(page, beta).click()
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("a mouse click did not open the conversation")
    page.wait_for_timeout(300)
    _focus_title(page, gamma)
    page.keyboard.press("Enter")
    if not _wait_until(page, ACTIVE_JS, gamma):
        fail("the first Enter after a mouse click did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta, gamma]:
        fail(f"a mouse click then Enter opened {_opens(page)}, expected [beta, gamma]")

    # A mouse drag along a row is a gesture, not an activation: the click the
    # browser sends after it must not open the conversation.
    _open(page, alpha)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.evaluate(COUNT_OPENS_JS)
    box = _title(page, beta).bounding_box()
    x, y = box["x"] + min(20, box["width"] / 3), box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 30, y, steps=5)
    page.mouse.up()
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) or not page.evaluate(ACTIVE_JS, alpha):
        fail(f"a mouse drag along a row opened {_opens(page)}")

    # Press a title, drag onto the same row's actions button, release: the
    # button stops pointerup from bubbling, and the browser's click then
    # targets the row. Nothing may open (master keeps the current one).
    _open(page, alpha)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.evaluate(COUNT_OPENS_JS)
    box = _title(page, beta).bounding_box()
    x, y = box["x"] + min(20, box["width"] / 3), box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    trigger = page.evaluate_handle(ACTIONS_TRIGGER_JS, beta).as_element()
    tbox = trigger.bounding_box() if trigger else None
    if not tbox or not tbox["width"]:
        fail("the beta row's actions button has no box while the row is hovered")
    else:
        page.mouse.down()
        page.mouse.move(tbox["x"] + tbox["width"] / 2, tbox["y"] + tbox["height"] / 2, steps=8)
        page.mouse.up()
        page.wait_for_timeout(SETTLE_MS)
        if _opens(page) or not page.evaluate(ACTIVE_JS, alpha):
            fail(f"dragging from a title onto its actions button opened {_opens(page)}")
    page.evaluate("() => typeof closeSessionActionMenu === 'function' && closeSessionActionMenu()")

    # A mouse press must not park focus on a title: the list would stay
    # :focus-within, and refreshes deferred while the user interacts would
    # never apply after the mouse leaves. Jittery 8 px press, no open.
    _open(page, alpha)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.evaluate(COUNT_OPENS_JS)
    box = _title(page, beta).bounding_box()
    x, y = box["x"] + min(20, box["width"] / 3), box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 8, y, steps=4)
    page.mouse.up()
    page.mouse.move(0, 0)
    page.wait_for_timeout(SETTLE_MS)
    if page.evaluate(FOCUS_IN_LIST_JS):
        fail("a mouse press on a title left keyboard focus inside the conversation list")
    page.evaluate("() => renderSessionList({deferWhileInteracting: true})")
    if not _wait_until(page, "() => !_isSessionListUserInteracting() && !_pendingSessionListPayload", timeout_ms=2500):
        fail("a deferred sidebar refresh was still pending 2.5 s after a mouse press on a title")

    _open(page, alpha)
    page.evaluate(COUNT_OPENS_JS)
    trigger = page.evaluate_handle("""(sid) => {
      const row = Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
        .find(node => node.dataset.sid === sid);
      return row && row.querySelector('.session-actions-trigger');
    }""", beta).as_element()
    if trigger is None:
        fail("the beta row has no actions trigger")
    else:
        trigger.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            fail("Enter on the actions trigger did not open its menu")
        if _opens(page) or not page.evaluate(ACTIVE_JS, alpha):
            fail(f"Enter on the actions trigger opened a conversation: {_opens(page)}")
    page.keyboard.press("Escape")
    page.evaluate("() => typeof closeSessionActionMenu === 'function' && closeSessionActionMenu()")
    page.wait_for_timeout(300)

    # A press on the title that already has focus (a screen reader's activate
    # command starts with mousedown) must leave focus there, as a native press
    # on a focusable element would. Otherwise an activation that then opens
    # nothing would drop the reading position to <body>. This pins the
    # mousedown step only.
    _open(page, alpha)
    _focus_title(page, beta)
    page.evaluate("""(el) => el.dispatchEvent(new MouseEvent('mousedown',
      {bubbles: true, cancelable: true, view: window, detail: 1, button: 0}))""", _title(page, beta))
    if page.evaluate(FOCUSED_SID_JS) != beta:
        fail("a screen reader's press on the focused title moved focus off it")
    page.wait_for_timeout(300)

    # Double-click still renames (the title drops mouse focus, not the press).
    _open(page, alpha)
    if not _dblclick_rename(page, beta):
        fail("double-clicking a title no longer starts a rename")
    page.keyboard.press("Escape")
    if not _wait_until(page, "() => !_renamingSid && !document.querySelector('#sessionList .session-title-input')", timeout_ms=2000):
        fail("Escape did not end the rename started by double-click")
    page.wait_for_timeout(300)

    # A press on another title while a rename is active must still commit and
    # end the rename. The title's mousedown prevents the default (so it doesn't
    # take focus), which would otherwise also keep focus in the rename input and
    # swallow the click until Enter or Escape. Like master, that click does not
    # also open the other conversation: committing re-renders the list.
    _open(page, alpha)
    if not _dblclick_rename(page, beta):
        fail("double-clicking a title no longer starts a rename (rename-then-click case)")
    else:
        page.keyboard.press("End")
        suffix = _rename_suffix()
        page.keyboard.type(suffix)
        _title(page, gamma).click()
        if not _wait_until(page, RENAME_ENDED_JS, timeout_ms=2000):
            fail("clicking another title during a rename left the rename active")
        if not _wait_until(page, RENAMED_JS, [beta, suffix], timeout_ms=3000):
            fail("clicking another title during a rename did not commit the rename")
    page.wait_for_timeout(300)

    # The same with the name left unchanged: the click still ends the rename.
    # Committing then needs no save, so the rename ends on the press and the
    # release opens the other conversation, as on master; whether it opens is
    # not asserted here.
    _open(page, alpha)
    if not _dblclick_rename(page, beta):
        fail("double-clicking a title no longer starts a rename (unchanged-name case)")
    else:
        _title(page, gamma).click()
        if not _wait_until(page, RENAME_ENDED_JS, timeout_ms=2000):
            fail("clicking another title during an unchanged rename left the rename active")
        page.wait_for_timeout(SETTLE_MS)
    page.wait_for_timeout(300)

    # The same for other editors that save on blur, here a project rename:
    # the press on a title must still move focus out of it, so the new name is
    # saved before opening the conversation rebuilds the sidebar.
    _open(page, alpha)
    suffix = _rename_suffix()
    project_id = page.evaluate(START_PROJECT_RENAME_JS)
    if not _wait_until(page, PROJECT_INPUT_FOCUSED_JS, timeout_ms=2000):
        fail(f"could not start a project rename ({project_id})")
    else:
        page.keyboard.press("End")
        page.keyboard.type(suffix)
        _title(page, gamma).click()
        if not _wait_until(page, PROJECT_RENAMED_JS, [project_id, suffix], timeout_ms=3000):
            fail("clicking a conversation title during a project rename did not save the new project name")
        # Let the app's own refresh after the rename land before going on.
        elif not _wait_until(page, "(suffix) => Array.from(document.querySelectorAll('.project-chip')).some(c => c.textContent.includes(suffix))", suffix, timeout_ms=3000):
            fail("the sidebar did not show the renamed project")
        # Remove the project so later runs and screenshots show the usual sidebar.
        page.evaluate(DELETE_GATE_PROJECT_JS, project_id)
    page.wait_for_timeout(300)

    page.evaluate("() => toggleSessionSelectMode()")
    page.wait_for_timeout(300)
    titles = page.evaluate(TITLES_JS)
    if any(t['tag'] == 'BUTTON' or t['tabindex'] >= 0 for t in titles):
        fail(f"selection mode still exposes focusable titles: {titles}")
    page.evaluate("() => exitSessionSelectMode()")
    page.wait_for_timeout(300)
    return failures


def _open_drawer_if_phone(page, phone):
    if not phone:
        return
    page.evaluate(
        "() => { if (typeof toggleMobileSidebar === 'function'"
        " && !document.querySelector('.sidebar.mobile-open')) toggleMobileSidebar(); }"
    )
    page.wait_for_timeout(400)


def _touch_point(box):
    """Where native touches land on a row title: near its start, mid-height."""
    return box["x"] + min(20, box["width"] / 3), box["y"] + box["height"] / 2


def _native_short_swipe(cdp, box, dx=30, steps=3):
    """A horizontal touch drag through Chromium's native input pipeline (not
    dispatchEvent), shorter than the archive/delete thresholds: trusted
    touch/pointer events and no compatibility click."""
    x, y = _touch_point(box)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x + dx * i / steps, "y": y}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _native_long_press(page, cdp, box):
    """Hold a touch on a row through Chromium's native input pipeline, well
    past the app's long-press delay, then lift it."""
    hold_ms = page.evaluate("() => SESSION_LONG_PRESS_DELAY_MS") + 300
    x, y = _touch_point(box)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    time.sleep(hold_ms / 1000)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _wait_since(page, started, ms):
    left = ms - (time.time() - started) * 1000
    if left > 0:
        page.wait_for_timeout(left)


AT_ACTIVATE_JS = """(el) => {
  for (const type of ['mousedown', 'mouseup', 'click']) {
    el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window, detail: 1, button: 0}));
  }
}"""


def _check_touch(page, ctx, seed, label, phone):
    """Mixed touch + keyboard/AT input in a touch context."""
    failures = []
    alpha, beta, gamma = seed["alpha"], seed["beta"], seed["gamma"]
    cdp = ctx.new_cdp_session(page)

    def fail(message):
        failures.append(f"  [{label}] {message}")

    # A fresh context may still be loading the list; wait for the seeded rows.
    page.evaluate("() => renderSessionList({deferWhileInteracting: false})")
    if not _wait_until(page, "(sids) => sids.every(sid => document.querySelector('#sessionList .session-item[data-sid=\"' + sid + '\"]'))", [alpha, beta, gamma], timeout_ms=10000):
        fail("the seeded conversations did not appear in the sidebar")
        return failures

    # An ordinary tap opens exactly once.
    _open(page, alpha)
    _open_drawer_if_phone(page, phone)
    page.evaluate(COUNT_OPENS_JS)
    _title(page, beta).tap()
    if not _wait_until(page, ACTIVE_JS, beta):
        fail("a tap no longer opens the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [beta]:
        fail(f"a tap opened {_opens(page)}, expected exactly [beta]")

    # A tap, then Enter on another title: each opens once.
    if not phone:  # on a phone the opened conversation closes the drawer
        page.evaluate(COUNT_OPENS_JS)
        _focus_title(page, gamma)
        page.keyboard.press("Enter")
        if not _wait_until(page, ACTIVE_JS, gamma):
            fail("Enter after a tap did not open the conversation")
        page.wait_for_timeout(SETTLE_MS)
        if _opens(page) != [gamma]:
            fail(f"Enter after a tap opened {_opens(page)}, expected exactly [gamma]")

    # A short native swipe (no compatibility click), then the FIRST Enter on
    # another title about 200 ms after release must open it.
    _open(page, alpha)
    _open_drawer_if_phone(page, phone)
    page.evaluate(COUNT_OPENS_JS)
    _native_short_swipe(cdp, _title(page, beta).bounding_box())
    released = time.time()
    _focus_title(page, gamma)
    _wait_since(page, released, 200)
    page.keyboard.press("Enter")
    if not _wait_until(page, ACTIVE_JS, gamma):
        fail("the first Enter about 200 ms after a short swipe did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [gamma]:
        fail(f"short swipe then Enter opened {_opens(page)}, expected exactly [gamma]")
    in_sidebar = page.evaluate("() => !!(document.activeElement && document.activeElement.closest('.sidebar'))")
    if phone and in_sidebar:
        fail("after Enter opened a conversation, focus stayed inside the closed drawer")
    if not phone and page.evaluate(FOCUSED_SID_JS) != gamma:
        fail("after Enter opened a conversation, focus was not on its title in the sidebar")

    # The same with a screen reader's activate command (mouse events, no
    # pointer events) instead of Enter.
    _open(page, alpha)
    _open_drawer_if_phone(page, phone)
    page.evaluate(COUNT_OPENS_JS)
    _native_short_swipe(cdp, _title(page, beta).bounding_box())
    released = time.time()
    _wait_since(page, released, 200)
    page.evaluate(AT_ACTIVATE_JS, _title(page, gamma))
    if not _wait_until(page, ACTIVE_JS, gamma):
        fail("assistive activation about 200 ms after a short swipe did not open the conversation")
    page.wait_for_timeout(SETTLE_MS)
    if _opens(page) != [gamma]:
        fail(f"short swipe then assistive activation opened {_opens(page)}, expected exactly [gamma]")

    # A tap moves focus out of a focused editor, as on master: the search box
    # here, so the on-screen keyboard closes (other editors save on that blur).
    # The tap's compatibility mousedown, which did that, is cancelled. Not on a
    # phone: there the opened conversation closes the drawer, which moves focus
    # out of the search box anyway, so the check would prove nothing.
    if not phone:
        _open(page, alpha)
        page.evaluate("() => document.getElementById('sessionSearch').focus()")
        _title(page, gamma).tap()
        if not _wait_until(page, ACTIVE_JS, gamma):
            fail("a tap with the search box focused did not open the conversation")
        page.wait_for_timeout(SETTLE_MS)
        if page.evaluate("() => document.activeElement === document.getElementById('sessionSearch')"):
            fail("a tap on a title left focus (and the on-screen keyboard) in the search box")

    # A long press opens the row's action menu with focus on its first item;
    # releasing the finger must leave focus there (touchend releases focus only
    # for a tap).
    _open(page, alpha)
    _open_drawer_if_phone(page, phone)
    _native_long_press(page, cdp, _title(page, beta).bounding_box())
    page.wait_for_timeout(300)
    if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
        fail("a long press did not open the action menu")
    else:
        if not page.evaluate("() => !!document.activeElement && !!document.activeElement.closest('.session-action-menu')"):
            fail("releasing a long press took focus out of the action menu")
        page.keyboard.press("Escape")
        if not _wait_until(page, "() => !document.querySelector('.session-action-menu')", timeout_ms=2000):
            fail("Escape did not close the action menu opened by a long press")
    page.evaluate("() => typeof closeSessionActionMenu === 'function' && closeSessionActionMenu()")
    page.wait_for_timeout(300)

    # A long press on a tag chip still opens the row's action menu, as on master, and its release
    # neither filters nor closes that menu. A plain tap on the tag filters and opens nothing.
    delta = seed["delta"]
    _open(page, alpha)
    _open_drawer_if_phone(page, phone)
    page.evaluate(CLEAR_SEARCH_JS)
    tag = page.locator(f'#sessionList .session-item[data-sid="{delta}"] .session-tag').first
    if not _wait_until(page, "(sid) => !!document.querySelector('#sessionList .session-item[data-sid=\"' + sid + '\"] .session-tag')", delta):
        fail("the tagged conversation has no tag chip")
    else:
        page.evaluate(COUNT_OPENS_JS)
        _native_long_press(page, cdp, tag.bounding_box())
        page.wait_for_timeout(300)
        if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            fail("a long press on a tag no longer opens the action menu")
        if page.locator('#sessionSearch').input_value() != '':
            fail("releasing a long press on a tag also filtered by the tag")
        if _opens(page):
            fail(f"a long press on a tag opened {_opens(page)}")
        page.evaluate("() => typeof closeSessionActionMenu === 'function' && closeSessionActionMenu()")
        page.wait_for_timeout(300)
        page.evaluate(COUNT_OPENS_JS)
        tag.tap()
        page.wait_for_timeout(SETTLE_MS)
        if page.locator('#sessionSearch').input_value() != '#short':
            fail("a tap on a tag no longer filters by it")
        if _opens(page):
            fail(f"a tap on a tag opened {_opens(page)}")
        if page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            fail("a tap on a tag opened the action menu")
        # With an action menu open, a tag tap closes the menu first, as on master, and the sidebar never shows a
        # tag search it has not applied (Codex round F, 2026-10-23: the menu stayed open, the search field read
        # "#short" and every conversation was still listed).
        page.evaluate(CLEAR_SEARCH_JS)
        page.wait_for_timeout(300)
        _native_long_press(page, cdp, tag.bounding_box())
        # Wait out the double-click interval: Chromium turns a long press and a tap on the same spot into a
        # dblclick, which reaches the row after the tag filter has replaced it and starts a rename that never
        # ends (#8503, also on master). This case is about the tap while the menu is open, not that.
        page.wait_for_timeout(700)
        if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            fail("setup: a long press on the tag did not open the action menu before the menu-open tap")
        else:
            page.evaluate(COUNT_OPENS_JS)
            tag.tap()
            page.wait_for_timeout(SETTLE_MS)
            if page.evaluate("() => !!document.querySelector('.session-action-menu')"):
                fail("a tag tap with the action menu open left the menu open")
            if _opens(page):
                fail(f"a tag tap with the action menu open opened {_opens(page)}")
            query = page.locator('#sessionSearch').input_value()
            alpha_listed = page.evaluate("""(sid) => {
              const row = document.querySelector('#sessionList .session-item[data-sid="' + sid + '"]');
              return !!(row && row.offsetParent !== null);
            }""", alpha)
            if query == '#short' and alpha_listed:
                fail("after a tag tap with the action menu open the search reads #short but the sidebar is unfiltered")
            page.evaluate("() => typeof closeSessionActionMenu === 'function' && closeSessionActionMenu()")
            page.wait_for_timeout(300)
        # A short swipe that starts on the tag is a drag: it opens nothing and leaves the row in place.
        page.evaluate(CLEAR_SEARCH_JS)
        page.wait_for_timeout(300)
        page.evaluate(COUNT_OPENS_JS)
        _native_short_swipe(cdp, tag.bounding_box())
        page.wait_for_timeout(SETTLE_MS)
        if _opens(page):
            fail(f"a short swipe starting on a tag opened {_opens(page)}")
        shifted = page.evaluate("""(sid) => {
          const row = document.querySelector('#sessionList .session-item[data-sid="' + sid + '"]');
          if (!row) return 'no row';
          const off = row.style.getPropertyValue('--session-swipe-offset').trim();
          const swiping = ['swiping-right', 'swiping-left', 'swipe-committed'].filter(c => row.classList.contains(c));
          return (off && off !== '0px') || swiping.length ? (off + ' ' + swiping.join(' ')).trim() : '';
        }""", delta)
        if shifted:
            fail(f"a short swipe starting on a tag left the row shifted ({shifted})")
    page.evaluate(CLEAR_SEARCH_JS)
    page.wait_for_timeout(300)

    # A tap on another title while a rename is active must commit and end the
    # rename, as on master. The gesture refuses to open a row during a rename;
    # touchend cancels the tap (and so its compatibility mousedown) and
    # releases focus itself, which blurs (and so commits) the rename input.
    _open(page, alpha)
    _open_drawer_if_phone(page, phone)
    page.evaluate(START_RENAME_JS, beta)
    focused_input = RENAME_INPUT_FOCUSED_JS
    if not _wait_until(page, focused_input, timeout_ms=2000):
        fail("could not start a rename (rename-then-tap case)")
    else:
        page.keyboard.press("End")
        suffix = _rename_suffix()
        page.keyboard.type(suffix)
        _title(page, gamma).tap()
        if not _wait_until(page, RENAME_ENDED_JS, timeout_ms=2000):
            fail("tapping another title during a rename left the rename active")
        if not _wait_until(page, RENAMED_JS, [beta, suffix], timeout_ms=3000):
            fail("tapping another title during a rename did not commit the rename")
    page.wait_for_timeout(300)

    # The same with the name left unchanged: the tap ends the rename and does
    # not also open the other conversation (as on master). Committing then
    # needs no save, so nothing else is still blocking the tap's compatibility
    # click from the row's assistive-activation listener.
    _open(page, alpha)
    _open_drawer_if_phone(page, phone)
    page.evaluate(START_RENAME_JS, beta)
    if not _wait_until(page, focused_input, timeout_ms=2000):
        fail("could not start a rename (unchanged-name tap case)")
    else:
        page.evaluate(COUNT_OPENS_JS)
        _title(page, gamma).tap()
        if not _wait_until(page, RENAME_ENDED_JS, timeout_ms=2000):
            fail("tapping another title during an unchanged rename left the rename active")
        page.wait_for_timeout(SETTLE_MS)
        if _opens(page) or not page.evaluate(ACTIVE_JS, alpha):
            fail(f"tapping another title during an unchanged rename opened {_opens(page)}")
    page.wait_for_timeout(300)

    return failures


def _screenshots(browser, directory, seed):
    os.makedirs(directory, exist_ok=True)
    for width, height, touch in ((1440, 900, False), (390, 844, True)):
        extra = {"has_touch": True, "is_mobile": True} if touch else {}
        ctx, page, _errors = _new_page(browser, viewport={"width": width, "height": height}, **extra)
        if page is None:
            continue
        _open(page, seed["alpha"])
        if touch:
            page.evaluate(
                "() => { if (typeof toggleMobileSidebar === 'function'"
                " && !document.querySelector('.sidebar.mobile-open')) toggleMobileSidebar(); }"
            )
            page.wait_for_timeout(400)
        page.focus("#sessionSearch")
        for _ in range(25):
            page.keyboard.press("Tab")
            if page.evaluate(FOCUSED_SID_JS) == seed["beta"]:
                break
        page.wait_for_timeout(200)
        path = os.path.join(directory, f"session-row-focus-{width}x{height}.png")
        page.screenshot(path=path)
        print(f"shot {path}")
        ctx.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    shots = None
    if "--screenshots" in sys.argv:
        shots = sys.argv[sys.argv.index("--screenshots") + 1]

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-session-row-activation-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    if _port_in_use(PORT):
        print(f"SETUP FAIL: port {PORT} is already in use; refusing to test another server", file=sys.stderr)
        return 2

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(proc, timeout=30):
            print("SETUP FAIL: this gate's server exited or did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        seed = None
        with sync_playwright() as pw:
            engines = [("chromium", pw.chromium, {"args": ["--no-sandbox", "--disable-dev-shm-usage"]})]
            for name, engine in (("firefox", pw.firefox), ("webkit", pw.webkit)):
                if os.path.exists(engine.executable_path):
                    engines.append((name, engine, {}))
                else:
                    print(f"NOTE {name}: Playwright's {name} build is not installed; skipped")
            for name, engine, launch_args in engines:
                try:
                    browser = engine.launch(headless=True, timeout=15000, **launch_args)
                except Exception as exc:  # e.g. WebKit without its system libraries
                    if name == "chromium":
                        raise
                    print(f"NOTE {name}: could not launch ({str(exc).strip().splitlines()[0]}); skipped")
                    continue
                ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
                if page is None:
                    failures.append(f"  [{name}] {errors}")
                    browser.close()
                    continue
                if seed is None:
                    seed = page.evaluate(SEED_JS)
                else:
                    page.evaluate("() => renderSessionList({deferWhileInteracting: false})")
                    page.wait_for_timeout(500)
                found = _check_desktop(page, seed, name)
                failures.extend(found)
                if not found:
                    print(f"OK  {name} — rows open from assistive activation, click(), Enter and the mouse; Enter keeps focus on the opened row; drags and presses neither open nor park focus; repaints never steal focus")
                failures.extend(f"  [{name}] pageerror: {err}" for err in errors)
                ctx.close()

                if name == "chromium":
                    ctx, page, errors = _new_page(
                        browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
                    )
                    if page is None:
                        failures.append(f"  [touch] {errors}")
                    else:
                        found = _check_touch(page, ctx, seed, "phone 390x844", phone=True)
                        failures.extend(found)
                        if not found:
                            print("OK  phone 390x844 — tap opens once; short swipe never swallows the next Enter or AT activation")
                        failures.extend(f"  [phone 390x844] pageerror: {err}" for err in errors)
                        ctx.close()
                    ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 1000}, has_touch=True)
                    if page is None:
                        failures.append(f"  [touch 1440x1000] {errors}")
                    else:
                        found = _check_touch(page, ctx, seed, "touch 1440x1000", phone=False)
                        failures.extend(found)
                        if not found:
                            print("OK  touch 1440x1000 — tap opens once; tap/short swipe never swallow the next Enter or AT activation")
                        failures.extend(f"  [touch 1440x1000] pageerror: {err}" for err in errors)
                        ctx.close()
                    if shots and seed:
                        _screenshots(browser, shots, seed)
                browser.close()

        if failures:
            print("\nSESSION ROW ACTIVATION FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nSESSION ROW ACTIVATION PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        # The state dir holds the server's caches; /tmp is often RAM-backed.
        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
