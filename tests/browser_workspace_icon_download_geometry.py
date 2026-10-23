#!/usr/bin/env python3
"""
Real-browser gate — workspace tree icon download/preview split + row geometry.

WHY THIS EXISTS
  The node-VM tests in test_workspace_tree_icon_download.py execute the icon
  handler block with a mocked DOM, so they cannot see layout or real event
  dispatch. Two review findings on #7810 (2026-10-06) live exactly there:

    1. `.file-icon-download { min-width:44px; min-height:44px }` inflated the
       layout box: PDF/ZIP rows rendered 56px tall next to 31px for every
       other file, and their names shifted 30px right — at desktop AND mobile
       widths. The fix keeps the icon's layout box identical to `.file-icon`;
       this gate proves row height and name alignment are uniform again.
    2. The icon's `onkeydown` called `downloadFile` on every keydown,
       including auto-repeats: one held Space (press + repeats) started one
       download per repeat event. The fix returns early on `e.repeat` while
       still `preventDefault()`-ing repeats; this gate proves a held Space
       downloads once and a fresh press after release downloads again.

  It also proves the core contract in a real page at both widths: clicking a
  download-glyph icon downloads (never previews), clicking the file NAME
  previews (never downloads, #1707 debounce), and a non-download-glyph icon
  (.md) still bubbles to the row's preview handler.

SCOPE
  AGENT-FREE like browser_smoke.py: boots the real server.py on an ephemeral
  port with an isolated temp state dir, seeds a workspace, creates a session
  via /api/session/new, and loads it with the app's own loadSession().

USAGE
  python tests/browser_workspace_icon_download_geometry.py

EXIT CODES
  0 — geometry + behavior contracts hold at 1920x1080 and 390x844
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

# Installs call recorders over the sibling-script globals the handlers resolve
# at call time, then reads back row geometry.
ROW_GEOMETRY_JS = """
() => {
  window.__dl = []; window.__open = [];
  if (typeof downloadFile === 'function') {
    window.downloadFile = (p) => { window.__dl.push(String(p)); };
  }
  if (typeof openFile === 'function') {
    window.openFile = function () { window.__open.push(Array.from(arguments).map(String)); };
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
CLICK_NAME_JS = "() => { const row = " + ROW_FOR % "'%s'" + """;
  const nameEl = row.querySelector('.file-name') || row.querySelector('[class*="name"]');
  nameEl.click(); }"""
KEY_JS = """() => { const icon = """ + ROW_FOR % "'%s'" + """.querySelector('.file-icon');
  icon.focus();
  icon.dispatchEvent(new KeyboardEvent('keydown', {key:' ', repeat: %s, bubbles:true, cancelable:true})); }"""


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
        f"(the download icon must not inflate its row)")
    assert pdf["nameLeft"] == md["nameLeft"] == zipped["nameLeft"], (
        f"{label}: name alignment diverges — pdf={pdf['nameLeft']} md={md['nameLeft']} zip={zipped['nameLeft']}")
    assert "file-icon-download" in pdf["iconClass"], f"{label}: pdf icon missing download glyph class: {pdf['iconClass']}"
    assert "file-icon-download" in zipped["iconClass"], f"{label}: zip icon missing download glyph class: {zipped['iconClass']}"
    assert "file-icon-download" not in md["iconClass"], f"{label}: md icon must stay a plain file-type glyph: {md['iconClass']}"

    # PDF icon click → exactly one download, no preview.
    page.evaluate(CLICK_ICON_JS % "receipt.pdf")
    dl = page.evaluate("window.__dl.slice()")
    opens = page.evaluate("window.__open.length")
    assert len(dl) == 1 and dl[0].endswith("receipt.pdf"), f"{label}: pdf icon click must download once; got {dl}"
    assert opens == 0, f"{label}: pdf icon click must not preview; open calls={opens}"

    # PDF filename click → preview (after the #1707 300ms debounce), no download.
    page.evaluate(CLICK_NAME_JS % "receipt.pdf")
    page.wait_for_timeout(600)
    dl = page.evaluate("window.__dl.length")
    opens = page.evaluate("window.__open.length")
    assert dl == 1, f"{label}: filename click must not download; dl={dl}"
    assert opens >= 1, f"{label}: filename click must preview; open calls={opens}"

    # .md icon click → no download handler; bubbles to the row's preview.
    page.evaluate(CLICK_ICON_JS % "notes.md")
    dl = page.evaluate("window.__dl.length")
    opens = page.evaluate("window.__open.length")
    assert dl == 1, f"{label}: md icon click must not download; dl={dl}"
    assert opens >= 2, f"{label}: md icon click must bubble to preview; open calls={opens}"

    # Held Space: press + 3 auto-repeats → exactly one download from the keys
    # (repeats suppressed); a fresh press after release downloads again.
    page.evaluate(KEY_JS % ("receipt.pdf", "false"))
    page.evaluate(KEY_JS % ("receipt.pdf", "true"))
    page.evaluate(KEY_JS % ("receipt.pdf", "true"))
    page.evaluate(KEY_JS % ("receipt.pdf", "true"))
    dl = page.evaluate("window.__dl.length")
    # dl = icon click + filename-independent key press + initial icon click…
    # precisely: [icon click, press] → 2; leaked repeats would make it 5.
    assert dl == 2, (
        f"{label}: held Space must download once per press, not per auto-repeat; "
        f"dl after press+3 repeats = {dl} (expected 2: icon click + press)")
    page.evaluate(KEY_JS % ("receipt.pdf", "false"))
    dl = page.evaluate("window.__dl.length")
    assert dl == 3, f"{label}: fresh press after release must download again; dl={dl}"
    print(f"  [{label}] row height uniform ({pdf['rowH']}px), names aligned, "
          f"icon→download, name→preview, held-Space→1 download: OK")


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
        print("PASS: workspace icon download/preview split + row geometry hold at both widths")
        return 0
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
