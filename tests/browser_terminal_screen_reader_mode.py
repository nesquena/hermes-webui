#!/usr/bin/env python3
"""
Headless browser gate: the "Screen reader mode" preference drives xterm.js
``screenReaderMode`` in the composer terminal, including on a terminal that is
already open.

WHY THIS EXISTS
  xterm.js renders terminal rows on a canvas-like DOM that screen readers cannot
  read. With ``screenReaderMode`` it adds an accessibility tree of the visible
  rows and a live region that announces new output. The WebUI exposes this as an
  opt-in preference. A blind tester confirmed with NVDA + Firefox that a terminal
  opened with the mode on is read well, but could not observe the live toggle:
  the terminal lives in the chat view and the Preferences are a separate panel.

WHAT IT CHECKS, in Chromium and Firefox
  - with the preference off (default), an open terminal has no accessibility
    tree;
  - checking the box in Settings > Preferences (real click, real autosave) adds
    the tree to the same terminal instance, and the tree contains output that
    was printed before the switch;
  - unchecking it removes the tree from that terminal again;
  - after a reload with the preference on, a newly opened terminal starts with
    the tree.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state and a temp workspace. The terminal runs a real
  local shell in that workspace. The page loads xterm.js from cdn.jsdelivr.net,
  so this gate needs network access. It proves page behaviour; how a screen
  reader voices the tree is owned by the browser and the screen reader.

USAGE
  python tests/browser_terminal_screen_reader_mode.py
  (TERMINAL_SR_MODE_PORT pins the port; by default a free one is picked.)
  (Requires: playwright + chromium; firefox is used when installed.)

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, xterm.js
      not reachable on cdn.jsdelivr.net, etc.)
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


PORT = int(os.getenv("TERMINAL_SR_MODE_PORT") or _free_port())
BASE = f"http://127.0.0.1:{PORT}"
MARKER = "sr-mode-marker-42"
# The shell expands this to MARKER, so MARKER appears only in the output row,
# never in the typed command line.
MARKER_COMMAND = "echo sr-mode-marker-$((40+2))"


class _SetupError(Exception):
    """The environment, not the product, kept the gate from running."""


def _wait_for_health(proc, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        # A child that exited cannot be the server answering /health.
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _wait_until(page, expression, timeout_ms=8000):
    """Poll ``expression`` with page.evaluate. The app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(100)
    return False


BUFFER_HAS_TEXT = (
    "(()=>{const b=TERMINAL_UI.term.buffer.active;for(let i=0;i<b.length;i++)"
    "{const l=b.getLine(i);if(l&&l.translateToString(true).trim())return true}"
    "return false})()"
)
SHORT_PROMPT_SHOWN = (
    "(()=>{const b=TERMINAL_UI.term.buffer.active;for(let i=0;i<b.length;i++)"
    "{const l=b.getLine(i);if(l&&l.translateToString(true).trim()==='$')return true}"
    "return false})()"
)
TREE = "!!document.querySelector('#terminalSurface .xterm-accessibility-tree')"
TREE_HAS_MARKER_ROW = (
    "[...document.querySelectorAll('#terminalSurface .xterm-accessibility-tree > div')]"
    f".some(r=>r.textContent.trim()==='{MARKER}')"
)


def _open_terminal(page):
    """Open the terminal the way the /terminal command does (cmdTerminal())."""
    if not _wait_until(page, "typeof S!=='undefined'&&!!S._bootReady", 20000):
        return False
    page.evaluate("cmdTerminal()")
    return _wait_until(
        page,
        "!!(TERMINAL_UI.open&&TERMINAL_UI.term&&TERMINAL_UI.sessionId&&TERMINAL_UI.source)",
        15000,
    )


def _set_preference(page, checked):
    """Set the preference through the Settings panel; return an error text or None."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    try:
        return _set_preference_or_raise(page, checked)
    except PlaywrightTimeout as e:
        return f"timed out: {str(e).splitlines()[0]}"


def _set_preference_or_raise(page, checked):
    # Wait for the panel to hydrate from the server, or it would overwrite the click.
    with page.expect_response(
        lambda r: r.url.endswith("/api/settings") and r.request.method == "GET",
        timeout=8000,
    ):
        page.evaluate("toggleSettings()")
    page.evaluate("switchSettingsSection('preferences')")
    box = page.locator("#settingsScreenReaderMode")
    box.wait_for(state="visible", timeout=8000)
    page.wait_for_timeout(300)
    if box.is_checked() != checked:
        with page.expect_response(
            lambda r: r.url.endswith("/api/settings") and r.request.method == "POST",
            timeout=8000,
        ) as resp:
            box.click()
        body = resp.value.json()
        if not _wait_until(page, f"window._screenReaderMode==={'true' if checked else 'false'}", 3000):
            return "the runtime flag did not follow the saved setting"
        if bool(body.get("screen_reader_mode")) is not checked:
            return f"server saved screen_reader_mode={body.get('screen_reader_mode')!r}"
    page.evaluate("toggleSettings()")
    return None


def _check(browser, name):
    failures = []
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE, wait_until="domcontentloaded")
    if not _wait_until(page, "!!document.querySelector('#msg')", 20000):
        ctx.close()
        raise _SetupError(f"[{name}] the composer did not load")
    if not _wait_until(page, "typeof window.Terminal==='function'", 20000):
        ctx.close()
        raise _SetupError(
            f"[{name}] xterm.js did not load from cdn.jsdelivr.net. The page loads it from that CDN, "
            "so this gate needs network access to it. This is an environment problem, not a regression."
        )

    if not _open_terminal(page):
        ctx.close()
        return [f"  [{name}] terminal did not open"]
    page.evaluate("TERMINAL_UI.term._srGateId=1")
    # Type only once the shell has printed its prompt, or the command can reach it
    # before it reads input.
    if not _wait_until(page, BUFFER_HAS_TEXT, 10000):
        ctx.close()
        return [f"  [{name}] the shell printed no prompt"]
    page.evaluate("focusComposerTerminalInput()")
    # Set a short prompt first. xterm's accessibility tree mirrors only the viewport, and
    # opening Settings resizes the terminal, so the shell redraws its prompt as narrow
    # fragments. A long prompt (a long $TMPDIR in the cwd, as on macOS) then pushes the
    # marker above the viewport and the check below fails on a working build.
    with page.expect_response(
        lambda r: "/api/terminal/input" in r.url and r.request.method == "POST",
        timeout=10000,
    ):
        page.keyboard.insert_text("PS1='$ '")
    page.keyboard.press("Enter")
    if not _wait_until(page, SHORT_PROMPT_SHOWN, 10000):
        ctx.close()
        return [f"  [{name}] the shell did not show the short prompt"]
    # Send the command as one input POST and wait for it before Enter. Typing key by
    # key sends one POST per character, and those can reach the shell out of order.
    with page.expect_response(
        lambda r: "/api/terminal/input" in r.url and r.request.method == "POST",
        timeout=10000,
    ):
        page.keyboard.insert_text(MARKER_COMMAND)
    page.keyboard.press("Enter")
    if not _wait_until(
        page,
        f"(()=>{{const b=TERMINAL_UI.term.buffer.active;for(let i=0;i<b.length;i++)"
        f"{{const l=b.getLine(i);if(l&&l.translateToString(true).trim()==='{MARKER}')return true}}"
        f"return false}})()",
        10000,
    ):
        failures.append(f"  [{name}] the shell did not echo the marker")

    if page.evaluate(TREE):
        failures.append(f"  [{name}] preference off: terminal already has an accessibility tree")

    err = _set_preference(page, True)
    if err:
        failures.append(f"  [{name}] enabling: {err}")
    if not page.evaluate("TERMINAL_UI.term&&TERMINAL_UI.term._srGateId===1"):
        failures.append(f"  [{name}] enabling replaced the terminal instance")
    if not _wait_until(page, TREE):
        failures.append(f"  [{name}] enabling: the open terminal got no accessibility tree")
    # xterm fills the rows of a freshly built tree on a later render pass.
    elif not _wait_until(page, TREE_HAS_MARKER_ROW):
        failures.append(f"  [{name}] enabling: earlier output is missing from the accessibility tree")

    err = _set_preference(page, False)
    if err:
        failures.append(f"  [{name}] disabling: {err}")
    if not _wait_until(page, "!" + TREE):
        failures.append(f"  [{name}] disabling: the accessibility tree stayed on the open terminal")

    err = _set_preference(page, True)
    if err:
        failures.append(f"  [{name}] re-enabling before the reload: {err}")
    page.reload(wait_until="domcontentloaded")
    # Without a saved "on", the checks after the reload would only repeat that failure.
    if not err:
        if not _wait_until(page, "typeof window.Terminal==='function'&&window._screenReaderMode===true", 20000):
            failures.append(f"  [{name}] after reload: the saved preference was not applied on boot")
        elif not _open_terminal(page):
            failures.append(f"  [{name}] after reload: terminal did not open")
        elif not _wait_until(page, TREE):
            failures.append(f"  [{name}] after reload: a new terminal started without the accessibility tree")
    # Leave the shared server with the preference off for the next browser.
    err = _set_preference(page, False)
    if err:
        failures.append(f"  [{name}] resetting the preference: {err}")

    for e in errors:
        failures.append(f"  [{name}] pageerror: {e}")
    if not failures:
        print(f"OK  {name} — off by default, live on/off on the open terminal, on after reload")
    ctx.close()
    return failures


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    # A server already listening on PORT would answer /health and the checks would
    # silently run against ITS code and state, not this checkout's. Refuse instead.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"SETUP FAIL: port {PORT} is already in use; pick another TERMINAL_SR_MODE_PORT or unset it", file=sys.stderr)
            return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-terminal-sr-mode-")
    workspace = os.path.join(state_dir, "workspace")
    os.makedirs(workspace)
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
        "HERMES_WEBUI_DEFAULT_WORKSPACE": workspace,
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(proc, timeout=30):
            print("SETUP FAIL: server exited or did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        with sync_playwright() as pw:
            engines = [("chromium", pw.chromium, ["--no-sandbox", "--disable-dev-shm-usage"])]
            engines.append(("firefox", pw.firefox, []))
            for name, engine, args in engines:
                try:
                    browser = engine.launch(headless=True, args=args)
                except Exception as e:  # engine not installed
                    if name == "chromium":
                        print(f"SETUP FAIL: chromium did not launch: {e}", file=sys.stderr)
                        return 2
                    print(f"SKIP {name}: {str(e).splitlines()[0]}")
                    continue
                try:
                    failures.extend(_check(browser, name))
                except _SetupError as e:
                    if failures:
                        print("\nTERMINAL SCREEN READER MODE FAILED before the setup failure:", file=sys.stderr)
                        print("\n".join(failures), file=sys.stderr)
                    print(f"SETUP FAIL: {e}", file=sys.stderr)
                    return 2
                finally:
                    browser.close()

        if failures:
            print("\nTERMINAL SCREEN READER MODE FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nTERMINAL SCREEN READER MODE PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
