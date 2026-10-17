#!/usr/bin/env python3
"""
Headless browser gate: focusing a long prefill-context label must never push the
Send control out of the viewport (#7856).

WHY THIS EXISTS
  `setComposerStatus()` marks the composer status row focusable so a tap (or Tab)
  reveals a label that the row normally clips. The first implementation expanded
  the row's max-width to `min(100%,70ch)` with `overflow:visible` and no token
  breaking. On a 390px phone a long unbroken label (a filename-like context name)
  grew the flex item to ~445px and displaced Send to x≈739 — entirely off-screen,
  while `documentElement.scrollWidth` stayed 390, so no horizontal scroll could
  recover it. The reviewer reproduced it by hand; this file makes it a gate.

WHAT IT CHECKS, at 390x844 and 360x740
  - the status row is reachable and focusable while it carries a label;
  - after a real tap focuses it, the Send button's rect stays fully inside the
    viewport with its full tap target, for BOTH a spaced name and an unbroken one;
  - the focused label itself stays inside the viewport (it may wrap, not clip);
  - an ordinary send still works after the disclosure.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state, an isolated Chromium mobile context.

USAGE
  <webui venv>/bin/python tests/browser_composer_status_label.py
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

PORT = int(os.getenv("COMPOSER_STATUS_PORT", "8814"))
BASE = f"http://127.0.0.1:{PORT}"
SETTLE_SECONDS = 1.2

# Spaced name (~55 chars) and an unbroken one, both plausible prefill labels.
LABEL_SPACED = "Production emergency guardrails review staging"
LABEL_UNBROKEN = "ProductionEmergencyGuardrailsWithoutSpaces" * 3

VIEWPORTS = ((390, 844), (360, 740))


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
    """Poll with page.evaluate — the app's CSP has no 'unsafe-eval'."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(100)
    return False


_SET_LABEL = """
(label) => {
  // Use the real setter, not DOM poking: it is what makes the row visible,
  // focusable and titled in production. #composerStatus starts display:none.
  if (typeof setComposerStatus !== 'function') return 'no-setter';
  setComposerStatus(label, 0);
  const row = document.querySelector('.composer-status');
  if (!row) return 'no-row';
  const r = row.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return 'collapsed';
  return 'ok';
}
"""

# Geometry after focusing the row. Returns plain data for the assertion messages.
_MEASURE = """
() => {
  const row = document.querySelector('.composer-status');
  const send = document.querySelector('.send-btn') || document.querySelector('#btnSend');
  const vw = window.innerWidth;
  const r = row ? row.getBoundingClientRect() : null;
  const s = send ? send.getBoundingClientRect() : null;
  return {
    vw,
    row: r ? {left: r.left, right: r.right, width: r.width, height: r.height} : null,
    send: s ? {left: s.left, right: s.right, width: s.width, height: s.height} : null,
    docScrollWidth: document.documentElement.scrollWidth,
    focused: document.activeElement === row,
    focusedStyleMaxWidth: row ? getComputedStyle(row).maxWidth : null,
  };
}
"""


def _check(browser, width, height, label, label_name):
    """Return a list of failure lines for one viewport/label pair."""
    fails = []
    ctx = browser.new_context(
        viewport={"width": width, "height": height},
        base_url=BASE,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2,
    )
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    if not page.wait_for_selector("#msg", timeout=15000):
        ctx.close()
        return [f"  [{width}x{height}/{label_name}] composer never appeared"]
    if not _wait_until(page, "typeof S !== 'undefined'", 15000):
        ctx.close()
        return [f"  [{width}x{height}/{label_name}] the app did not initialize"]
    time.sleep(SETTLE_SECONDS)

    if page.evaluate(_SET_LABEL, label) != "ok":
        reason = page.evaluate(_SET_LABEL, label)
        ctx.close()
        return [f"  [{width}x{height}/{label_name}] could not establish the status label: {reason}"]

    # A real tap on the status row focuses it (the reveal path the report used).
    try:
        page.locator(".composer-status").first.tap(timeout=5000)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the driver
        fails.append(f"  [{width}x{height}/{label_name}] tap on the status row failed: {exc}")
    page.wait_for_timeout(250)

    geo = page.evaluate(_MEASURE)
    vw = geo["vw"]
    send = geo["send"]
    row = geo["row"]

    if not geo["focused"]:
        fails.append(
            f"  [{width}x{height}/{label_name}] the tap did not focus the status row"
            f" (focused={geo['focused']}, applied max-width={geo['focusedStyleMaxWidth']})"
        )

    if send is None:
        fails.append(f"  [{width}x{height}/{label_name}] Send control not found")
    else:
        # Fully inside the viewport, with the mobile 44px tap target intact.
        if send["right"] > vw + 0.5 or send["left"] < -0.5:
            fails.append(
                f"  [{width}x{height}/{label_name}] Send displaced outside the viewport:"
                f" x={send['left']:.0f}-{send['right']:.0f}, viewport={vw}px"
                f" (status row width={row['width']:.0f})"
            )
        if send["width"] < 43.5 or send["height"] < 43.5:
            fails.append(
                f"  [{width}x{height}/{label_name}] Send tap target shrank to"
                f" {send['width']:.0f}x{send['height']:.0f} (needs >=44x44)"
            )

    if row is not None and row["right"] > vw + 0.5:
        fails.append(
            f"  [{width}x{height}/{label_name}] the focused label itself overflows:"
            f" right={row['right']:.0f} > viewport={vw}px"
        )

    # The document must not be relying on horizontal scroll to hide the problem.
    if geo["docScrollWidth"] > vw + 1:
        fails.append(
            f"  [{width}x{height}/{label_name}] document scrollWidth={geo['docScrollWidth']}"
            f" exceeds the {vw}px viewport"
        )

    # An ordinary send still works after the disclosure. Intercept the POST so
    # the assertion is agent-free and deterministic: send() catches a rejected
    # request and shows an error bubble WITHOUT raising a page error, so a bare
    # Enter press can pass while nothing was ever sent.
    sent = []
    page.route(
        "**/api/chat/start",
        lambda route: (
            sent.append(route.request.post_data or ""),
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"stream_id": "probe", "session_id": "probe"}',
            ),
        ) and None,
    )
    try:
        page.click("#msg")
        page.keyboard.type("hello")
        # Click the real Send control: this build's composer has no Enter
        # handler on #msg, and the button only enables once the input event
        # has run updateSendBtn(), which typing already triggered.
        page.click("#btnSend")
        page.wait_for_timeout(600)
    except Exception as exc:  # noqa: BLE001
        fails.append(f"  [{width}x{height}/{label_name}] send after reveal failed: {exc}")
    finally:
        page.unroute("**/api/chat/start")

    if not sent:
        fails.append(
            f"  [{width}x{height}/{label_name}] Send did not send: no POST to"
            f" /api/chat/start reached the server"
        )
    else:
        import json as _json
        try:
            payload = _json.loads(sent[0])
        except ValueError:
            payload = {}
        if payload.get("message") != "hello":
            fails.append(
                f"  [{width}x{height}/{label_name}] the sent message was not the typed"
                f" text: {payload.get('message')!r}"
            )

    if errors:
        fails.append(f"  [{width}x{height}/{label_name}] page errors: {errors[:3]}")

    ctx.close()
    return fails


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-composer-status-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    # The deployment exports TLS certs and the WebUI password; the harness serves
    # plain http with no auth. An inherited password turns every page into /login.
    for k in ("HERMES_WEBUI_TLS_CERT", "HERMES_WEBUI_TLS_KEY",
              "HERMES_WEBUI_PASSWORD", "API_SERVER_KEY",
              "HERMES_WEBUI_EXTENSION_DIR", "HERMES_WEBUI_EXTENSION_MANIFEST"):
        env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
        "TMPDIR": state_dir,
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
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
            for width, height in VIEWPORTS:
                failures.extend(_check(browser, width, height, LABEL_SPACED, "spaced"))
                failures.extend(_check(browser, width, height, LABEL_UNBROKEN, "unbroken"))
            browser.close()

        if failures:
            print("\nCOMPOSER STATUS LABEL FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nCOMPOSER STATUS LABEL PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
