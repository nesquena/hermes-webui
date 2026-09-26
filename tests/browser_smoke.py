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
import json
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


def _draft_text(sid):
    """Server-side composer draft text for the session, or None."""
    try:
        url = f"{BASE}/api/session?session_id={sid}&messages=0&resolve_model=0"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return ((data.get("session") or {}).get("composer_draft") or {}).get("text")
    except Exception:
        return None


def _wait_draft(sid, want, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _draft_text(sid) == want:
            return True
        time.sleep(0.5)
    return False


def _post_draft(sid, text):
    """Persist the session's composer draft server-side (test seeding).

    Mirrors the app's debounced save payload exactly. Used where a bare tab
    boot must keep an otherwise-empty session: a zero-message session with no
    draft is intentionally discarded, so a background tab we want to keep
    typing into needs a draft already on the server.
    """
    body = json.dumps({"session_id": sid, "text": text, "files": []}).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/session/draft", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except Exception:
        return None


def _check_new_chat_middle_click(browser):
    """The + conversation control, end to end (#7824).

    Covers: the middle-click opens the native link in a background tab without
    touching this chat; booting the empty background tab must not displace the
    owner tab's draft candidate; New Chat from another session returns to the
    owner tab and restores its persisted draft; and a draft entered in a
    background tab re-homes the candidate, which New Chat then follows.

    Every step that needs a NEW request runs with a single active tab: each tab
    holds several long-lived streams, and a fully streamed second tab exhausts
    the browser's per-host connection budget, queueing the request indefinitely
    (same discipline as _check_middle_click_from_deep_url).
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
        original_url = page.url
        original_id = original_url.split("/session/", 1)[1].split("?", 1)[0]
        page.locator("#msg").fill("Draft stays in the original tab")
        # Server-persist the draft before any tab juggling: the restore path
        # validates drafts server-side, so the assertion target must exist.
        assert _wait_draft(original_id, "Draft stays in the original tab"), (
            "the owner tab's draft did not persist"
        )

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

        # The candidate is claimed by the first nonempty draft, not by session
        # creation (#7824 review): booting the empty background tab must leave
        # the owner tab's candidate untouched.
        assert page.evaluate(
            "() => localStorage.getItem('hermes-new-chat-draft-session')"
        ) == original_id, (
            "booting the empty background tab displaced the owner tab's draft candidate"
        )
        assert new_page.evaluate(
            "() => localStorage.getItem('hermes-new-chat-draft-session')"
        ) == original_id

        # Close the background tab before the restore path runs: its streams
        # would otherwise queue the owner tab's session read indefinitely
        # (per-host connection budget).
        new_page.close()
        time.sleep(0.6)

        # New Chat from another session must return to the owner tab and
        # restore its persisted draft.
        page.evaluate("(sid) => loadSession(sid)", new_id)
        page.wait_for_function(
            "(sid) => typeof S !== 'undefined' && S.session && S.session.session_id === sid",
            arg=new_id, timeout=25000,
        )
        # Make the current chat non-reusable so New Chat runs the restore path.
        # The flag and the click go in one synchronous evaluate: a separate
        # round-trip could let a background refresh replace S.session
        # (resetting message_count to 0) before the click lands.
        page.evaluate(
            "() => { S.session.message_count = 1;"
            " document.querySelector('#btnNewChat').click(); }"
        )
        page.wait_for_function(
            "(sid) => { const t = document.querySelector('#msg');"
            " return !!t && t.value === 'Draft stays in the original tab'"
            " && typeof S !== 'undefined' && S.session && S.session.session_id === sid; }",
            arg=original_id, timeout=25000,
        )

        # Inverse case, verified explicitly: a nonempty draft entered in a
        # background tab re-homes the shared candidate to that tab — the
        # candidate is the most recent nonempty draft, and New Chat follows it.
        page.close()
        time.sleep(0.6)
        # Seed the background tab's draft so its bare deep load keeps the
        # session (zero-message sessions without a draft are discarded).
        assert _post_draft(new_id, "seed draft") == 200, "could not seed the background draft"
        assert _wait_draft(new_id, "seed draft"), "the seeded draft was not stored"
        bg = ctx.new_page()
        bg.goto(f"/session/{new_id}", wait_until="domcontentloaded")
        bg.wait_for_function(
            "(sid) => typeof S !== 'undefined' && S._bootReady && S.session && "
            "S.session.session_id === sid",
            arg=new_id, timeout=25000,
        )
        bg.locator("#msg").fill("Draft in the background tab")
        bg.wait_for_function(
            "(sid) => localStorage.getItem('hermes-new-chat-draft-session') === sid",
            arg=new_id, timeout=5000,
        )
        # The debounced save must reach the server with this tab alone active.
        assert _wait_draft(new_id, "Draft in the background tab"), (
            "the background tab's draft did not persist"
        )
        bg.close()
        time.sleep(0.6)

        # New Chat from the original session now follows the moved candidate.
        owner = ctx.new_page()
        owner.goto(f"/session/{original_id}", wait_until="domcontentloaded")
        owner.wait_for_function(
            "(sid) => typeof S !== 'undefined' && S._bootReady && S.session && "
            "S.session.session_id === sid",
            arg=original_id, timeout=25000,
        )
        owner.evaluate(
            "() => { S.session.message_count = 1;"
            " document.querySelector('#btnNewChat').click(); }"
        )
        owner.wait_for_function(
            "(sid) => { const t = document.querySelector('#msg');"
            " return !!t && t.value === 'Draft in the background tab'"
            " && typeof S !== 'undefined' && S.session && S.session.session_id === sid; }",
            arg=new_id, timeout=25000,
        )
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


def _check_middle_click_from_deep_url(browser):
    """From a directly loaded /session/<id> URL, the + gesture must open a new
    conversation whose own URL is directly restorable after a hard reload.

    Pins the base-URL contract for the middle-click gesture (#7824 review): the
    page's base URL stays at the app root on session routes, so the popup never
    builds /session/session/<id> and a reload restores the same conversation
    instead of reading "session" as the id.
    """
    ctx = browser.new_context(base_url=BASE)
    try:
        page = ctx.new_page()
        page.goto("/", wait_until="domcontentloaded")
        page.locator("#btnNewChat").click()
        page.wait_for_url("**/session/*", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#btnNewChat').getAttribute('aria-disabled') !== 'true'",
            timeout=15000,
        )
        origin_id = page.url.split("/session/", 1)[1].split("?", 1)[0]
        page.locator("#msg").fill("Draft keeps this page restorable")
        # The draft must be server-persisted before relying on the deep load:
        # a zero-message session without a draft is intentionally discarded.
        assert _wait_draft(origin_id, "Draft keeps this page restorable"), (
            "the origin draft did not persist"
        )
        # Directly load the session route, then run the gesture from there.
        page.goto(f"/session/{origin_id}", wait_until="domcontentloaded")
        page.wait_for_function(
            "(sid) => typeof S !== 'undefined' && S._bootReady && S.session && "
            "S.session.session_id === sid",
            arg=origin_id, timeout=15000,
        )
        resolved = page.evaluate("() => document.querySelector('#btnNewChat').href")
        assert "/session/" not in resolved.split("?", 1)[0], (
            f"the + link must resolve against the app root, got {resolved!r}"
        )
        with ctx.expect_page(timeout=8000) as opened:
            page.locator("#btnNewChat").click(button="middle")
        popup = opened.value
        popup.wait_for_url("**/session/*", timeout=25000)
        popup_id = popup.url.split("/session/", 1)[1].split("?", 1)[0]
        assert "/" not in popup_id, f"the popup landed on an unrestorable URL: {popup.url}"
        popup.wait_for_function(
            "(sid) => typeof S !== 'undefined' && S._bootReady && S.session && "
            "S.session.session_id === sid",
            arg=popup_id, timeout=20000,
        )
        # Free the origin tab's streams before the reload (per-host connection
        # budget), then pin the hard-reload contract: the same conversation
        # comes back with its draft.
        page.close()
        time.sleep(0.6)
        popup.locator("#msg").fill("Popup draft survives reload")
        assert _wait_draft(popup_id, "Popup draft survives reload"), (
            "the popup draft did not persist"
        )
        popup.reload(wait_until="domcontentloaded")
        popup.wait_for_function(
            "(sid) => typeof S !== 'undefined' && S._bootReady && S.session && "
            "S.session.session_id === sid",
            arg=popup_id, timeout=25000,
        )
        assert popup.locator("#msg").input_value() == "Popup draft survives reload", (
            "the reloaded conversation lost its draft"
        )
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
            try:
                _check_middle_click_from_deep_url(browser)
                print("OK  new conversation middle-click from deep URL — popup URL stays restorable")
            except Exception as exc:
                failures.append(f"  [new conversation middle-click deep url] {exc}")
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
