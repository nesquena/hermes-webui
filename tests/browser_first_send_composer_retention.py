#!/usr/bin/env python3
"""
Headless browser gate: input typed during the first Send survives the create.

WHY THIS EXISTS
  The first Send on an empty app creates the conversation and submits the
  message in one call. POST /api/session/new is in flight while the composer
  stays editable; text typed during that window is newer than the message
  snapshot the send took, so the hand-off must keep it in the created
  conversation's composer instead of clearing it with the send that preceded
  it.

WHAT IT CHECKS
  With POST /api/session/new held and "hello" submitted:
  - the composer accepts " world" while the create is pending;
  - after the created conversation is current, the composer still holds
    " world" (the submitted snapshot is not resurrected, the newer text is not
    cleared);
  - the message the send actually posts is "hello", not "hello world".

SCOPE
  Agent-free: the real server.py on an ephemeral port with isolated temp state.
  /api/chat/start is answered locally by the test (the send path up to and
  including the create is the production one) so no agent turn is started.

USAGE
  python tests/browser_first_send_composer_retention.py
  (Requires: playwright + chromium.)

EXIT CODES
  0 — the composer retained the input typed during the first Send
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = int(os.getenv("FIRST_SEND_RETENTION_PORT", "8797"))
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


def _wait_until(page, expression, timeout_ms):
    """Poll with page.evaluate: the app's CSP has no 'unsafe-eval' for
    Playwright's interval-polled wait_for_function."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(100)
    return False


def _check(browser):
    """Return a list of failure lines for the first-Send retention check."""
    failures = []
    ctx = browser.new_context(base_url=BASE)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    if not _wait_until(
        page, "typeof S !== 'undefined' && typeof send === 'function'", 15000
    ):
        ctx.close()
        return ["  the app did not initialize"]
    page.wait_for_timeout(1200)
    if not _wait_until(page, "S.session === null || !S.session", 5000):
        ctx.close()
        return ["  the app was not on the blank page (a session was already open)"]

    page.click("#msg")
    page.keyboard.type("hello")
    if page.eval_on_selector("#msg", "el => el.value") != "hello":
        ctx.close()
        return ["  the first message never reached the composer"]

    held = []
    page.route(
        "**/api/session/new",
        lambda route: held.append(route)
        if route.request.url.split("?")[0].endswith("/api/session/new")
        else route.continue_(),
    )
    started = []
    page.route(
        "**/api/chat/start",
        lambda route: (
            started.append(route.request.post_data or ""),
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"error": "gate: no agent turn"}),
            ),
        )
        if route.request.url.split("?")[0].endswith("/api/chat/start")
        else route.continue_(),
    )

    page.keyboard.press("Enter")
    deadline = time.time() + 6
    while not held and time.time() < deadline:
        page.wait_for_timeout(50)
    if not held:
        page.unroute("**/api/session/new")
        page.unroute("**/api/chat/start")
        ctx.close()
        return ["  the create request was never issued (nothing was held)"]

    if page.eval_on_selector("#msg", "el => !!el.disabled"):
        failures.append("  the composer was disabled while the create was pending")
    can_type = True
    try:
        page.click("#msg", timeout=1200)
    except Exception:
        can_type = False
        failures.append("  the composer could not be focused while the create was pending")
    if can_type:
        page.keyboard.type(" world")
    during = page.eval_on_selector("#msg", "el => el.value")
    if can_type and during != "hello world":
        failures.append(
            f"  typed input did not reach the composer while pending: {during!r}"
        )

    held[0].continue_()
    page.unroute("**/api/session/new")
    deadline = time.time() + 12
    after = ""
    created = None
    while time.time() < deadline:
        created = page.evaluate("S.session && S.session.session_id")
        if created:
            page.wait_for_timeout(600)
            after = page.eval_on_selector("#msg", "el => el.value")
            break
        page.wait_for_timeout(150)
    if not created:
        failures.append("  the created session never became current")
    if can_type and after != " world":
        failures.append(
            "  text typed during the first Send was not retained: "
            f"expected {' world'!r}, got {after!r}"
        )
    deadline = time.time() + 6
    while not started and time.time() < deadline:
        page.wait_for_timeout(50)
    messages = []
    for raw in started:
        try:
            messages.append(json.loads(raw).get("message"))
        except Exception:
            messages.append(None)
    # A first Send that never posts must fail the gate; an empty list is not a
    # pass just because the composer retained the newer text.
    if not messages:
        failures.append("  the first Send never posted to /api/chat/start")
    elif len(messages) != 1 or messages[0] != "hello":
        failures.append(f"  the first Send posted the wrong message: {messages!r}")
    page.unroute("**/api/chat/start")
    for err in errors:
        failures.append(f"  pageerror: {err}")
    if not failures:
        print(
            "OK  first-send composer retention — "
            + json.dumps({"during": during, "after": after, "posted": messages})
        )
    ctx.close()
    return failures


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_script = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-first-send-retention-")
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
    # HERMES_WEBUI_PYTHON mirrors the suite's interpreter discovery: the browser
    # driver may run on a different Python than the one that serves the app.
    server_py = os.environ.get("HERMES_WEBUI_PYTHON") or sys.executable
    proc = subprocess.Popen(
        [server_py, server_script], cwd=repo_root, env=env,
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

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            failures = _check(browser)
            browser.close()

        if failures:
            print("\nFIRST SEND COMPOSER RETENTION FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nFIRST SEND COMPOSER RETENTION PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
