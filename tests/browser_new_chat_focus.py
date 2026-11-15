#!/usr/bin/env python3
"""
Headless browser gate: a fresh chat focuses the composer without waiting for the
session list (#7936, /new in #7996, and the first message in #8004).

WHY THIS EXISTS
  `newSession()` already schedules the sidebar refresh in the background. The
  New Chat button and Cmd/Ctrl+K also awaited a second `renderSessionList()`
  before focusing the composer, and the render queue runs that second refresh
  only after the first one's `/api/sessions` and `/api/projects` reads finish.
  On a long session list that held the composer for seconds.

WHAT IT CHECKS, for the button, Cmd/Ctrl+K, the /new command, and sending the
first message with no conversation open
  - with every `/api/sessions` response held, the composer is focused (for /new,
    its toast is shown; for the first message, `POST /api/chat/start` is sent)
    and the new conversation is current;
  - one fresh-chat action issues one session-list read;
  - once the held reads are released, the new conversation's sidebar row appears.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state. Without an agent nothing can send a message, so
  the current conversation is marked as having one in page state; an empty
  current conversation makes New Chat reuse it instead of creating another (#1171).

USAGE
  python tests/browser_new_chat_focus.py
  (Requires: playwright + chromium.)

EXIT CODES
  0 — every path passed
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

PORT = int(os.getenv("NEW_CHAT_FOCUS_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"
FOCUS_TIMEOUT_MS = 5000
SETTLE_SECONDS = 1.5


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


def _is_session_list(url):
    return urlsplit(url).path == "/api/sessions"


def _wait_until(page, expression, timeout_ms):
    """Poll ``expression`` with page.evaluate. The app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(100)
    return False


def _start_fresh_chat(page, trigger):
    """Start a fresh chat via ``trigger``; return failure lines for the driver itself."""
    if trigger == "button":
        page.click("#btnNewChat")
    elif trigger == "slash":
        # This agent-free gate checks /new and session focus, not installed skills.
        # Give the optional skills loader a valid empty result before autocomplete
        # warms, so a missing skills module cannot reopen the dropdown between Enters.
        page.route(
            "**/api/skills",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body='{"skills":[]}'
            ),
        )
        page.evaluate(
            "() => Promise.all([loadSkillCommands(), loadBundleCommands(),"
            " loadAgentCommandMetadata()])"
        )
        # As typed: the first Enter takes the open autocomplete, the second sends.
        page.click("#msg")
        page.keyboard.type("/new")
        if not _wait_until(page, "!!document.querySelector('.cmd-dropdown.open')", FOCUS_TIMEOUT_MS):
            return ["  [slash] the /new autocomplete never opened"]
        page.keyboard.press("Enter")
        if not _wait_until(page, "!document.querySelector('.cmd-dropdown.open')", FOCUS_TIMEOUT_MS):
            return ["  [slash] the autocomplete did not close after taking /new"]
        page.keyboard.press("Enter")
    elif trigger == "send":
        page.click("#msg")
        page.keyboard.type("hello there")
        page.keyboard.press("Enter")
    else:
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.keyboard.press("Meta+k" if sys.platform == "darwin" else "Control+k")
    return []


def _done(trigger):
    """What the trigger shows once it has finished. /new is typed in the composer,
    so focus alone proves nothing there: its toast comes after the same await."""
    focused = "!!(document.activeElement && document.activeElement.id === 'msg')"
    if trigger == "slash":
        return focused + " && $('toast').dataset.toastMessage === t('new_session')"
    return focused


def _check(browser, trigger):
    """Return a list of failure lines for one fresh-chat trigger."""
    failures = []
    ctx = browser.new_context(base_url=BASE)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    if not _wait_until(page, "typeof S !== 'undefined' && typeof newSession === 'function'", 15000):
        ctx.close()
        return [f"  [{trigger}] the app did not initialize"]
    time.sleep(SETTLE_SECONDS)

    if trigger == "send":
        # A fresh page with no conversation open: the first message creates one.
        page.evaluate("() => { S.session = null; S.messages = []; }")
    else:
        # A current conversation with a message, so New Chat creates a new one.
        page.evaluate(
            "async () => { if (!S.session) { await newSession(); }"
            " S.messages = [{role: 'user', content: 'earlier turn'}]; }"
        )
    before = page.evaluate("S.session && S.session.session_id")
    page.evaluate("document.activeElement && document.activeElement.blur()")
    time.sleep(SETTLE_SECONDS)

    held = []
    page.route("**/api/sessions*", lambda route: held.append(route)
               if _is_session_list(route.request.url) else route.continue_())
    chat_starts = []
    page.on("request", lambda r: chat_starts.append(r.url)
            if r.method == "POST" and urlsplit(r.url).path == "/api/chat/start" else None)

    driver_failures = _start_fresh_chat(page, trigger)
    reads_before_done = None
    if driver_failures:
        finished = True
        failures.extend(driver_failures)
    elif trigger == "send":
        deadline = time.time() + FOCUS_TIMEOUT_MS / 1000
        while not chat_starts and time.time() < deadline:
            page.wait_for_timeout(100)
        finished = bool(chat_starts)
        # The send's own stream refreshes the list again afterwards; what matters
        # is what was read before the message went out.
        reads_before_done = len(held)
    else:
        finished = _wait_until(page, _done(trigger), FOCUS_TIMEOUT_MS)
    if not finished:
        what = {"slash": "composer focused, toast shown", "send": "message sent"}.get(
            trigger, "composer focused"
        )
        failures.append(
            f"  [{trigger}] not finished ({what}) within {FOCUS_TIMEOUT_MS} ms "
            f"while the session list was held ({len(held)} list read(s) held)"
        )
    after = page.evaluate("S.session && S.session.session_id")
    if not after or after == before:
        failures.append(f"  [{trigger}] no new conversation is current (before={before}, after={after})")

    released = 0
    deadline = time.time() + 10
    while time.time() < deadline:
        while released < len(held):
            held[released].continue_()
            released += 1
        page.wait_for_timeout(int(SETTLE_SECONDS * 1000))
        if released == len(held):
            break
    page.unroute("**/api/sessions*")

    reads = len(held) if reads_before_done is None else reads_before_done
    if reads != 1:
        failures.append(f"  [{trigger}] {reads} session-list reads for one fresh chat, expected 1")
    if after:
        try:
            page.wait_for_selector(f'[data-sid="{after}"]', timeout=FOCUS_TIMEOUT_MS)
        except Exception:
            failures.append(f"  [{trigger}] the new conversation's sidebar row did not appear")
    for err in errors:
        failures.append(f"  [{trigger}] pageerror: {err}")
    if not failures:
        print(f"OK  {trigger} — done with the list held, {reads} list read, row shown")
    ctx.close()
    return failures


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-new-chat-focus-")
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
            for trigger in ("button", "shortcut", "slash", "send"):
                failures.extend(_check(browser, trigger))
            browser.close()

        if failures:
            print("\nNEW CHAT FOCUS FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nNEW CHAT FOCUS PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
