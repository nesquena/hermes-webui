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
    their compact height;
  - the same for the conversation's ⋮ menu, which opens the picker: 44px rows
    under a coarse pointer, compact ones with a mouse; and on a phone on its
    side, where ten such rows do not fit, the menu stays on the screen and
    scrolls to its last row, and End, Home and the arrow keys bring the row
    they move focus to into the menu's box; the same in a 300px-tall desktop
    window, which the compact rows do not fit either;
  - opened by a real right click upright, End, then the screen turned on its
    side: the row that has focus is brought back into the menu's box;
  - opened by a real touch held on the row and lifted beside the menu (CDP
    touch events, not a helper): focus stays in the menu, and End goes to its
    last row and not to the conversation list;
  - opened by a real touch held on a row in the middle of the screen, where
    the menu opens over the finger (390x844 and 844x390), and lifted: no click
    reaches a menu row, no request is sent, the menu stays open with focus in
    it, and a tap on a row afterwards works; the same on a fork's row, whose
    next tap must open the fork;
  - opened by a real right click at 1440x900, then keys only: Tab from the
    first row goes to the second, End to the last, and three Tabs from there
    leave the menu and reach the page instead of being brought back to it;
    the menu stays open as it did, and Escape closes it; focus the script
    blurs after the first Tab is still brought back;
  - opened by a pen (the browser's pen pointer: pointer events and a click, no
    touch events) held on a row where the menu opens over it, 390x844, and on
    a fork at 844x390, and lifted: the menu stays open with focus in it, no
    menu row is clicked, no request is sent, and the next pen tap runs the
    menu row or opens the fork; and a pen pressed on a fork, dragged off it at
    once and lifted on another row (390x844) neither clicks nor opens the fork;
    and a pen pressed on a conversation or a fork, moved off its row in one
    step and lifted before the long press is due, gets no menu afterwards;
  - a finger and a pen held on a conversation and on a fork until the menu
    opens (844x390), then dragged right, left or up and lifted: no request is
    sent (a swipe to the right archives, one to the left asks to delete), no
    row is drawn swiped or dragged, no dialog opens, and the menu stays open
    on the row that was pressed;
  - with one conversation's picker open, another row's menu opened by a long
    press (390x844) or a right click (1440x900): the picker closes, and
    Escape leaves neither open.
  the focus cue, on the dark and on the light theme
  - on the light theme the focused row's ring is the skin's --accent-text,
    solid, at least 3:1 against the picker (the translucent --focus-ring was
    1.4:1), also on "+ New project", which is drawn at full opacity while it
    has keyboard focus so that its ring is not dimmed with it; a row under the
    pointer has the theme's --hover-bg wash (the white one cannot be seen
    there); on the dark theme the ring and the wash are what they were; on
    both, "+ New project" keeps its accent tint under the pointer.
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
# Longer than the app's SESSION_LONG_PRESS_DELAY_MS (400).
LONG_PRESS_MS = 700
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

    # The same after a background sidebar repaint replaced the trigger the
    # picker opened from.
    problem = _open_single_picker(page, alpha)
    if problem:
        return failures + [f"  [single] reopen: {problem}"]
    page.evaluate("_repaintSidebarForBackgroundChurn()")
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


# The conversation's ⋮ menu, as data: its rows' heights, where it sits, and
# whether its last row can be brought into its box.
# On a phone the ⋮ menu opens from a long press on the row, so the row is the
# anchor there; with a mouse it is the row's ⋮ trigger.
OPEN_MENU_JS = """({sid, fromRow}) => {
  const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const anchor = row && (fromRow ? row : row.querySelector('.session-actions-trigger'));
  const session = _allSessions.find(s => s && s.session_id === sid);
  if (!anchor || !session) return 'the conversation has no row or no ⋮ trigger';
  if (row.getBoundingClientRect().left < 0) return 'the conversation row is off screen: is the drawer closed?';
  _openSessionActionMenu(session, anchor);
  return document.querySelector('.session-action-menu') ? null : 'the ⋮ menu did not open';
}"""

MENU_ROWS_JS = """() => {
  const menu = document.querySelector('.session-action-menu');
  if (!menu) return {problem: 'the ⋮ menu is gone'};
  const rows = Array.from(menu.querySelectorAll('.session-action-opt'));
  const box = menu.getBoundingClientRect();
  menu.scrollTop = menu.scrollHeight;
  const last = rows[rows.length - 1].getBoundingClientRect();
  const state = {
    heights: rows.map(opt => Math.round(opt.getBoundingClientRect().height)),
    top: Math.round(box.top), bottom: Math.round(box.bottom), viewport: window.innerHeight,
    lastRowInBox: last.top >= box.top - 1 && last.bottom <= box.bottom + 1,
  };
  closeSessionActionMenu();
  return state;
}"""


def _check_menu_rows(page, seed, *, coarse, label=None):
    """The ⋮ menu that opens the picker: finger-sized rows on a touch screen,
    compact ones with a mouse, on the screen, its last row reachable."""
    label = label or ("touch" if coarse else "mouse")
    if page.evaluate("matchMedia('(pointer:coarse)').matches") != coarse:
        return [f"  [⋮ menu, {label}] the context's pointer is not {'coarse' if coarse else 'fine'}"]
    problem = page.evaluate(OPEN_MENU_JS, {"sid": seed["beta"], "fromRow": coarse})
    if problem:
        return [f"  [⋮ menu, {label}] {problem}"]
    # The menu scales in over 450ms; a rectangle read before that is too small.
    page.wait_for_timeout(SETTLE_MS)
    state = page.evaluate(MENU_ROWS_JS)
    if state.get("problem"):
        return [f"  [⋮ menu, {label}] {state['problem']}"]
    failures = []
    heights = state["heights"]
    if coarse and min(heights) < MIN_TOUCH_ROW_PX:
        failures.append(f"  [⋮ menu, {label}] rows are {heights}px tall, under {MIN_TOUCH_ROW_PX}px")
    if not coarse and max(heights) >= MIN_TOUCH_ROW_PX:
        failures.append(f"  [⋮ menu, {label}] rows grew to {heights}px with a fine pointer")
    if state["top"] < 0 or state["bottom"] > state["viewport"]:
        failures.append(
            f"  [⋮ menu, {label}] the menu spans {state['top']}..{state['bottom']}px"
            f" of a {state['viewport']}px screen"
        )
    if not state["lastRowInBox"]:
        failures.append(f"  [⋮ menu, {label}] the last row cannot be scrolled into the menu")
    return failures


# Where the focused row of the open ⋮ menu is, against the menu's own box.
MENU_FOCUS_JS = """() => {
  const menu = document.querySelector('.session-action-menu');
  const row = document.activeElement;
  if (!menu || !row || !menu.contains(row)) return {problem: 'focus is not in the ⋮ menu'};
  const box = menu.getBoundingClientRect(), rect = row.getBoundingClientRect();
  const rows = Array.from(menu.querySelectorAll('.session-action-opt:not([disabled])'));
  return {
    text: row.textContent.trim(),
    first: rows[0].textContent.trim(), last: rows[rows.length - 1].textContent.trim(),
    top: Math.round(rect.top), bottom: Math.round(rect.bottom),
    boxTop: Math.round(box.top), boxBottom: Math.round(box.bottom),
    scrolls: menu.scrollHeight > menu.clientHeight + 1,
  };
}"""


def _check_menu_keyboard_reveal(page, seed, label, *, from_row=True):
    """A menu too tall for the screen scrolls inside itself; the row a key moves
    focus to must be brought into its box, since the focus itself does not
    scroll. Returns failure lines."""
    problem = page.evaluate(OPEN_MENU_JS, {"sid": seed["beta"], "fromRow": from_row})
    if problem:
        return [f"  [⋮ menu keys, {label}] {problem}"]
    page.wait_for_timeout(SETTLE_MS)
    failures = []
    scrolls = None
    # End, Home, then ArrowUp from the first row, which wraps to the last, and
    # ArrowDown from the last, which wraps to the first.
    for key, lands_on in (("End", "last"), ("Home", "first"), ("ArrowUp", "last"), ("ArrowDown", "first")):
        page.keyboard.press(key)
        page.wait_for_timeout(150)
        state = page.evaluate(MENU_FOCUS_JS)
        if state.get("problem"):
            failures.append(f"  [⋮ menu keys, {label}] after {key}: {state['problem']}")
            break
        scrolls = state["scrolls"]
        if state["text"] != state[lands_on]:
            failures.append(
                f"  [⋮ menu keys, {label}] {key} put focus on '{state['text']}',"
                f" not on the {lands_on} row '{state[lands_on]}'"
            )
        if state["top"] < state["boxTop"] - 1 or state["bottom"] > state["boxBottom"] + 1:
            failures.append(
                f"  [⋮ menu keys, {label}] {key} put focus on '{state['text']}' at"
                f" y={state['top']}..{state['bottom']}, outside the menu's {state['boxTop']}..{state['boxBottom']}"
            )
    if scrolls is False:
        # Guard: on these screens the menu is taller than its box, or the
        # checks above would pass without anything being revealed.
        failures.append(f"  [⋮ menu keys, {label}] the menu does not scroll here, so nothing was tested")
    page.evaluate("() => closeSessionActionMenu()")
    return failures


# Where the conversation list is scrolled to, and the box of the open ⋮ menu.
LIST_AND_MENU_JS = """() => {
  const list = document.querySelector('.session-list, #sessionList');
  const menu = document.querySelector('.session-action-menu');
  const box = menu ? menu.getBoundingClientRect() : null;
  return {
    listScroll: list ? Math.round(list.scrollTop) : null,
    menu: box ? {left: box.left, right: box.right, top: box.top, bottom: box.bottom} : null,
  };
}"""

# The left end of a conversation's row: a point the ⋮ menu, which hangs from
# the row's right end, does not cover once it is open.
ROW_LEFT_END_JS = """(sid) => {
  const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
  if (!row) return null;
  const rect = row.getBoundingClientRect();
  return {x: rect.left + 6, y: rect.top + rect.height / 2};
}"""


def _focus_outside_menu_box(state):
    return state["top"] < state["boxTop"] - 1 or state["bottom"] > state["boxBottom"] + 1


def _check_menu_resize_reveal(browser, seed):
    """The ⋮ menu opened by a real right click on a phone upright, End, and
    then the screen turns: the menu is capped shorter than its rows and the
    cap leaves it scrolled to the top, with focus on a row below the fold.
    Returns failure lines."""
    label = "⋮ menu, 390x844 turned to 844x390"
    ctx, page, errors = _new_page(
        browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    try:
        _open_mobile_drawer(page)
        page.click(f'.session-item[data-sid="{seed["beta"]}"] .session-title', button="right")
        page.wait_for_timeout(SETTLE_MS)
        page.keyboard.press("End")
        page.wait_for_timeout(150)
        before = page.evaluate(MENU_FOCUS_JS)
        if before.get("problem"):
            return [f"  [{label}] after a right click and End: {before['problem']}"]
        if before["text"] != before["last"]:
            failures.append(f"  [{label}] End put focus on '{before['text']}', not on the last row '{before['last']}'")
        page.set_viewport_size({"width": 844, "height": 390})
        page.wait_for_timeout(SETTLE_MS)
        after = page.evaluate(MENU_FOCUS_JS)
        if after.get("problem"):
            return [f"  [{label}] after the turn: {after['problem']}"]
        if after["text"] != before["text"]:
            failures.append(f"  [{label}] focus moved from '{before['text']}' to '{after['text']}'")
        if _focus_outside_menu_box(after):
            failures.append(
                f"  [{label}] '{after['text']}' has focus at y={after['top']}..{after['bottom']},"
                f" outside the menu's {after['boxTop']}..{after['boxBottom']}"
            )
        if not after["scrolls"]:
            # Guard: turned, the menu must be taller than its box, or the row
            # would be in view without anything having revealed it.
            failures.append(f"  [{label}] the menu does not scroll after the turn, so nothing was tested")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


def _check_touch_release_keeps_menu_focus(browser, seed):
    """A real touch held on a conversation until the ⋮ menu opens, then
    lifted where the menu is not: the browser's own mousedown for that touch
    lands on the row, which takes no focus. Focus must stay in the menu, so
    that End reaches its last row and not the list behind. Returns failure
    lines."""
    label = "⋮ menu, long press and release at 844x390"
    ctx, page, errors = _new_page(
        browser, viewport={"width": 844, "height": 390}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    try:
        _open_mobile_drawer(page)
        point = page.evaluate(ROW_LEFT_END_JS, seed["beta"])
        if not point:
            return [f"  [{label}] the conversation has no row"]
        touch = ctx.new_cdp_session(page)
        touch.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": point["x"], "y": point["y"], "id": 1}]},
        )
        page.wait_for_timeout(LONG_PRESS_MS)
        held = page.evaluate(LIST_AND_MENU_JS)
        touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(SETTLE_MS)
        if not held["menu"]:
            return [f"  [{label}] the long press did not open the ⋮ menu"]
        box = held["menu"]
        if box["left"] <= point["x"] <= box["right"] and box["top"] <= point["y"] <= box["bottom"]:
            # Guard: lifted over the menu, the touch would press one of its rows.
            return [f"  [{label}] the finger is over the menu, so the release was not tested"]
        released = page.evaluate(MENU_FOCUS_JS)
        if released.get("problem"):
            return [f"  [{label}] after the finger lifts: {released['problem']}"]
        page.keyboard.press("End")
        page.wait_for_timeout(150)
        end = page.evaluate(MENU_FOCUS_JS)
        if end.get("problem"):
            return [f"  [{label}] after End: {end['problem']}"]
        last = page.evaluate(
            "() => { const rows = document.querySelectorAll('.session-action-menu .session-action-opt:not([disabled])');"
            " return rows[rows.length - 1].textContent.trim(); }"
        )
        if end["text"] != last:
            failures.append(f"  [{label}] End put focus on '{end['text']}', not on the last row '{last}'")
        if _focus_outside_menu_box(end):
            failures.append(
                f"  [{label}] after End '{end['text']}' is at y={end['top']}..{end['bottom']},"
                f" outside the menu's {end['boxTop']}..{end['boxBottom']}"
            )
        if not end["scrolls"]:
            failures.append(f"  [{label}] the menu does not scroll here, so End revealed nothing")
        scrolled = page.evaluate(LIST_AND_MENU_JS)["listScroll"]
        if scrolled != held["listScroll"]:
            failures.append(
                f"  [{label}] End scrolled the conversation list from {held['listScroll']} to {scrolled}"
            )
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


# The centre of the title of the conversation row nearest the middle of the
# screen: where a menu too tall to fit above or below its row ends up over the
# finger that opened it.
MIDDLE_ROW_JS = """() => {
  const rows = Array.from(document.querySelectorAll('.session-item')).filter(row => {
    const box = row.getBoundingClientRect();
    return box.top > 60 && box.bottom < innerHeight - 20 && row.querySelector('.session-title');
  });
  if (!rows.length) return null;
  const middle = innerHeight / 2;
  rows.sort((a, b) => Math.abs(a.getBoundingClientRect().top - middle) - Math.abs(b.getBoundingClientRect().top - middle));
  const title = rows[0].querySelector('.session-title').getBoundingClientRect();
  // Three quarters along the title, still on it: master's rows give the title
  // less room, and from its centre a drag left is too short to tell from a
  // swipe and the ⋮ menu (aligned to the row's right) opens beside the finger.
  return {x: title.left + title.width * 0.75, y: title.top + title.height / 2};
}"""

# Every click from here on, by what it landed on.
RECORD_CLICKS_JS = """() => {
  window.__clicks = [];
  document.addEventListener('click', e => {
    const row = e.target.closest('.session-action-opt');
    const fork = e.target.closest('.session-child-session-main');
    window.__clicks.push(row ? 'menu row: ' + row.textContent.trim() : fork ? 'fork: ' + fork.textContent.trim()
      : e.target.tagName.toLowerCase() + '.' + String(e.target.className).split(' ')[0]);
  }, true);
}"""

# A fork's row inside its expanded parent, and whether the one-shot guard that
# swallows the click after a long press is still armed on it.
FORK_ROW_JS = """(sid) => {
  const parent = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const row = parent && parent.querySelector('.session-child-session');
  const main = row && row.querySelector('.session-child-session-main');
  if (!main) return null;
  const box = main.getBoundingClientRect();
  return {x: box.left + box.width / 2, y: box.top + box.height / 2, armed: !!row._skipNextChildOpen,
          sid: row.dataset.sid || null, active: main.classList.contains('active')};
}"""


def _touch(session, kind, point=None):
    points = [{"x": point["x"], "y": point["y"], "id": 1}] if point else []
    session.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})


def _pen(session, kind, point):
    """A stylus, which the browser reports as a pointer of type "pen": pointer
    events and a click, no touch events."""
    session.send(
        "Input.dispatchMouseEvent",
        {"type": kind, "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1,
         "buttons": 0 if kind == "mouseReleased" else 1, "pointerType": "pen"},
    )


# The boot's fire-and-forget update check (static/boot.js, once per tab session)
# is a POST that can land after recording starts. No gesture sends it.
BACKGROUND_WRITES = ("updates/check",)


def _record_writes(page):
    """Every request from here on that is not a GET, as "METHOD path", other
    than the boot's background update check."""
    writes = []

    def record(request):
        if "/api/" not in request.url or request.method == "GET":
            return
        path = request.url.split("/api/")[-1]
        if path.split("?")[0] in BACKGROUND_WRITES:
            return
        writes.append(f"{request.method} {path}")

    page.on("request", record)
    return writes


def _over(box, point):
    return bool(box) and box["left"] <= point["x"] <= box["right"] and box["top"] <= point["y"] <= box["bottom"]


def _check_long_press_runs_nothing(browser, seed, size):
    """A real touch held on a conversation until the ⋮ menu opens over the
    finger, then lifted. The browser's own click for that touch would land on
    the menu row under it and run it (copy the link, pin, duplicate). Nothing
    may run, the menu must stay open with focus in it, and a deliberate tap on
    a menu row afterwards must still work. Returns failure lines."""
    width, height = size
    label = f"⋮ menu, long press and release over the menu at {width}x{height}"
    ctx, page, errors = _new_page(
        browser, viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    writes = _record_writes(page)
    try:
        _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        point = page.evaluate(MIDDLE_ROW_JS)
        if not point:
            return [f"  [{label}] no conversation row in the middle of the screen"]
        page.evaluate(RECORD_CLICKS_JS)
        touch = ctx.new_cdp_session(page)
        _touch(touch, "touchStart", point)
        page.wait_for_timeout(LONG_PRESS_MS)
        held = page.evaluate(LIST_AND_MENU_JS)
        _touch(touch, "touchEnd")
        page.wait_for_timeout(SETTLE_MS)
        if not held["menu"]:
            return [f"  [{label}] the long press did not open the ⋮ menu"]
        if not _over(held["menu"], point):
            # Guard: beside the menu the click lands on the row, which is the
            # other check's case.
            return [f"  [{label}] the menu did not open over the finger, so nothing was tested"]
        clicks = page.evaluate("() => window.__clicks")
        if clicks:
            failures.append(f"  [{label}] lifting the finger clicked {clicks}")
        if writes:
            failures.append(f"  [{label}] lifting the finger sent {writes}")
        released = page.evaluate(MENU_FOCUS_JS)
        if released.get("problem"):
            failures.append(f"  [{label}] after the finger lifts: {released['problem']}")
            return failures
        # A tap on purpose still reaches the row it lands on.
        first = page.evaluate(
            "() => { const row = document.querySelector('.session-action-menu .session-action-opt:not([disabled])');"
            " const box = row.getBoundingClientRect();"
            " return {x: box.left + box.width / 2, y: box.top + box.height / 2, text: row.textContent.trim()}; }"
        )
        _touch(touch, "touchStart", first)
        page.wait_for_timeout(60)
        _touch(touch, "touchEnd")
        page.wait_for_timeout(SETTLE_MS)
        clicks = page.evaluate("() => window.__clicks")
        if clicks != [f"menu row: {first['text']}"]:
            failures.append(f"  [{label}] a tap on '{first['text']}' afterwards clicked {clicks}")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


def _check_fork_long_press_runs_nothing(browser, seed):
    """The same on a fork's row inside its expanded parent, which has gesture
    handlers of its own: lifting the finger opens nothing, and the guard that
    used to swallow that click is not left armed, so the next tap on the fork
    opens it. Returns failure lines."""
    label = "⋮ menu, long press and release on a fork at 844x390"
    # On its side, where the menu has no room but over the row.
    ctx, page, errors = _new_page(
        browser, viewport={"width": 844, "height": 390}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    writes = _record_writes(page)
    try:
        _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        placed = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": 60})
        if placed.get("problem"):
            return [f"  [{label}] {placed['problem']}"]
        page.wait_for_timeout(SETTLE_MS)
        fork = page.evaluate(FORK_ROW_JS, seed["beta"])
        if not fork:
            return [f"  [{label}] the parent has no fork row"]
        page.evaluate(RECORD_CLICKS_JS)
        touch = ctx.new_cdp_session(page)
        _touch(touch, "touchStart", fork)
        page.wait_for_timeout(LONG_PRESS_MS)
        held = page.evaluate(LIST_AND_MENU_JS)
        _touch(touch, "touchEnd")
        page.wait_for_timeout(SETTLE_MS)
        if not held["menu"]:
            return [f"  [{label}] the long press did not open the ⋮ menu"]
        if not _over(held["menu"], fork):
            return [f"  [{label}] the menu did not open over the finger, so nothing was tested"]
        clicks = page.evaluate("() => window.__clicks")
        if clicks:
            failures.append(f"  [{label}] lifting the finger clicked {clicks}")
        if writes:
            failures.append(f"  [{label}] lifting the finger sent {writes}")
        if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            failures.append(f"  [{label}] the menu closed when the finger lifted")
        elif page.evaluate(MENU_FOCUS_JS).get("problem"):
            failures.append(f"  [{label}] after the finger lifts, focus is not in the ⋮ menu")
        after = page.evaluate(FORK_ROW_JS, seed["beta"])
        if after and after["armed"]:
            failures.append(f"  [{label}] the fork row still swallows its next click")
        # Close the menu, then tap the fork on purpose: it must open.
        page.evaluate("() => closeSessionActionMenu()")
        page.wait_for_timeout(200)
        fork = page.evaluate(FORK_ROW_JS, seed["beta"])
        if not fork:
            return failures + [f"  [{label}] the fork row is gone after the menu closed"]
        if fork["active"]:
            return failures + [f"  [{label}] the fork is already the open conversation, so the tap proves nothing"]
        _touch(touch, "touchStart", fork)
        page.wait_for_timeout(60)
        _touch(touch, "touchEnd")
        _wait_until(
            page,
            "() => { const main = document.querySelector('.session-item[data-sid=\"%s\"] .session-child-session-main');"
            " return !main || main.classList.contains('active'); }" % seed["beta"],
        )
        opened = page.evaluate(FORK_ROW_JS, seed["beta"])
        if opened and not opened["active"]:
            failures.append(f"  [{label}] a tap on the fork afterwards did not open it")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


# A row's own ⋮ button: its centre, whether a tap there reaches it, and the
# clicks since COUNT_CLICKS_JS: all of them, and those that landed on a ⋮ button.
TRIGGER_JS = """({sid, fork}) => {
  const item = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const row = fork ? item && item.querySelector('.session-child-session') : item;
  const button = row && row.querySelector('.session-actions-trigger');
  if (!button) return null;
  const box = button.getBoundingClientRect();
  const x = box.left + box.width / 2, y = box.top + box.height / 2;
  const hit = document.elementFromPoint(x, y);
  const counted = window.__counted || {all: 0, onButton: 0};
  return {x, y, reachable: !!hit && button.contains(hit), clicks: counted.all, onButton: counted.onButton,
          menu: !!document.querySelector('.session-action-menu')};
}"""

COUNT_CLICKS_JS = """() => {
  if (window.__counted) { window.__counted.all = 0; window.__counted.onButton = 0; return; }
  window.__counted = {all: 0, onButton: 0};
  document.addEventListener('click', e => {
    window.__counted.all += 1;
    if (e.target.closest && e.target.closest('.session-actions-trigger')) window.__counted.onButton += 1;
  }, true);
}"""


def _check_button_tap_after_long_press(browser, seed, *, fork):
    """A touch that opened the ⋮ menu by a long press cancels its own click.
    Once that menu is dismissed, a touch on the row's ⋮ button begins no
    gesture on the row, so nothing may be left over from the long press to
    cancel it too: the tap must open the menu.

    The button only exists where the mouse is the main pointer (the touch
    layout hides it), so this is a touch laptop: no touch emulation, real
    touch events, and the mouse resting on the row, which is what shows a
    conversation's button and holds a fork's still under the tap. Returns
    failure lines."""
    what = "a fork" if fork else "a conversation"
    label = f"⋮ button, a tap after a long press on {what}, touch laptop 1280x800"
    ctx, page, errors = _new_page(browser, viewport={"width": 1280, "height": 800})
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    try:
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        if fork:
            placed = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": 300})
            if placed.get("problem"):
                return [f"  [{label}] {placed['problem']}"]
            page.wait_for_timeout(SETTLE_MS)
            point = page.evaluate(FORK_ROW_JS, seed["beta"])
            sid = seed["beta"]
        else:
            point = page.evaluate(MIDDLE_ROW_JS)
            sid = point and page.evaluate(
                "(p) => document.elementFromPoint(p.x, p.y).closest('.session-item').dataset.sid", point
            )
        if not point:
            return [f"  [{label}] no row to press"]
        touch = ctx.new_cdp_session(page)
        _touch(touch, "touchStart", point)
        page.wait_for_timeout(LONG_PRESS_MS)
        _touch(touch, "touchEnd")
        page.wait_for_timeout(SETTLE_MS)
        if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            return [f"  [{label}] the long press did not open the ⋮ menu"]
        page.keyboard.press("Escape")
        page.wait_for_timeout(SETTLE_MS)
        if page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            return [f"  [{label}] Escape did not dismiss the ⋮ menu"]
        page.mouse.move(point["x"], point["y"])
        page.wait_for_timeout(300)
        button = page.evaluate(TRIGGER_JS, {"sid": sid, "fork": fork})
        if not button or not button["reachable"]:
            return [f"  [{label}] the ⋮ button cannot be tapped, so nothing was tested"]
        page.evaluate(COUNT_CLICKS_JS)
        _touch(touch, "touchStart", button)
        page.wait_for_timeout(60)
        _touch(touch, "touchEnd")
        page.wait_for_timeout(SETTLE_MS)
        counted = page.evaluate("() => window.__counted")
        if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            failures.append(
                f"  [{label}] a tap on the ⋮ button did not open the menu"
                f" ({counted['all']} clicks followed it, {counted['onButton']} on the button)"
            )
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


# What has focus, and whether that is inside the open ⋮ menu.
FOCUS_AND_MENU_JS = """() => {
  const el = document.activeElement;
  const menu = document.querySelector('.session-action-menu');
  const rows = menu ? Array.from(menu.querySelectorAll('.session-action-opt:not([disabled])')) : [];
  return {
    menuOpen: !!menu, inMenu: !!(menu && el && menu.contains(el)),
    onNothing: !el || el === document.body || el === document.documentElement,
    text: el ? el.textContent.trim().slice(0, 40) : '', index: rows.indexOf(el), rows: rows.length,
  };
}"""


def _check_tab_leaves_the_menu(browser, seed):
    """Tab is the keyboard's way out of the ⋮ menu. Focus that falls out of
    the open menu onto nothing is brought back to it (a finger lifting after a
    long press does that), and Tab from the last row leaves focus on nothing
    too, on its way out of the page: that one must be let go. Opened by a real
    right click, then keys only; the script never sets focus, and takes it
    away once, to see that it is still brought back. Returns failure lines."""
    label = "⋮ menu, right click and Tab at 1440x900"
    ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    try:
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        point = page.evaluate(MIDDLE_ROW_JS)
        if not point:
            return [f"  [{label}] no conversation row in the middle of the screen"]
        page.mouse.click(point["x"], point["y"], button="right")
        page.wait_for_timeout(SETTLE_MS)
        opened = page.evaluate(FOCUS_AND_MENU_JS)
        if not opened["menuOpen"] or opened["index"] != 0:
            return [f"  [{label}] the right click did not open the ⋮ menu with focus on its first row"]
        # Between rows Tab is the browser's own, as it was.
        page.keyboard.press("Tab")
        page.wait_for_timeout(150)
        second = page.evaluate(FOCUS_AND_MENU_JS)
        if second["index"] != 1:
            failures.append(f"  [{label}] Tab from the first row put focus on '{second['text']}', not on the second row")
        # That Tab is over: focus dropped on nothing now (by the script, the one
        # place it touches focus) is still brought back to the row it left.
        page.evaluate("() => document.activeElement.blur()")
        page.wait_for_timeout(150)
        back = page.evaluate(FOCUS_AND_MENU_JS)
        if back["index"] != 1:
            failures.append(
                f"  [{label}] after a Tab between rows, focus dropped on nothing was not brought back"
                f" to the second row (it is on '{back['text']}')"
            )
        page.keyboard.press("End")
        page.wait_for_timeout(150)
        last = page.evaluate(FOCUS_AND_MENU_JS)
        if last["index"] != last["rows"] - 1:
            return failures + [f"  [{label}] End put focus on '{last['text']}', not on the last row"]
        # Out of the page first, then into it again from its start.
        after = []
        for _press in range(3):
            page.keyboard.press("Tab")
            page.wait_for_timeout(150)
            after.append(page.evaluate(FOCUS_AND_MENU_JS))
        for count, state in enumerate(after, start=1):
            if state["inMenu"]:
                failures.append(
                    f"  [{label}] Tab {count} from the last row left focus in the menu, on '{state['text']}'"
                )
        if after[-1]["onNothing"]:
            failures.append(f"  [{label}] three Tabs from the last row reached no control outside the menu")
        if not after[-1]["menuOpen"]:
            failures.append(f"  [{label}] Tab closed the ⋮ menu, which it did not do before")
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        if page.evaluate(FOCUS_AND_MENU_JS)["menuOpen"]:
            failures.append(f"  [{label}] Escape, with focus outside the menu, did not close it")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


def _check_pen_long_press_keeps_the_menu(browser, seed, *, fork):
    """A stylus held on a row until the ⋮ menu opens over it, then lifted. A
    pen sends no touch events, and its release and its click go to what is
    under it by then: the menu. The row never saw the press end, and the
    click, which landed on neither the row nor a menu row, counted as a click
    outside the menu and closed it. The menu must stay open, nothing may run,
    and the next tap must do what it is for: on a menu row, run it; on the
    fork, open it. One spot on one screen, with the browser's pen pointer; no
    real stylus. Returns failure lines."""
    width, height = (844, 390) if fork else (390, 844)
    label = f"⋮ menu, pen held and lifted over the menu on {'a fork' if fork else 'a conversation'} at {width}x{height}"
    ctx, page, errors = _new_page(
        browser, viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    writes = _record_writes(page)
    try:
        _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        if fork:
            placed = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": 60})
            if placed.get("problem"):
                return [f"  [{label}] {placed['problem']}"]
            page.wait_for_timeout(SETTLE_MS)
            point = page.evaluate(FORK_ROW_JS, seed["beta"])
        else:
            point = page.evaluate(MIDDLE_ROW_JS)
        if not point:
            return [f"  [{label}] no row to press"]
        if fork and point["active"]:
            return [f"  [{label}] the fork is already the open conversation, so the tap would prove nothing"]
        page.evaluate(RECORD_CLICKS_JS)
        pen = ctx.new_cdp_session(page)
        _pen(pen, "mousePressed", point)
        page.wait_for_timeout(LONG_PRESS_MS)
        held = page.evaluate(LIST_AND_MENU_JS)
        _pen(pen, "mouseReleased", point)
        page.wait_for_timeout(SETTLE_MS)
        if not held["menu"]:
            return [f"  [{label}] the long press did not open the ⋮ menu"]
        if not _over(held["menu"], point):
            return [f"  [{label}] the menu did not open over the pen, so nothing was tested"]
        clicks = page.evaluate("() => window.__clicks")
        on_menu = [click for click in clicks if click.startswith("menu row")]
        if on_menu:
            failures.append(f"  [{label}] lifting the pen clicked {on_menu}")
        if writes:
            failures.append(f"  [{label}] lifting the pen sent {writes}")
        if not page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            return failures + [f"  [{label}] the menu closed when the pen lifted (its click landed on {clicks})"]
        if page.evaluate(MENU_FOCUS_JS).get("problem"):
            failures.append(f"  [{label}] after the pen lifts, focus is not in the ⋮ menu")
        if not fork:
            first = page.evaluate(
                "() => { const row = document.querySelector('.session-action-menu .session-action-opt:not([disabled])');"
                " const box = row.getBoundingClientRect();"
                " return {x: box.left + box.width / 2, y: box.top + box.height / 2, text: row.textContent.trim()}; }"
            )
            page.evaluate("() => { window.__clicks.length = 0; }")
            _pen(pen, "mousePressed", first)
            page.wait_for_timeout(60)
            _pen(pen, "mouseReleased", first)
            page.wait_for_timeout(SETTLE_MS)
            clicks = page.evaluate("() => window.__clicks")
            if clicks != [f"menu row: {first['text']}"]:
                failures.append(f"  [{label}] a pen tap on '{first['text']}' afterwards clicked {clicks}")
        else:
            if page.evaluate(FORK_ROW_JS, seed["beta"])["armed"]:
                failures.append(f"  [{label}] the fork row still swallows its next click")
            page.evaluate("() => closeSessionActionMenu()")
            page.wait_for_timeout(200)
            again = page.evaluate(FORK_ROW_JS, seed["beta"])
            if not again:
                return failures + [f"  [{label}] the fork row is gone after the menu closed"]
            _pen(pen, "mousePressed", again)
            page.wait_for_timeout(60)
            _pen(pen, "mouseReleased", again)
            _wait_until(
                page,
                "() => { const main = document.querySelector('.session-item[data-sid=\"%s\"] .session-child-session-main');"
                " return !main || main.classList.contains('active'); }" % seed["beta"],
            )
            opened = page.evaluate(FORK_ROW_JS, seed["beta"])
            if opened and not opened["active"]:
                failures.append(f"  [{label}] a pen tap on the fork afterwards did not open it")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


# What is under a point: the fork's own button, or something else.
ON_FORK_BUTTON_JS = """({sid, x, y}) => {
  const parent = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const main = parent && parent.querySelector('.session-child-session .session-child-session-main');
  const hit = document.elementFromPoint(x, y);
  return !!(main && hit && main.contains(hit));
}"""


def _check_pen_drag_off_a_fork_opens_nothing(browser, seed):
    """A pen pressed on a fork, dragged off it at once and lifted somewhere
    else. That is no click on the fork, and it must not open it: the pen is
    kept on the row it pressed only once its long press has opened the menu,
    not from the press on. Returns failure lines."""
    label = "pen pressed on a fork, dragged off and lifted at 390x844"
    ctx, page, errors = _new_page(
        browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    try:
        _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        placed = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": 300})
        if placed.get("problem"):
            return [f"  [{label}] {placed['problem']}"]
        page.wait_for_timeout(SETTLE_MS)
        fork = page.evaluate(FORK_ROW_JS, seed["beta"])
        if not fork:
            return [f"  [{label}] the parent has no fork row"]
        if fork["active"]:
            return [f"  [{label}] the fork is already the open conversation, so nothing can be told"]
        # Straight up, past the parent's own row and onto the conversation above.
        away = {"x": fork["x"], "y": fork["y"] - 160}
        if page.evaluate(ON_FORK_BUTTON_JS, {"sid": seed["beta"], **away}):
            return [f"  [{label}] the pen would lift on the fork's button, so nothing was tested"]
        page.evaluate(RECORD_CLICKS_JS)
        pen = ctx.new_cdp_session(page)
        _pen(pen, "mousePressed", fork)
        for step in range(1, 9):
            _pen(pen, "mouseMoved", {"x": fork["x"], "y": fork["y"] - 20 * step})
            page.wait_for_timeout(20)
        _pen(pen, "mouseReleased", away)
        page.wait_for_timeout(SETTLE_MS)
        clicks = [click for click in page.evaluate("() => window.__clicks") if click.startswith("fork")]
        if clicks:
            failures.append(f"  [{label}] lifting the pen away from the fork clicked {clicks}")
        after = page.evaluate(FORK_ROW_JS, seed["beta"])
        if after and after["active"]:
            failures.append(f"  [{label}] the fork was opened")
        if page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            failures.append(f"  [{label}] the ⋮ menu opened")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


def _check_pen_lifted_off_a_row_opens_no_menu(browser, seed, *, fork):
    """A pen pressed on a row, moved off it in one step and lifted there, all
    before the long press is due. The row saw no move and no release, only the
    pen leaving: the press is over, and no ⋮ menu may open for it afterwards.
    Returns failure lines."""
    label = f"pen pressed on {'a fork' if fork else 'a conversation'}, lifted off the row before the long press, 390x844"
    ctx, page, errors = _new_page(
        browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    try:
        _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        if fork:
            placed = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": 300})
            if placed.get("problem"):
                return [f"  [{label}] {placed['problem']}"]
            page.wait_for_timeout(SETTLE_MS)
            point = page.evaluate(FORK_ROW_JS, seed["beta"])
        else:
            point = page.evaluate(MIDDLE_ROW_JS)
        if not point:
            return [f"  [{label}] no row to press"]
        row_of = (
            "(p) => { const hit = document.elementFromPoint(p.x, p.y);"
            " const row = hit && hit.closest('.session-child-session, .session-item');"
            " return row ? (row.dataset.sid || '') + '|' + row.className.split(' ')[0] : null; }"
        )
        away = {"x": point["x"], "y": point["y"] - 160}
        pressed, lifted = page.evaluate(row_of, point), page.evaluate(row_of, away)
        if not pressed or pressed == lifted:
            return [f"  [{label}] the pen would lift on the row it pressed, so nothing was tested"]
        opened_before = page.evaluate("() => S.session && S.session.session_id")
        pen = ctx.new_cdp_session(page)
        _pen(pen, "mousePressed", point)
        _pen(pen, "mouseMoved", away)
        _pen(pen, "mouseReleased", away)
        page.wait_for_timeout(LONG_PRESS_MS + SETTLE_MS)
        if page.evaluate("() => !!document.querySelector('.session-action-menu')"):
            failures.append(f"  [{label}] the ⋮ menu opened after the pen had lifted")
        if page.evaluate("() => S.session && S.session.session_id") != opened_before:
            failures.append(f"  [{label}] a conversation was opened")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


# After a long press: is the ⋮ menu open and whose is it, is any row drawn
# part-way through a swipe or a drag, and is the confirm dialog up.
AFTER_LONG_PRESS_JS = """() => {
  const rows = Array.from(document.querySelectorAll('.session-item, .session-child-session'));
  const offset = row => row.style.getPropertyValue('--session-swipe-offset');
  const dialog = document.getElementById('appDialogOverlay');
  const shown = !!dialog && getComputedStyle(dialog).display !== 'none';
  return {
    menu: !!document.querySelector('.session-action-menu'),
    menuFor: typeof _sessionActionSessionId === 'undefined' ? null : _sessionActionSessionId,
    moved: rows.filter(row => row.classList.contains('dragging') || row.classList.contains('swipe-committed')
      || (offset(row) && parseFloat(offset(row)) !== 0))
      .map(row => (row.classList.contains('session-child-session') ? 'fork' : 'conversation') + ' ' + (row.dataset.sid || '').slice(0, 8)),
    dialog: shown ? dialog.textContent.trim().replace(/\\s+/g, ' ').slice(0, 80) : '',
    opened: (S.session && S.session.session_id) || null,
  };
}"""

DRAGS_AFTER_LONG_PRESS = ("right", "left", "up")


def _check_drag_after_long_press_does_nothing(browser, seed, *, fork, pointer):
    """Once a long press has opened the ⋮ menu, the rest of that gesture does
    nothing. The finger or the pen is held until the menu opens, dragged, and
    lifted: to the right (a swipe that far archives the conversation), to the
    left (it asks to delete it) and up. No request may be sent, no row may be
    drawn swiped or dragged, during the drag or after it, no dialog may open,
    no conversation may be opened, and the menu must still be open on the row
    that was pressed. Each drag has a page of its own, and requests other than
    GET are refused, so that a failure cannot archive the fixtures the later
    checks use. Returns failure lines."""
    failures = []
    for direction in DRAGS_AFTER_LONG_PRESS:
        failures += _drag_after_long_press(browser, seed, fork=fork, pointer=pointer, direction=direction)
    return failures


def _drag_after_long_press(browser, seed, *, fork, pointer, direction):
    what = "a fork" if fork else "a conversation"
    label = f"long press on {what}, then a drag {direction}, {pointer}, 844x390"
    ctx, page, errors = _new_page(
        browser, viewport={"width": 844, "height": 390}, has_touch=True, is_mobile=True
    )
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    writes = []

    def refuse_writes(route, request):
        if request.method == "GET":
            route.continue_()
            return
        writes.append(f"{request.method} {request.url.split('/api/')[-1]}")
        route.abort()

    try:
        _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        if fork:
            placed = page.evaluate(TALL_ANCHOR_AT_JS, {"sid": seed["beta"], "offset": 60})
            if placed.get("problem"):
                return [f"  [{label}] {placed['problem']}"]
            page.wait_for_timeout(SETTLE_MS)
            start = page.evaluate(FORK_ROW_JS, seed["beta"])
        else:
            start = page.evaluate(MIDDLE_ROW_JS)
        if not start:
            return [f"  [{label}] no row to press"]
        pressed = page.evaluate(
            "(p) => { const row = document.elementFromPoint(p.x, p.y).closest('.session-child-session, .session-item');"
            " return row ? row.dataset.sid : null; }",
            start,
        )
        if not pressed or (fork and pressed != start["sid"]):
            return [f"  [{label}] the press would not land on the row, so nothing was tested"]
        dx, dy = {"right": (464, 0), "left": (4 - start["x"], 0), "up": (0, -120)}[direction]
        if dy == 0 and abs(dx) < 136:
            return [f"  [{label}] only {abs(dx):.0f}px to drag, too near a swipe's 128px, so nothing was tested"]
        page.route("**/api/**", refuse_writes)
        before = page.evaluate(AFTER_LONG_PRESS_JS)
        session = ctx.new_cdp_session(page)
        if pointer == "touch":
            _touch(session, "touchStart", start)
        else:
            _pen(session, "mousePressed", start)
        page.wait_for_timeout(LONG_PRESS_MS)
        held = page.evaluate(AFTER_LONG_PRESS_JS)
        end = start
        if held["menu"]:
            for step in range(1, 11):
                end = {"x": start["x"] + dx * step / 10, "y": start["y"] + dy * step / 10}
                if pointer == "touch":
                    _touch(session, "touchMove", end)
                else:
                    _pen(session, "mouseMoved", end)
                page.wait_for_timeout(25)
        dragging = page.evaluate(AFTER_LONG_PRESS_JS)
        if pointer == "touch":
            _touch(session, "touchEnd")
        else:
            _pen(session, "mouseReleased", end)
        page.wait_for_timeout(SETTLE_MS + 400)
        if not held["menu"] or held["menuFor"] != pressed:
            return [f"  [{label}] the long press did not open the pressed row's ⋮ menu"]
        after = page.evaluate(AFTER_LONG_PRESS_JS)
        if writes:
            failures.append(f"  [{label}] sent {sorted(set(writes))}")
        if dragging["moved"] or after["moved"]:
            failures.append(
                f"  [{label}] a row is drawn swiped or dragged"
                f" (during the drag: {dragging['moved']}, after it: {after['moved']})"
            )
        if after["dialog"]:
            failures.append(f"  [{label}] a dialog opened: {after['dialog']!r}")
        if not after["menu"] or after["menuFor"] != pressed:
            failures.append(f"  [{label}] the ⋮ menu is no longer open on the row that was pressed")
        if after["opened"] != before["opened"]:
            failures.append(f"  [{label}] a conversation was opened")
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


# The conversation rows on screen that a press can reach, each by the centre of
# its title; and how many pickers and ⋮ menus are open, and whose the menu is.
PRESSABLE_ROWS_JS = """() => Array.from(document.querySelectorAll('#sessionList .session-item[data-sid]')).map(row => {
  const title = row.querySelector(':scope > .session-title, :scope .session-title');
  if (!title) return null;
  const box = title.getBoundingClientRect();
  const x = box.left + box.width / 2, y = box.top + box.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (!hit || hit.closest('.session-item') !== row || hit.closest('.project-picker, .session-action-menu')) return null;
  return {sid: row.dataset.sid, x, y};
}).filter(Boolean)"""

PICKERS_AND_MENUS_JS = """() => ({
  pickers: document.querySelectorAll('.project-picker').length,
  menus: document.querySelectorAll('.session-action-menu').length,
  menuFor: typeof _sessionActionSessionId === 'undefined' ? null : _sessionActionSessionId,
})"""

MOVE_ROW_JS = """() => {
  const label = t('session_move_project');
  const row = Array.from(document.querySelectorAll('.session-action-menu .session-action-opt'))
    .find(opt => opt.textContent.trim() === label);
  if (!row) return null;
  const box = row.getBoundingClientRect();
  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
}"""


def _check_a_new_menu_closes_the_picker(browser, seed, *, touch):
    """One conversation's "Move to project" picker is open, and the ⋮ menu of
    another is opened beside it. The picker used to be closed by the click of
    the press that opened the menu; a long press no longer sends that click
    (it would run the menu row under the finger), and a right click never did.
    The picker must go when the menu opens, and Escape must then leave neither.
    With a finger at 390x844: a long press, a tap on "Move to project", a long
    press on another row. With a mouse at 1440x900: the keyboard opens the
    picker, a right click the other menu. Returns failure lines."""
    label = "a picker open, then another row's ⋮ menu, " + (
        "by long press at 390x844" if touch else "by right click at 1440x900"
    )
    if touch:
        ctx, page, errors = _new_page(
            browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
        )
    else:
        ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
    if page is None:
        return [f"  [{label}] {errors}"]
    failures = []
    try:
        finger = ctx.new_cdp_session(page)
        if touch:
            _open_mobile_drawer(page)
        page.evaluate("renderSessionList()")
        page.wait_for_timeout(SETTLE_MS)
        if touch:
            first = page.evaluate(MIDDLE_ROW_JS)
            if not first:
                return [f"  [{label}] no conversation row in the middle of the screen"]
            first_sid = page.evaluate(
                "(p) => document.elementFromPoint(p.x, p.y).closest('.session-item').dataset.sid", first
            )
            _touch(finger, "touchStart", first)
            page.wait_for_timeout(LONG_PRESS_MS)
            _touch(finger, "touchEnd")
            page.wait_for_timeout(SETTLE_MS)
            move = page.evaluate(MOVE_ROW_JS)
            if not move:
                return [f"  [{label}] the long press opened no menu with a 'Move to project' row"]
            _touch(finger, "touchStart", move)
            page.wait_for_timeout(60)
            _touch(finger, "touchEnd")
            page.wait_for_timeout(SETTLE_MS)
        else:
            first_sid = seed["alpha"]
            problem = _open_single_picker(page, first_sid)
            if problem:
                return [f"  [{label}] {problem}"]
            page.wait_for_timeout(SETTLE_MS)
        before = page.evaluate(PICKERS_AND_MENUS_JS)
        if before != {"pickers": 1, "menus": 0, "menuFor": None}:
            return [f"  [{label}] the picker is not open on its own: {before}"]
        others = [row for row in page.evaluate(PRESSABLE_ROWS_JS) if row["sid"] != first_sid]
        if not others:
            return [f"  [{label}] no other conversation row can be pressed beside the picker"]
        other = others[-1]
        if touch:
            _touch(finger, "touchStart", other)
            page.wait_for_timeout(LONG_PRESS_MS)
            _touch(finger, "touchEnd")
        else:
            page.mouse.click(other["x"], other["y"], button="right")
        page.wait_for_timeout(SETTLE_MS)
        after = page.evaluate(PICKERS_AND_MENUS_JS)
        if after["menus"] != 1 or after["menuFor"] != other["sid"]:
            return [f"  [{label}] the other row's ⋮ menu did not open: {after}"]
        if after["pickers"]:
            failures.append(f"  [{label}] the first conversation's picker is still open under the new menu")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        closed = page.evaluate(PICKERS_AND_MENUS_JS)
        if closed["menus"] or closed["pickers"]:
            failures.append(
                f"  [{label}] after Escape {closed['menus']} menu and {closed['pickers']} picker are still open"
            )
        failures.extend(f"  [{label}] pageerror: {err}" for err in errors)
    finally:
        ctx.close()
    return failures


# The focused row of the open single picker, as its ring and wash are painted,
# and the two custom properties a ring can come from, resolved to colours.
FOCUS_CUE_JS = """() => {
  const row = document.activeElement;
  if (!row || !row.classList.contains('project-picker-item')) return {problem: 'focus is not on a picker row'};
  const resolved = name => {
    const probe = document.createElement('span');
    probe.style.color = 'var(' + name + ')';
    document.body.appendChild(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return colour;
  };
  const parse = c => { const m = c.match(/[\\d.]+/g).map(Number); return {r: m[0], g: m[1], b: m[2], a: m.length > 3 ? m[3] : 1}; };
  const over = (top, under) => ({
    r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a),
    b: top.b * top.a + under.b * (1 - top.a), a: 1,
  });
  const channel = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const luminance = c => 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
  const style = getComputedStyle(row);
  // What the ring is seen against: the picker's background over whatever is
  // behind it. The batch picker's own background is a 3% tint, not a colour.
  const chain = [];
  for (let node = row.closest('.project-picker'); node; node = node.parentElement) chain.unshift(node);
  let picker = {r: 255, g: 255, b: 255, a: 1};
  for (const node of chain) {
    const colour = parse(getComputedStyle(node).backgroundColor);
    if (colour.a > 0) picker = over(colour, picker);
  }
  // A row's opacity dims its outline with it ("+ New project" is drawn at 70%).
  const opacity = parseFloat(style.opacity);
  const drawn = parse(style.outlineColor);
  drawn.a *= opacity;
  const ring = over(drawn, picker);
  const light = luminance(ring), dark = luminance(picker);
  return {
    dark: document.documentElement.classList.contains('dark'),
    create: row.classList.contains('project-picker-create'), opacity,
    ring: style.outlineColor, ringStyle: style.outlineStyle, ringWidth: style.outlineWidth,
    accent: resolved('--accent'), accentText: resolved('--accent-text'), focusRing: resolved('--focus-ring'),
    contrast: Math.round((Math.max(light, dark) + 0.05) / (Math.min(light, dark) + 0.05) * 100) / 100,
  };
}"""

# The row under the pointer, and the two custom properties its wash can come
# from, resolved to colours.
HOVER_WASH_JS = """() => {
  const resolved = name => {
    const probe = document.createElement('span');
    probe.style.backgroundColor = 'var(' + name + ')';
    document.body.appendChild(probe);
    const colour = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return colour;
  };
  const row = document.querySelector('.project-picker:not(.batch-project-picker) .project-picker-item:hover');
  return {
    wash: row ? getComputedStyle(row).backgroundColor : null,
    image: row ? getComputedStyle(row).backgroundImage : null,
    create: !!row && row.classList.contains('project-picker-create'),
    focused: !!row && row === document.activeElement,
    hoverBg: resolved('--hover-bg'), accentBg: resolved('--accent-bg'), sidebar: resolved('--sidebar'),
  };
}"""

# WCAG 2.2 SC 1.4.11: a focus indicator needs 3:1 against what is next to it.
MIN_RING_CONTRAST = 3.0
# The light skins whose accent is palest: with --accent as the ring they are
# 2.69:1, 2.69:1 and 2.85:1 against the picker.
PALE_ACCENT_SKINS = ("terracotta", "sienna", "neon-paint")


def _light_ring_problems(cue, label):
    """A focused row on a light theme: the ring is the skin's --accent-text and
    reads at 3:1 or more against the picker."""
    if cue.get("problem"):
        return [f"  [focus cue, {label}] {cue['problem']}"]
    problems = []
    if cue["dark"]:
        problems.append(f"  [focus cue, {label}] the page is not on the light theme")
    if cue["ring"] != cue["accentText"]:
        problems.append(
            f"  [focus cue, {label}] the ring is {cue['ring']}, expected --accent-text ({cue['accentText']})"
        )
    if cue["contrast"] < MIN_RING_CONTRAST:
        problems.append(
            f"  [focus cue, {label}] the ring is {cue['contrast']}:1 on the picker, under {MIN_RING_CONTRAST}:1"
        )
    return problems


def _create_row_ring_problems(page, label):
    """End puts focus on "+ New project". The row is drawn at 70% opacity,
    which would dim its ring to under 3:1; with keyboard focus on a light theme
    it is drawn in full."""
    page.keyboard.press("End")
    page.wait_for_timeout(SETTLE_MS)
    cue = page.evaluate(FOCUS_CUE_JS)
    if cue.get("problem"):
        return [f"  [focus cue, {label}, + New project] {cue['problem']}"]
    if not cue["create"]:
        return [f"  [focus cue, {label}, + New project] End did not put focus on '+ New project'"]
    problems = _light_ring_problems(cue, f"{label}, + New project")
    if cue["opacity"] != 1:
        problems.append(
            f"  [focus cue, {label}, + New project] the focused row is drawn at opacity {cue['opacity']}"
        )
    # Back to a project row, so that what follows starts where it did before.
    page.keyboard.press("Home")
    page.wait_for_timeout(150)
    return problems


def _check_focus_cue(page, seed):
    """On a light theme the focused row's ring is the skin's --accent-text, not
    the translucent --focus-ring, and a hovered row shows a wash; a dark theme
    keeps the ring and the wash it had."""
    failures = []
    try:
        for theme in ("dark", "light"):
            page.evaluate("(name) => _applyTheme(name)", theme)
            page.wait_for_timeout(200)
            problem = _open_single_picker(page, seed["alpha"])
            if problem:
                failures.append(f"  [focus cue, {theme}] {problem}")
                continue
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(150)
            cue = page.evaluate(FOCUS_CUE_JS)
            if cue.get("problem"):
                failures.append(f"  [focus cue, {theme}] {cue['problem']}")
            else:
                if cue["dark"] != (theme == "dark"):
                    failures.append(f"  [focus cue, {theme}] the page is not on the {theme} theme")
                if (cue["ringStyle"], cue["ringWidth"]) != ("solid", "2px"):
                    failures.append(
                        f"  [focus cue, {theme}] the ring is {cue['ringStyle']} {cue['ringWidth']}, expected solid 2px"
                    )
                if theme == "dark" and cue["ring"] != cue["focusRing"]:
                    failures.append(
                        f"  [focus cue, dark] the ring is {cue['ring']}, expected --focus-ring ({cue['focusRing']})"
                    )
                if theme == "dark":
                    # "+ New project" is left as master draws it on dark: since the sticky
                    # row (27ad682b9) that is full opacity, dimmed by a 70% colour mix instead.
                    page.keyboard.press("End")
                    page.wait_for_timeout(SETTLE_MS)
                    create = page.evaluate(FOCUS_CUE_JS)
                    if not create.get("create") or create.get("opacity") != 1:
                        failures.append(
                            f"  [focus cue, dark, + New project] focused row: create={create.get('create')},"
                            f" opacity {create.get('opacity')}, expected master's 1"
                        )
                    # Back to a project row: the pointer checks below are for
                    # rows that do not also have keyboard focus.
                    page.keyboard.press("Home")
                    page.wait_for_timeout(150)
                if theme == "light":
                    failures += _light_ring_problems(cue, "light")
                    failures += _create_row_ring_problems(page, "light")
                    if cue["accentText"] == cue["accent"]:
                        failures.append(
                            "  [focus cue, light] --accent-text equals --accent on the default skin:"
                            " the ring's colour is not told apart"
                        )
            # The pointer on another row: the wash under it. On light it is the
            # theme's own --hover-bg, on dark the white wash the rows always had.
            # Home left keyboard focus on the first row, so the pointer goes to
            # the second: a row under the pointer and nothing else.
            page.hover(f"{SINGLE} .project-picker-item >> nth=1")
            page.wait_for_timeout(250)
            hovered = page.evaluate(HOVER_WASH_JS)
            wanted = "rgba(255, 255, 255, 0.08)" if theme == "dark" else hovered["hoverBg"]
            if hovered["focused"]:
                failures.append(
                    f"  [focus cue, {theme}] the hovered row also has keyboard focus: the wash is not told from the focus"
                )
            if hovered["create"] or hovered["wash"] != wanted:
                failures.append(
                    f"  [focus cue, {theme}] a hovered row's background is {hovered['wash']}, expected {wanted}"
                )
            if theme == "light" and hovered["hoverBg"] != "rgba(0, 0, 0, 0.05)":
                failures.append(f"  [focus cue, light] --hover-bg is {hovered['hoverBg']} on the default skin")
            # "+ New project" keeps its own accent tint under the pointer, on
            # both themes: the rows' wash must not take it over. The row is sticky
            # over a scrolling list, so master (27ad682b9) paints the tint as a
            # gradient over an opaque --sidebar: rows under it must not show through.
            page.hover(f"{SINGLE} .project-picker-create")
            page.wait_for_timeout(250)
            hovered = page.evaluate(HOVER_WASH_JS)
            if (
                not hovered["create"]
                or hovered["accentBg"] not in (hovered["image"] or "")
                or hovered["wash"] != hovered["sidebar"]
            ):
                failures.append(
                    f"  [focus cue, {theme}] '+ New project' under the pointer is {hovered['image']} over"
                    f" {hovered['wash']}, expected --accent-bg ({hovered['accentBg']}) over an opaque --sidebar"
                    f" ({hovered['sidebar']})"
                )
            page.mouse.move(700, 450)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        # A skin with a hover wash of its own (Codex: 4% black, not 5%): the
        # rows follow the skin, they do not carry a colour of their own.
        page.evaluate("() => { _applySkin('codex'); _applyTheme('light'); }")
        page.wait_for_timeout(250)
        problem = _open_single_picker(page, seed["alpha"])
        if problem:
            failures.append(f"  [focus cue, codex light] {problem}")
        else:
            page.hover(f"{SINGLE} .project-picker-item >> nth=0")
            page.wait_for_timeout(250)
            hovered = page.evaluate(HOVER_WASH_JS)
            if hovered["hoverBg"] == "rgba(0, 0, 0, 0.05)":
                failures.append("  [focus cue, codex light] the skin's --hover-bg is the default one: nothing is tested")
            if hovered["wash"] != hovered["hoverBg"]:
                failures.append(
                    f"  [focus cue, codex light] a hovered row's background is {hovered['wash']},"
                    f" expected the skin's --hover-bg ({hovered['hoverBg']})"
                )
            page.mouse.move(700, 450)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        # The skins with the palest accents, where the accent itself is under
        # 3:1 as a ring.
        for skin in PALE_ACCENT_SKINS:
            page.evaluate("(name) => { _applySkin(name); _applyTheme('light'); }", skin)
            page.wait_for_timeout(250)
            problem = _open_single_picker(page, seed["alpha"])
            if problem:
                failures.append(f"  [focus cue, {skin} light] {problem}")
                continue
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(150)
            failures += _light_ring_problems(page.evaluate(FOCUS_CUE_JS), f"{skin} light")
            failures += _create_row_ring_problems(page, f"{skin} light")
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        # The batch picker sits on the selection bar, a darker ground than the
        # single picker's: with --accent the default skin's ring is 2.64:1 there.
        page.evaluate("() => { _applySkin('default'); _applyTheme('light'); }")
        page.wait_for_timeout(250)
        problem = _open_batch_picker(page, seed)
        if problem:
            failures.append(f"  [focus cue, batch light] {problem}")
        else:
            page.wait_for_timeout(150)
            failures += _light_ring_problems(page.evaluate(FOCUS_CUE_JS), "batch light")
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        page.evaluate("() => exitSessionSelectMode()")
        page.wait_for_timeout(150)
    finally:
        page.evaluate("() => { _applySkin('default'); _applyTheme('dark'); }")
        page.wait_for_timeout(200)
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
            page.evaluate("_repaintSidebarForBackgroundChurn()")
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
            page.evaluate("_repaintSidebarForBackgroundChurn()")
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
            found = _check_menu_rows(page, seed, coarse=False)
            failures.extend(found)
            if not found:
                print("OK  mouse — the ⋮ menu's rows keep their compact height")
            found = _check_focus_cue(page, seed)
            failures.extend(found)
            if not found:
                print("OK  focus cue — --accent-text as the ring on the light theme (default and the three palest skins, single and batch picker) and a wash under the pointer, the dark theme as it was")
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
                found = _check_menu_rows(page, seed, coarse=True)
                failures.extend(found)
                if not found:
                    print(f"OK  touch — every row of the ⋮ menu is at least {MIN_TOUCH_ROW_PX}px tall")
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
                # A phone on its side has no room for ten 44px rows: the menu
                # must stay on the screen and scroll to its last one.
                page.evaluate("() => { document.querySelector('.session-list, #sessionList').scrollTop = 0; }")
                page.wait_for_timeout(200)
                found = _check_menu_rows(page, seed, coarse=True, label=f"touch {size}")
                found += _check_menu_keyboard_reveal(page, seed, size)
                failures.extend(found)
                if not found:
                    print(f"OK  ⋮ menu {size} — finger-sized rows, on the screen, the last one reachable, and End, Home and the arrows reveal the row they focus")
                failures.extend(f"  [tall anchor, short list {size}] pageerror: {err}" for err in errors)
                ctx.close()

            for check, done in (
                (_check_menu_resize_reveal, "⋮ menu, right click, End, 390x844 turned to 844x390 — the focused last row is still in the menu's box"),
                (_check_touch_release_keeps_menu_focus, "⋮ menu, real long press and release at 844x390 — focus stays in the menu, End reaches its last row"),
            ):
                found = check(browser, seed)
                failures.extend(found)
                if not found:
                    print(f"OK  {done}")

            # With the long list: a row in the middle of the screen, whose menu
            # has no room above or below it and opens over the finger.
            for size in ((390, 844), (844, 390)):
                found = _check_long_press_runs_nothing(browser, seed, size)
                failures.extend(found)
                if not found:
                    print(f"OK  ⋮ menu, real long press and release over the menu at {size[0]}x{size[1]} — nothing runs, the menu stays open, a tap on a row afterwards works")
            found = _check_fork_long_press_runs_nothing(browser, seed)
            failures.extend(found)
            if not found:
                print("OK  ⋮ menu, real long press and release on a fork at 844x390 — nothing opens, and the next tap on the fork opens it")
            for fork in (False, True):
                found = _check_button_tap_after_long_press(browser, seed, fork=fork)
                failures.extend(found)
                if not found:
                    print(f"OK  ⋮ button, touch laptop — after a long press on {'a fork' if fork else 'a conversation'} and Escape, a tap on its ⋮ button opens the menu")
            found = _check_tab_leaves_the_menu(browser, seed)
            failures.extend(found)
            if not found:
                print("OK  ⋮ menu, right click, End and Tab at 1440x900 — three Tabs from the last row leave the menu and reach the page, the menu stays open, Escape closes it")
            for fork in (False, True):
                found = _check_pen_long_press_keeps_the_menu(browser, seed, fork=fork)
                failures.extend(found)
                if not found:
                    print(f"OK  ⋮ menu, pen held and lifted over the menu on {'a fork at 844x390' if fork else 'a conversation at 390x844'} — the menu stays open, nothing runs, the next pen tap works")
            found = _check_pen_drag_off_a_fork_opens_nothing(browser, seed)
            failures.extend(found)
            if not found:
                print("OK  pen pressed on a fork, dragged off it and lifted at 390x844 — the fork is not clicked and does not open")
            for fork in (False, True):
                found = _check_pen_lifted_off_a_row_opens_no_menu(browser, seed, fork=fork)
                failures.extend(found)
                if not found:
                    print(f"OK  pen pressed on {'a fork' if fork else 'a conversation'} and lifted off its row before the long press at 390x844 — no ⋮ menu opens afterwards")
            for fork in (False, True):
                for pointer in ("touch", "pen"):
                    found = _check_drag_after_long_press_does_nothing(browser, seed, fork=fork, pointer=pointer)
                    failures.extend(found)
                    if not found:
                        print(f"OK  long press on {'a fork' if fork else 'a conversation'}, then a drag right, left and up, {pointer}, 844x390 — nothing is sent, swiped or opened, and the menu stays open")
            for touch in (True, False):
                found = _check_a_new_menu_closes_the_picker(browser, seed, touch=touch)
                failures.extend(found)
                if not found:
                    print(f"OK  picker open, then another row's ⋮ menu by {'long press at 390x844' if touch else 'right click at 1440x900'} — the picker closes, Escape leaves nothing open")

            # Still three projects, and the long list of conversations.
            found = _check_resize(browser) + _check_resize_hides_the_sidebar(browser)
            failures.extend(found)
            if not found:
                print("OK  resize — an open picker follows a shorter window and a turned tablet, and closes without taking focus when the resize hides its sidebar")

            # With a mouse the rows are compact, and ten of them are still
            # taller than a 300px window: the same reveal, on master's own sizes.
            ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 300})
            if page is None:
                failures.append(f"  [⋮ menu keys, short window] {errors}")
            else:
                found = _check_menu_keyboard_reveal(page, seed, "1440x300, mouse", from_row=False)
                failures.extend(found)
                if not found:
                    print("OK  ⋮ menu 1440x300 — End, Home and the arrows reveal the row they focus in a short window")
                failures.extend(f"  [⋮ menu keys, short window] pageerror: {err}" for err in errors)
                ctx.close()

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
