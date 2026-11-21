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

WHAT IT CHECKS
  Three viewports (390x844, 360x740, 320x700) x two labels (a ~55-char spaced
  name and an unbroken one) x two focus paths (a real tap, and Tab from the
  composer), so twelve cases. For every case:
  - the status row is reachable and focusable while it carries a label;
  - the focused label is actually readable: it wraps inside its own box and
    nothing is clipped (scrollWidth/scrollHeight fit clientWidth/clientHeight);
  - the Send button's rect stays fully inside the viewport, keeps its 44x44 tap
    target, and its centre is hit-testable (nothing overlays it);
  - the document does not hide the overflow behind an unscrollable clip;
  - an ordinary send still works: the typed text reaches /api/chat/start.

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
import json
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
# A short label and a medium one. Long labels are the case that displaces Send;
# short ones are the case a percentage cap resolved against the content-sized
# `.composer-right` collapses to 0px — the row then renders as an empty column
# and the footer grows. Both must stay readable once focused.
LABEL_SHORT = "prefill: dev"
LABEL_MEDIUM = "production staging guardrails"

VIEWPORTS = ((390, 844), (360, 740), (320, 700))
FOCUS_MODES = ("tap", "tab")


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
  const cs = row ? getComputedStyle(row) : null;
  let hitSendCentre = null;
  if (send) {
    const el = document.elementFromPoint((s.left + s.right) / 2, (s.top + s.bottom) / 2);
    hitSendCentre = !!(el && (el === send || send.contains(el)));
  }
  return {
    vw,
    row: r ? {
      left: r.left, right: r.right, width: r.width, height: r.height,
      scrollWidth: row.scrollWidth, clientWidth: row.clientWidth,
      scrollHeight: row.scrollHeight, clientHeight: row.clientHeight,
    } : null,
    send: s ? {left: s.left, right: s.right, width: s.width, height: s.height} : null,
    hitSendCentre,
    docScrollWidth: document.documentElement.scrollWidth,
    focused: document.activeElement === row,
    focusedStyleMaxWidth: cs ? cs.maxWidth : null,
    whiteSpace: cs ? cs.whiteSpace : null,
    text: row ? row.textContent : null,
  };
}
"""

_ACTIVE_IS_STATUS = """
() => {
  const a = document.activeElement;
  return !!(a && a.classList && a.classList.contains('composer-status'));
}
"""


def _focus_by_tab(page, max_presses=25):
    """Walk the real tab order from the composer to the status row."""
    page.click("#msg")
    for _ in range(max_presses):
        page.keyboard.press("Tab")
        page.wait_for_timeout(40)
        if page.evaluate(_ACTIVE_IS_STATUS):
            return True
    return False


def _check(browser, width, height, label, label_name, focus_mode):
    """Return a list of failure lines for one viewport/label/focus-mode case."""
    fails = []
    tag = f"{width}x{height}/{label_name}/{focus_mode}"
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
        return [f"  [{tag}] composer never appeared"]
    if not _wait_until(page, "typeof S !== 'undefined'", 15000):
        ctx.close()
        return [f"  [{tag}] the app did not initialize"]
    time.sleep(SETTLE_SECONDS)

    state = page.evaluate(_SET_LABEL, label)
    if state != "ok":
        ctx.close()
        return [f"  [{tag}] could not establish the status label: {state}"]

    if focus_mode == "tap":
        # A real tap on the status row focuses it (the reveal path the report used).
        try:
            page.locator(".composer-status").first.tap(timeout=5000)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the driver
            fails.append(f"  [{tag}] tap on the status row failed: {exc}")
    else:
        # The keyboard path a desktop / keyboard user takes. `setComposerStatus`
        # is what puts the row in the tab order, so this also guards tabIndex.
        if not _focus_by_tab(page):
            fails.append(f"  [{tag}] the status row is not reachable with Tab")
    page.wait_for_timeout(250)

    geo = page.evaluate(_MEASURE)
    vw = geo["vw"]
    send = geo["send"]
    row = geo["row"]

    if not geo["focused"]:
        fails.append(
            f"  [{tag}] the row did not receive focus"
            f" (focused={geo['focused']}, applied max-width={geo['focusedStyleMaxWidth']})"
        )
    else:
        # The disclosure must actually reveal the label: normal wrapping, and no
        # content spilling past the box (which `overflow:hidden` would clip).
        if geo["whiteSpace"] != "normal":
            fails.append(
                f"  [{tag}] the focused row still uses white-space={geo['whiteSpace']};"
                f" the label stays on one clipped line"
            )
        if row is not None:
            if row["scrollWidth"] > row["clientWidth"] + 1:
                fails.append(
                    f"  [{tag}] the label is clipped horizontally:"
                    f" scrollWidth={row['scrollWidth']:.0f} > clientWidth={row['clientWidth']:.0f}"
                )
            if row["scrollHeight"] > row["clientHeight"] + 1:
                fails.append(
                    f"  [{tag}] the label is clipped vertically:"
                    f" scrollHeight={row['scrollHeight']:.0f} > clientHeight={row['clientHeight']:.0f}"
                )
        if geo["text"] != label:
            fails.append(f"  [{tag}] the rendered label no longer matches the value")

    if send is None:
        fails.append(f"  [{tag}] Send control not found")
    else:
        # Fully inside the viewport, with the mobile 44px tap target intact.
        if send["right"] > vw + 0.5 or send["left"] < -0.5:
            row_w = row["width"] if row else 0
            fails.append(
                f"  [{tag}] Send displaced outside the viewport:"
                f" x={send['left']:.0f}-{send['right']:.0f}, viewport={vw}px"
                f" (status row width={row_w:.0f})"
            )
        if send["width"] < 43.5 or send["height"] < 43.5:
            fails.append(
                f"  [{tag}] Send tap target shrank to"
                f" {send['width']:.0f}x{send['height']:.0f} (needs >=44x44)"
            )
        if geo["hitSendCentre"] is False:
            fails.append(
                f"  [{tag}] Send is covered: elementFromPoint at its centre is not the button"
            )

    if row is not None and row["right"] > vw + 0.5:
        fails.append(
            f"  [{tag}] the focused label itself overflows:"
            f" right={row['right']:.0f} > viewport={vw}px"
        )

    # The document must not be relying on horizontal scroll to hide the problem.
    if geo["docScrollWidth"] > vw + 1:
        fails.append(
            f"  [{tag}] document scrollWidth={geo['docScrollWidth']}"
            f" exceeds the {vw}px viewport"
        )

    # An ordinary send still works after the disclosure. Intercept the POST so
    # the assertion is agent-free and deterministic: send() catches a rejected
    # request and shows an error bubble WITHOUT raising a page error, so a click
    # that never reached the server must not pass.
    page.route(
        "**/api/chat/start*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"stream_id": "probe", "session_id": "probe"}',
        ),
    )
    try:
        page.click("#msg")
        page.keyboard.type("hello")
        # Click the real Send control: this build's composer has no Enter
        # handler on #msg, and the button only enables once the input event
        # has run updateSendBtn(), which typing already triggered.
        page.wait_for_selector("#btnSend:not([disabled])", timeout=5000)
        with page.expect_request(
            lambda req: "/api/chat/start" in req.url, timeout=10000
        ) as req_info:
            page.click("#btnSend")
        payload = json.loads(req_info.value.post_data or "{}")
    except Exception as exc:  # noqa: BLE001
        fails.append(
            f"  [{tag}] Send did not send: no POST to /api/chat/start"
            f" reached the server ({type(exc).__name__}: {exc})"
        )
    else:
        if payload.get("message") != "hello":
            fails.append(
                f"  [{tag}] the sent message was not the typed text:"
                f" {payload.get('message')!r}"
            )
    finally:
        page.unroute("**/api/chat/start*")

    if errors:
        fails.append(f"  [{tag}] page errors: {errors[:3]}")

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
                for label, label_name in (
                    (LABEL_SHORT, "short"),
                    (LABEL_MEDIUM, "medium"),
                    (LABEL_SPACED, "spaced"),
                    (LABEL_UNBROKEN, "unbroken"),
                ):
                    for focus_mode in FOCUS_MODES:
                        failures.extend(
                            _check(browser, width, height, label, label_name, focus_mode)
                        )
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
