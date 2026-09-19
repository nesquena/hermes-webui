#!/usr/bin/env python3
"""
Headless browser regression test — mobile rightpanel (workspace/artifact)
dismiss scrim.

WHY THIS EXISTS
  On a phone the workspace/artifact panel is a 300px slide-over. Before the fix
  it had no dimming scrim behind it (unlike the left sidebar), so an open
  artifact just floated over the chat with no visual "this is a layer" cue and
  no obvious way to get back — users reported they "cannot close the artifact
  and come back to the PWA." The fix adds a `#mobileRightpanelOverlay` scrim
  that appears at phone width (<=640px) and dismisses the panel on tap.

  This test drives the real behavior in headless Chromium (not just "page
  loads"): it confirms the scrim appears when the panel opens at phone width,
  that tapping the scrim closes the panel, and that the scrim stays hidden at
  desktop width (where the panel is a normal column, not a slide-over).

SCOPE
  AGENT-FREE, like tests/browser_smoke.py: boots server.py on an ephemeral port
  with an isolated temp state dir and no agent. Not collected by pytest (no
  `test_` prefix) — run it directly:

    python tests/browser_smoke_overlay.py
  (Requires: playwright + chromium.)

EXIT CODES
  0 — scrim behavior correct at phone and desktop widths
  1 — a behavior assertion failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

PORT = int(os.getenv("SMOKE_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"


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

    state_dir = tempfile.mkdtemp(prefix="hermes-overlay-smoke-")
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
    failures = []
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            # ---- PHONE WIDTH (390x844, iPhone) ----
            ctx = browser.new_context(viewport={"width": 390, "height": 844})
            page = ctx.new_page()
            page.goto(BASE + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(800)

            st = page.evaluate("""() => {
                const o = document.getElementById('mobileRightpanelOverlay');
                const p = document.querySelector('.rightpanel');
                return {
                    overlayExists: !!o,
                    overlayVisible: o ? o.classList.contains('visible') : null,
                    panelMobileOpen: p ? p.classList.contains('mobile-open') : null,
                };
            }""")
            print("phone initial:", st)
            if not st["overlayExists"]:
                failures.append("phone: #mobileRightpanelOverlay element missing")
            if st["overlayVisible"]:
                failures.append("phone: overlay visible while panel closed")
            if st["panelMobileOpen"]:
                failures.append("phone: panel open while it should be closed")

            page.evaluate("() => { if (typeof toggleWorkspacePanel==='function') toggleWorkspacePanel(true); }")
            page.wait_for_timeout(400)
            st = page.evaluate("""() => {
                const o = document.getElementById('mobileRightpanelOverlay');
                const p = document.querySelector('.rightpanel');
                return {
                    overlayVisible: o ? o.classList.contains('visible') : null,
                    panelMobileOpen: p ? p.classList.contains('mobile-open') : null,
                };
            }""")
            print("phone after open:", st)
            if not st["overlayVisible"]:
                failures.append("phone: overlay NOT visible after opening panel")
            if not st["panelMobileOpen"]:
                failures.append("phone: panel NOT mobile-open after opening")

            page.evaluate("""() => {
                const o = document.getElementById('mobileRightpanelOverlay');
                if (o) o.click();
            }""")
            page.wait_for_timeout(400)
            st = page.evaluate("""() => {
                const o = document.getElementById('mobileRightpanelOverlay');
                const p = document.querySelector('.rightpanel');
                return {
                    overlayVisible: o ? o.classList.contains('visible') : null,
                    panelMobileOpen: p ? p.classList.contains('mobile-open') : null,
                };
            }""")
            print("phone after scrim tap:", st)
            if st["overlayVisible"]:
                failures.append("phone: overlay still visible after scrim tap")
            if st["panelMobileOpen"]:
                failures.append("phone: panel still open after scrim tap")

            ctx.close()

            # ---- DESKTOP WIDTH (1280x800) ----
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(BASE + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            page.evaluate("() => { if (typeof toggleWorkspacePanel==='function') toggleWorkspacePanel(true); }")
            page.wait_for_timeout(400)
            st = page.evaluate("""() => {
                const o = document.getElementById('mobileRightpanelOverlay');
                const p = document.querySelector('.rightpanel');
                return {
                    overlayVisible: o ? o.classList.contains('visible') : null,
                    panelMobileOpen: p ? p.classList.contains('mobile-open') : null,
                };
            }""")
            print("desktop after open:", st)
            if st["overlayVisible"]:
                failures.append("desktop: overlay visible but should be hidden at desktop width")
            if st["panelMobileOpen"]:
                failures.append("desktop: panel mobile-open but should be a normal column")

            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()

    if failures:
        print("\nOVERLAY SMOKE FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nOVERLAY SMOKE PASSED — scrim shows at phone width, tap dismisses, hidden on desktop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
