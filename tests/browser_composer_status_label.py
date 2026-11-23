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
  Part 1 — portrait matrix. Three viewports (390x844, 360x740, 320x700) x two
  labels (a ~55-char spaced name and an unbroken one) x two focus paths (a real
  tap, and Tab from the composer), so twelve cases. For every case:
  - the status row is reachable and focusable while it carries a label;
  - the disclosure actually reveals the label: it is an out-of-flow bubble that
    carries the whole value, wraps, and is bounded by the composer row;
  - the row itself keeps its collapsed geometry — the reveal must not grow it
    in place (that is the press-then-blur defect: the button moved between
    pointer-up and click);
  - the Send button's rect does not move across the reveal, stays fully inside
    the viewport, keeps its 44x44 tap target, and its centre is hit-testable;
  - the document does not hide the overflow behind an unscrollable clip;
  - an ordinary send still works: the typed text reaches /api/chat/start.

  Part 2 — reveal-then-send at every width (the #7856 gate-certificate
  blockers). Six schedules — 1440x900 click, 1920x1000 click, 800x900 click,
  844x390 touch, 320x700 touch, 360x740 touch — each: type the draft FIRST with
  the status collapsed, reveal the long (~128-char) label, then press the
  primary button while the row still has focus, and require the POST to carry
  the typed message. Both arms also require Send to stay within
  SEND_STABILITY_PX of where it was pressed, because pointer-down on Send blurs
  the row and a disclosure that resizes the composer row moves the button out
  from under pointer-up: the click then lands on a container and nothing is
  sent. The disclosure is an out-of-flow bubble on `.composer-footer::after` at
  every width, capped with `100cqw` against the composer row. The `lost-click`
  arm asserts only stability + delivery, so it passes on master (where the row
  is not focusable); the `reveal` arm asserts the disclosure itself and is
  expected to fail master for that new behavior.

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

# A long, permitted label: ~128 characters, well past the 52ch cap the focused
# desktop row used to resolve against its own content-sized flex parent. This is
# the case that displaced Send above 640px (#7856 gate certificate).
LABEL_DISCLOSURE = (
    "Production emergency guardrails review staging for the customer escalation"
    " handoff packet that arrives on the Tuesday morning bridge call"
)
# The disclosure must not change the row's geometry at ANY width, so the
# schedule below is: type a draft, reveal the label, then press the primary
# button without touching the input again. Desktop/tablet/landscape first, then
# the two portrait phones the gate certificate lost the press on (320x700 and
# 360x740, touch). On the portrait entries the reveal is a real tap on the
# status row and the press is a real tap on the button.
SEND_VIEWPORTS = (
    (1440, 900, False),
    (1920, 1000, False),
    (800, 900, False),
    (844, 390, True),
    (320, 700, True),
    (360, 740, True),
)
# The disclosure must keep Send within this many px of where the operator
# pressed. Layout noise (a `cf-icons` stage flip, subpixel rounding) is smaller;
# the >640px defect this guards moved the button ~400px in one frame.
SEND_STABILITY_PX = 1.5


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


# Geometry for the reveal: the composer row, Send, and the disclosure bubble the
# footer renders on `::after` at every width.
_MEASURE_SEND = """
() => {
  const row = document.querySelector('.composer-status');
  const send = document.querySelector('#btnSend');
  const foot = document.querySelector('.composer-footer');
  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {left: r.left, top: r.top, right: r.right, bottom: r.bottom,
            width: r.width, height: r.height};
  };
  const s = box(send);
  const after = foot ? getComputedStyle(foot, '::after') : null;
  let hitSendCentre = null;
  if (send) {
    const el = document.elementFromPoint((s.left + s.right) / 2, (s.top + s.bottom) / 2);
    hitSendCentre = !!(el && (el === send || send.contains(el)));
  }
  return {
    send: s,
    row: box(row),
    footer: box(foot),
    focused: !!(document.activeElement && document.activeElement.classList
                && document.activeElement.classList.contains('composer-status')),
    tabindex: row ? row.getAttribute('tabindex') : null,
    whiteSpace: row ? getComputedStyle(row).whiteSpace : null,
    rowScrollWidth: row ? row.scrollWidth : 0,
    rowClientWidth: row ? row.clientWidth : 0,
    text: row ? row.textContent : null,
    bubble: after ? after.content : null,
    bubblePosition: after ? after.position : null,
    bubbleMaxWidth: after ? parseFloat(after.maxWidth) : null,
    bubbleWhiteSpace: after ? after.whiteSpace : null,
    bubblePointerEvents: after ? after.pointerEvents : null,
    hitSendCentre: hitSendCentre,
    docScrollWidth: document.documentElement.scrollWidth,
    vw: window.innerWidth,
  };
}
"""


def _settle_composer_stages(page):
    """Run the footer's fit pass so a `cf-icons`/`cf-burger` flip cannot land in
    the middle of the measurement and be mistaken for the disclosure moving Send."""
    for _ in range(2):
        page.evaluate(
            "() => { if (window._fitComposerFooter) window._fitComposerFooter(); }"
        )
        page.wait_for_timeout(150)


def _check_send_after_reveal(browser, width, height, touch, require_reveal):
    """The gate certificate's lost-click schedule, at one viewport.

    Type the draft FIRST, with the status still collapsed. Then reveal the label
    (a real tap, or Tab on a keyboard) and press Send while the row still has
    focus — the schedule that lost the click: pointer-down lands on Send, the
    blur collapses the reveal, and the button moves away before pointer-up.

    `require_reveal=True` is the new-behavior case: it asserts the row is
    focusable and the whole label is disclosed. `require_reveal=False` is the
    lost-click regression proper: it asserts only that revealing the label does
    not move Send and that the typed message still leaves, so it also passes on
    master, where the row is not focusable at all.
    """
    fails = []
    kind = "reveal" if require_reveal else "lost-click"
    tag = f"{width}x{height}/{'touch' if touch else 'click'}/{kind}"
    ctx = browser.new_context(
        viewport={"width": width, "height": height},
        base_url=BASE,
        is_mobile=touch,
        has_touch=touch,
        device_scale_factor=2 if touch else 1,
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

    state = page.evaluate(_SET_LABEL, LABEL_DISCLOSURE)
    if state != "ok":
        ctx.close()
        return [f"  [{tag}] could not establish the status label: {state}"]
    _settle_composer_stages(page)

    # Draft before the reveal (the certificate's schedule).
    page.click("#msg")
    page.keyboard.type("hello")
    page.wait_for_selector("#btnSend:not([disabled])", timeout=5000)
    page.wait_for_timeout(150)
    before = page.evaluate(_MEASURE_SEND)

    if require_reveal:
        if touch:
            try:
                page.locator(".composer-status").first.tap(timeout=5000)
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                fails.append(f"  [{tag}] tap on the status row failed: {exc}")
        elif not _focus_by_tab(page):
            fails.append(f"  [{tag}] the status row is not reachable with Tab")
    else:
        # Best-effort reveal, exactly as a pointer/keyboard user could leave the
        # row: no assertion that it took, because master has no focus reveal.
        page.evaluate(
            "() => { const r = document.querySelector('.composer-status');"
            " if (r && r.focus) r.focus(); }"
        )
    page.wait_for_timeout(250)

    after = page.evaluate(_MEASURE_SEND)
    vw = after["vw"]

    if require_reveal:
        if not after["focused"]:
            fails.append(
                f"  [{tag}] the row did not receive focus"
                f" (tabindex={after['tabindex']!r})"
            )
        elif after["tabindex"] != "0":
            fails.append(f"  [{tag}] the row is not in the tab order")
        # The disclosure is an out-of-flow bubble at every width: it must carry
        # the whole label and must not be an in-flow box that resizes the row.
        if after["bubblePosition"] != "absolute":
            fails.append(
                f"  [{tag}] the disclosure is not an out-of-flow bubble"
                f" (position={after['bubblePosition']!r})"
            )
        if LABEL_DISCLOSURE not in str(after["bubble"]):
            fails.append(
                f"  [{tag}] the focused disclosure does not carry the full"
                f" label: {str(after['bubble'])[:80]!r}"
            )
        if after["whiteSpace"] == "normal":
            fails.append(
                f"  [{tag}] the focused row expanded in place"
                f" (white-space=normal); the disclosure must be out of flow"
            )

    # The invariant both arms share: revealing the label must not move Send.
    send_before, send_after = before["send"], after["send"]
    if send_before is None or send_after is None:
        fails.append(f"  [{tag}] Send control not found")
    else:
        moved = {
            edge: round(send_after[edge] - send_before[edge], 2)
            for edge in ("left", "top", "right", "bottom")
            if abs(send_after[edge] - send_before[edge]) > SEND_STABILITY_PX
        }
        if moved:
            fails.append(
                f"  [{tag}] revealing the label moved Send by {moved}px"
                f" (before={send_before['left']:.0f},{send_before['top']:.0f}"
                f" after={send_after['left']:.0f},{send_after['top']:.0f})"
            )
        if after["hitSendCentre"] is False:
            fails.append(
                f"  [{tag}] Send is covered: elementFromPoint at its centre is"
                f" not the button"
            )
        if send_after["right"] > vw + 0.5 or send_after["left"] < -0.5:
            fails.append(
                f"  [{tag}] Send left the viewport:"
                f" x={send_after['left']:.0f}-{send_after['right']:.0f},"
                f" viewport={vw}px"
            )

    # The document must not be relying on horizontal scroll to hide the problem,
    # and the disclosure must not add overflow of its own. A pre-existing few-px
    # document overflow is identical on both arms at these desktop fixtures (the
    # gate certificate measured the same 14px on master), so the assertion is
    # "the reveal did not widen the document", not "the document is exactly the
    # viewport".
    if after["docScrollWidth"] > before["docScrollWidth"] + 1:
        fails.append(
            f"  [{tag}] the reveal widened the document:"
            f" scrollWidth {before['docScrollWidth']} -> {after['docScrollWidth']}"
            f" at a {vw}px viewport"
        )

    # Press Send with the row still focused: the request must carry the draft.
    page.route(
        "**/api/chat/start*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"stream_id": "probe", "session_id": "probe"}',
        ),
    )
    try:
        with page.expect_request(
            lambda req: "/api/chat/start" in req.url, timeout=10000
        ) as req_info:
            if touch:
                page.locator("#btnSend").tap(timeout=5000)
            else:
                page.locator("#btnSend").click(timeout=5000)
        payload = json.loads(req_info.value.post_data or "{}")
    except Exception as exc:  # noqa: BLE001
        fails.append(
            f"  [{tag}] Send did not send after the reveal: no POST to"
            f" /api/chat/start reached the server"
            f" ({type(exc).__name__}: {exc})"
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
    _settle_composer_stages(page)
    before = page.evaluate(_MEASURE_SEND)

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

    geo = page.evaluate(_MEASURE_SEND)
    vw = geo["vw"]
    send = geo["send"]
    row = geo["row"]

    if not geo["focused"]:
        fails.append(
            f"  [{tag}] the row did not receive focus"
            f" (tabindex={geo['tabindex']!r})"
        )
    else:
        if geo["tabindex"] != "0":
            fails.append(f"  [{tag}] the row is not in the tab order")
        # The reveal is an out-of-flow bubble at EVERY width. It must carry the
        # whole label, wrap, and stay bounded by the composer row; it must also
        # be click-through so a press that starts on it cannot swallow the next
        # Send tap.
        if geo["bubblePosition"] != "absolute":
            fails.append(
                f"  [{tag}] the disclosure is not an out-of-flow bubble"
                f" (position={geo['bubblePosition']!r}); an in-flow reveal"
                f" moves Send between pointer-down and pointer-up"
            )
        if label not in str(geo["bubble"]):
            fails.append(
                f"  [{tag}] the focused disclosure does not carry the full"
                f" label: {str(geo['bubble'])[:80]!r}"
            )
        if geo["bubbleWhiteSpace"] != "normal":
            fails.append(
                f"  [{tag}] the disclosure does not wrap"
                f" (white-space={geo['bubbleWhiteSpace']!r})"
            )
        if geo["bubblePointerEvents"] != "none":
            fails.append(
                f"  [{tag}] the disclosure is not click-through"
                f" (pointer-events={geo['bubblePointerEvents']!r})"
            )
        if geo["bubbleMaxWidth"] is None or geo["bubbleMaxWidth"] > vw:
            fails.append(
                f"  [{tag}] the disclosure is not bounded by the row:"
                f" max-width={geo['bubbleMaxWidth']!r} at a {vw}px viewport"
            )
        # The row itself must NOT grow: the reveal is out of flow, so its box
        # keeps the collapsed ellipsized geometry.
        if geo["whiteSpace"] == "normal":
            fails.append(
                f"  [{tag}] the focused row expanded in place"
                f" (white-space=normal); the disclosure must be out of flow"
            )
        if geo["text"] != label:
            fails.append(f"  [{tag}] the rendered label no longer matches the value")

    # The invariant at every width: revealing the label must not move Send, and
    # must not change the height of the row it lives in.
    if before["send"] is not None and send is not None:
        moved = {
            edge: round(send[edge] - before["send"][edge], 2)
            for edge in ("left", "top", "right", "bottom")
            if abs(send[edge] - before["send"][edge]) > SEND_STABILITY_PX
        }
        if moved:
            fails.append(
                f"  [{tag}] revealing the label moved Send by {moved}px"
                f" (collapsed={before['send']['left']:.0f},{before['send']['top']:.0f}"
                f" revealed={send['left']:.0f},{send['top']:.0f})"
            )
    if before["row"] is not None and row is not None:
        grew = round(row["height"] - before["row"]["height"], 2)
        if abs(grew) > 0.5:
            fails.append(
                f"  [{tag}] the reveal grew the status row by {grew}px"
                f" ({before['row']['height']:.1f} -> {row['height']:.1f})"
            )

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
    if geo["docScrollWidth"] > before["docScrollWidth"] + 1:
        fails.append(
            f"  [{tag}] the reveal widened the document:"
            f" scrollWidth {before['docScrollWidth']} -> {geo['docScrollWidth']}"
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
            # Above 640px: the disclosure must not move Send at all. Run the
            # certificate's schedule twice per viewport — once as the new-behavior
            # case (row focusable, label disclosed) and once as the lost-click
            # regression (Send stable, typed text delivered) — so the regression
            # is the arm that also passes on master.
            for width, height, touch in SEND_VIEWPORTS:
                failures.extend(
                    _check_send_after_reveal(browser, width, height, touch, True)
                )
                failures.extend(
                    _check_send_after_reveal(browser, width, height, touch, False)
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
