#!/usr/bin/env python3
"""
Headless browser gate: Enter approves a pending tool action only when no control
has focus (#8130).

WHY THIS EXISTS
  While the approval card is on screen, the global keydown handler in boot.js
  turned Enter into "Allow once" whenever focus was outside a text field, and
  cancelled the key's own default. A keyboard user who tabbed to "Deny" (or
  "Always allow") and pressed Enter approved the command once instead, and Enter
  on any other focused control (the conversation's ⋮ trigger, a menu item, the
  project picker) approved the pending action and did nothing else.

WHAT IT CHECKS, with the approval card visible and respondApproval recorded
  - Enter with nothing focused still means "Allow once" (the shortcut stays);
  - Enter on each card button runs that button's own choice, exactly once;
  - Enter on a button outside the card activates that button and approves nothing;
  - Enter on a menu item outside the card (role="menuitem") approves nothing;
  - Enter in the composer approves nothing;
  - Enter in an editor that blurs itself, or on a control that removes itself,
    approves nothing (the shortcut judges the key's target, not where focus ended);
  - Space on "Deny" denies (control: Space was never intercepted);
  - with the card hidden, Enter approves nothing.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state. The card is put on screen the way the handler
  reads it (the `visible` class), and respondApproval is replaced by a recorder
  so nothing is sent.

USAGE
  python tests/browser_approval_enter_shortcut.py
  (Requires: playwright + chromium.)

EXIT CODES
  0 — every check passed
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

PORT = int(os.getenv("APPROVAL_ENTER_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"

SETUP_JS = """() => {
  window.__approvals = [];
  window.__probeClicks = 0;
  window.respondApproval = function (choice) {
    window.__approvals.push(choice);
    return Promise.resolve();
  };
  if (!document.getElementById('approvalEnterProbe')) {
    const probe = document.createElement('button');
    probe.type = 'button';
    probe.id = 'approvalEnterProbe';
    probe.textContent = 'Probe';
    probe.addEventListener('click', () => { window.__probeClicks += 1; });
    document.body.appendChild(probe);
    const item = document.createElement('div');
    item.id = 'approvalEnterMenuItem';
    item.setAttribute('role', 'menuitem');
    item.tabIndex = 0;
    item.textContent = 'Menu item';
    document.body.appendChild(item);
    // Like the queued-message editor: Enter commits and blurs the editor, so by the
    // time the document listener runs, focus is on the body.
    const editor = document.createElement('div');
    editor.id = 'approvalEnterSelfBlurEditor';
    editor.contentEditable = 'true';
    editor.textContent = 'queued message';
    editor.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); editor.blur(); } });
    document.body.appendChild(editor);
  }
  // Like "Show earlier steps": Enter activates the control and it removes itself.
  // Rebuilt for every case so each run starts with a fresh control.
  const old = document.getElementById('approvalEnterSelfRemoving');
  if (old) old.remove();
  const pill = document.createElement('button');
  pill.type = 'button';
  pill.id = 'approvalEnterSelfRemoving';
  pill.textContent = 'Show earlier steps';
  pill.addEventListener('keydown', (e) => { if (e.key === 'Enter') { window.__probeClicks += 1; pill.remove(); } });
  document.body.appendChild(pill);
  return typeof window.respondApproval === 'function';
}"""

SHOW_CARD_JS = """(visible) => {
  const card = document.getElementById('approvalCard');
  if (!card) return false;
  if (visible) {
    card.hidden = false;
    card.removeAttribute('inert');
    card.setAttribute('aria-hidden', 'false');
    card.classList.add('visible');
  } else {
    card.classList.remove('visible');
    card.hidden = true;
  }
  return true;
}"""

FOCUS_JS = """(selector) => {
  window.__approvals = [];
  window.__probeClicks = 0;
  if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
  if (!selector) return document.activeElement === document.body;
  const el = document.querySelector(selector);
  if (!el) return false;
  el.focus();
  return document.activeElement === el;
}"""

STATE_JS = "() => ({approvals: window.__approvals.slice(), probe: window.__probeClicks})"

# (label, selector or None for "nothing focused", key, card visible, expected approvals, expected probe clicks)
CASES = [
    ("Enter with nothing focused", None, "Enter", True, ["once"], 0),
    ("Enter on Allow once", "#approvalBtnOnce", "Enter", True, ["once"], 0),
    ("Enter on Allow session", "#approvalBtnSession", "Enter", True, ["session"], 0),
    ("Enter on Always allow", "#approvalBtnAlways", "Enter", True, ["always"], 0),
    ("Enter on Deny", "#approvalBtnDeny", "Enter", True, ["deny"], 0),
    ("Enter on a button outside the card", "#approvalEnterProbe", "Enter", True, [], 1),
    ("Enter on a menu item outside the card", "#approvalEnterMenuItem", "Enter", True, [], 0),
    ("Enter in the composer", "#msg", "Enter", True, [], 0),
    ("Enter in an editor that blurs itself on Enter", "#approvalEnterSelfBlurEditor", "Enter", True, [], 0),
    ("Enter on a control that removes itself on Enter", "#approvalEnterSelfRemoving", "Enter", True, [], 1),
    ("Space on Deny", "#approvalBtnDeny", "Space", True, ["deny"], 0),
    ("Enter with the card hidden", None, "Enter", False, [], 0),
]


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


def _check(browser):
    failures = []
    ctx = browser.new_context(base_url=BASE, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", timeout=15000)
    page.wait_for_timeout(1000)
    if not page.evaluate(SETUP_JS):
        ctx.close()
        return ["  setup: respondApproval could not be recorded"]
    for label, selector, key, visible, want, want_probe in CASES:
        page.evaluate(SETUP_JS)
        if not page.evaluate(SHOW_CARD_JS, visible):
            failures.append(f"  [{label}] the approval card is missing")
            continue
        if not page.evaluate(FOCUS_JS, selector):
            failures.append(f"  [{label}] could not focus {selector or 'the page'}")
            continue
        page.keyboard.press(key)
        page.wait_for_timeout(150)
        got = page.evaluate(STATE_JS)
        if got["approvals"] != want or got["probe"] != want_probe:
            failures.append(
                f"  [{label}] approvals {got['approvals']} (want {want}), "
                f"probe clicks {got['probe']} (want {want_probe})"
            )
        else:
            print(f"OK  {label}: approvals {want}, probe clicks {want_probe}")
    page.evaluate(SHOW_CARD_JS, False)
    for err in errors:
        failures.append(f"  pageerror: {err}")
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
    state_dir = tempfile.mkdtemp(prefix="hermes-approval-enter-")
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

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            failures = _check(browser)
            browser.close()

        if failures:
            print("\nAPPROVAL ENTER SHORTCUT FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("\nAPPROVAL ENTER SHORTCUT PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
