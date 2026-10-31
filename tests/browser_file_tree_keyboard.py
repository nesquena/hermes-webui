#!/usr/bin/env python3
"""
Headless browser gate: the workspace file tree is a real ARIA tree that the
keyboard and a screen reader can use.

WHY THIS EXISTS
  Rows in the Files tab were plain <div>s with no role and no tabindex. A
  screen reader read the tab as "▸ .cache × ▸ .cloak-venv ×" (arrow glyph,
  name, multiplication sign) and the keyboard could not reach a single row;
  the only Tab stops were the invisible "×" delete buttons.

WHAT IT CHECKS (Chromium, plus Firefox when Playwright's build is installed)
  - the container is role=tree with a name; rows are treeitems with
    aria-level; folders carry aria-expanded, files do not; names say
    "x folder" / "y file, 1.5k" (name first, then kind and the size column) /
    "z symlink" (with the target as description);
    arrows and icons are aria-hidden;
  - the tree is one Tab stop: Tab reaches a row, the next Tab leaves the tree,
    and the delete buttons are out of the Tab order with a name that says what
    they delete; the focused row keeps an outline under forced colors; the
    scrolling container is not a Tab stop of its own (Firefox made it one, so
    Shift+Tab landed on an empty "tree"), and a navigation key on the
    focused container enters the tree;
  - Up/Down/Home/End move between rows without wrapping; Right expands a folder
    and then enters it; Left goes to the parent and collapses; focus stays on
    the toggled folder across the re-render; a second Right during a slow
    listing does not collapse it again; an empty folder announces an inert
    placeholder row that keeps focus across a re-render;
  - Enter on a file opens its preview, once; Space on a folder toggles it;
  - a background re-render keeps the tab stop and never steals focus; the
    end-of-turn loadDir('.') (two renders) leaves focus on a nested row, and a
    key pressed between its two renders wins; when a row and both neighbours
    disappear, focus goes to the nearest folder above it;
  - a mouse click on a row moves the tab stop to it;
  - Delete on a row asks for confirmation; cancelling keeps the file and puts
    focus back on the row; confirming removes it and focus moves to the next
    row, not the top of the tree, also for two deletes back to back;
  - keys typed into the inline rename input (Home, End, Enter, Space) stay in
    the input: Enter renames, never opens or toggles a row, and focus ends on
    the renamed row;
  - when the tree empties, focus moves to the Files tab and the container
    drops its tree role and name.

  --screenshots DIR writes the Files tab with keyboard focus on a row at
  1440x900 and 390x844.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state and a temp workspace. It proves page
  behaviour; how a screen reader voices the tree is owned by the browser and
  the screen reader.

USAGE
  python tests/browser_file_tree_keyboard.py
  (FILE_TREE_KEYBOARD_PORT pins the port; by default a free one is picked.)

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
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
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


PORT = int(os.getenv("FILE_TREE_KEYBOARD_PORT") or _free_port())
BASE = f"http://127.0.0.1:{PORT}"
WAIT_MS = 8000


def _wait_for_health(proc, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
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


def _make_workspace(root):
    """Folders sort before files: alpha/, beta/, delta/, epsilon/, then
    filler-00.txt ... filler-39.txt (so the tree scrolls), gamma.txt (2048
    bytes), note.md (1536 bytes), zeta.txt (10 bytes). delta/ holds link.txt,
    an internal symlink to gamma.txt (the app lists symlinks first, so it lives
    in its own folder); epsilon/ holds a.txt, b.txt and c.txt; alpha/ holds
    sub/ and inner.txt."""
    os.makedirs(os.path.join(root, "alpha", "sub"))
    with open(os.path.join(root, "alpha", "inner.txt"), "w") as f:
        f.write("inner\n")
    os.makedirs(os.path.join(root, "beta"))
    # Sizes the size column shows as 2.0k, 1.5k and 0.0k.
    for name, size in (("gamma.txt", 2048), ("note.md", 1536), ("zeta.txt", 10)):
        with open(os.path.join(root, name), "w") as f:
            f.write("x" * size)
    # Enough files that the tree scrolls: Firefox makes a scrollable container
    # a Tab stop of its own unless it opts out. They sort between epsilon/ and
    # gamma.txt, so the first and last rows stay the same.
    for i in range(40):
        with open(os.path.join(root, f"filler-{i:02d}.txt"), "w") as f:
            f.write("x\n")
    os.makedirs(os.path.join(root, "epsilon"))
    for name in ("a.txt", "b.txt", "c.txt"):
        with open(os.path.join(root, "epsilon", name), "w") as f:
            f.write(name + "\n")
    os.makedirs(os.path.join(root, "delta"))
    os.symlink(os.path.join("..", "gamma.txt"), os.path.join(root, "delta", "link.txt"))


ROWS_JS = """() => Array.from(document.querySelectorAll('#fileTree .file-item')).map(row => {
  const del = row.querySelector('.file-del-btn');
  const arrow = row.querySelector('.file-tree-toggle');
  const icon = row.querySelector('.file-icon');
  return {
    path: row.dataset.a11yTreePath || null,
    role: row.getAttribute('role'),
    level: row.getAttribute('aria-level'),
    expanded: row.getAttribute('aria-expanded'),
    disabled: row.getAttribute('aria-disabled'),
    label: row.getAttribute('aria-label'),
    tabindex: row.getAttribute('tabindex'),
    arrowHidden: arrow ? arrow.getAttribute('aria-hidden') : 'none',
    iconHidden: icon ? icon.getAttribute('aria-hidden') : 'none',
    delLabel: del ? del.getAttribute('aria-label') : null,
    delTabindex: del ? del.getAttribute('tabindex') : null,
  };
})"""

FOCUSED_JS = """() => {
  const a = document.activeElement;
  if (!a) return null;
  if (a.getAttribute && a.getAttribute('role') === 'treeitem') return 'row:' + (a.dataset.a11yTreePath || '');
  return (a.tagName || '').toLowerCase() + (a.id ? '#' + a.id : '') + (a.className ? '.' + String(a.className).split(' ')[0] : '');
}"""

# Barriers for /api/list responses: __hold(key) makes every list request whose
# URL contains key wait until __release(key); __releaseFirst(key) and
# __releaseLast(key) let only the oldest or newest waiting one through, and
# __failFirst(key) makes the oldest one fail with a server error;
# __held(key) counts the waiting ones; __unhold() releases everything and stops
# holding. __hideInRoot = name drops that entry from root listings, as if it
# had been deleted. Installed once; api() goes through window.fetch.
HOLD_FETCH_JS = r"""() => {
  if (window.__hold) return;
  const original = window.fetch.bind(window);
  const holds = {};
  window.__hold = (key) => { holds[key] = holds[key] || []; };
  window.__release = (key) => { const waiting = holds[key] || []; delete holds[key]; waiting.forEach(w => w.go()); };
  window.__releaseFirst = (key) => { const w = (holds[key] || []).shift(); if (w) w.go(); };
  window.__releaseLast = (key) => { const w = (holds[key] || []).pop(); if (w) w.go(); };
  window.__failFirst = (key) => { const w = (holds[key] || []).shift(); if (w) w.fail(); };
  window.__hideInRoot = null;
  const hide = (url, response) => {
    const name = window.__hideInRoot;
    if (!name || !/[?&]path=\.(&|$)/.test(url)) return response;
    return response.clone().json().then((data) => {
      data.entries = (data.entries || []).filter((e) => e.name !== name);
      return new Response(JSON.stringify(data), {status: response.status, headers: response.headers});
    });
  };
  window.__held = (key) => (holds[key] || []).length;
  window.__unhold = () => Object.keys(holds).forEach(window.__release);
  window.fetch = (input, init) => {
    const url = String((input && input.url) || input);
    const key = url.includes('/api/list?') && Object.keys(holds).find(k => url.includes(k));
    if (!key) return original(input, init).then((r) => hide(url, r));
    // fail() answers 500, which api() does not retry (a TypeError it would).
    return new Promise((go) => holds[key].push({go: () => go(true), fail: () => go(false)}))
      .then((ok) => ok ? original(input, init)
        : new Response(JSON.stringify({error: 'held request failed'}), {status: 500, headers: {'Content-Type': 'application/json'}}));
  };
}"""

ROW_SEL = "#fileTree [role=treeitem][data-a11y-tree-path=\"{}\"]"

# Count preview opens: openFile is a global function the row handlers call by name.
COUNT_OPENS_JS = """() => {
  if (!window.__treeOpens) {
    window.__treeOpens = [];
    const original = openFile;
    window.openFile = async (path, opts) => { window.__treeOpens.push(path); return original(path, opts); };
  }
  window.__treeOpens.length = 0;
}"""


def _new_page(browser, **context_args):
    ctx = browser.new_context(**context_args)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/", wait_until="domcontentloaded")
    if not _wait_until(page, "typeof S!=='undefined'&&!!S._bootReady", timeout_ms=20000):
        return ctx, None, "app did not boot"
    page.evaluate("async () => { await newSession(); openWorkspacePanel('browse'); }")
    if not _wait_until(page, "document.querySelectorAll('#fileTree [role=treeitem]').length >= 6"):
        return ctx, None, "file tree did not render six treeitems"
    return ctx, page, errors


def _focused(page):
    return page.evaluate(FOCUSED_JS)


def _wait_for_focus(page, want, timeout_ms=WAIT_MS):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if _focused(page) == want:
            return True
        page.wait_for_timeout(50)
    return False


def _row(page, path):
    return page.evaluate("(p) => { const r = document.querySelector('#fileTree [data-a11y-tree-path=\"' + p + '\"]');"
                         " return r ? {expanded: r.getAttribute('aria-expanded'), tabindex: r.getAttribute('tabindex')} : null; }", path)


def _check(page, name, workspace):
    failures = []

    def fail(message):
        failures.append(f"  [{name}] {message}")

    # ── Contract ────────────────────────────────────────────────────────────
    tree = page.evaluate("() => { const t = document.getElementById('fileTree');"
                         " return {role: t.getAttribute('role'), label: t.getAttribute('aria-label')}; }")
    if tree != {"role": "tree", "label": "Workspace files"}:
        fail(f"container contract: {tree}")
    rows = page.evaluate(ROWS_JS)
    by_path = {r["path"]: r for r in rows}
    want = {
        "alpha": ("1", "false", "alpha folder"),
        "beta": ("1", "false", "beta folder"),
        "gamma.txt": ("1", None, "gamma.txt file, 2.0k"),
        "delta": ("1", "false", "delta folder"),
        "epsilon": ("1", "false", "epsilon folder"),
        "note.md": ("1", None, "note.md file, 1.5k"),
        "zeta.txt": ("1", None, "zeta.txt file, 0.0k"),
    }
    for path, (level, expanded, label) in want.items():
        r = by_path.get(path)
        if not r:
            fail(f"no treeitem for {path}: {rows}")
            continue
        got = (r["role"], r["level"], r["expanded"], r["label"])
        if got != ("treeitem", level, expanded, label):
            fail(f"{path}: role/level/expanded/label = {got}")
        if r["iconHidden"] != "true" or r["arrowHidden"] not in ("true", "none"):
            fail(f"{path}: decoration not aria-hidden: {r}")
        if r["delLabel"] != f"Delete {path}" or r["delTabindex"] != "-1":
            fail(f"{path}: delete button name/tabindex = {r['delLabel']!r}/{r['delTabindex']!r}")
    if page.evaluate("() => document.querySelector('#fileTree [data-a11y-tree-path=\"alpha\"]').hasAttribute('aria-description')"):
        fail("a plain folder row got an aria-description")
    page.evaluate("() => document.querySelector('#fileTree [data-a11y-tree-path=\"delta\"]').click()")
    if _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"delta/link.txt\"]')"):
        link = {r["path"]: r for r in page.evaluate(ROWS_JS)}.get("delta/link.txt") or {}
        if link.get("level") != "2" or not str(link.get("label")).startswith("link.txt symlink"):
            fail(f"internal symlink row: {link}")
        desc = page.evaluate("() => document.querySelector('#fileTree [data-a11y-tree-path=\"delta/link.txt\"]')"
                             ".getAttribute('aria-description')")
        if not desc or "gamma.txt" not in desc:
            fail(f"internal symlink row does not describe its target: {desc!r}")
        page.evaluate("() => document.querySelector('#fileTree [data-a11y-tree-path=\"delta\"]').click()")
        _wait_until(page, "() => !document.querySelector('#fileTree [data-a11y-tree-path=\"delta/link.txt\"]')")
    else:
        fail("delta did not expand to show the symlink")
    stops = [r["path"] for r in page.evaluate(ROWS_JS) if r["tabindex"] == "0"]
    if stops != ["alpha"]:
        fail(f"expected one tab stop on alpha, got {stops}")

    # ── One Tab stop ─────────────────────────────────────────────────────────
    page.focus("#workspaceFilesTab")
    reached = None
    for _ in range(8):
        page.keyboard.press("Tab")
        f = _focused(page)
        if f and f.startswith("row:"):
            reached = f
            break
    if reached != "row:alpha":
        fail(f"Tab from the Files tab did not reach the tree's tab stop (last focus {_focused(page)})")
    else:
        # The Files tree is the last Tab stop in the page and headless Firefox
        # does not move focus out of the document, so a sentinel button after
        # it shows where the next Tab goes.
        page.evaluate("() => { const b = document.createElement('button'); b.id = 'treeGateSentinel';"
                      " b.textContent = 'after'; document.body.appendChild(b); }")
        page.keyboard.press("Tab")
        after = _focused(page)
        if after != "button#treeGateSentinel":
            fail(f"the Tab after the tree's tab stop went to {after}, not out of the tree")
        page.evaluate("() => document.getElementById('treeGateSentinel').remove()")
        page.focus(ROW_SEL.format("alpha"))
        page.keyboard.press("Shift+Tab")
        back = _focused(page)
        if not back or back.startswith("row:") or "file-del-btn" in back or "fileTree" in back:
            fail(f"Shift+Tab from the tab stop stayed inside the tree: {back}")
    if not page.evaluate("() => { const t = document.getElementById('fileTree'); return t.scrollHeight > t.clientHeight; }"):
        fail("setup: the tree does not scroll, so the scroller Tab-stop check proves nothing")
    # Focus can sit on the container itself (a click on blank space, a screen
    # reader moving there). It stays put, and a navigation key enters the
    # tree: arrows at the tab stop, Home/End at the ends.
    if page.evaluate("() => document.getElementById('fileTree').getAttribute('tabindex')") != "-1":
        fail("the scrolling tree container is not opted out of the Tab order")
    page.focus(ROW_SEL.format("beta"))
    for key, want in (("ArrowDown", "row:beta"), ("End", "row:zeta.txt"), ("Home", "row:alpha")):
        page.evaluate("() => document.getElementById('fileTree').focus()")
        page.wait_for_timeout(50)
        if _focused(page) != "div#fileTree.file-tree":
            fail(f"focusing the container moved focus to {_focused(page)}")
        page.keyboard.press(key)
        if not _wait_for_focus(page, want, 2000):
            fail(f"{key} on the focused container went to {_focused(page)}, expected {want}")
        if key == "ArrowDown":
            page.focus(ROW_SEL.format("beta"))
    # Enter on the focused container while a tool approval waits enters the
    # tree; it must not count as "no control focused" and approve the action.
    page.focus(ROW_SEL.format("beta"))
    approvals = page.evaluate("""() => {
      const card = document.getElementById('approvalCard');
      if (!card) return null;
      window.__approvals = [];
      window.__realRespondApproval = window.respondApproval;
      window.respondApproval = (choice) => { window.__approvals.push(choice); };
      card.classList.add('visible');
      document.getElementById('fileTree').focus();
      return true;
    }""")
    if approvals is None:
        fail("setup: #approvalCard not found")
    else:
        # Positive control: with nothing focused, the same Enter approves.
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        if page.evaluate("() => window.__approvals.slice()") != ["once"]:
            fail("setup: Enter with nothing focused did not reach the mocked approval")
        page.evaluate("() => { window.__approvals.length = 0; document.getElementById('fileTree').focus(); }")
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        got = page.evaluate("""() => {
          document.getElementById('approvalCard').classList.remove('visible');
          window.respondApproval = window.__realRespondApproval;
          return window.__approvals.slice();
        }""")
        if got:
            fail(f"Enter on the focused tree container answered the pending approval: {got}")
        if _focused(page) != "row:beta":
            fail(f"Enter on the focused container went to {_focused(page)}, expected the tab-stop row")
    # A mouse click on blank tree space (the padding) focuses the container,
    # like a click on any focusable element; the arrows then enter the tree.
    page.focus(ROW_SEL.format("beta"))
    page.focus("#msg")
    # Scrolled to the top, the container's top padding is blank space.
    box = page.evaluate("() => { const t = document.getElementById('fileTree'); t.scrollTop = 0;"
                        " const r = t.getBoundingClientRect(); return [r.left, r.top]; }")
    page.mouse.click(box[0] + 4, box[1] + 3)
    page.wait_for_timeout(100)
    if _focused(page) in ("textarea#msg",) or (_focused(page) or "").startswith("row:"):
        fail(f"a click on blank tree space left focus on {_focused(page)}")
    page.keyboard.press("ArrowDown")
    if not _wait_for_focus(page, "row:beta", 2000):
        fail(f"ArrowDown after a click on blank tree space went to {_focused(page)}, expected row:beta")
    # ... also after a re-render replaced the rows.
    page.focus(ROW_SEL.format("beta"))
    page.focus("#msg")
    page.evaluate("() => { renderFileTree(); document.getElementById('fileTree').focus(); }")
    page.keyboard.press("ArrowUp")
    if not _wait_for_focus(page, "row:beta", 2000):
        fail(f"after a re-render, ArrowUp on the container went to {_focused(page)}, not the tab-stop row")

    # ── Arrow navigation ─────────────────────────────────────────────────────
    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowUp")
    if not _wait_for_focus(page, "row:alpha", 1000):
        fail(f"ArrowUp on the first row moved focus: {_focused(page)}")
    page.keyboard.press("ArrowDown")
    if not _wait_for_focus(page, "row:beta"):
        fail(f"ArrowDown did not move to beta: {_focused(page)}")
    elif _row(page, "beta")["tabindex"] != "0" or _row(page, "alpha")["tabindex"] != "-1":
        fail("roving tab stop did not follow ArrowDown")
    page.keyboard.press("End")
    if not _wait_for_focus(page, "row:zeta.txt"):
        fail(f"End did not reach the last row: {_focused(page)}")
    page.keyboard.press("ArrowDown")
    if not _wait_for_focus(page, "row:zeta.txt", 1000):
        fail(f"ArrowDown on the last row wrapped or moved: {_focused(page)}")
    page.keyboard.press("Home")
    if not _wait_for_focus(page, "row:alpha"):
        fail(f"Home did not reach the first row: {_focused(page)}")

    # ── The focus ring survives forced colors (box-shadow would not) ─────────
    page.emulate_media(forced_colors="active")
    ring = page.evaluate("() => { const s = getComputedStyle(document.activeElement); return [s.outlineStyle, s.outlineWidth]; }")
    page.emulate_media(forced_colors="none")
    if ring[0] != "solid" or ring[1] == "0px":
        fail(f"focused row has no outline under forced colors: {ring}")

    # ── Expand / enter / parent / collapse ───────────────────────────────────
    page.keyboard.press("ArrowRight")
    if not _wait_until(page, "() => { const r = document.querySelector('#fileTree [data-a11y-tree-path=\"alpha\"]');"
                             " return !!r && r.getAttribute('aria-expanded') === 'true'; }"):
        fail("ArrowRight did not expand alpha")
    elif not _wait_for_focus(page, "row:alpha"):
        fail(f"focus left alpha after the expand re-render: {_focused(page)}")
    else:
        kids = {r["path"]: r for r in page.evaluate(ROWS_JS)}
        for child, label in (("alpha/sub", "sub folder"), ("alpha/inner.txt", "inner.txt file, 0.0k")):
            r = kids.get(child)
            if not r or r["level"] != "2" or r["label"] != label:
                fail(f"child row {child}: {r}")
        page.keyboard.press("ArrowRight")
        if not _wait_for_focus(page, "row:alpha/sub"):
            fail(f"ArrowRight on an expanded folder did not enter it: {_focused(page)}")
        page.keyboard.press("ArrowDown")
        if not _wait_for_focus(page, "row:alpha/inner.txt"):
            fail(f"ArrowDown inside alpha: {_focused(page)}")
        page.keyboard.press("ArrowRight")
        if not _wait_for_focus(page, "row:alpha/inner.txt", 1000):
            fail(f"ArrowRight on a file moved focus: {_focused(page)}")
        page.keyboard.press("ArrowLeft")
        if not _wait_for_focus(page, "row:alpha"):
            fail(f"ArrowLeft on a child did not go to its parent: {_focused(page)}")
        page.keyboard.press("ArrowLeft")
        if not _wait_until(page, "() => { const r = document.querySelector('#fileTree [data-a11y-tree-path=\"alpha\"]');"
                                 " return !!r && r.getAttribute('aria-expanded') === 'false'; }"):
            fail("ArrowLeft on an expanded folder did not collapse it")
        elif not _wait_for_focus(page, "row:alpha"):
            fail(f"focus left alpha after the collapse re-render: {_focused(page)}")

    # ── Space toggles a folder; an empty folder has an inert placeholder ────
    page.focus(ROW_SEL.format("beta"))
    page.keyboard.press("Space")
    if not _wait_until(page, "() => !!document.querySelector('#fileTree .file-empty[role=treeitem][aria-disabled=true]')"):
        fail("Space did not expand beta into an inert placeholder treeitem")
    elif not _wait_for_focus(page, "row:beta"):
        fail(f"focus left beta after Space: {_focused(page)}")
    else:
        level = page.evaluate("() => document.querySelector('#fileTree .file-empty').getAttribute('aria-level')")
        if level != "2":
            fail(f"placeholder aria-level {level!r}, expected '2'")
        page.keyboard.press("ArrowDown")
        if not _wait_for_focus(page, "row:beta/", 2000):
            fail(f"ArrowDown from beta did not reach its placeholder: {_focused(page)}")
        page.keyboard.press("Enter")
        page.keyboard.press("Space")
        page.wait_for_timeout(300)
        if (_row(page, "beta") or {}).get("expanded") != "true" or _focused(page) != "row:beta/":
            fail(f"Enter/Space on the inert placeholder did something: beta={_row(page, 'beta')} focus={_focused(page)}")
        page.evaluate("() => renderFileTree()")
        if not _wait_for_focus(page, "row:beta/", 2000):
            fail(f"a re-render dropped focus from the placeholder: {_focused(page)}")
        page.keyboard.press("ArrowLeft")
        if not _wait_for_focus(page, "row:beta"):
            fail(f"ArrowLeft on the placeholder did not return to beta: {_focused(page)}")
        page.keyboard.press("Space")
        if not _wait_until(page, "() => !document.querySelector('#fileTree .file-empty')"):
            fail("Space did not collapse beta")

    # ── An end-of-turn refresh keeps focus on a nested row ───────────────────
    # loadDir('.') clears the folder listings, renders, then renders again once
    # they load; focus must come back to the nested row, not the top.
    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowRight")
    if _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')"):
        page.focus(ROW_SEL.format("alpha/inner.txt"))
        page.evaluate("() => { window.__treeFocusins = 0; document.getElementById('fileTree')"
                      ".addEventListener('focusin', () => { window.__treeFocusins++; }); }")
        page.evaluate("() => loadDir('.', {preservePreview: true})")
        page.wait_for_timeout(800)
        if _focused(page) != "row:alpha/inner.txt":
            fail(f"after a background loadDir focus is on {_focused(page)}, not alpha/inner.txt")
        page.focus(ROW_SEL.format("alpha"))
        page.keyboard.press("ArrowLeft")
        _wait_until(page, "() => !document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')")
    else:
        fail("could not expand alpha for the refresh check")

    # A second Right while the listing is still loading must not collapse it.
    page.evaluate("() => { delete S._dirCache['alpha']; }")
    page.route("**/api/list?*", lambda route: (time.sleep(0.6), route.continue_()))
    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    ok = _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=4000)
    page.unroute("**/api/list?*")
    page.wait_for_timeout(300)
    if not ok or (_row(page, "alpha") or {}).get("expanded") != "true":
        fail(f"a second ArrowRight during a slow listing collapsed alpha: {_row(page, 'alpha')}")
    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowLeft")
    _wait_until(page, "() => !document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')")

    # A key pressed between the two renders of a refresh wins over the
    # remembered position: the second render must not pull focus back.
    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowRight")
    _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')")
    page.focus(ROW_SEL.format("alpha/inner.txt"))
    page.route("**/api/list?*path=alpha*", lambda route: (time.sleep(1.0), route.continue_()))
    page.evaluate("() => { window.__refresh = loadDir('.', {preservePreview: true}); }")
    if not _wait_until(page, "() => !document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=3000):
        fail("the refresh did not render a partial tree first")
    else:
        landed = _focused(page)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(100)
        moved_to = _focused(page)
        page.evaluate("() => window.__refresh")
        _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=4000)
        page.wait_for_timeout(200)
        if _focused(page) != moved_to:
            fail(f"partial render put focus on {landed}, ArrowDown moved it to {moved_to}, "
                 f"but the completing render moved it to {_focused(page)}")
    page.unroute("**/api/list?*path=alpha*")

    # A folder listing that lands between the two renders of a root refresh
    # renders the tree once more (new row objects) - that is not the user
    # moving, so the completing render still brings focus back to the nested
    # row. Deterministic: listings wait on barriers released from the test.
    page.evaluate(HOLD_FETCH_JS)
    page.evaluate("() => { delete S._dirCache['beta']; window.__hold('path=beta'); }")
    # The delayed expansion of beta starts first, by its real row handler.
    page.evaluate("() => document.querySelector('#fileTree [data-a11y-tree-path=\"beta\"]').click()")
    page.focus(ROW_SEL.format("alpha/inner.txt"))
    page.evaluate("() => { window.__hold('path=alpha');"
                  " window.__refresh = loadDir('.', {preservePreview: true}); }")
    if not _wait_until(page, "() => !document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=3000):
        fail("the refresh with a pending folder did not render a partial tree first")
    else:
        page.evaluate("() => window.__release('path=beta')")
        if not _wait_until(page, "() => document.querySelector('#fileTree [data-a11y-tree-path=\"beta\"]')"
                                 ".getAttribute('aria-expanded') === 'true'", timeout_ms=3000):
            fail("the delayed beta listing did not render between the refresh's two renders")
        page.evaluate("() => window.__release('path=alpha')")
        page.evaluate("() => window.__refresh")
        if not _wait_for_focus(page, "row:alpha/inner.txt", 3000):
            fail(f"a folder listing rendered between a refresh's two renders; focus ended on "
                 f"{_focused(page)}, not alpha/inner.txt")
    page.evaluate("() => window.__unhold()")
    page.focus(ROW_SEL.format("beta"))
    page.keyboard.press("ArrowLeft")

    # Control: moving off the fallback row and back during the gap is the
    # user's choice, so the completing render leaves focus where they are.
    page.focus(ROW_SEL.format("alpha/inner.txt"))
    page.evaluate("() => { window.__hold('path=alpha');"
                  " window.__refresh = loadDir('.', {preservePreview: true}); }")
    # The row after alpha/inner.txt (beta) is the partial render's fallback.
    if not _wait_for_focus(page, "row:beta", 3000):
        fail(f"the partial render put focus on {_focused(page)}, not beta")
    else:
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowUp")
        page.evaluate("() => window.__release('path=alpha')")
        page.evaluate("() => window.__refresh")
        _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=3000)
        page.wait_for_timeout(200)
        if _focused(page) != "row:beta":
            fail(f"after moving away and back during a refresh, the completing render "
                 f"moved focus to {_focused(page)}, not left it on beta")
    page.evaluate("() => window.__unhold()")

    def _start_held_refresh():
        """Focus alpha/inner.txt, start a root refresh whose alpha listing is
        held, and wait for the partial render's fallback (beta). True if it
        got there."""
        if not _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=3000):
            fail("alpha/inner.txt is not shown before a refresh check")
            return False
        page.focus(ROW_SEL.format("alpha/inner.txt"))
        page.evaluate("() => { window.__hold('path=alpha');"
                      " window.__refresh = loadDir('.', {preservePreview: true}); }")
        if not _wait_for_focus(page, "row:beta", 3000):
            fail(f"the partial render put focus on {_focused(page)}, not beta")
            page.evaluate("() => window.__unhold()")
            return False
        return True

    def _finish_held_refresh():
        page.evaluate("() => window.__release('path=alpha')")
        page.evaluate("() => window.__refresh")
        _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=3000)
        page.wait_for_timeout(200)

    # Acting on the fallback row itself (expanding it) is the user's choice
    # too, although focus does not move.
    if _start_held_refresh():
        page.keyboard.press("ArrowRight")
        _wait_until(page, "() => document.querySelector('#fileTree [data-a11y-tree-path=\"beta\"]')"
                          ".getAttribute('aria-expanded') === 'true'", timeout_ms=3000)
        _finish_held_refresh()
        if _focused(page) != "row:beta":
            fail(f"after expanding the fallback row during a refresh, the completing render "
                 f"moved focus to {_focused(page)}, not left it on beta")
    page.evaluate("() => window.__unhold()")
    page.focus(ROW_SEL.format("beta"))
    page.keyboard.press("ArrowLeft")

    # Activating the fallback row without a key or pointer (a screen reader's
    # browse-mode Enter or double-tap sends only a click) is an action too.
    if _start_held_refresh():
        page.evaluate("() => document.querySelector('#fileTree [data-a11y-tree-path=\"beta\"]').click()")
        _wait_until(page, "() => document.querySelector('#fileTree [data-a11y-tree-path=\"beta\"]')"
                          ".getAttribute('aria-expanded') === 'true'", timeout_ms=3000)
        _finish_held_refresh()
        if _focused(page) != "row:beta":
            fail(f"after activating the fallback row by click during a refresh, the completing "
                 f"render moved focus to {_focused(page)}, not left it on beta")
    page.evaluate("() => window.__unhold()")
    page.focus(ROW_SEL.format("beta"))
    page.keyboard.press("ArrowLeft")

    # Keys the tree does not act on - a modifier alone (Ctrl silences NVDA's
    # speech), PageDown, a page shortcut - are not a move.
    if _start_held_refresh():
        page.keyboard.press("Control")
        page.keyboard.press("PageDown")
        page.keyboard.press("Control+KeyB")
        _finish_held_refresh()
        if _focused(page) != "row:alpha/inner.txt":
            fail(f"keys the tree ignores dropped the carried position during a refresh: focus "
                 f"is on {_focused(page)}, not alpha/inner.txt")
    page.evaluate("() => window.__unhold()")

    # Opening the context menu on the fallback row (to rename or copy from it)
    # is acting on it: focus stays there.
    if _start_held_refresh():
        page.click(ROW_SEL.format("beta"), button="right")
        if not _wait_until(page, "() => !!document.querySelector('.file-ctx-menu')", timeout_ms=2000):
            fail("right-click on the fallback row opened no context menu")
        _finish_held_refresh()
        page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(m => m.remove())")
        if _focused(page) != "row:beta":
            fail(f"after opening the context menu on the fallback row during a refresh, the "
                 f"completing render moved focus to {_focused(page)}, not left it on beta")
    page.evaluate("() => window.__unhold()")

    # Focus that left the tree in the gap (by Shift+Tab, as a keyboard user
    # does - the tree is the page's last Tab stop and headless Firefox does not
    # Tab out of the document - and by moving it directly) is not taken back
    # by the carried position; the tab stop still goes to the nested row.
    for how in ("Shift+Tab", "focus"):
        if not _start_held_refresh():
            continue
        if how == "Shift+Tab":
            page.keyboard.press("Shift+Tab")
        else:
            page.focus("#msg")
        left_to = _focused(page)
        _finish_held_refresh()
        if (left_to or "").startswith("row:"):
            fail(f"{how} out of the tree did not leave it: {left_to}")
        elif _focused(page) != left_to:
            fail(f"a refresh completing after {how} out of the tree moved focus from {left_to} "
                 f"to {_focused(page)}")
        elif (_row(page, "alpha/inner.txt") or {}).get("tabindex") != "0":
            fail(f"after {how} out of the tree the completing render did not put the tab stop "
                 f"back on alpha/inner.txt: {_row(page, 'alpha/inner.txt')}")
        page.evaluate("() => window.__unhold()")

    # Two overlapping refreshes, in both completion orders: the newer one joins
    # the carried position, so whichever shows the original row first restores
    # it, and the other completing later does not move focus again.
    if _start_held_refresh():
        page.evaluate("() => { window.__refresh2 = loadDir('.', {preservePreview: true}); }")
        if not _wait_until(page, "() => window.__held('path=alpha') >= 2", timeout_ms=3000):
            fail("the second refresh did not request alpha's listing")
        else:
            page.evaluate("() => window.__releaseFirst('path=alpha')")
            page.evaluate("() => window.__refresh")
            if not _wait_for_focus(page, "row:alpha/inner.txt", 3000):
                fail(f"the older refresh completed first but left focus on {_focused(page)}, "
                     f"not alpha/inner.txt")
            page.evaluate("() => window.__release('path=alpha')")
            page.evaluate("() => window.__refresh2")
            page.wait_for_timeout(200)
            if _focused(page) != "row:alpha/inner.txt":
                fail(f"the newer refresh completing last moved focus to {_focused(page)}")
    page.evaluate("() => window.__unhold()")

    # The refresh that completes first cannot show the original row (its
    # listing failed): it does not use the position up, so the other one,
    # whose listing loads, still brings it back.
    if _start_held_refresh():
        page.evaluate("() => { window.__refresh2 = loadDir('.', {preservePreview: true}); }")
        if not _wait_until(page, "() => window.__held('path=alpha') >= 2", timeout_ms=3000):
            fail("the second refresh did not request alpha's listing")
        else:
            page.evaluate("() => window.__failFirst('path=alpha')")
            page.evaluate("() => window.__refresh")
            page.wait_for_timeout(200)
            page.evaluate("() => window.__release('path=alpha')")
            page.evaluate("() => window.__refresh2")
            if not _wait_for_focus(page, "row:alpha/inner.txt", 3000):
                fail(f"after the first refresh's listing failed and the second one's loaded, "
                     f"focus is on {_focused(page)}, not alpha/inner.txt")
    page.evaluate("() => window.__unhold()")

    # A refresh of another tree (here: a new tree generation, as after a
    # workspace or session switch) does not carry the old tree's position,
    # even when its rows have the same paths.
    if _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=3000):
        page.focus(ROW_SEL.format("alpha/inner.txt"))
        page.evaluate("() => { bumpWorkspaceTreeGen(); window.__hold('path=alpha');"
                      " window.__refresh = loadDir('.', {preservePreview: true}); }")
        if not _wait_for_focus(page, "row:beta", 3000):
            fail(f"the new tree's partial render put focus on {_focused(page)}, not beta")
        else:
            _finish_held_refresh()
            if _focused(page) != "row:beta":
                fail(f"a refresh of a new tree carried the old tree's position: focus moved to "
                     f"{_focused(page)}, not left on beta")
    page.evaluate("() => window.__unhold()")

    # The newer refresh's partial tree has lost the fallback row too (beta
    # deleted meanwhile): the original position still comes back.
    if _start_held_refresh():
        page.evaluate("() => { window.__hideInRoot = 'beta';"
                      " window.__refresh2 = loadDir('.', {preservePreview: true}); }")
        if not _wait_until(page, "() => window.__held('path=alpha') >= 2", timeout_ms=3000):
            fail("the second refresh did not request alpha's listing")
        else:
            page.evaluate("() => { window.__hideInRoot = null; window.__release('path=alpha'); }")
            page.evaluate("() => Promise.all([window.__refresh, window.__refresh2])")
            if not _wait_for_focus(page, "row:alpha/inner.txt", 3000):
                fail(f"when the fallback row vanished in a newer refresh, focus ended on "
                     f"{_focused(page)}, not alpha/inner.txt")
    page.evaluate("() => { window.__hideInRoot = null; window.__unhold(); }")
    page.evaluate("() => loadDir('.', {preservePreview: true})")

    if _start_held_refresh():
        page.evaluate("() => { window.__refresh2 = loadDir('.', {preservePreview: true}); }")
        if not _wait_until(page, "() => window.__held('path=alpha') >= 2", timeout_ms=3000):
            fail("the second refresh did not request alpha's listing")
        else:
            page.evaluate("() => window.__releaseLast('path=alpha')")
            page.evaluate("() => window.__refresh2")
            if not _wait_for_focus(page, "row:alpha/inner.txt", 3000):
                fail(f"the newer refresh completed first but left focus on {_focused(page)}, "
                     f"not alpha/inner.txt")
            page.evaluate("() => window.__release('path=alpha')")
            page.evaluate("() => window.__refresh")
            page.wait_for_timeout(200)
            if _focused(page) != "row:alpha/inner.txt":
                fail(f"the older refresh completing last moved focus to {_focused(page)}")
    page.evaluate("() => window.__unhold()")

    # A refill that is abandoned (here: the workspace tree generation moved
    # on, as on a workspace switch) drops its carried position, and a refresh
    # of the new tree - started before or after the old one gives up - does
    # not take it over and move the user.
    for started in ("after", "before"):
        if not _start_held_refresh():
            continue
        page.evaluate("() => bumpWorkspaceTreeGen()")
        if started == "before":
            page.evaluate("() => { window.__refresh2 = loadDir('.', {preservePreview: true}); }")
            _wait_until(page, "() => window.__held('path=alpha') >= 2", timeout_ms=3000)
        page.evaluate("() => window.__release('path=alpha')")
        page.evaluate("() => window.__refresh")
        if started == "after":
            # The abandoned refill rendered nothing more: still the partial tree.
            page.evaluate("() => { window.__refresh2 = loadDir('.', {preservePreview: true}); }")
        page.evaluate("() => window.__refresh2")
        _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')", timeout_ms=3000)
        page.wait_for_timeout(200)
        if _focused(page) != "row:beta":
            fail(f"an abandoned refresh's position leaked into a refresh started {started} it "
                 f"gave up: focus moved to {_focused(page)}, not left on beta")
        page.evaluate("() => window.__unhold()")

    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowLeft")
    _wait_until(page, "() => !document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')")

    # When a row and both its neighbours disappear, focus goes to the nearest
    # folder above it that is still shown, not the top of the tree.
    page.focus(ROW_SEL.format("epsilon"))
    page.keyboard.press("ArrowRight")
    if _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"epsilon/b.txt\"]')"):
        page.focus(ROW_SEL.format("epsilon/b.txt"))
        page.evaluate("() => { S._expandedDirs.delete('epsilon'); renderFileTree(); }")
        if not _wait_for_focus(page, "row:epsilon", 2000):
            fail(f"after its folder collapsed under it, focus went to {_focused(page)}, not epsilon")
        page.evaluate("() => { if (typeof _saveExpandedDirs === 'function') _saveExpandedDirs(); }")
    else:
        fail("could not expand epsilon")

    # ── A held Enter or Space acts once ──────────────────────────────────────
    # Auto-repeated keydowns (repeat=true) neither open a file nor toggle a
    # folder again.
    page.evaluate(COUNT_OPENS_JS)
    for path, key in (("gamma.txt", "Enter"), ("beta", "Enter"), ("beta", " ")):
        page.evaluate("""([sel, key]) => { const r = document.querySelector(sel); r.focus();
          r.dispatchEvent(new KeyboardEvent('keydown', {key, repeat: true, bubbles: true, cancelable: true})); }""",
                      [ROW_SEL.format(path), key])
        page.wait_for_timeout(300)
    if page.evaluate("() => window.__treeOpens.slice()"):
        fail(f"an auto-repeated Enter opened {page.evaluate('() => window.__treeOpens.slice()')}")
    if (_row(page, "beta") or {}).get("expanded") != "false":
        fail("an auto-repeated Enter or Space toggled beta")

    # ── Enter on a file opens it once ────────────────────────────────────────
    page.evaluate(COUNT_OPENS_JS)
    page.focus(ROW_SEL.format("gamma.txt"))
    page.keyboard.press("Enter")
    if not _wait_until(page, "() => window.__treeOpens.length >= 1"):
        fail("Enter on gamma.txt did not open it")
    else:
        page.wait_for_timeout(400)
        opens = page.evaluate("() => window.__treeOpens.slice()")
        if opens != ["gamma.txt"]:
            fail(f"Enter opened {opens}, expected exactly ['gamma.txt']")
    page.evaluate("() => clearPreview({keepPanelOpen: true})")
    _wait_until(page, "() => document.getElementById('fileTree').style.display !== 'none'")

    # ── Background re-render keeps the tab stop and does not steal focus ────
    page.focus(ROW_SEL.format("note.md"))
    page.focus("#msg")
    page.evaluate("() => renderFileTree()")
    page.wait_for_timeout(150)
    if _focused(page) != "textarea#msg":
        fail(f"a background re-render moved focus to {_focused(page)}")
    if (_row(page, "note.md") or {}).get("tabindex") != "0":
        fail("a background re-render lost the tab stop on note.md")

    # ── A mouse click moves the tab stop ─────────────────────────────────────
    page.click(ROW_SEL.format("beta") + " .file-icon")
    if not _wait_until(page, "() => { const r = document.querySelector('#fileTree [data-a11y-tree-path=\"beta\"]');"
                             " return !!r && r.getAttribute('tabindex') === '0'; }"):
        fail("a mouse click on beta did not move the tab stop to it")
    if page.evaluate("() => !!document.querySelector('#fileTree .file-empty')"):
        page.focus(ROW_SEL.format("beta"))
        page.keyboard.press("ArrowLeft")
        _wait_until(page, "() => !document.querySelector('#fileTree .file-empty')")

    # ── Delete key ───────────────────────────────────────────────────────────
    # alpha is expanded so every refresh after a delete renders twice (first
    # without alpha's listing), and two deletes run back to back.
    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowRight")
    _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')")
    dialog_open = "() => document.getElementById('appDialogOverlay').style.display !== 'none'"
    page.focus(ROW_SEL.format("gamma.txt"))
    page.keyboard.press("Shift+Delete")
    page.wait_for_timeout(200)
    if page.evaluate(dialog_open):
        fail("Shift+Delete opened the delete confirmation")
        page.keyboard.press("Escape")
    page.keyboard.press("Delete")
    if not _wait_until(page, dialog_open):
        fail("Delete on gamma.txt did not ask for confirmation")
    else:
        page.keyboard.press("Escape")
        if not _wait_for_focus(page, "row:gamma.txt"):
            fail(f"cancelling the delete did not return focus to the row: {_focused(page)}")
        if not os.path.exists(os.path.join(workspace, "gamma.txt")):
            fail("cancelling the delete removed the file")
        for path, after in (("gamma.txt", "note.md"), ("note.md", "zeta.txt")):
            page.keyboard.press("Delete")
            if not _wait_until(page, dialog_open):
                fail(f"Delete on {path} did not ask for confirmation")
                break
            page.click("#appDialogConfirm")
            if not _wait_until(page, f"() => !document.querySelector('#fileTree [data-a11y-tree-path=\"{path}\"]')"):
                fail(f"confirming the delete did not remove {path} from the tree")
                break
            page.wait_for_timeout(500)
            if _focused(page) != f"row:{after}":
                fail(f"after deleting {path} focus went to {_focused(page)}, expected the next row {after}")
                break
            if os.path.exists(os.path.join(workspace, path)):
                fail(f"confirming the delete left {path} on disk")
    page.focus(ROW_SEL.format("alpha"))
    page.keyboard.press("ArrowLeft")
    _wait_until(page, "() => !document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/inner.txt\"]')")

    # ── Keys in the inline rename input stay in the input ────────────────────
    page.evaluate(COUNT_OPENS_JS)
    page.focus(ROW_SEL.format("zeta.txt"))
    page.dblclick(ROW_SEL.format("zeta.txt") + " .file-name")
    if not _wait_until(page, "() => document.activeElement && document.activeElement.classList.contains('file-rename-input')"):
        fail("double-click did not start an inline rename")
    else:
        for key in ("Home", "End", "ArrowUp", "ArrowDown", "Space"):
            page.keyboard.press(key)
            if not page.evaluate("() => document.activeElement && document.activeElement.classList.contains('file-rename-input')"):
                fail(f"{key} in the rename input moved focus to {_focused(page)}")
                break
        page.evaluate("() => { const i = document.querySelector('#fileTree .file-rename-input'); i.value = 'omega.txt'; }")
        page.keyboard.press("Enter")
        if not _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"omega.txt\"]')"):
            fail("Enter in the rename input did not rename zeta.txt")
        elif not _wait_for_focus(page, "row:omega.txt"):
            fail(f"after the rename focus is on {_focused(page)}, not the renamed row")
        page.wait_for_timeout(400)
        opens = page.evaluate("() => window.__treeOpens.slice()")
        if opens:
            fail(f"Enter in the rename input also opened {opens}")
        if page.evaluate("() => document.getElementById('previewArea').classList.contains('visible')"):
            fail("Enter in the rename input opened a preview")

    # ── A tree that becomes empty hands focus to the Files tab ───────────────
    page.focus(ROW_SEL.format("beta"))
    page.evaluate("() => loadDir('beta')")
    if not _wait_until(page, "() => !document.querySelector('#fileTree [role=treeitem]')"):
        fail("navigating into the empty folder beta left rows in the tree")
    else:
        if not _wait_for_focus(page, "button#workspaceFilesTab.workspace-panel-tab", 2000):
            fail(f"focus after the tree emptied: {_focused(page)}, expected the Files tab")
        attrs = page.evaluate("() => { const t = document.getElementById('fileTree');"
                              " return [t.getAttribute('role'), t.getAttribute('aria-label')]; }")
        if attrs != [None, None]:
            fail(f"an empty #fileTree kept its tree role/name: {attrs}")
    page.evaluate("() => loadDir('.')")
    _wait_until(page, "() => !!document.querySelector('#fileTree [role=treeitem]')")
    # The same when focus was on the container itself, not on a row.
    page.evaluate("() => document.getElementById('fileTree').focus()")
    page.evaluate("() => loadDir('beta')")
    if _wait_until(page, "() => !document.querySelector('#fileTree [role=treeitem]')"):
        if not _wait_for_focus(page, "button#workspaceFilesTab.workspace-panel-tab", 2000):
            fail(f"focus on the container after the tree emptied: {_focused(page)}, expected the Files tab")
    page.evaluate("() => loadDir('.')")
    _wait_until(page, "() => !!document.querySelector('#fileTree [role=treeitem]')")
    return failures


def _screenshots(browser, directory):
    os.makedirs(directory, exist_ok=True)
    for label, viewport in (("desktop-1440x900", {"width": 1440, "height": 900}),
                            ("phone-390x844", {"width": 390, "height": 844})):
        ctx, page, errors = _new_page(browser, viewport=viewport)
        if page is None:
            print(f"NOTE screenshots {label}: {errors}")
            ctx.close()
            continue
        page.focus(ROW_SEL.format("alpha"))
        page.keyboard.press("ArrowRight")
        _wait_until(page, "() => !!document.querySelector('#fileTree [data-a11y-tree-path=\"alpha/sub\"]')")
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowDown")
        _wait_for_focus(page, "row:alpha/inner.txt")
        page.wait_for_timeout(200)
        path = os.path.join(directory, f"file-tree-focus-{label}.png")
        page.screenshot(path=path)
        print(f"wrote {path}")
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
    state_dir = tempfile.mkdtemp(prefix="hermes-file-tree-keyboard-")
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
    proc = None
    try:
        failures = []
        with sync_playwright() as pw:
            engines = [("chromium", pw.chromium, {"args": ["--no-sandbox", "--disable-dev-shm-usage"]})]
            if os.path.exists(pw.firefox.executable_path):
                engines.append(("firefox", pw.firefox, {}))
            else:
                print("NOTE firefox: Playwright's firefox build is not installed; skipped")
            for name, engine, launch_args in engines:
                # Every engine gets a fresh workspace: the run deletes and renames files.
                workspace = os.path.join(state_dir, f"workspace-{name}")
                _make_workspace(workspace)
                env["HERMES_WEBUI_DEFAULT_WORKSPACE"] = workspace
                proc = subprocess.Popen(
                    [sys.executable, server_py], cwd=repo_root, env=env,
                    stdout=log, stderr=subprocess.STDOUT,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
                )
                if not _wait_for_health(proc, timeout=30):
                    print("SETUP FAIL: this gate's server exited or did not become healthy in 30s", file=sys.stderr)
                    log.flush()
                    with open(os.path.join(state_dir, "server.log")) as f:
                        print(f.read()[-2000:], file=sys.stderr)
                    return 2
                try:
                    browser = engine.launch(headless=True, **launch_args)
                except Exception as exc:
                    if name == "chromium":
                        raise
                    print(f"NOTE {name}: could not launch ({str(exc).strip().splitlines()[0]}); skipped")
                    browser = None
                if browser:
                    # Screenshots first: the checks below delete and rename files.
                    if shots and name == "chromium":
                        _screenshots(browser, shots)
                    ctx, page, errors = _new_page(browser, viewport={"width": 1440, "height": 900})
                    if page is None:
                        failures.append(f"  [{name}] {errors}")
                    else:
                        found = _check(page, name, workspace)
                        failures.extend(found)
                        if not found:
                            print(f"OK  {name} — tree contract, one Tab stop, arrows/Home/End, expand/enter/parent/collapse, "
                                  "Enter/Space, focus kept across re-renders, Delete with confirmation, rename keys stay in the input")
                        failures.extend(f"  [{name}] pageerror: {err}" for err in errors)
                    ctx.close()
                    browser.close()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                proc = None

        if failures:
            print("\nFILE TREE KEYBOARD FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nFILE TREE KEYBOARD PASSED")
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log.close()
        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
