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
