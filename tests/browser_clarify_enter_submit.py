#!/usr/bin/env python3
"""
Headless browser gate: Enter submits the clarify answer whatever the send key (#8769).

WHY THIS EXISTS
  The clarify card's answer field is a single-line <input>. #8629 routed its Enter
  key through the composer's send-key decision (_composerShouldSendOnKey). Where that
  decision says "newline", a single-line field has nothing to insert, so Enter did
  nothing at all:
    - on a touch-only phone with the default send key (a phone keyboard has no
      Ctrl/Cmd, so the keyboard could not submit the answer at all);
    - with the Shift+Enter or Ctrl+Enter send key, for plain Enter.
  Through exp-v0.52.619, every Enter submitted the answer. #8629's IME guard is
  worth keeping: a keydown that commits an IME composition must not submit.

WHAT IT CHECKS, on a desktop page and on an emulated touch-only phone, for each send
key (enter, shift+enter, ctrl+enter), with the real clarify card shown through
showClarifyCard() and respondClarify replaced by a recorder:
  - Enter, Shift+Enter, Ctrl+Enter and Cmd+Enter each submit the typed answer once;
  - an Enter keydown with isComposing, with keyCode 229, or while the composer's IME
    flag is set submits nothing and is not cancelled;
  - another key submits nothing.
It first checks that the phone context really takes the touch-only branch
(_isTouchOnlyDevice() is true there and false on desktop), so the phone cases cannot
pass by testing the desktop path twice.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral port
  with isolated temp state. Nothing is sent to an agent.

USAGE
  python tests/browser_clarify_enter_submit.py
  (Requires: playwright + chromium. Set CLARIFY_ENTER_PORT to pin the port.)

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SID = "clarify-enter-test"
SEND_KEYS = ("enter", "shift+enter", "ctrl+enter")
# (label, key chord) — every chord must submit the answer exactly once.
SUBMIT_CHORDS = [
    ("Enter", "Enter"),
    ("Shift+Enter", "Shift+Enter"),
    ("Ctrl+Enter", "Control+Enter"),
    ("Cmd+Enter", "Meta+Enter"),
]
# (label, synthetic keydown spec) — none may submit or be cancelled.
HELD_EVENTS = [
    ("Enter while composing (isComposing)", {"key": "Enter", "isComposing": True}),
    ("Enter with keyCode 229", {"key": "Enter", "keyCode": 229}),
    ("Enter while the IME flag is set", {"key": "Enter", "imeFlag": True}),
    ("another key", {"key": "a"}),
]

SHOW_CARD_JS = """(sid) => {
  window.__clarifySends = [];
  window.respondClarify = function () {
    window.__clarifySends.push(document.getElementById('clarifyInput').value);
    return Promise.resolve();
  };
  S.session = Object.assign({}, S.session || {}, {session_id: sid});
  showClarifyCard({_session_id: sid, clarify_id: 'clarify-enter-1',
                   question: 'Which colour should the button be?', choices_offered: []});
  const card = document.getElementById('clarifyCard');
  const input = document.getElementById('clarifyInput');
  return !!(card && card.classList.contains('visible') && input && typeof input.onkeydown === 'function');
}"""

PREPARE_JS = """([sendKey, answer]) => {
  window._sendKey = sendKey;
  try { localStorage.setItem('hermes-pref-send_key', sendKey); } catch (_) {}
  window.__clarifySends = [];
  const input = document.getElementById('clarifyInput');
  input.value = answer;
  input.focus();
  return document.activeElement === input;
}"""

SYNTHETIC_JS = """(spec) => {
  const input = document.getElementById('clarifyInput');
  const ev = new KeyboardEvent('keydown', {key: spec.key, isComposing: !!spec.isComposing,
                                           bubbles: true, cancelable: true});
  if (spec.keyCode) Object.defineProperty(ev, 'keyCode', {get: () => spec.keyCode});
  let restore = null;
  if (spec.imeFlag) {
    // The composer's IME flag (boot.js _imeComposing), as set between compositionstart and compositionend.
    const msg = document.getElementById('msg');
    msg.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
    restore = () => msg.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true}));
  }
  input.dispatchEvent(ev);
  if (restore) restore();
  return {prevented: ev.defaultPrevented, sends: window.__clarifySends.slice()};
}"""

STATE_JS = "() => window.__clarifySends.slice()"

CONTEXTS = [
    ("desktop", {"viewport": {"width": 1440, "height": 900}}, False),
    ("touch-only phone", {
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    }, True),
]


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _check_context(browser, base, name, options, touch_only):
    failures = []
    ctx = browser.new_context(base_url=base, **options)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", state="attached", timeout=15000)
    page.wait_for_timeout(1000)
    got_touch = page.evaluate("() => _isTouchOnlyDevice()")
    if got_touch is not touch_only:
        ctx.close()
        return [f"  [{name}] setup: _isTouchOnlyDevice() is {got_touch}, want {touch_only}"]
    if not page.evaluate(SHOW_CARD_JS, SID):
        ctx.close()
        return [f"  [{name}] setup: the clarify card did not show"]
    for send_key in SEND_KEYS:
        for label, chord in SUBMIT_CHORDS:
            answer = f"teal ({send_key}, {label})"
            if not page.evaluate(PREPARE_JS, [send_key, answer]):
                failures.append(f"  [{name} / {send_key} / {label}] could not focus the clarify input")
                continue
            page.keyboard.press(chord)
            page.wait_for_timeout(100)
            sends = page.evaluate(STATE_JS)
            if sends != [answer]:
                failures.append(f"  [{name} / send key {send_key} / {label}] submitted {sends} (want [{answer!r}])")
            else:
                print(f"OK  {name} / send key {send_key} / {label}: submitted once")
        for label, spec in HELD_EVENTS:
            page.evaluate(PREPARE_JS, [send_key, "half-composed"])
            got = page.evaluate(SYNTHETIC_JS, spec)
            if got["sends"] or got["prevented"]:
                failures.append(
                    f"  [{name} / send key {send_key} / {label}] submitted {got['sends']}, "
                    f"cancelled={got['prevented']} (want nothing)"
                )
            else:
                print(f"OK  {name} / send key {send_key} / {label}: nothing submitted")
    for err in errors:
        failures.append(f"  [{name}] pageerror: {err}")
    ctx.close()
    return failures


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    port = int(os.getenv("CLARIFY_ENTER_PORT") or _free_port())
    base = f"http://127.0.0.1:{port}"
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-clarify-enter-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        # Isolate the server from this machine's own Hermes install: a temporary HOME, and no lazy dependency
        # installs, so it can't discover an installed Agent and stall while preparing its dependencies.
        "HOME": state_dir,
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    # A server already listening on the port would answer /health and the checks would
    # silently run against ITS code, not this checkout's. Refuse instead.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"SETUP FAIL: port {port} is already in use; set CLARIFY_ENTER_PORT", file=sys.stderr)
            return 2

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(base, timeout=30):
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
            for name, options, touch_only in CONTEXTS:
                failures += _check_context(browser, base, name, options, touch_only)
            browser.close()

        if failures:
            print("\nCLARIFY ENTER SUBMIT FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nCLARIFY ENTER SUBMIT PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
