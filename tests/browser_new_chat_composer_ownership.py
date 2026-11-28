#!/usr/bin/env python3
"""
Headless browser gate: New Chat keeps each chat's unsent draft with its chat.

WHY THIS EXISTS
  The composer is not session-scoped by itself: it is one textarea that every
  conversation transition repaints. Before the ownership transaction, typing a
  draft and clicking New Chat left that draft in the textarea, so the new
  conversation appeared to own text the user wrote for the previous one. This
  gate pins the behavioural contract that the ownership work exists for: the
  destination starts with its own draft, and the source's draft is still on the
  server for the source.

WHAT IT CHECKS
  With an ordinary New Chat from a conversation whose composer holds a draft:
  - the created conversation's composer does not contain the source draft;
  - the source conversation's stored composer draft still holds it.

SCOPE
  Agent-free: the real server.py on an ephemeral port with isolated temp state.
  No /api/chat/start is sent; create, draft persistence and rendering are the
  production paths.

USAGE
  python tests/browser_new_chat_composer_ownership.py
  (Requires: playwright + chromium.)

EXIT CODES
  0 — the draft stayed with its chat
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

PORT = int(os.getenv("NEW_CHAT_OWNERSHIP_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"
DRAFT = "draft for the first chat"


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
    failures = []
    ctx = browser.new_context(base_url=BASE)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    if not _wait_until(
        page, "typeof S !== 'undefined' && typeof newSession === 'function'", 15000
    ):
        ctx.close()
        return ["  the app did not initialize"]
    page.wait_for_timeout(1200)
    page.evaluate(
        "async () => { if (!S.session) { await newSession(); }"
        " S.messages = [{role: 'user', content: 'earlier turn'}]; }"
    )
    source_sid = page.evaluate("S.session && S.session.session_id")

    page.click("#msg")
    page.keyboard.type(DRAFT)
    if page.eval_on_selector("#msg", "el => el.value") != DRAFT:
        ctx.close()
        return ["  the draft never reached the composer"]
    page.click("#btnNewChat")
    deadline = time.time() + 12
    destination = None
    while time.time() < deadline:
        destination = page.evaluate("S.session && S.session.session_id")
        if destination and destination != source_sid:
            page.wait_for_timeout(500)
            break
        page.wait_for_timeout(150)
    if not destination or destination == source_sid:
        ctx.close()
        return ["  the created conversation never became current"]

    destination_value = page.eval_on_selector("#msg", "el => el.value")
    if DRAFT in destination_value:
        failures.append(
            "  the source chat's draft leaked into the new conversation: "
            f"{destination_value!r}"
        )
    source_draft = page.evaluate(
        "async (sid) => { const r = await fetch("
        "`/api/session?session_id=${sid}&messages=0&resolve_model=0`);"
        " const d = await r.json();"
        " return (d.session && d.session.composer_draft) || null; }",
        source_sid,
    )
    source_text = (source_draft or {}).get("text") or ""
    if DRAFT not in source_text:
        failures.append(
            f"  the source chat's stored draft lost its text: {source_text!r}"
        )
    for err in errors:
        failures.append(f"  pageerror: {err}")
    if not failures:
        print(
            "OK  composer ownership — "
            + json.dumps({"destination": destination_value, "source_draft": source_text})
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
    state_dir = tempfile.mkdtemp(prefix="hermes-new-chat-ownership-")
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
            print("\nNEW CHAT COMPOSER OWNERSHIP FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nNEW CHAT COMPOSER OWNERSHIP PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
