#!/usr/bin/env python3
"""
Real-browser gate — workspace tree download placement (context menu) + row geometry.

WHY THIS EXISTS
  The node-VM tests in test_workspace_tree_icon_download.py execute the icon
  block with a mocked DOM, so they cannot see layout or real event dispatch.
  The #7810 design review (nesquena-hermes, 2026-10-28, head 35184264) found
  the download action belonged in the context menu rather than on the leading
  type icon; the rework moved it there. This gate proves the reworked
  contract in a real page at both widths:

    1. Row geometry stays uniform: PDF rows render the same height as every
       other file row and the file names stay left-aligned (the rows
       themselves are checked, per the review's "point it at the rows").
    2. No icon carries a download affordance: the .file-icon-download class
       is gone; icons are plain type glyphs.
    3. Icon clicks bubble to the row's own routing: preview for previewable
       types (pdf, md), whole-row download for DOWNLOAD_EXTS (zip) via
       openFile — and never a direct icon download.
    4. The file context menu offers Download for files (media_download label,
       downloadFile(item.path)) next to the existing Download Folder item for
       directories, and clicking it downloads exactly once.
    5. The file NAME click still previews after the #1707 debounce.

SCOPE
  AGENT-FREE like browser_smoke.py: boots the real server.py on an ephemeral
  port with an isolated temp state dir, seeds a workspace, creates a session
  via /api/session/new, and loads it with the app's own loadSession().

USAGE
  python tests/browser_workspace_icon_download_geometry.py

EXIT CODES
  0 — geometry + placement contracts hold at 1920x1080 and 390x844
  1 — an assertion failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing)
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

VIEWPORTS = [
    (1920, 1080, "desktop"),
    (390, 844, "mobile"),
]

SEED_FILES = {
    "receipt.pdf": b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<<>>\n%%EOF\n",
    "notes.md": b"# notes\nprobe file\n",
    "archive.zip": b"PK\x05\x06" + b"\x00" * 18,
    "photo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    "data.csv": b"a,b\n1,2\n",
}
# Extra rows so the tree is taller than the panel and genuinely scrolls: the
# lower-edge clamp probe needs to place a row at a chosen viewport position.
SEED_FILES.update({f"bulk_{i:02d}.txt": f"bulk {i}\n".encode() for i in range(40)})
SEED_DIRS = ["subdir"]  # exercises the existing Download Folder item

# Installs call recorders over the sibling-script globals the handlers resolve
# at call time, then reads back row geometry. openFile is wrapped, not
# replaced: calls are recorded AND pass through, so the row → openFile →
# DOWNLOAD_EXTS → downloadFile chain stays real (the recorder on downloadFile
# is the only thing standing in front of the browser download).
ROW_GEOMETRY_JS = """
() => {
  window.__dl = []; window.__open = [];
  if (typeof downloadFile === 'function') {
    window.downloadFile = (p) => { window.__dl.push(String(p)); };
  }
  if (typeof openFile === 'function') {
    const realOpenFile = openFile;
    window.openFile = function () {
      window.__open.push(Array.from(arguments).map(String));
      return realOpenFile.apply(this, arguments);
    };
  }
  const rows = Array.from(document.querySelectorAll('.file-item'));
  const byFile = {};
  for (const row of rows) {
    const nameEl = row.querySelector('.file-name') || row.querySelector('[class*="name"]');
    const iconEl = row.querySelector('.file-icon');
    const r = row.getBoundingClientRect();
    const nr = nameEl ? nameEl.getBoundingClientRect() : null;
    byFile[(nameEl ? nameEl.textContent : '?').trim()] = {
      rowH: r.height,
      nameLeft: nr ? nr.left : null,
      iconClass: iconEl ? iconEl.className : '',
    };
  }
  return byFile;
}
"""

ROW_FOR = "Array.from(document.querySelectorAll('.file-item')).find(r => (r.textContent||'').includes(%s))"
CLICK_ICON_JS = "() => { " + ROW_FOR % "'%s'" + ".querySelector('.file-icon').click(); }"
CLICK_NAME_JS = """() => { const row = """ + ROW_FOR % "'%s'" + """;
  const nameEl = row.querySelector('.file-name') || row.querySelector('[class*="name"]');
  nameEl.click(); }"""
# Row click (not just the icon): exercises the row's own routing directly.
CLICK_ROW_JS = "() => { " + ROW_FOR % "'%s'" + ".click(); }"
OPEN_MENU_JS = """() => { const row = """ + ROW_FOR % "'%s'" + """;
  const r = row.getBoundingClientRect();
  row.dispatchEvent(new MouseEvent('contextmenu', {
    bubbles: true, cancelable: true,
    clientX: Math.round(r.left + r.width / 2), clientY: Math.round(r.top + r.height / 2),
  }));
  return document.querySelectorAll('.file-ctx-menu').length; }"""
MENU_ITEMS_JS = """() => Array.from(document.querySelectorAll('.file-ctx-menu div'))
  .map((el) => el.textContent.trim())"""
CLICK_MENU_ITEM_JS = """(label) => {
  const items = Array.from(document.querySelectorAll('.file-ctx-menu div'));
  const hit = items.find((el) => el.textContent.trim() === label);
  if (!hit) return false;
  hit.click();
  return true;
}"""


def _wait_for_health(base, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def _probe_width(page, width, height, label, artifact_dir):
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => { if (typeof toggleWorkspacePanel === 'function') toggleWorkspacePanel(true); }")
    page.wait_for_selector(".file-item", timeout=15000)
    page.screenshot(path=str(artifact_dir / f"workspace-tree-{label}.png"))

    # Installs the recorders and captures row geometry in one pass.
    rows = page.evaluate(ROW_GEOMETRY_JS)
    pdf, md, zipped = rows.get("receipt.pdf"), rows.get("notes.md"), rows.get("archive.zip")
    assert pdf and md and zipped, f"{label}: expected pdf/md/zip rows; got {sorted(rows)}"
    assert pdf["rowH"] == md["rowH"] == zipped["rowH"], (
        f"{label}: row heights diverge — pdf={pdf['rowH']} md={md['rowH']} zip={zipped['rowH']} "
        f"(the tree's uniform rows are the existing layout)")
    assert pdf["nameLeft"] == md["nameLeft"] == zipped["nameLeft"], (
        f"{label}: name alignment diverges — pdf={pdf['nameLeft']} md={md['nameLeft']} zip={zipped['nameLeft']}")
    for name, info in rows.items():
        assert "file-icon-download" not in info["iconClass"], (
            f"{label}: {name} still carries .file-icon-download — the class was "
            f"dropped with the icon handlers (review 2026-10-28)")

    # PDF icon click → bubbles to the row: preview, never a direct download.
    page.evaluate(CLICK_ICON_JS % "receipt.pdf")
    dl = page.evaluate("window.__dl.slice()")
    opens = page.evaluate("window.__open.length")
    assert dl == [], f"{label}: pdf icon click must not download; got {dl}"
    assert opens >= 1, f"{label}: pdf icon click must bubble to preview; open calls={opens}"

    # .md icon click → same: bubbles to preview, no download.
    page.evaluate(CLICK_ICON_JS % "notes.md")
    dl = page.evaluate("window.__dl.length")
    opens2 = page.evaluate("window.__open.length")
    assert dl == 0, f"{label}: md icon click must not download; dl={dl}"
    assert opens2 > opens, f"{label}: md icon click must bubble to preview; open calls={opens2}"

    # Row click on the archive → whole-row DOWNLOAD_EXTS routing downloads
    # (openFile routes it); unchanged master behavior, honest glyph.
    page.evaluate(CLICK_ROW_JS % "archive.zip")
    dl = page.evaluate("window.__dl.slice()")
    assert len(dl) == 1 and dl[0].endswith("archive.zip"), (
        f"{label}: zip row click must still download via row routing; got {dl}")

    # PDF filename click → preview (after the #1707 300ms debounce), no download.
    page.evaluate(CLICK_NAME_JS % "receipt.pdf")
    page.wait_for_timeout(600)
    dl = page.evaluate("window.__dl.length")
    opens3 = page.evaluate("window.__open.length")
    assert dl == 1, f"{label}: filename click must not download; dl={dl}"
    assert opens3 > opens2, f"{label}: filename click must preview; open calls={opens3}"

    # Context menu on a FILE row → Download item downloads exactly once.
    n_menus = page.evaluate(OPEN_MENU_JS % "receipt.pdf")
    assert n_menus == 1, f"{label}: contextmenu on pdf row must open the file menu; got {n_menus}"
    items = page.evaluate(MENU_ITEMS_JS)
    assert "Download" in items, f"{label}: file menu must offer Download; got {items}"
    dl_before = page.evaluate("window.__dl.length")
    clicked = page.evaluate(CLICK_MENU_ITEM_JS, "Download")
    assert clicked, f"{label}: Download menu item not clickable; items={items}"
    dl = page.evaluate("window.__dl.slice()")
    assert len(dl) == dl_before + 1 and dl[-1].endswith("receipt.pdf"), (
        f"{label}: menu Download must call downloadFile(receipt.pdf); got {dl}")
    page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")

    # Context menu on a DIRECTORY row → the existing Download Folder stays,
    # and no plain Download item leaks onto directory rows.
    n_menus = page.evaluate(OPEN_MENU_JS % "subdir")
    assert n_menus == 1, f"{label}: contextmenu on dir row must open the menu; got {n_menus}"
    items = page.evaluate(MENU_ITEMS_JS)
    assert any("Download Folder" in it for it in items), (
        f"{label}: dir menu must keep Download Folder; got {items}")
    assert "Download" not in items, (
        f"{label}: dir menu must not offer the file Download item; got {items}")
    page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")

    # Close any preview the probes opened (pass-through openFile really
    # previews): on mobile the preview pane takes over the screen and would
    # hide the workspace tree from the next viewport's probe.
    page.evaluate("() => { if (typeof clearPreview === 'function') clearPreview({keepPanelOpen:true}); }")

    print(f"  [{label}] rows uniform ({pdf['rowH']}px, names aligned), icons plain, "
          f"icon→row routing, menu Download→file, dir menu keeps Download Folder: OK")


# Lower-edge clamp probe (2026-11-20 gate RED): the menu is placed with a
# pre-append height guess, so a pointer near the bottom of the viewport left
# the last rows — Delete — below the fold once Download made the menu taller.
# This probe right-clicks a REAL pointer at a deterministic lower-edge
# position (the tree is scrolled so the row center sits at vh-90) and then
# measures the rendered menu: it must fit inside the viewport with Delete
# hit-testable at its center. Mirrors the gate's matched base/integration
# comparison without any destructive Delete operation.
SCROLL_ROW_JS = """
(args) => {
  const rows = Array.from(document.querySelectorAll('.file-item'));
  const row = rows.find(r => (r.textContent||'').includes(args.file));
  if (!row) return {error: 'row not found'};
  let el = row.parentElement;
  while (el && el !== document.body) {
    const cs = getComputedStyle(el);
    if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight) {
      const r = row.getBoundingClientRect();
      el.scrollTop += (r.top + r.height / 2) - args.targetY;
      break;
    }
    el = el.parentElement;
  }
  const r = row.getBoundingClientRect();
  return {cx: r.left + r.width / 2, cy: r.top + r.height / 2};
}
"""

MENU_CLAMP_JS = """
() => {
  const menu = document.querySelector('.file-ctx-menu');
  if (!menu) return {error: 'menu not found'};
  const r = menu.getBoundingClientRect();
  const items = Array.from(menu.querySelectorAll('div'));
  const del = items.find(el => el.textContent.trim() === 'Delete');
  let delInfo = null;
  if (del) {
    const dr = del.getBoundingClientRect();
    const cx = dr.left + dr.width / 2, cy = dr.top + dr.height / 2;
    const inVp = dr.top >= 0 && dr.bottom <= window.innerHeight;
    let hit = false;
    if (inVp && cy >= 0 && cy < window.innerHeight) {
      const hitEl = document.elementFromPoint(cx, cy);
      hit = hitEl === del || (hitEl && del.contains(hitEl));
    }
    delInfo = {top: dr.top, bottom: dr.bottom, inVp, hit};
  }
  return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
          vh: window.innerHeight, vw: window.innerWidth,
          maxHeight: menu.style.maxHeight || '', overflowY: menu.style.overflowY || '',
          del: delInfo, itemCount: items.length};
}
"""


def _open_menu_at_lower_edge(page, file, vh, label):
    info = page.evaluate(SCROLL_ROW_JS, {"file": file, "targetY": vh - 90})
    assert "error" not in info, f"{label}: {info}"
    page.mouse.click(info["cx"], info["cy"], button="right")
    page.wait_for_selector(".file-ctx-menu", timeout=5000)
    return page.evaluate(MENU_CLAMP_JS)


def _probe_menu_clamp(page, width, height, label):
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => { if (typeof toggleWorkspacePanel === 'function') toggleWorkspacePanel(true); }")
    page.wait_for_selector(".file-item", timeout=15000)
    m = _open_menu_at_lower_edge(page, "receipt.pdf", height, label)
    assert m["bottom"] <= m["vh"] and m["top"] >= 0, (
        f"{label}: lower-edge menu overflows the viewport — top={m['top']:.1f} "
        f"bottom={m['bottom']:.1f} vh={m['vh']} (Download made the menu taller than "
        f"the fixed placement allowance; Delete fell below the fold)")
    assert m["del"] and m["del"]["inVp"] and m["del"]["hit"], (
        f"{label}: Delete must be visible and hit-testable at the lower edge; "
        f"got {m['del']} (menu bottom={m['bottom']:.1f}, vh={m['vh']})")
    assert m["left"] >= 0 and m["right"] <= m["vw"], (
        f"{label}: menu must stay inside the viewport horizontally; got "
        f"left={m['left']:.1f} right={m['right']:.1f} vw={m['vw']}")
    page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")
    print(f"  [{label}] lower-edge right-click: menu clamped inside viewport, "
          f"Delete visible and hit-testable (menu bottom {m['bottom']:.1f} / vh {m['vh']}): OK")


MENU_ROWS_JS = """
() => {
  const menu = document.querySelector('.file-ctx-menu');
  if (!menu) return {error: 'menu not found'};
  const rows = Array.from(menu.children).filter(el => el.tagName === 'DIV');
  return {coarse: matchMedia('(pointer:coarse)').matches,
          rows: rows.map(el => ({label: (el.textContent || '').trim(),
                                 h: el.getBoundingClientRect().height,
                                 minHeight: getComputedStyle(el).minHeight}))};
}
"""


def _open_menu_by_contextmenu_event(page, file, target_y):
    # Touch browsers that open a context menu (Android long-press) deliver a
    # contextmenu event; dispatch that event on the row, as they do.
    info = page.evaluate(SCROLL_ROW_JS, {"file": file, "targetY": target_y})
    assert "error" not in info, info
    page.evaluate("""(a) => {
      const row = Array.from(document.querySelectorAll('.file-item'))
        .find(r => (r.textContent || '').includes(a.file));
      row.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,
                                                        clientX: a.cx, clientY: a.cy}));
    }""", {"file": file, "cx": info["cx"], "cy": info["cy"]})
    page.wait_for_selector(".file-ctx-menu", timeout=5000)


def _probe_touch_menu_targets(browser, base, sid):
    """On a coarse pointer every action row of the file context menu is a full
    44px touch target, and the lower-edge clamp still keeps Delete on screen
    with the taller rows. A fine pointer keeps the compact desktop rows."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3,
                              is_mobile=True, has_touch=True)
    try:
        page = ctx.new_page()
        page.goto(base, wait_until="domcontentloaded")
        for _ in range(150):
            try:
                if page.evaluate("(typeof S!=='undefined') && S._bootReady===true && typeof loadSession==='function'"):
                    break
            except Exception:
                pass
            page.wait_for_timeout(200)
        else:
            raise AssertionError("touch: page never reached boot-ready")
        page.evaluate("async (sid) => loadSession(sid)", sid)
        page.wait_for_timeout(1200)
        page.evaluate("() => { if (typeof toggleWorkspacePanel === 'function') toggleWorkspacePanel(true); }")
        page.wait_for_selector(".file-item", timeout=15000)
        _open_menu_by_contextmenu_event(page, "receipt.pdf", 300)
        m = page.evaluate(MENU_ROWS_JS)
        assert m.get("coarse") is True, f"touch context must report a coarse pointer: {m}"
        labels = [r["label"] for r in m["rows"]]
        assert "Download" in labels and "Delete" in labels, f"touch menu rows: {labels}"
        short = [r for r in m["rows"] if r["h"] < 44]
        assert not short, f"touch: every file-menu row must be >=44px tall; short rows: {short}"
        page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")
        _open_menu_by_contextmenu_event(page, "receipt.pdf", 844 - 90)
        c = page.evaluate(MENU_CLAMP_JS)
        assert c["bottom"] <= c["vh"] and c["top"] >= 0 and c["del"] and c["del"]["hit"], (
            f"touch: lower-edge menu with 44px rows must stay inside the viewport with "
            f"Delete hit-testable; got {c}")
        page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")
        print(f"  [touch] coarse pointer: {len(m['rows'])} menu rows all >=44px "
              f"(min {min(r['h'] for r in m['rows']):.1f}px); lower-edge clamp keeps Delete "
              f"hit-testable (bottom {c['bottom']:.1f} / vh {c['vh']}): OK")
    finally:
        ctx.close()


def _probe_fine_pointer_rows_compact(page):
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.evaluate("() => { if (typeof toggleWorkspacePanel === 'function') toggleWorkspacePanel(true); }")
    page.wait_for_selector(".file-item", timeout=15000)
    _open_menu_by_contextmenu_event(page, "receipt.pdf", 400)
    m = page.evaluate(MENU_ROWS_JS)
    assert m.get("coarse") is False, m
    # Height alone can't tell: a long label may wrap near the viewport edge.
    padded = [r for r in m["rows"] if r["minHeight"] not in ("auto", "0px")]
    assert not padded, f"desktop: fine-pointer menu rows must keep the compact layout; got {padded}"
    page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")
    print(f"  [desktop] fine pointer: no touch min-height on {len(m['rows'])} menu rows: OK")


def _probe_root_menu_short_touch(browser, base, sid):
    """The workspace root menu shares the file menu's clamp: on a short
    landscape phone its 44px rows don't fit, so it pins to the edge and
    scrolls, and its last action stays reachable."""
    ctx = browser.new_context(viewport={"width": 480, "height": 215}, device_scale_factor=3,
                              is_mobile=True, has_touch=True)
    try:
        page = ctx.new_page()
        page.goto(base, wait_until="domcontentloaded")
        for _ in range(150):
            try:
                if page.evaluate("(typeof S!=='undefined') && S._bootReady===true && typeof loadSession==='function'"):
                    break
            except Exception:
                pass
            page.wait_for_timeout(200)
        else:
            raise AssertionError("root: page never reached boot-ready")
        page.evaluate("async (sid) => loadSession(sid)", sid)
        page.wait_for_timeout(800)
        r = page.evaluate("""() => {
          document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove());
          _showWorkspaceRootContextMenu({clientX: 100, clientY: 150});
          const m = document.querySelector('.workspace-root-ctx-menu');
          if (!m) return {error: 'no root menu'};
          const rows = Array.from(m.children).filter(e => e.tagName === 'DIV');
          const last = rows[rows.length - 1];
          last.scrollIntoView({block: 'nearest'});
          const mr = m.getBoundingClientRect(), lr = last.getBoundingClientRect();
          const hit = document.elementFromPoint(lr.left + lr.width / 2, lr.top + lr.height / 2);
          return {top: mr.top, bottom: mr.bottom, vh: innerHeight, last: last.textContent.trim(),
                  lastH: lr.height, lastHit: hit === last || last.contains(hit),
                  scrolls: m.scrollHeight > m.clientHeight + 1};
        }""")
        assert "error" not in r, r
        assert r["top"] >= 0 and r["bottom"] <= r["vh"] and r["lastHit"] and r["lastH"] >= 44, (
            f"root: on a 480x215 touch screen the root menu must stay inside the viewport "
            f"and its last action ({r.get('last')}) must be reachable; got {r}")
        page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")
        print(f"  [root] 480x215 touch: root menu inside viewport ({r['top']:.0f}-{r['bottom']:.0f} / "
              f"vh {r['vh']}), scrolls={r['scrolls']}, last action '{r['last']}' reachable: OK")
    finally:
        ctx.close()


def _probe_menu_scroll_fallback(page, label):
    """When the menu is taller than the viewport it must clamp to the edge and
    scroll, not overflow: maxHeight + overflowY, with Delete reachable."""
    width, height = 480, 380
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => { if (typeof toggleWorkspacePanel === 'function') toggleWorkspacePanel(true); }")
    page.wait_for_selector(".file-item", timeout=15000)
    m = _open_menu_at_lower_edge(page, "receipt.pdf", height, label)
    assert m["bottom"] <= m["vh"] and m["top"] >= 0, (
        f"{label}: tiny-viewport menu overflows — top={m['top']:.1f} "
        f"bottom={m['bottom']:.1f} vh={m['vh']}")
    if m["maxHeight"]:
        assert m["overflowY"] == "auto", (
            f"{label}: clamped tall menu must scroll (overflowY=auto); got "
            f"maxHeight={m['maxHeight']} overflowY={m['overflowY']!r}")
        page.evaluate("""() => {
          const items = Array.from(document.querySelectorAll('.file-ctx-menu div'));
          const del = items.find(el => el.textContent.trim() === 'Delete');
          if (del) del.scrollIntoView({block: 'nearest'});
        }""")
        m2 = page.evaluate(MENU_CLAMP_JS)
        assert m2["del"] and m2["del"]["hit"], (
            f"{label}: Delete must be reachable by scrolling the clamped menu; "
            f"got {m2['del']}")
        print(f"  [{label}] tiny viewport: menu pinned to edge + scrollable "
              f"(maxHeight {m['maxHeight']}), Delete reachable after scroll: OK")
    else:
        assert m["del"] and m["del"]["inVp"] and m["del"]["hit"], (
            f"{label}: menu fit without scrolling but Delete is not visible/"
            f"hit-testable; got {m['del']}")
        print(f"  [{label}] tiny viewport: menu fully visible without scroll, "
              f"Delete hit-testable: OK")
    page.evaluate("() => document.querySelectorAll('.file-ctx-menu').forEach(el => el.remove())")


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    if not os.path.exists(server_py):
        print(f"SETUP FAIL: server.py not found at {server_py}", file=sys.stderr)
        return 2

    state_dir = tempfile.mkdtemp(prefix="hermes-ws-icon-geometry-")
    workspace = os.path.join(state_dir, "workspace")
    os.makedirs(workspace)
    for name, blob in SEED_FILES.items():
        with open(os.path.join(workspace, name), "wb") as f:
            f.write(blob)
    for name in SEED_DIRS:
        os.makedirs(os.path.join(workspace, name), exist_ok=True)
    os.makedirs(os.path.join(state_dir, "no-agent"), exist_ok=True)
    # activate_managed_agent() only skips dependency activation when the agent
    # dir carries run_agent.py (a deliberate browser-only stub); an empty dir
    # falls through to real agent-checkout discovery + its relaunch machinery.
    with open(os.path.join(state_dir, "no-agent", "run_agent.py"), "w") as f:
        f.write('"""Browser-only agent stub (no bootstrap layer)."""\n')

    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
        # Every path or profile the server reads from HERMES_* comes from the
        # disposable state below; an inherited override (for example
        # HERMES_WEBUI_SETTINGS_FILE) would point this server at the caller's
        # real settings, attachments or plugins.
        elif k.startswith("HERMES_"):
            env.pop(k, None)
    for k in ("API_SERVER_KEY", "HERMES_WEBUI_PASSWORD", "HERMES_WEBUI_EXTENSION_DIR",
              "HERMES_WEBUI_EXTENSION_MANIFEST"):
        env.pop(k, None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env.update({
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": os.path.join(state_dir, "webui-state"),
        "HERMES_HOME": os.path.join(state_dir, "hermes-home"),
        "HERMES_BASE_HOME": os.path.join(state_dir, "hermes-home"),
        "HERMES_CONFIG_PATH": os.path.join(state_dir, "hermes-home", "config.yaml"),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": workspace,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(base):
            proc.terminate()
            log.close()
            print("SETUP FAIL: server did not become healthy; log tail:", file=sys.stderr)
            print(open(os.path.join(state_dir, "server.log")).read()[-2000:], file=sys.stderr)
            return 2

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(base, wait_until="domcontentloaded")
            for _ in range(150):
                try:
                    if page.evaluate("(typeof S!=='undefined') && S._bootReady===true && typeof loadSession==='function'"):
                        break
                except Exception:
                    pass
                page.wait_for_timeout(200)
            else:
                print("SETUP FAIL: page never reached boot-ready", file=sys.stderr)
                return 2

            new_sess = page.evaluate(
                "async () => api('/api/session/new', {method:'POST', body: JSON.stringify({})})")
            sid = (new_sess.get("session") or {}).get("session_id") if isinstance(new_sess, dict) else None
            if not sid:
                print(f"SETUP FAIL: /api/session/new returned no session_id: {json.dumps(new_sess)[:200]}", file=sys.stderr)
                return 2
            page.evaluate("async (sid) => loadSession(sid)", sid)
            page.wait_for_timeout(1200)

            artifact_dir = os.path.join(state_dir, "artifacts")
            os.makedirs(artifact_dir, exist_ok=True)
            print(f"artifacts (screenshots): {artifact_dir}")
            for width, height, label in VIEWPORTS:
                _probe_width(page, width, height, label, __import__("pathlib").Path(artifact_dir))
                _probe_menu_clamp(page, width, height, label)
            _probe_menu_scroll_fallback(page, "tiny")
            _probe_fine_pointer_rows_compact(page)
            _probe_touch_menu_targets(browser, base, sid)
            _probe_root_menu_short_touch(browser, base, sid)
            browser.close()
        print("PASS: workspace download placement (context menu) + row geometry hold at both widths")
        return 0
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
