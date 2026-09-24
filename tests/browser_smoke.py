#!/usr/bin/env python3
"""
Headless browser smoke test — the console-error page-load gate.

WHY THIS EXISTS
  `node --check`, ESLint, and the (mocked) pytest suite cannot see the class of
  bug that has actually bricked releases: JavaScript that parses fine but throws
  at *runtime* when a real browser executes the page. Examples that shipped:
    - a `const` reassigned at runtime (v0.51.168 "Failed to load conversation
      messages" — #3162)
    - a `function X(){}` colliding with a `window.X = {}` in classic scripts
      (#2715 / #2771)
  Every one of those throws on load or first interaction and produces a blank or
  broken page for *every* user. This smoke boots the real server.py and loads
  the key pages in headless Chromium, failing if ANY uncaught exception or
  console error fires.

SCOPE
  Deliberately AGENT-FREE so it runs in CI (which does not install hermes-agent):
  it verifies the page loads and its JS initializes cleanly — it does NOT drive a
  full chat (that needs the agent + mock provider and runs in the private QA
  harness's golden-path E2E). This is the "does the app even come up without
  throwing" gate, which is the highest-frequency brick class.

USAGE
  python tests/browser_smoke.py
  (Requires: playwright + chromium. Boots server.py on an ephemeral port with an
  isolated temp state dir and no agent.)

EXIT CODES
  0 — all pages loaded with zero console errors / uncaught exceptions
  1 — a console error or uncaught exception was detected (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

PORT = int(os.getenv("SMOKE_PORT", "8796"))
BASE = f"http://127.0.0.1:{PORT}"

# Pages that must load cleanly. Hash routes are how the SPA exposes views.
PAGES = [
    "/",
    "/#settings",
    "/#sessions",
]

# Known-benign console noise (extend deliberately, each with a reason). Every
# entry here is a blind spot, so keep the list short.
BENIGN = [
    "favicon",          # favicon 404 in bare env — not app code
    "manifest.json",    # PWA manifest probe under headless http
    "serviceworker",    # SW registration noise under headless http
    "sw.js",            # service worker fetch noise
    "the server responded with a status of 404",  # static asset 404 in bare env
]


def _is_benign(text):
    t = text.lower()
    return any(p.lower() in t for p in BENIGN)


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


def _check_new_chat_middle_click(browser):
    """Chromium must open the native link in a new tab without touching this chat."""
    ctx = browser.new_context(base_url=BASE)
    try:
        page = ctx.new_page()
        page.goto("/", wait_until="load")
        page.locator("#btnNewChat").click()
        page.wait_for_url("**/session/*", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#btnNewChat').getAttribute('aria-disabled') !== 'true'",
            timeout=15000,
        )
        original_url = page.url
        original_id = original_url.split("/session/", 1)[1].split("?", 1)[0]
        page.locator("#msg").fill("Draft stays in the original tab")

        with ctx.expect_page(timeout=5000) as opened:
            page.locator("#btnNewChat").click(button="middle")
        new_page = opened.value
        new_page.wait_for_url("**/session/*", timeout=15000)
        new_id = new_page.url.split("/session/", 1)[1].split("?", 1)[0]
        assert new_id != original_id, "middle click reopened the same session"
        assert page.url == original_url, "middle click navigated the original tab"
        assert page.locator("#msg").input_value() == "Draft stays in the original tab"
        assert new_page.locator("#msg").input_value() == "", "draft leaked into the new tab"

        # An ordinary click on a reusable empty conversation must still focus
        # the composer instead of navigating or discarding its unsent draft.
        page.locator("#btnNewChat").click()
        assert page.url == original_url, "ordinary click navigated away from reusable chat"
        assert page.locator("#msg").input_value() == "Draft stays in the original tab"
        page.locator("#btnNewChat").focus()
        page.keyboard.press("Enter")
        assert page.url == original_url, "keyboard activation navigated away from reusable chat"
        assert page.locator("#msg").input_value() == "Draft stays in the original tab"

        # The middle-click tab must finish booting into its own new conversation
        # with creation no longer pending.
        new_page.wait_for_function(
            "previousId => typeof S !== 'undefined' && S._bootReady && S.session && "
            "S.session.session_id === previousId && "
            "document.querySelector('#btnNewChat').getAttribute('aria-disabled') !== 'true'",
            arg=new_id,
            timeout=15000,
        )

        # A link has no native disabled property. While creation is pending,
        # actual mouse gestures must not follow it to create another session.
        page.evaluate("_setNewSessionPending(true)")
        try:
            assert page.locator("#btnNewChat").get_attribute("aria-disabled") == "true"
            box = page.locator("#btnNewChat").bounding_box()
            assert box, "new conversation link is not visible"
            pages_before = len(ctx.pages)
            page.mouse.click(box["x"] + box["width"] / 2,
                             box["y"] + box["height"] / 2, button="middle")
            page.wait_for_timeout(250)
            assert len(ctx.pages) == pages_before, "middle click bypassed pending state"
            assert page.url == original_url
        finally:
            page.evaluate("_setNewSessionPending(false)")
    finally:
        ctx.close()


def _check_nonreusable_left_click(browser):
    """With a non-empty chat, a normal left click must still create a session here.

    Runs in its own single-tab context so it verifies the link's left-click
    behavior rather than cross-tab stream timing.
    """
    ctx = browser.new_context(base_url=BASE)
    try:
        page = ctx.new_page()
        page.goto("/", wait_until="load")
        page.locator("#btnNewChat").click()
        page.wait_for_url("**/session/*", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#btnNewChat').getAttribute('aria-disabled') !== 'true'",
            timeout=15000,
        )
        first_id = page.url.split("/session/", 1)[1].split("?", 1)[0]
        # Make the chat non-empty so the reusable-empty shortcut is bypassed.
        page.evaluate("S.session.message_count=1")
        page.locator("#btnNewChat").click()
        page.wait_for_function(
            "previousId => S.session && S.session.session_id !== previousId",
            arg=first_id,
            timeout=15000,
        )
        second_id = page.url.split("/session/", 1)[1].split("?", 1)[0]
        assert second_id != first_id, "left click did not create a new session"
        assert "action=new-chat" not in page.url, "ordinary click followed the native link"
    finally:
        ctx.close()


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

    state_dir = tempfile.mkdtemp(prefix="hermes-browser-smoke-")
    env = os.environ.copy()
    # Strip real provider keys so nothing leaks into the smoke server.
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
        # Point agent discovery at a path that doesn't exist — the server is
        # designed to boot and serve the UI even when the agent is absent.
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
            for path in PAGES:
                ctx = browser.new_context(base_url=BASE)
                page = ctx.new_page()
                errors = []
                page.on("console", lambda m: errors.append(("console", m.text))
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(("pageerror", str(e))))

                page.goto(path, wait_until="domcontentloaded")
                # Give boot.js / view init time to run and throw if it's going to.
                try:
                    page.wait_for_selector("#msg, .app, body", timeout=10000)
                except Exception:
                    pass
                time.sleep(1.5)

                meaningful = [(kind, txt) for (kind, txt) in errors if not _is_benign(txt)]
                if meaningful:
                    for kind, txt in meaningful:
                        failures.append(f"  [{path}] {kind}: {txt}")
                else:
                    print(f"OK  {path} — no console errors")
                ctx.close()
            try:
                _check_new_chat_middle_click(browser)
                print("OK  new conversation middle-click — separate tab, original draft intact")
            except Exception as exc:
                failures.append(f"  [new conversation middle-click] {exc}")
            try:
                _check_nonreusable_left_click(browser)
                print("OK  new conversation left-click — still creates a session in this tab")
            except Exception as exc:
                failures.append(f"  [new conversation left click] {exc}")
            browser.close()

        if failures:
            print("\nBROWSER SMOKE FAILED — runtime JS errors detected:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nBROWSER SMOKE PASSED — all pages loaded with zero console errors")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
