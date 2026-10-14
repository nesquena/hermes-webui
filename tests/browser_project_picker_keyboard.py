#!/usr/bin/env python3
"""
Headless browser gate: the "Move to project" pickers work from the keyboard,
return focus when they close, are translated, and have finger-sized rows (#8044).

WHY THIS EXISTS
  The single-conversation picker and the batch picker built their rows as
  click-only <div>s: no role, no tabindex, no key handling. A keyboard or
  screen-reader user could open the conversation's ⋮ menu, choose "Move to
  project", and then reach nothing. "No project" and "+ New project" were
  hard-coded English, and a row was about 24px tall on a phone.

WHAT IT CHECKS
  single-conversation picker, opened from the ⋮ menu with the keyboard
  - the picker is a named menu of buttons, and focus lands on the
    conversation's current project;
  - ArrowDown / ArrowUp wrap, Home / End jump;
  - Tab closes it instead of leaving it open behind the focus;
  - Escape closes it and focus returns to the conversation's ⋮ trigger, also
    when the sidebar was repainted while the picker was open (the original
    trigger is gone by then);
  - opened by a right click on the row instead (the anchor is then the row,
    which cannot take focus), Escape still returns focus to its ⋮ trigger;
  - Enter on a project and Space on "No project" send the move, as a mouse
    click on a row does;
  - "No project" and "+ New project" follow the interface language.
  batch picker, opened from the selection bar
  - rows are buttons, focus lands on the first, "No project" is translated;
  - Escape closes it and focus returns to the bar's "Move to project" button;
  - it still sits inside the selection bar.
  a conversation with a fork, its row expanded
  - the parent row then holds the fork's row, and the fork's ⋮ trigger comes
    first inside it; opened from the parent by a right click, Escape returns
    focus to the parent's own trigger, also after a sidebar repaint.
  a long list (twelve more projects, a 420px-tall window)
  - the picker scrolls inside itself, and the row that End, Home or opening
    puts focus on is inside the picker's visible box;
  - the batch picker, which with a mouse grows inside the conversation list
    instead: the row End, Home or ArrowUp puts focus on is on screen.
  rows under a coarse pointer (a touch context, 390x844, the sidebar drawer open)
  - every row of both pickers is at least 44px tall; with a mouse they keep
    their compact height.
  a long list of conversations (forty more), still three projects
  - on a phone upright (390x844, 375x667, touch) the five rows show whole, below
    their anchor, and above it from the last conversation on screen;
  - on a phone on its side (844x390, 667x375), opened from a parent
    conversation whose four open forks make its row taller than the room
    above or below it, at every 20px list position that shows the row: the
    five rows show whole and do not scroll, slid up over the anchor from the
    bottom of the screen only as far as they must.
  - an open picker follows a resize: after a window is made shorter (opened
    at the top, the middle and the bottom of the list, and once more after a
    sidebar repaint replaced the row it was opened from) and a tablet is
    turned, every row can still be tapped and the picker still lines up with
    its anchor;
  - a phone turned on its side closes its drawer and collapses the sidebar:
    the picker closes with it instead of floating over the composer.
  the same list of conversations and fifteen projects, with a mouse
  - a list that fits a 900px-tall window and not a 420px one: after the
    resize the row the keyboard was on is inside the picker's box.
  the same list of conversations and fifteen projects
  - with a mouse, a wheel over the open batch picker scrolls the conversation
    list: the picker sits in that list and must not take the scroll;
  - on a phone upright, a small phone and a phone on its side (390x844,
    375x667, 844x390, 667x375, touch), the single picker opened from the first, middle
    and last conversation on screen lets every row be tapped, scrolling inside
    itself where they do not fit; the batch picker stays within its cap,
    scrolls inside it, and every row can be tapped;
  - on a phone on its side, the same from the parent conversation with its
    forks open, at every list position: the list is now taller than the
    screen, so the picker is pinned to it and scrolls inside.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state. Conversations are imported and projects created
  through the public API. Placement is checked on touch screens only; with a
  mouse the gate checks the keyboard, not where the picker sits.

USAGE
  python tests/browser_project_picker_keyboard.py
  python tests/browser_project_picker_keyboard.py --screenshots DIR
      also writes the single-conversation picker, opened from the middle of
      the list, at 390x844, 820x1180, 844x390 and 1440x900 (many projects, long
      names, German) into DIR, and at 844x390 once more from a parent
      conversation with its forks open.
  (Requires: playwright + chromium.)

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

PORT = int(os.getenv("PROJECT_PICKER_PORT", "8798"))
BASE = f"http://127.0.0.1:{PORT}"
WAIT_MS = 5000
# After a move, a selection change or entering selection mode the sidebar is
# repainted once more about 300 ms later, which replaces every row, trigger and
# selection-bar button. A person is slower than that; the driver waits it out.
SETTLE_MS = 600
MIN_TOUCH_ROW_PX = 44
PROJECTS = ["Research", "Client work", "Reading list"]
LONG_PROJECTS = [
    "Quartalsbericht und Budgetplanung für das kommende Geschäftsjahr",
    "Kundengespräche",
    "Übersetzungen",
    "Архив переписки",
    "読書メモ",
    "Maintenance",
    "Onboarding",
    "Hiring",
    "Infrastructure",
    "Design reviews",
    "Release notes",
    "Experiments",
]


def _wait_for_health(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _wait_until(page, expression, timeout_ms=WAIT_MS):
    """Poll ``expression`` with page.evaluate. The app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(50)
    return False


SEED_JS = """async ({projects}) => {
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
  for (const title of ['Alpha conversation', 'Beta conversation']) {
    const data = await post('/api/session/import', {title, messages});
    sids.push(data.session.session_id);
  }
  const ids = [];
  for (const name of projects) {
    const data = await post('/api/projects/create', {name, color: '#7cb9ff'});
    ids.push(data.project.project_id);
  }
  await post('/api/session/move', {session_id: sids[0], project_id: ids[0]});
  await renderSessionList();
  return {alpha: sids[0], beta: sids[1], projects: ids};
}"""

# What the open picker looks like, as data.
PICKER_JS = """(selector) => {
  const picker = document.querySelector(selector);
  if (!picker) return null;
  const rows = Array.from(picker.querySelectorAll('.project-picker-item'));
  return {
    role: picker.getAttribute('role'),
    label: picker.getAttribute('aria-label'),
    insideBatchBar: !!picker.closest('#batchActionBar'),
    rows: rows.map(row => ({
      tag: row.tagName,
      type: row.getAttribute('type'),
      role: row.getAttribute('role'),
      checked: row.getAttribute('aria-checked'),
      text: row.textContent.trim(),
      focused: row === document.activeElement,
      height: Math.round(row.getBoundingClientRect().height),
    })),
  };
}"""

FOCUSED_TRIGGER_JS = """(sid) => {
  const active = document.activeElement;
  if (!active || !active.classList.contains('session-actions-trigger')) return false;
  const row = active.closest('.session-item,.session-child-session');
  return !!row && row.dataset.sid === sid && active.isConnected;
}"""

SINGLE = ".project-picker:not(.batch-project-picker)"
BATCH = ".batch-project-picker"


def _new_page(browser, **context_args):
    ctx = browser.new_context(base_url=BASE, **context_args)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    ready = "typeof S !== 'undefined' && typeof renderSessionList === 'function'"
    if not _wait_until(page, ready, 15000):
        ctx.close()
        return None, None, ["the app did not initialize"]
    page.wait_for_timeout(1000)
    return ctx, page, errors


def _open_single_picker(page, sid):
    """Open the picker the way a keyboard user does: ⋮ trigger, then the menu's
    "Move to project" item. Returns a failure line, or None.

    A sidebar repaint replaces the row and its trigger, so the trigger focused
    here can be gone before Enter is pressed. That race is this driver's: let the
    sidebar settle first, and focus again and retry if it still happens."""
    problem = None
    page.wait_for_timeout(SETTLE_MS)
    for _attempt in range(3):
        focused = page.evaluate(
            """(sid) => {
              const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
              const trigger = row && row.querySelector('.session-actions-trigger');
              if (!trigger) return false;
              trigger.focus();
              return document.activeElement === trigger;
            }""",
            sid,
        )
        if not focused:
            problem = "the conversation's ⋮ trigger could not be focused"
            page.wait_for_timeout(200)
            continue
        page.keyboard.press("Enter")
        if not _wait_until(page, "!!document.querySelector('.session-action-menu')", 1500):
            problem = "the ⋮ menu did not open from the keyboard"
            continue
        moved = page.evaluate(
            """() => {
              const label = t('session_move_project');
              const item = Array.from(document.querySelectorAll('.session-action-menu .session-action-opt'))
                .find(opt => opt.textContent.trim() === label);
              if (!item) return false;
              item.focus();
              return document.activeElement === item;
            }"""
        )
        if not moved:
            return "the ⋮ menu has no focusable 'Move to project' item"
        page.keyboard.press("Enter")
        if _wait_until(page, f"!!document.querySelector('{SINGLE}')", 1500):
            return None
        problem = "the project picker did not open"
    return problem


def _focused_text(page):
    return page.evaluate("document.activeElement ? document.activeElement.textContent.trim() : ''")


def _check_single(page, seed):
    failures = []
    alpha = seed["alpha"]

    def fail(message):
        failures.append(f"  [single] {message}")

    problem = _open_single_picker(page, alpha)
    if problem:
        return [f"  [single] {problem}"]
    picker = page.evaluate(PICKER_JS, SINGLE)
    texts = [row["text"] for row in picker["rows"]]
    expected = ["No project", *PROJECTS, "+ New project"]
    if texts != expected:
        fail(f"rows are {texts}, expected {expected}")
    if picker["role"] != "menu":
        fail(f"the picker's role is {picker['role']!r}, expected 'menu'")
    if picker["label"] != "Move to project":
        fail(f"the picker's accessible name is {picker['label']!r}, expected 'Move to project'")
    not_buttons = [row["text"] for row in picker["rows"] if row["tag"] != "BUTTON" or row["type"] != "button"]
    if not_buttons:
        fail(f"rows that are not <button type=button>: {not_buttons}")
    roles = [row["role"] for row in picker["rows"]]
    if roles != ["menuitemradio"] * (len(PROJECTS) + 1) + ["menuitem"]:
        fail(f"row roles are {roles}")
    checked = [row["text"] for row in picker["rows"] if row["checked"] == "true"]
    if checked != [PROJECTS[0]]:
        fail(f"checked rows are {checked}, expected only the current project")
    focused = [row["text"] for row in picker["rows"] if row["focused"]]
    if focused != [PROJECTS[0]]:
        fail(f"focus opened on {focused}, expected the current project")
    if failures:
        return failures

    # Arrows wrap, Home and End jump.
    steps = [
        ("ArrowDown", PROJECTS[1]),
        ("ArrowUp", PROJECTS[0]),
        ("Home", "No project"),
        ("ArrowUp", "+ New project"),
        ("ArrowDown", "No project"),
        ("End", "+ New project"),
    ]
    for key, want in steps:
        page.keyboard.press(key)
        got = _focused_text(page)
        if got != want:
            fail(f"{key} moved focus to {got!r}, expected {want!r}")

    # Escape closes and returns focus to the conversation's trigger.
    page.keyboard.press("Escape")
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("Escape did not close the picker")
    if not page.evaluate(FOCUSED_TRIGGER_JS, alpha):
        fail("after Escape focus is not on the conversation's ⋮ trigger")

    # The same after a sidebar repaint replaced the trigger the picker opened from.
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.evaluate("renderSessionListFromCache()")
    if not _wait_until(page, f"!!document.querySelector('{SINGLE}')", 500):
        fail("a sidebar repaint closed the picker")
    else:
        page.evaluate(f"document.querySelector('{SINGLE} .project-picker-item').focus()")
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
        if not page.evaluate(FOCUSED_TRIGGER_JS, alpha):
            fail("after a sidebar repaint, Escape did not return focus to the conversation's new ⋮ trigger")

    # Tab closes the picker. Left open, it would sit there with focus gone
    # from it and no key able to reach it.
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.keyboard.press("End")
    page.keyboard.press("Tab")
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')", 1500):
        fail("Tab left the picker open")
        page.evaluate(f"document.querySelectorAll('{SINGLE}').forEach(p => p.remove())")
    elif page.evaluate("!document.activeElement || document.activeElement === document.body"):
        fail("after Tab focus is on nothing")
    elif page.evaluate(FOCUSED_TRIGGER_JS, alpha):
        # The Tab itself is the browser's: from the trigger it moves on.
        fail("Tab was swallowed: focus stopped on the conversation's ⋮ trigger")

    # Opened by a right click on the row: the menu's anchor is the row (or its
    # actions box), not the trigger. Escape still lands on the trigger.
    page.wait_for_timeout(SETTLE_MS)
    page.click(f'.session-item[data-sid="{alpha}"]', button="right")
    if not _wait_until(page, "!!document.querySelector('.session-action-menu')", 1500):
        fail("a right click on the row did not open the ⋮ menu")
    else:
        page.evaluate(
            """() => {
              const label = t('session_move_project');
              Array.from(document.querySelectorAll('.session-action-menu .session-action-opt'))
                .find(opt => opt.textContent.trim() === label).focus();
            }"""
        )
        page.keyboard.press("Enter")
        if not _wait_until(page, f"!!document.querySelector('{SINGLE}')", 1500):
            fail("the picker did not open from the right-click menu")
        else:
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)
            if not page.evaluate(FOCUSED_TRIGGER_JS, alpha):
                fail("opened by right click, Escape did not return focus to the conversation's ⋮ trigger")

    # Enter on a project moves the conversation.
    moves = []
    page.on(
        "request",
        lambda r: moves.append(r.post_data_json)
        if r.method == "POST" and urlsplit(r.url).path == "/api/session/move" else None,
    )
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    target = {"session_id": alpha, "project_id": seed["projects"][1]}
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("Enter on a project did not close the picker")
    page.wait_for_timeout(300)
    if moves != [target]:
        fail(f"Enter on a project sent {moves}, expected one move {target}")

    # Space on "No project" unassigns.
    moves.clear()
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.keyboard.press("Home")
    page.keyboard.press("Space")
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("Space on 'No project' did not close the picker")
    page.wait_for_timeout(300)
    if moves != [{"session_id": alpha, "project_id": None}]:
        fail(f"Space on 'No project' sent {moves}, expected one unassign")

    # A mouse click moves too.
    moves.clear()
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.click(f"{SINGLE} .project-picker-item:nth-child(2)")
    if not _wait_until(page, f"!document.querySelector('{SINGLE}')"):
        fail("a click on a project did not close the picker")
    page.wait_for_timeout(300)
    if moves != [{"session_id": alpha, "project_id": seed["projects"][0]}]:
        fail(f"a click on a project sent {moves}")

    # The two labels follow the interface language.
    page.evaluate("setLocale('de')")
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen in German: {problem}"]
    texts = [row["text"] for row in page.evaluate(PICKER_JS, SINGLE)["rows"]]
    if texts[0] != "Kein Projekt" or texts[-1] != "+ Neues Projekt":
        fail(f"in German the picker reads {texts[0]!r} … {texts[-1]!r}")
    page.keyboard.press("Escape")
    page.evaluate("setLocale('en')")
    return failures


def _open_batch_picker(page, seed):
    # Entering selection mode repaints the sidebar and, a moment later, rebuilds
    # the selection bar and replaces its buttons. That race is this driver's, not
    # the picker's: select, let the bar settle, then focus and press Enter, and
    # retry if the button was replaced all the same.
    page.evaluate(
        """({alpha, beta}) => {
          if (!_sessionSelectMode) toggleSessionSelectMode();
          setSessionSelected(alpha, true);
          setSessionSelected(beta, true);
        }""",
        seed,
    )
    page.wait_for_timeout(SETTLE_MS)
    for _attempt in range(3):
        focused = page.evaluate(
            """({alpha, beta}) => {
              if (!_sessionSelectMode) toggleSessionSelectMode();
              setSessionSelected(alpha, true);
              setSessionSelected(beta, true);
              const label = t('session_batch_move');
              const button = Array.from(document.querySelectorAll('#batchActionBar .batch-action-btn'))
                .find(btn => btn.textContent.trim() === label);
              if (!button) return false;
              button.focus();
              return document.activeElement === button;
            }""",
            seed,
        )
        if not focused:
            return "the selection bar has no focusable 'Move to project' button"
        page.keyboard.press("Enter")
        if _wait_until(page, f"!!document.querySelector('{BATCH}')", 1500):
            return None
    return "the batch picker did not open"


def _check_batch(page, seed):
    failures = []

    def fail(message):
        failures.append(f"  [batch] {message}")

    page.evaluate("setLocale('de')")
    problem = _open_batch_picker(page, seed)
    if problem:
        return [f"  [batch] {problem}"]
    picker = page.evaluate(PICKER_JS, BATCH)
    if not picker["insideBatchBar"]:
        fail("the batch picker is not inside the selection bar")
    if picker["role"] != "menu":
        fail(f"the picker's role is {picker['role']!r}, expected 'menu'")
    if picker["label"] != "Zum Projekt verschieben":
        fail(f"the picker's accessible name is {picker['label']!r}, expected the Move button's label in German")
    not_buttons = [row["text"] for row in picker["rows"] if row["tag"] != "BUTTON" or row["type"] != "button"]
    if not_buttons:
        fail(f"rows that are not <button type=button>: {not_buttons}")
    if [row["role"] for row in picker["rows"]] != ["menuitem"] * len(picker["rows"]):
        fail(f"row roles are {[row['role'] for row in picker['rows']]}")
    texts = [row["text"] for row in picker["rows"]]
    if texts != ["Kein Projekt", *PROJECTS]:
        fail(f"rows are {texts}")
    if [row["text"] for row in picker["rows"] if row["focused"]] != ["Kein Projekt"]:
        fail("focus did not open on the first row")
    page.keyboard.press("End")
    if _focused_text(page) != PROJECTS[-1]:
        fail(f"End moved focus to {_focused_text(page)!r}")
    on_move_button = (
        "(() => { const a = document.activeElement; return !!a && a.isConnected"
        " && a.classList.contains('batch-action-btn') && a.textContent.trim() === t('session_batch_move'); })()"
    )
    page.keyboard.press("Escape")
    if not _wait_until(page, f"!document.querySelector('{BATCH}')"):
        fail("Escape did not close the batch picker")
    if not page.evaluate(on_move_button):
        fail("after Escape focus is not on the bar's 'Move to project' button")
    page.evaluate("() => { exitSessionSelectMode(); setLocale('en'); }")
    return failures


# Opens both pickers and measures their rows in one step: a background sidebar
# refresh rebuilds the selection bar, and with it the batch picker, at any time.
ROW_HEIGHTS_JS = """({alpha, beta}) => {
  const heights = picker => Array.from(picker.querySelectorAll('.project-picker-item'))
    .map(row => Math.round(row.getBoundingClientRect().height));
  const row = document.querySelector('.session-item[data-sid="' + alpha + '"]');
  const session = _allSessions.find(s => s && s.session_id === alpha);
  if (!row || !session) return {problem: 'the conversation has no sidebar row'};
  const rowLeft = Math.round(row.getBoundingClientRect().left);
  if (!_sessionSelectMode) toggleSessionSelectMode();
  setSessionSelected(alpha, true);
  setSessionSelected(beta, true);
  const button = Array.from(document.querySelectorAll('#batchActionBar .batch-action-btn'))
    .find(btn => btn.textContent.trim() === t('session_batch_move'));
  if (!button) return {problem: 'the selection bar has no Move button'};
  _showBatchProjectPicker(button);
  const batch = document.querySelector('.batch-project-picker');
  if (!batch) return {problem: 'the batch picker did not open'};
  // Measured before the other picker opens: opening one removes every other.
  const result = {batch: heights(batch)};
  _showProjectPicker(session, document.querySelector('.session-item[data-sid="' + alpha + '"]') || row);
  const single = document.querySelector('.project-picker:not(.batch-project-picker)');
  if (!single) return {problem: 'the conversation picker did not open'};
  result.single = heights(single);
  result.rowLeft = rowLeft;
  document.querySelectorAll('.project-picker').forEach(p => p.remove());
  exitSessionSelectMode();
  return result;
}"""


def _row_heights(page, seed):
    """Row heights of both pickers on this page: (single, batch). The pickers are
    opened directly, from a row that is on screen: how a picker is reached is the
    keyboard checks' business."""
    result = page.evaluate(ROW_HEIGHTS_JS, seed)
    if result.get("problem"):
        return None, None, result["problem"]
    if result["rowLeft"] < 0:
        # A picker anchored on a row that is off screen is not what a person
        # opens, and a picker that follows its anchor would be torn down there.
        return None, None, f"the conversation's row is off screen (x={result['rowLeft']}): is the drawer closed?"
    return result["single"], result["batch"], None


def _open_mobile_drawer(page):
    """On a phone the sidebar is a drawer, closed at first, with its rows off screen."""
    page.evaluate(
        "() => { if (typeof toggleMobileSidebar === 'function'"
        " && !document.querySelector('.sidebar.mobile-open')) toggleMobileSidebar(); }"
    )
    page.wait_for_timeout(400)


def _check_heights(page, seed, *, coarse):
    label = "touch" if coarse else "mouse"
    if page.evaluate("matchMedia('(pointer:coarse)').matches") != coarse:
        return [f"  [{label}] the context's pointer is not {'coarse' if coarse else 'fine'}"]
    single, batch, problem = _row_heights(page, seed)
    if problem:
        return [f"  [{label}] {problem}"]
    failures = []
    for name, heights in (("single", single), ("batch", batch)):
        if coarse and min(heights) < MIN_TOUCH_ROW_PX:
            failures.append(f"  [touch] {name} picker rows are {heights}px tall, under {MIN_TOUCH_ROW_PX}px")
        if not coarse and max(heights) >= MIN_TOUCH_ROW_PX:
            failures.append(f"  [mouse] {name} picker rows grew to {heights}px with a fine pointer")
    return failures


FORK_SETUP_JS = """async (sid) => {
  const response = await fetch('/api/session/branch', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sid, title: 'Beta fork'}),
  });
  const data = await response.json();
  if (!response.ok || !data.session_id) return {problem: 'fork failed: ' + JSON.stringify(data)};
  await renderSessionList();
  const row = () => document.querySelector('.session-item[data-sid="' + sid + '"]');
  const toggle = row() && row().querySelector('.session-child-count');
  if (!toggle) return {problem: 'the parent row has no child-count toggle'};
  toggle.click();
  const triggers = Array.from(row().querySelectorAll('.session-actions-trigger'));
  const rowOf = el => el.closest('.session-item,.session-child-session');
  return {
    fork: data.session_id,
    triggers: triggers.length,
    firstBelongsToParent: triggers.length ? rowOf(triggers[0]) === row() : null,
  };
}"""


def _check_fork_parent(page, seed):
    """The parent's picker returns focus to the parent's trigger, not the fork's."""
    beta = seed["beta"]
    setup = page.evaluate(FORK_SETUP_JS, beta)
    if setup.get("problem"):
        return [f"  [fork parent] {setup['problem']}"]
    if setup["triggers"] < 2 or setup["firstBelongsToParent"]:
        # Without this shape the check below would pass for the wrong reason.
        return [f"  [fork parent] the expanded parent row does not hold a fork's trigger first: {setup}"]
    failures = []
    for repaint in (False, True):
        label = "after a sidebar repaint, " if repaint else ""
        page.wait_for_timeout(SETTLE_MS)
        page.click(f'.session-item[data-sid="{beta}"] .session-title', button="right")
        if not _wait_until(page, "!!document.querySelector('.session-action-menu')", 1500):
            return failures + ["  [fork parent] a right click on the parent row did not open the ⋮ menu"]
        page.evaluate(
            """() => {
              const label = t('session_move_project');
              Array.from(document.querySelectorAll('.session-action-menu .session-action-opt'))
                .find(opt => opt.textContent.trim() === label).focus();
            }"""
        )
        page.keyboard.press("Enter")
        if not _wait_until(page, f"!!document.querySelector('{SINGLE}')", 1500):
            return failures + ["  [fork parent] the picker did not open from the parent's menu"]
        if repaint:
            page.evaluate("renderSessionListFromCache()")
            page.evaluate(f"document.querySelector('{SINGLE} .project-picker-item').focus()")
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
        if not page.evaluate(FOCUSED_TRIGGER_JS, beta):
            where = page.evaluate(
                "(() => { const a = document.activeElement; const row = a && a.closest"
                " && a.closest('.session-item,.session-child-session');"
                " return (a ? a.tagName + '.' + a.className : 'nothing') + ' in row ' + (row ? row.dataset.sid : 'none'); })()"
            )
            failures.append(
                f"  [fork parent] {label}Escape returned focus to {where}, expected the parent's ⋮ trigger"
            )
    return failures


ROW_IN_BOX_JS = """() => {
  const picker = document.querySelector('.project-picker:not(.batch-project-picker)');
  const row = document.activeElement;
  if (!picker || !row || !picker.contains(row)) return {problem: 'focus is not on a picker row'};
  // The box inside the picker's border: a row on the border has its focus ring clipped.
  const top = picker.getBoundingClientRect().top + picker.clientTop;
  const bottom = top + picker.clientHeight;
  const rect = row.getBoundingClientRect();
  return {
    text: row.textContent.trim(),
    inside: rect.top >= top - 0.5 && rect.bottom <= bottom + 0.5,
    scrolls: picker.scrollHeight > picker.clientHeight + 1,
  };
}"""


def _check_long_list(page, seed):
    """With more rows than fit, the row focus lands on is inside the picker's box."""
    failures = []
    problem = _open_single_picker(page, seed["alpha"])
    if problem:
        return [f"  [long list] {problem}"]
    opened = page.evaluate(ROW_IN_BOX_JS)
    if opened.get("problem"):
        return [f"  [long list] {opened['problem']}"]
    if not opened["scrolls"]:
        failures.append("  [long list] the picker does not scroll inside itself")
    if opened["text"] != LONG_PROJECTS[-1]:
        failures.append(f"  [long list] focus opened on {opened['text']!r}, expected the current project")
    if not opened["inside"]:
        failures.append(f"  [long list] on open the focused row {opened['text']!r} is outside the picker's box")
    for key, want in (("End", "+ New project"), ("Home", "No project"), ("ArrowUp", "+ New project")):
        page.keyboard.press(key)
        state = page.evaluate(ROW_IN_BOX_JS)
        if state.get("problem") or state["text"] != want:
            failures.append(f"  [long list] {key} put focus on {state.get('text')!r}, expected {want!r}")
        elif not state["inside"]:
            failures.append(f"  [long list] after {key} the focused row {want!r} is outside the picker's box")
    page.keyboard.press("Escape")
    return failures


FOCUSED_ROW_ON_SCREEN_JS = """() => {
  const row = document.activeElement;
  const picker = document.querySelector('.batch-project-picker');
  if (!picker || !row || !picker.contains(row)) return {problem: 'focus is not on a batch picker row'};
  const rect = row.getBoundingClientRect();
  const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
  return {
    text: row.textContent.trim(),
    onScreen: !!hit && (hit === row || row.contains(hit)),
    y: Math.round(rect.top) + '..' + Math.round(rect.bottom) + ' of ' + innerHeight,
    taller: picker.getBoundingClientRect().height > innerHeight - picker.getBoundingClientRect().top,
  };
}"""


def _check_batch_long_list(page, seed):
    """The batch picker sits in the conversation list and, with a mouse, grows
    with its rows. The row the keyboard puts focus on is scrolled onto the screen."""
    problem = _open_batch_picker(page, seed)
    if problem:
        return [f"  [batch long list] {problem}"]
    failures = []
    first = page.evaluate(FOCUSED_ROW_ON_SCREEN_JS)
    if first.get("problem"):
        return [f"  [batch long list] {first['problem']}"]
    if not first["taller"]:
        failures.append("  [batch long list] the batch picker fits the window: the long list was not seeded")
    for key, want in (("End", LONG_PROJECTS[-1]), ("Home", "No project"), ("ArrowUp", LONG_PROJECTS[-1])):
        page.keyboard.press(key)
        page.wait_for_timeout(150)
        state = page.evaluate(FOCUSED_ROW_ON_SCREEN_JS)
        if state.get("problem") or state["text"] != want:
            failures.append(f"  [batch long list] {key} put focus on {state.get('text')!r}, expected {want!r}")
        elif not state["onScreen"]:
            failures.append(
                f"  [batch long list] after {key} the focused row {want!r} is off screen (y={state['y']})"
            )
    page.keyboard.press("Escape")
    page.evaluate("exitSessionSelectMode()")
    return failures


MANY_CONVERSATIONS = 40
# A phone upright, a small phone, and each of them on its side.
PHONE_VIEWPORTS = ((390, 844), (375, 667), (844, 390), (667, 375))
LANDSCAPE_VIEWPORTS = tuple(size for size in PHONE_VIEWPORTS if size[0] > size[1])

SEED_MANY_JS = """async (count) => {
  const messages = [
    {role: 'user', content: 'hello'},
    {role: 'assistant', content: 'hello back'},
  ];
  for (let n = 1; n <= count; n++) {
    const response = await fetch('/api/session/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: 'Filler conversation ' + n, messages}),
    });
    if (!response.ok) throw new Error('import failed: ' + response.status);
  }
  await renderSessionList();
}"""

# Scrolls the list alone, so the first conversation is at its top. On a phone on
# its side the conversations start below the list's fold.
FIRST_CONVERSATION_TO_TOP_JS = """() => {
  const list = document.getElementById('sessionList');
  const row = list && list.querySelector('.session-item[data-sid]');
  if (row) list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top;
}"""

# The conversations whose rows are wholly on screen, top to bottom.
VISIBLE_ROWS_JS = """() => Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]'))
  .filter(row => { const r = row.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0; })
  .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
  .map(row => row.dataset.sid)"""

# Opens the picker from a conversation's row, as a long press does on a phone,
# and asks of every row: once scrolled to inside the picker, is it under a finger?
REACHABLE_JS = """(sid) => {
  const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const session = _allSessions.find(s => s && s.session_id === sid);
  if (!row || !session) return {problem: 'the conversation has no sidebar row'};
  const anchorTop = Math.round(row.getBoundingClientRect().top);
  const anchorBottom = Math.round(row.getBoundingClientRect().bottom);
  _showProjectPicker(session, row);
  const picker = document.querySelector('.project-picker:not(.batch-project-picker)');
  if (!picker) return {problem: 'the picker did not open'};
  const unreachable = [];
  const items = Array.from(picker.querySelectorAll('.project-picker-item'));
  for (const item of items) {
    item.scrollIntoView({block: 'nearest'});
    const rect = item.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    if (!hit || !(hit === item || item.contains(hit)))
      unreachable.push(item.textContent.trim() + ' (y=' + Math.round(rect.top) + '..' + Math.round(rect.bottom) + ')');
  }
  const box = picker.getBoundingClientRect();
  return {
    anchorTop, anchorBottom, rows: items.length, unreachable,
    top: Math.round(box.top), bottom: Math.round(box.bottom), viewport: innerHeight,
    content: picker.scrollHeight,
    scrolls: picker.scrollHeight > picker.clientHeight + 1,
  };
}"""
# Three touch rows: a picker squeezed under this shows too little to choose from.
MIN_PICKER_PX = 3 * MIN_TOUCH_ROW_PX


def _misplaced(state):
    """A failure fragment when the picker leaves the screen, or scrolls a list
    the screen has room for: it should slide over its anchor instead."""
    if state["top"] < 0 or state["bottom"] > state["viewport"]:
        return f"the picker spans y={state['top']}..{state['bottom']} of {state['viewport']}"
    if state["scrolls"] and state["content"] <= state["viewport"] - 16:
        return (
            f"the picker scrolls its {state['rows']} rows inside y={state['top']}..{state['bottom']}"
            f" although their {state['content']}px fit the screen"
        )
    return None


def _wrong_side(state):
    """A failure fragment when a picker the screen has room for is not where the
    ⋮ menu would be: below its anchor, else above it, else slid up from below
    only as far as it must, which leaves the top of the sidebar uncovered."""
    if state["content"] > state["viewport"] - 16:
        return None
    height = state["bottom"] - state["top"]
    span = f"y={state['top']}..{state['bottom']}, anchor y={state['anchorTop']}..{state['anchorBottom']}"
    fits_below = state["anchorBottom"] + 4 + height <= state["viewport"] - 8
    fits_above = state["anchorTop"] > height + 12
    if fits_below and state["top"] < state["anchorBottom"]:
        return f"the picker has room below its anchor but is not there ({span})"
    if not fits_below and fits_above and state["bottom"] > state["anchorTop"]:
        return f"the picker has room above its anchor but is not there ({span})"
    if not fits_below and not fits_above and state["bottom"] < state["viewport"] - 9:
        return f"the picker fits neither side of its anchor and was not slid up from the bottom ({span})"
    return None


def _too_short(state):
    """A failure fragment when the picker shows less than three rows of a longer list."""
    shown = state["bottom"] - state["top"]
    if shown < min(MIN_PICKER_PX, state["content"]) - 1:
        return f"the picker is only {shown}px tall for {state['rows']} rows"
    return None


def _check_phone_geometry(page, size, rows=10):
    """On a phone, with a screenful of conversations and 15 projects, every row of
    the picker can be tapped wherever in the list it was opened."""
    page.evaluate(FIRST_CONVERSATION_TO_TOP_JS)
    page.wait_for_timeout(200)
    visible = page.evaluate(VISIBLE_ROWS_JS)
    if len(visible) < 3:
        return [f"  [phone {size}] only {len(visible)} conversations are on screen: is the drawer open?"]
    failures = []
    anchors = (("first", visible[0]), ("middle", visible[len(visible) // 2]), ("last", visible[-1]))
    for where, sid in anchors:
        state = page.evaluate(REACHABLE_JS, sid)
        if state.get("problem"):
            failures.append(f"  [phone {size}] {where} conversation: {state['problem']}")
            continue
        if state["rows"] < rows:
            failures.append(f"  [phone {size}] the picker has only {state['rows']} rows: the list was not seeded")
        for problem in filter(None, (_misplaced(state), _wrong_side(state), _too_short(state))):
            failures.append(f"  [phone {size}] opened from the {where} conversation on screen: {problem}")
        if state["unreachable"]:
            failures.append(
                f"  [phone {size}] opened from the {where} conversation on screen (y={state['anchorTop']}), the picker spans"
                f" y={state['top']}..{state['bottom']} of {state['viewport']} and"
                f" {len(state['unreachable'])} of {state['rows']} rows cannot be tapped: {state['unreachable']}"
            )
        page.keyboard.press("Escape")
        if not _wait_until(page, f"!document.querySelector('{SINGLE}')", 1500):
            failures.append(f"  [phone {size}] the picker opened from the {where} conversation did not close on Escape")
            page.evaluate("document.querySelectorAll('.project-picker').forEach(p => p.remove())")
    return failures


# Gives the conversation four forks in all (it has one from the fork check), so
# its expanded row is taller than the picker's smallest useful height.
MORE_FORKS_JS = """async (sid) => {
  for (let n = 2; n <= 4; n++) {
    const response = await fetch('/api/session/branch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sid, title: 'Beta fork ' + n}),
    });
    if (!response.ok) return {problem: 'fork failed: ' + response.status};
  }
  return {};
}"""

# Expands the parent's forks and scrolls the list alone, so the parent row's top
# is `offset` px below the list's top. Returns the row's place on screen.
TALL_ANCHOR_AT_JS = """({sid, offset}) => {
  const list = document.getElementById('sessionList');
  const row = () => document.querySelector('.session-item[data-sid="' + sid + '"]');
  if (!list || !row()) return {problem: 'the parent conversation has no sidebar row'};
  if (!row().querySelector('.session-child-session')) {
    const toggle = row().querySelector('.session-child-count');
    if (!toggle) return {problem: 'the parent row has no child-count toggle'};
    toggle.click();
  }
  list.scrollTop += row().getBoundingClientRect().top - list.getBoundingClientRect().top - offset;
  const rect = row().getBoundingClientRect();
  const box = list.getBoundingClientRect();
  return {
    height: Math.round(rect.height), top: Math.round(rect.top),
    visible: rect.bottom > box.top + 20 && rect.top < Math.min(box.bottom, innerHeight) - 20,
  };
}"""


def _check_tall_anchor(page, seed, size, *, long_list):
    """A phone on its side, the picker opened from a parent conversation whose
    expanded forks make its row taller than the room left above or below it:
    every row can still be tapped, wherever the list is scrolled to. A short
    list shows whole, over its anchor if need be; a long one scrolls."""
    label = "tall anchor, long list" if long_list else "tall anchor, short list"
    failures = []
    tried = 0
    for offset in range(-140, 260, 20):
        where = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": offset})
        if where.get("problem"):
            return [f"  [{label} {size}] {where['problem']}"]
        if where["height"] < 132:
            return [f"  [{label} {size}] the expanded parent row is only {where['height']}px tall"]
        if not where["visible"]:
            continue
        tried += 1
        state = page.evaluate(REACHABLE_JS, seed["beta"])
        if not state.get("problem") and (state["content"] > state["viewport"] - 16) != long_list:
            failures.append(
                f"  [{label} {size}] the picker's {state['rows']} rows are {state['content']}px tall"
                f" on a {state['viewport']}px screen: the wrong list was seeded"
            )
        if state.get("problem"):
            failures.append(f"  [{label} {size}] row at y={where['top']}: {state['problem']}")
        elif _misplaced(state) or _wrong_side(state) or _too_short(state):
            problem = _misplaced(state) or _wrong_side(state) or _too_short(state)
            failures.append(f"  [{label} {size}] parent row at y={where['top']}: {problem}")
        elif state["unreachable"]:
            failures.append(
                f"  [{label} {size}] parent row at y={where['top']} ({where['height']}px tall): the picker spans"
                f" y={state['top']}..{state['bottom']} of {state['viewport']} and"
                f" {len(state['unreachable'])} of {state['rows']} rows cannot be tapped"
            )
        page.keyboard.press("Escape")
        if not _wait_until(page, f"!document.querySelector('{SINGLE}')", 1500):
            page.evaluate("document.querySelectorAll('.project-picker').forEach(p => p.remove())")
    if tried < 8:
        failures.append(f"  [{label} {size}] only {tried} list positions showed the parent row")
    return failures


# Opens the picker and leaves it open: from the row on a phone, as a long press
# does, from the row's ⋮ trigger with a mouse.
OPEN_AND_LEAVE_JS = """({sid, mobile}) => {
  const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const anchor = row && (mobile ? row : row.querySelector('.session-actions-trigger'));
  const session = _allSessions.find(s => s && s.session_id === sid);
  if (!anchor || !session) return false;
  _showProjectPicker(session, anchor);
  return !!document.querySelector('.project-picker:not(.batch-project-picker)');
}"""

# The picker that is already open: can every row be tapped where it now is, and
# does it still end at its anchor's right edge?
OPEN_PICKER_JS = """({sid, mobile}) => {
  const picker = document.querySelector('.project-picker:not(.batch-project-picker)');
  if (!picker) return {problem: 'the picker is no longer open'};
  const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const anchor = row && (mobile ? row : row.querySelector('.session-actions-trigger'));
  const unreachable = [];
  const items = Array.from(picker.querySelectorAll('.project-picker-item'));
  for (const item of items) {
    item.scrollIntoView({block: 'nearest'});
    const rect = item.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    if (!hit || !(hit === item || item.contains(hit))) unreachable.push(item.textContent.trim());
  }
  const box = picker.getBoundingClientRect();
  return {
    rows: items.length, unreachable,
    top: Math.round(box.top), bottom: Math.round(box.bottom), viewport: innerHeight,
    left: Math.round(box.left), right: Math.round(box.right),
    anchorRight: anchor ? Math.round(anchor.getBoundingClientRect().right) : null,
    anchorOnScreen: !!anchor && (r => r.left >= 0 && r.top >= 0 && r.bottom <= innerHeight)(anchor.getBoundingClientRect()),
  };
}"""

# (what, viewport, touch, which conversation on screen, the viewport afterwards, repaint first)
RESIZES = (
    ("a window made shorter, opened at the bottom", (1440, 900), False, "last", (1440, 420), False),
    ("a window made shorter, opened in the middle", (1440, 900), False, "middle", (1440, 420), False),
    # From the first conversation, which is still on screen afterwards to line up with.
    ("a window made shorter, opened at the top", (1440, 900), False, "first", (1440, 420), False),
    ("a window made shorter after a sidebar repaint", (1440, 900), False, "first", (1440, 420), True),
    ("a tablet turned on its side, opened in the middle", (820, 1180), True, "middle", (1180, 820), False),
    # The drawer stays open, as when a keyboard takes part of the screen: the
    # sidebar is not hidden, so the picker must not close.
    ("a phone made shorter with its drawer open", (390, 844), True, "middle", (390, 560), False),
)
# A resize can hide the sidebar the picker was opened from, and then the picker
# closes. Turning a phone on its side crosses the width at which the drawer
# closes and the sidebar collapses; turning a large phone upright, or narrowing a
# window to phone width, turns an open sidebar into a drawer that is closed.
# (what, viewport, touch, which conversation, the viewport afterwards, how the sidebar is hidden)
RESIZES_THAT_HIDE_THE_SIDEBAR = (
    ("a phone turned on its side, opened at the bottom", (390, 844), True, "last", (844, 390), "collapsed"),
    ("a phone turned on its side, opened in the middle", (390, 844), True, "middle", (844, 390), "collapsed"),
    ("a large phone turned upright, opened in the middle", (932, 430), True, "middle", (430, 932), "closed drawer"),
    ("a large phone turned upright, opened at the bottom", (915, 412), True, "last", (412, 915), "closed drawer"),
    ("a window narrowed to phone width, opened in the middle", (1000, 800), False, "middle", (600, 800), "closed drawer"),
    # 641-900px collapses the sidebar by default. Opened from the ⋮ trigger, which
    # took focus back for the 200ms the sidebar needed to turn invisible.
    ("a window narrowed until the sidebar collapses", (1000, 800), False, "middle", (800, 800), "collapsed"),
)

AFTER_HIDING_JS = """() => {
  const active = document.activeElement;
  const sidebar = document.querySelector('.sidebar');
  return {
    pickers: document.querySelectorAll('.project-picker').length,
    collapsed: document.querySelector('.layout').classList.contains('sidebar-collapsed'),
    sidebarVisibility: sidebar ? getComputedStyle(sidebar).visibility : null,
    drawerOpen: !!sidebar && sidebar.classList.contains('mobile-open'),
    sidebarRight: sidebar ? Math.round(sidebar.getBoundingClientRect().right) : null,
    focus: active ? active.tagName + (active.className ? '.' + String(active.className).split(' ')[0] : '') : null,
    focusInPicker: !!(active && active.closest && active.closest('.project-picker')),
    focusInSidebar: !!(active && active.closest && active.closest('.sidebar')),
  };
}"""


def _check_resize_hides_the_sidebar(browser):
    failures = []
    for what, before, touch, which, after, hidden_as in RESIZES_THAT_HIDE_THE_SIDEBAR:
        phone = before[0] <= 640
        ctx, page, errors = _new_page(
            browser, viewport={"width": before[0], "height": before[1]}, has_touch=touch, is_mobile=touch
        )
        if page is None:
            failures.append(f"  [resize] {what}: {errors}")
            continue
        if phone:
            _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        visible = page.evaluate(VISIBLE_ROWS_JS)
        if len(visible) < 3:
            failures.append(f"  [resize] {what}: only {len(visible)} conversations are on screen")
            ctx.close()
            continue
        index = {"middle": len(visible) // 2, "last": -1}[which]
        # A long press on the row with a finger, the row's ⋮ trigger with a mouse.
        if not page.evaluate(OPEN_AND_LEAVE_JS, {"sid": visible[index], "mobile": touch}):
            failures.append(f"  [resize] {what}: the picker did not open")
            ctx.close()
            continue
        page.set_viewport_size({"width": after[0], "height": after[1]})
        # Soon after: the picker is decided on the next frame, while the sidebar
        # is still sliding or fading out.
        page.wait_for_timeout(150)
        soon = page.evaluate(AFTER_HIDING_JS)
        if soon["pickers"]:
            failures.append(f"  [resize] {what}: the picker is still open 150ms after the resize")
        if soon["focusInSidebar"]:
            failures.append(
                f"  [resize] {what}: focus was handed to {soon['focus']}, inside the sidebar that is being hidden"
            )
        page.wait_for_timeout(450)
        state = page.evaluate(AFTER_HIDING_JS)
        if hidden_as == "collapsed":
            if not state["collapsed"] or state["sidebarVisibility"] != "hidden":
                failures.append(f"  [resize] {what}: the sidebar was expected to collapse, and did not ({state})")
        elif state["collapsed"] or state["drawerOpen"] or state["sidebarRight"] > 0:
            failures.append(f"  [resize] {what}: the sidebar was expected to be a closed drawer, and is not ({state})")
        if state["pickers"]:
            failures.append(f"  [resize] {what}: the picker is still open over a hidden sidebar")
        if state["focusInPicker"] or state["focusInSidebar"]:
            failures.append(f"  [resize] {what}: focus is on {state['focus']}, which is out of sight")
        failures.extend(f"  [resize] {what}: pageerror: {err}" for err in errors)
        ctx.close()
    return failures


def _check_resize(browser):
    """An open picker follows a resize or a rotation, as the ⋮ menu does: every row
    can still be tapped, also when the sidebar was repainted in between and the
    row it was opened from is a new element."""
    failures = []
    for what, before, touch, which, after, repaint in RESIZES:
        ctx, page, errors = _new_page(
            browser, viewport={"width": before[0], "height": before[1]}, has_touch=touch, is_mobile=touch
        )
        if page is None:
            failures.append(f"  [resize] {what}: {errors}")
            continue
        if touch:
            _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        page.evaluate(FIRST_CONVERSATION_TO_TOP_JS)
        page.wait_for_timeout(200)
        visible = page.evaluate(VISIBLE_ROWS_JS)
        if len(visible) < 3:
            failures.append(f"  [resize] {what}: only {len(visible)} conversations are on screen")
            ctx.close()
            continue
        index = {"first": 0, "middle": len(visible) // 2, "last": -1}[which]
        target = {"sid": visible[index], "mobile": touch}
        if not page.evaluate(OPEN_AND_LEAVE_JS, target):
            failures.append(f"  [resize] {what}: the picker did not open")
            ctx.close()
            continue
        if repaint:
            page.evaluate("renderSessionListFromCache()")
        page.set_viewport_size({"width": after[0], "height": after[1]})
        page.wait_for_timeout(400)
        state = page.evaluate(OPEN_PICKER_JS, target)
        if state.get("problem"):
            failures.append(f"  [resize] {what}: {state['problem']}")
        elif which == "first" and not state["anchorOnScreen"]:
            failures.append(f"  [resize] {what}: the first conversation is no longer on screen to line up with")
        else:
            if state["top"] < 0 or state["bottom"] > state["viewport"] or state["unreachable"]:
                failures.append(
                    f"  [resize] {what}: the picker spans y={state['top']}..{state['bottom']} of"
                    f" {state['viewport']} and {len(state['unreachable'])} of {state['rows']} rows cannot be tapped"
                )
            # Its right edge on its anchor's, or its left edge on the margin when it is wider than
            # that leaves room for. Turning a phone closes its drawer and takes the row off the
            # screen: nothing to line up with then.
            if state["anchorOnScreen"]:
                wanted = max(8, state["anchorRight"] - (state["right"] - state["left"]))
                if abs(state["left"] - wanted) > 2:
                    failures.append(
                        f"  [resize] {what}: the picker starts at x={state['left']}, not x={wanted},"
                        f" where its anchor (right edge x={state['anchorRight']}) puts it"
                    )
        failures.extend(f"  [resize] {what}: pageerror: {err}" for err in errors)
        ctx.close()
    return failures


# Opens the batch picker for two conversations and measures it in one step: a
# background sidebar refresh rebuilds the selection bar at any time.
BATCH_ON_TOUCH_JS = """({alpha, beta}) => {
  if (!_sessionSelectMode) toggleSessionSelectMode();
  setSessionSelected(alpha, true);
  setSessionSelected(beta, true);
  const button = Array.from(document.querySelectorAll('#batchActionBar .batch-action-btn'))
    .find(btn => btn.textContent.trim() === t('session_batch_move'));
  if (!button) return {problem: 'the selection bar has no Move button'};
  _showBatchProjectPicker(button);
  const picker = document.querySelector('.batch-project-picker');
  if (!picker) return {problem: 'the batch picker did not open'};
  const unreachable = [];
  const spilled = [];
  const items = Array.from(picker.querySelectorAll('.project-picker-item'));
  for (const item of items) {
    item.scrollIntoView({block: 'nearest'});
    const rect = item.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    if (!hit || !(hit === item || item.contains(hit))) unreachable.push(item.textContent.trim());
    // A cap that does not clip lets the rows run on over the conversations below.
    const box = picker.getBoundingClientRect();
    if (rect.top < box.top - 0.5 || rect.bottom > box.bottom + 0.5) spilled.push(item.textContent.trim());
  }
  const result = {
    rows: items.length, unreachable, spilled,
    height: Math.round(picker.getBoundingClientRect().height),
    content: picker.scrollHeight,
    scrolls: picker.scrollHeight > picker.clientHeight + 1,
  };
  picker.remove();
  exitSessionSelectMode();
  return result;
}"""
# About five and a half 44px rows: the cap the stylesheet gives the batch picker.
BATCH_TOUCH_CAP_PX = 250


def _check_batch_on_touch(page, size):
    """In the selection bar on a phone, a long project list scrolls inside a cap
    instead of pushing the chosen conversations off screen."""
    visible = page.evaluate(VISIBLE_ROWS_JS)
    if len(visible) < 2:
        return [f"  [phone {size}] fewer than two conversations are on screen for the batch picker"]
    state = page.evaluate(BATCH_ON_TOUCH_JS, {"alpha": visible[0], "beta": visible[1]})
    if state.get("problem"):
        return [f"  [phone {size}] batch picker: {state['problem']}"]
    failures = []
    if state["content"] <= BATCH_TOUCH_CAP_PX:
        failures.append(f"  [phone {size}] the batch picker's {state['rows']} rows fit its cap: the long list was not seeded")
    if state["height"] > BATCH_TOUCH_CAP_PX + 2:
        failures.append(f"  [phone {size}] the batch picker is {state['height']}px tall, over its {BATCH_TOUCH_CAP_PX}px cap")
    if not state["scrolls"]:
        failures.append(f"  [phone {size}] the capped batch picker does not scroll inside itself")
    if state["unreachable"]:
        failures.append(f"  [phone {size}] batch picker rows that cannot be tapped: {state['unreachable']}")
    if state["spilled"]:
        failures.append(f"  [phone {size}] batch picker rows shown outside its box: {state['spilled']}")
    return failures


def _check_resize_keeps_focus_in_view(page):
    """A list that fits a tall window and not a short one: when the window is made
    shorter, the row the keyboard is on is still inside the picker's box."""
    visible = page.evaluate(VISIBLE_ROWS_JS)
    if not visible:
        return ["  [resize, focused row] no conversation is on screen"]
    problem = _open_single_picker(page, visible[0])
    if problem:
        return [f"  [resize, focused row] {problem}"]
    failures = []
    page.keyboard.press("End")
    before = page.evaluate(ROW_IN_BOX_JS)
    if before.get("problem") or before["text"] != "+ New project":
        failures.append(f"  [resize, focused row] End put focus on {before.get('text')!r}")
    elif before["scrolls"]:
        failures.append("  [resize, focused row] the picker already scrolls in the tall window: nothing to cap")
    size = page.viewport_size
    page.set_viewport_size({"width": size["width"], "height": 420})
    page.wait_for_timeout(400)
    after = page.evaluate(ROW_IN_BOX_JS)
    if after.get("problem"):
        failures.append(f"  [resize, focused row] after the resize: {after['problem']}")
    else:
        if not after["scrolls"]:
            failures.append("  [resize, focused row] the picker does not scroll in the short window: nothing was capped")
        if after["text"] != "+ New project" or not after["inside"]:
            failures.append(
                f"  [resize, focused row] after the resize the focused row {after['text']!r} is outside the picker's box"
            )
    page.keyboard.press("Escape")
    page.set_viewport_size(size)
    page.wait_for_timeout(SETTLE_MS)
    return failures


def _check_batch_wheel(page):
    """A mouse wheel over the open batch picker still scrolls the conversation list."""
    visible = page.evaluate(VISIBLE_ROWS_JS)
    if len(visible) < 2:
        return ["  [wheel] fewer than two conversations are on screen"]
    scrollable = page.evaluate(
        "(() => { const list = document.getElementById('sessionList');"
        " return !!list && list.scrollHeight > list.clientHeight + 200; })()"
    )
    if not scrollable:
        return ["  [wheel] the conversation list does not scroll: too few conversations were seeded"]
    problem = _open_batch_picker(page, {"alpha": visible[0], "beta": visible[1]})
    if problem:
        return [f"  [wheel] {problem}"]
    box = page.evaluate(
        f"(() => {{ const r = document.querySelector('{BATCH}').getBoundingClientRect();"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
    )
    before = page.evaluate("document.getElementById('sessionList').scrollTop")
    page.mouse.move(box["x"], box["y"])
    for _tick in range(3):
        page.mouse.wheel(0, 100)
        page.wait_for_timeout(120)
    page.wait_for_timeout(300)
    after = page.evaluate("document.getElementById('sessionList').scrollTop")
    page.evaluate("() => { document.querySelectorAll('.project-picker').forEach(p => p.remove()); exitSessionSelectMode(); }")
    if after - before < 100:
        return [
            f"  [wheel] three wheel ticks over the batch picker moved the conversation list by {after - before}px"
        ]
    return []


def _screenshots(browser, directory, seed):
    """The picker opened from the middle of a long conversation list, with many
    projects and long names, in German; and, on a phone on its side, from a
    parent conversation with its forks open."""
    os.makedirs(directory, exist_ok=True)
    for width, height in ((390, 844), (820, 1180), (844, 390), (1440, 900)):
        mobile = width < 1000
        ctx, page, errors = _new_page(
            browser, viewport={"width": width, "height": height}, has_touch=mobile, is_mobile=mobile
        )
        if page is None:
            print(f"screenshot {width}x{height}: {errors}", file=sys.stderr)
            continue
        page.evaluate("() => { setLocale('de'); if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM(); }")
        if mobile:
            _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(600)
        page.evaluate(FIRST_CONVERSATION_TO_TOP_JS)
        page.wait_for_timeout(200)
        visible = page.evaluate(VISIBLE_ROWS_JS)
        if not visible:
            print(f"screenshot {width}x{height}: no conversation is on screen", file=sys.stderr)
            ctx.close()
            continue
        # On a phone the ⋮ menu opens from a long press on the row, so the row
        # is the anchor there; with a mouse it is the row's ⋮ trigger.
        opened = page.evaluate(
            """({sid, mobile}) => {
              const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
              const anchor = row && (mobile ? row : row.querySelector('.session-actions-trigger'));
              const session = _allSessions.find(s => s && s.session_id === sid);
              if (!anchor || !session) return false;
              _showProjectPicker(session, anchor);
              return !!document.querySelector('.project-picker');
            }""",
            {"sid": visible[len(visible) // 2], "mobile": mobile},
        )
        page.wait_for_timeout(300)
        path = os.path.join(directory, f"picker-{width}x{height}.png")
        page.screenshot(path=path)
        rows = page.evaluate(PICKER_JS, SINGLE) if opened else None
        heights = sorted({row["height"] for row in rows["rows"]}) if rows else None
        print(f"screenshot {path}: picker open={opened}, row heights={heights}")
        if mobile and width > height:
            page.keyboard.press("Escape")
            page.wait_for_timeout(SETTLE_MS)
            where = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": 0})
            state = None if where.get("problem") else page.evaluate(REACHABLE_JS, seed["beta"])
            page.evaluate(
                "(() => { const p = document.querySelector('.project-picker'); if (p) p.scrollTop = 0; })()"
            )
            page.wait_for_timeout(300)
            path = os.path.join(directory, f"picker-{width}x{height}-tall-anchor.png")
            page.screenshot(path=path)
            print(f"screenshot {path}: anchor={where}, picker={state}")
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
    state_dir = tempfile.mkdtemp(prefix="hermes-project-picker-")
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

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
            if page is None:
                print(f"SETUP FAIL: {errors}", file=sys.stderr)
                return 2
            seed = page.evaluate(SEED_JS, {"projects": PROJECTS})
            for name, check in (("single", _check_single), ("batch", _check_batch)):
                found = check(page, seed)
                failures.extend(found)
                if not found:
                    print(f"OK  {name} picker — keyboard, focus return, translated labels")
            found = _check_heights(page, seed, coarse=False)
            failures.extend(found)
            if not found:
                print("OK  mouse — rows keep their compact height")
            found = _check_fork_parent(page, seed)
            failures.extend(found)
            if not found:
                print("OK  fork parent — focus returns to the parent's own trigger")
            # For what follows: a list of conversations longer than any screen,
            # and a parent row made tall by four forks.
            page.evaluate(SEED_MANY_JS, MANY_CONVERSATIONS)
            forks = page.evaluate(MORE_FORKS_JS, seed["beta"])
            if forks.get("problem"):
                failures.append(f"  [tall anchor] {forks['problem']}")
            failures.extend(f"  [desktop] pageerror: {err}" for err in errors)
            ctx.close()

            ctx, page, errors = _new_page(
                browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
            )
            if page is None:
                failures.append(f"  [touch] {errors}")
            else:
                _open_mobile_drawer(page)
                page.evaluate("renderSessionList()")
                page.wait_for_timeout(500)
                found = _check_heights(page, seed, coarse=True)
                failures.extend(found)
                if not found:
                    print(f"OK  touch — every row is at least {MIN_TOUCH_ROW_PX}px tall")
                failures.extend(f"  [touch] pageerror: {err}" for err in errors)
                ctx.close()

            # Still three projects: five rows, which every phone has room for.
            for width, height in PHONE_VIEWPORTS:
                size = f"{width}x{height}"
                ctx, page, errors = _new_page(
                    browser, viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
                )
                if page is None:
                    failures.append(f"  [short list {size}] {errors}")
                    continue
                _open_mobile_drawer(page)
                page.evaluate("renderSessionList()")
                page.wait_for_timeout(SETTLE_MS)
                if width < height:
                    found = _check_phone_geometry(page, size, rows=5)
                    failures.extend(found)
                    if not found:
                        print(f"OK  short list {size} — below its anchor, and above it at the bottom of the list")
                    failures.extend(f"  [short list {size}] pageerror: {err}" for err in errors)
                    ctx.close()
                    continue
                found = _check_tall_anchor(page, seed, size, long_list=False)
                failures.extend(found)
                if not found:
                    print(f"OK  tall anchor, short list {size} — all five rows show without scrolling, at every list position")
                failures.extend(f"  [tall anchor, short list {size}] pageerror: {err}" for err in errors)
                ctx.close()

            # Still three projects, and the long list of conversations.
            found = _check_resize(browser) + _check_resize_hides_the_sidebar(browser)
            failures.extend(found)
            if not found:
                print("OK  resize — an open picker follows a shorter window and a turned tablet, and closes without taking focus when the resize hides its sidebar")

            ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 420})
            if page is None:
                failures.append(f"  [long list] {errors}")
            else:
                # The conversation goes into the last of them, so its current
                # project starts below the picker's fold.
                page.evaluate(
                    """async ({names, sid}) => {
                      const post = (path, body) => fetch(path, {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                      }).then(response => response.json());
                      let last = null;
                      for (const name of names)
                        last = (await post('/api/projects/create', {name, color: '#f5c542'})).project.project_id;
                      await post('/api/session/move', {session_id: sid, project_id: last});
                      await renderSessionList();
                    }""",
                    {"names": LONG_PROJECTS, "sid": seed["alpha"]},
                )
                found = _check_long_list(page, seed)
                failures.extend(found)
                if not found:
                    print("OK  long list — the focused row is inside the picker's box")
                found = _check_batch_long_list(page, seed)
                failures.extend(found)
                if not found:
                    print("OK  batch long list — the focused row is scrolled onto the screen")
                failures.extend(f"  [long list] pageerror: {err}" for err in errors)
                ctx.close()

            # From here on the server holds 15 projects.
            ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
            if page is None:
                failures.append(f"  [wheel] {errors}")
            else:
                page.wait_for_timeout(SETTLE_MS)
                found = _check_batch_wheel(page)
                failures.extend(found)
                if not found:
                    print("OK  wheel — the list scrolls with the pointer over the batch picker")
                found = _check_resize_keeps_focus_in_view(page)
                failures.extend(found)
                if not found:
                    print("OK  resize, focused row — the row the keyboard is on stays inside a picker the resize capped")
                failures.extend(f"  [wheel] pageerror: {err}" for err in errors)
                ctx.close()

            for width, height in PHONE_VIEWPORTS:
                size = f"{width}x{height}"
                ctx, page, errors = _new_page(
                    browser, viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
                )
                if page is None:
                    failures.append(f"  [phone {size}] {errors}")
                    continue
                _open_mobile_drawer(page)
                page.evaluate("renderSessionList()")
                page.wait_for_timeout(SETTLE_MS)
                found = _check_phone_geometry(page, size)
                page.wait_for_timeout(SETTLE_MS)
                found += _check_batch_on_touch(page, size)
                if width > height:
                    page.wait_for_timeout(SETTLE_MS)
                    tall = _check_tall_anchor(page, seed, size, long_list=True)
                    found += tall
                    if not tall:
                        print(f"OK  tall anchor, long list {size} — every row can be tapped from an expanded parent, at every list position")
                failures.extend(found)
                if not found:
                    print(f"OK  phone {size} — every row can be tapped: single picker from the top, middle and bottom, batch picker capped")
                failures.extend(f"  [phone {size}] pageerror: {err}" for err in errors)
                ctx.close()

            if shots:
                _screenshots(browser, shots, seed)
            browser.close()

        if failures:
            print("\nPROJECT PICKER KEYBOARD FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nPROJECT PICKER KEYBOARD PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
