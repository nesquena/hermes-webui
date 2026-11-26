#!/usr/bin/env python3
"""
Headless browser gate: unsent composer input survives a New Chat handoff.

WHY THIS EXISTS
  Creating a New Chat is an asynchronous owner swap: POST /api/session/new is
  in flight while the previous conversation is still the visible pane. The
  composer belongs to that transition, not to whichever session id happens to
  be current when a keystroke lands. A settle that restores the destination
  draft over live input, or a pending state that simply disables the textarea,
  silently drops text the user already typed.

WHAT IT CHECKS
  With POST /api/session/new held:
  - the composer stays editable, so a click on #msg focuses it and keystrokes
    reach it (a disabled textarea refuses both);
  - the text typed before the click and the text typed while the create is
    held are both present once the created session is current;
  - a clarification answer stashed on the source chat while the create is held
    does not drop the words typed for the destination before it arrived;
  - the poll → card → expiry path preserves words typed before and after the
    card for empty and nonempty source drafts, on successful and rejected
    creates, in the visible composer and the stored draft.

SCOPE
  Agent-free: the real server.py on an ephemeral port with isolated temp state.
  No /api/chat/start is sent; only the create request is held, and every other
  session, draft and render path is the production one.

USAGE
  python tests/browser_new_chat_composer_retention.py
  (Requires: playwright + chromium.)

EXIT CODES
  0 — the composer retained the input
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

PORT = int(os.getenv("NEW_CHAT_RETENTION_PORT", "8798"))
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
    """Poll ``expression`` with page.evaluate: the app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(100)
    return False


def _check(browser):
    """Return a list of failure lines for the held-create input check."""
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
    # A current conversation with a message, so New Chat creates a new one.
    page.evaluate(
        "async () => { if (!S.session) { await newSession(); }"
        " S.messages = [{role: 'user', content: 'earlier turn'}]; }"
    )
    before = page.evaluate("S.session && S.session.session_id")

    held = []
    page.route(
        "**/api/session/new",
        lambda route: held.append(route)
        if route.request.url.split("?")[0].endswith("/api/session/new")
        else route.continue_(),
    )
    page.click("#msg")
    page.keyboard.type("alpha")
    typed_before = page.eval_on_selector("#msg", "el => el.value")
    page.click("#btnNewChat")
    deadline = time.time() + 6
    while not held and time.time() < deadline:
        page.wait_for_timeout(50)
    if not held:
        page.unroute("**/api/session/new")
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
        page.keyboard.type(" beta")
    during = page.eval_on_selector("#msg", "el => el.value")
    if can_type and during != f"{typed_before} beta":
        failures.append(
            f"  typed input did not reach the composer while pending: {during!r}"
        )

    held[0].continue_()
    page.unroute("**/api/session/new")
    deadline = time.time() + 12
    after = ""
    while time.time() < deadline:
        after = page.eval_on_selector("#msg", "el => el.value")
        sid = page.evaluate("S.session && S.session.session_id")
        if sid and sid != before:
            page.wait_for_timeout(400)
            after = page.eval_on_selector("#msg", "el => el.value")
            break
        page.wait_for_timeout(150)
    sid_after = page.evaluate("S.session && S.session.session_id")
    if not sid_after or sid_after == before:
        failures.append("  the created session never became current")
    # Unsent input typed while the create is pending must survive the settle in
    # the new chat's composer. (Where the pre-click text belongs is the separate
    # ownership check in browser_new_chat_composer_ownership.py.)
    appended = during[len(typed_before):] if during.startswith(typed_before) else during
    if can_type and after != appended:
        failures.append(
            f"  unsent input was not retained across the handoff: "
            f"expected {appended!r}, got {after!r}"
        )
    for err in errors:
        failures.append(f"  pageerror: {err}")
    if not failures:
        print(
            "OK  composer retention — "
            + json.dumps({"during": during, "after": after})
        )
    ctx.close()
    return failures


def _read_composer_draft(page, sid):
    return page.evaluate(
        """async (sid) => {
          const data = await api('/api/session?session_id=' + encodeURIComponent(sid)
            + '&messages=0&resolve_model=0');
          return (((data || {}).session || {}).composer_draft || {}).text || '';
        }""",
        sid,
    )


def _check_clarification_expiry(browser, source_text, pre_type, reject):
    """Poll → card → expiry while a New Chat create is held.

    The clarification answer belongs to the source chat. Words typed before and
    after the card belong to the destination and must survive a successful
    settle; a rejected create must restore the source composer with the rescued
    answer exactly once. Each case asserts the visible textarea and the stored
    composer draft.
    """
    label = f"source={source_text!r} pre_type={pre_type} reject={reject}"
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
        return [f"  the app did not initialize ({label})"]
    page.wait_for_timeout(1200)
    page.evaluate(
        "async () => { if (!S.session) { await newSession(); }"
        " S.messages = [{role: 'user', content: 'earlier turn'}]; }"
    )
    before = page.evaluate("S.session && S.session.session_id")

    # Type the source chat's own draft before New Chat starts.
    if source_text:
        page.click("#msg")
        page.keyboard.type(source_text)

    held = []
    page.route(
        "**/api/session/new",
        lambda route: held.append(route)
        if route.request.url.split("?")[0].endswith("/api/session/new")
        else route.continue_(),
    )
    page.click("#btnNewChat")
    deadline = time.time() + 6
    while not held and time.time() < deadline:
        page.wait_for_timeout(50)
    if not held:
        ctx.close()
        return [f"  the create request was never issued ({label})"]

    # The composer stays editable during the handoff: words typed before the
    # card arrives belong to the destination.
    if pre_type:
        page.eval_on_selector(
            "#msg",
            "el => { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }",
        )
        page.keyboard.type(" beta")

    # Real poll path: /api/clarify/pending returns a live prompt, then null so
    # the production expiry handler runs and stashes the typed answer.
    poll_state = {"pending": True}

    def _poll(route):
        if poll_state["pending"]:
            now = int(time.time())
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "pending": {
                        "clarify_id": "retention-expiry",
                        "question": "Which option?",
                        "choices": [],
                        "requested_at": now,
                        "timeout_seconds": 3600,
                        "expires_at": now + 3600,
                    }
                }),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"pending": None}),
            )

    page.route("**/api/clarify/pending*", _poll)
    page.evaluate(
        "sid => { if (typeof startClarifyPolling === 'function') startClarifyPolling(sid); }",
        before,
    )
    card_visible = _wait_until(
        page,
        "(() => { const card = document.getElementById('clarifyCard');"
        " return !!(card && card.classList.contains('visible')); })()",
        8000,
    )
    if not card_visible:
        failures.append(f"  the clarification card never appeared ({label})")
    else:
        page.click("#clarifyInput")
        page.keyboard.type("answer")
        poll_state["pending"] = False
        if not _wait_until(
            page,
            "(() => { const el = document.getElementById('msg');"
            " return !!(el && el.value.includes('answer')); })()",
            9000,
        ):
            failures.append(
                f"  the clarification expiry never reached the composer ({label})"
            )
    after_clarify = page.eval_on_selector("#msg", "el => el.value")
    if pre_type and " beta" not in after_clarify:
        failures.append(
            f"  the clarification expiry dropped the pre-expiry input: "
            f"{after_clarify!r} ({label})"
        )

    page.eval_on_selector(
        "#msg",
        "el => { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }",
    )
    page.keyboard.type(" gamma")

    expected = (" beta" if pre_type else "") + " gamma"
    if reject:
        if source_text:
            expected = source_text + "\n\n" + "answer" + expected
        else:
            expected = "answer" + expected
        kept = held[0]
        kept.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps({"error": "create failed"}),
        )
        settled = _wait_until(
            page,
            f"(() => {{ const el = document.getElementById('msg');"
            f" return !!el && el.value === {json.dumps(expected)}; }})()",
            6000,
        )
        if not settled:
            after = page.eval_on_selector("#msg", "el => el.value")
            failures.append(
                f"  the rejected create did not restore the source composer: "
                f"expected {expected!r}, got {after!r} ({label})"
            )
        else:
            after = page.eval_on_selector("#msg", "el => el.value")
        deadline = time.time() + 5
        draft = ""
        while time.time() < deadline:
            draft = _read_composer_draft(page, before)
            if draft == expected:
                break
            page.wait_for_timeout(200)
        if draft != expected:
            failures.append(
                f"  the source draft lost or duplicated the rescued answer: "
                f"expected {expected!r}, got {draft!r} ({label})"
            )
        sid_after = page.evaluate("S.session && S.session.session_id")
        if sid_after != before:
            failures.append(
                f"  a rejected create left another session current: {sid_after!r} ({label})"
            )
    else:
        held[0].continue_()
        deadline = time.time() + 12
        after = ""
        sid_after = before
        while time.time() < deadline:
            sid_after = page.evaluate("S.session && S.session.session_id")
            if sid_after and sid_after != before:
                page.wait_for_timeout(400)
                after = page.eval_on_selector("#msg", "el => el.value")
                break
            page.wait_for_timeout(150)
        if not sid_after or sid_after == before:
            failures.append(
                f"  the created session never became current ({label})"
            )
        if after != expected:
            failures.append(
                f"  the successful handoff lost typed words: "
                f"expected {expected!r}, got {after!r} ({label})"
            )
        deadline = time.time() + 5
        draft = ""
        while time.time() < deadline:
            draft = _read_composer_draft(page, sid_after)
            if draft == expected:
                break
            page.wait_for_timeout(200)
        if draft != expected:
            failures.append(
                f"  the destination draft lost typed words: "
                f"expected {expected!r}, got {draft!r} ({label})"
            )

    for err in errors:
        # A deliberately rejected create surfaces its fetch error as an
        # unhandled click-handler rejection; that is the injected failure, not
        # a composer regression.
        if reject and err == "create failed":
            continue
        failures.append(f"  pageerror: {err} ({label})")
    if not failures:
        print(
            "OK  clarification expiry — "
            + json.dumps({
                "source": source_text,
                "pre_type": pre_type,
                "reject": reject,
                "during": after_clarify,
                "after": after,
            })
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
    state_dir = tempfile.mkdtemp(prefix="hermes-new-chat-retention-")
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
            for source_text in ("", "alpha"):
                for pre_type in (True, False):
                    for reject in (True, False):
                        failures += _check_clarification_expiry(
                            browser, source_text, pre_type, reject
                        )
            browser.close()

        if failures:
            print("\nNEW CHAT COMPOSER RETENTION FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nNEW CHAT COMPOSER RETENTION PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
