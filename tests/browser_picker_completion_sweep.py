#!/usr/bin/env python3
"""
Headless browser gate: user-initiated completions paint under a newer picker.

WHY THIS EXISTS
  The "keep project picker" contract must hold for every completion a user can
  trigger while another conversation's project picker is open. A completion
  must paint its durable result on the spot and keep (re-anchor) the newer
  picker — instead of deferring behind it (the sidebar stays stale) or
  retiring it. The maintainer gate reproduces the failure as: hold A's
  mutation POST, open B's picker, release. This script replays that ordering
  in Chromium for the completion types: pin, title regeneration, the
  picker's own move/unassign, and archive.

WHAT IT CHECKS
  For each completion type, with B's picker open and A's response held:
  - A's sidebar was rebuilt by the completion repaint the moment the response
    lands (the durable result paints now, not when the picker closes);
  - B's picker stays open, stays the tracked overlay, and is re-anchored to
    B's rebuilt row;
  - no uncaught page error is introduced.

SCOPE
  Agent-free, like tests/browser_project_picker_keyboard.py: the real
  server.py on an ephemeral port with isolated temp state. The held response
  is released through the page's real ``api()`` (the title completion
  resolves with a controlled payload so no agent is involved).

USAGE
  python tests/browser_picker_completion_sweep.py
  (Requires: playwright + chromium, same as the keyboard gate.)

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — environment/setup failure
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import browser_project_picker_keyboard as kb  # shared drivers (same directory)

PORT = int(os.getenv("PICKER_SWEEP_PORT", "8799"))
kb.PORT = PORT
kb.BASE = f"http://127.0.0.1:{PORT}"

HOLD_WRAPPER_JS = r"""
(() => {
  if (window.__sweepHoldInstalled) return true;
  const original = window.api;
  if (typeof original !== 'function') return false;
  const hold = { match: null, pending: null, release: null };
  window.__sweepHold = hold;
  window.api = function (path, opts) {
    const p = String(path || '');
    if (hold.match && !hold.pending && p.indexOf(hold.match) === 0) {
      hold.pending = { path, opts };
      return new Promise(function (resolve, reject) {
        hold.release = function (value) {
          const held = hold.pending;
          hold.pending = null;
          hold.release = null;
          if (value !== undefined) { resolve(value); return; }
          original(held.path, held.opts).then(resolve, reject);
        };
      });
    }
    return original(path, opts);
  };
  window.__sweepArm = function (match) { hold.match = match; hold.pending = null; hold.release = null; return true; };
  window.__sweepDisarm = function () { hold.match = null; hold.pending = null; hold.release = null; };
  window.__sweepRelease = function (value) {
    if (!hold.release) return false;
    hold.release(value);
    return true;
  };
  window.__sweepHoldInstalled = true;
  return true;
})()
"""

MENU_CLICK_JS = r"""
({ sid, key }) => {
  const row = document.querySelector('.session-item[data-sid="' + sid + '"], .session-child-session[data-sid="' + sid + '"]');
  const trigger = row && row.querySelector('.session-actions-trigger');
  const session = (typeof _allSessions !== 'undefined') ? _allSessions.find(s => s && s.session_id === sid) : null;
  if (!row || !trigger || !session) return 'no-row';
  _openSessionActionMenu(session, trigger);
  const menu = document.querySelector('.session-action-menu');
  if (!menu) return 'no-menu';
  const label = t(key);
  const item = Array.from(menu.querySelectorAll('.session-action-opt'))
    .find(option => option.textContent.trim() === label);
  if (!item) return 'no-item:' + label;
  item.click();
  return 'clicked';
}
"""

MOVE_NONE_CLICK_JS = r"""
() => {
  const picker = document.querySelector('.project-picker:not(.batch-project-picker)');
  if (!picker) return 'no-picker';
  const item = Array.from(picker.querySelectorAll('.project-picker-item'))
    .find(option => option.textContent.trim() === t('project_picker_none'));
  if (!item) return 'no-item';
  item.click();
  return 'clicked';
}
"""

STATE_JS = r"""
({ sid, betaSid }) => {
  const open = (typeof _openProjectPicker !== 'undefined') ? _openProjectPicker : null;
  const picker = document.querySelector('.project-picker:not(.batch-project-picker)');
  const row = document.querySelector('.session-item[data-sid="' + sid + '"]');
  const betaRow = document.querySelector('.session-item[data-sid="' + betaSid + '"]');
  const anchor = open && open.anchorEl;
  const session = (typeof _allSessions !== 'undefined') ? _allSessions.find(s => s && s.session_id === sid) : null;
  return {
    rowExists: !!row,
    rowsRebuilt: !!row && row !== window.__sweepRowBefore,
    rowTitle: row ? row.textContent.trim() : '',
    pickerOpen: !!picker && picker.isConnected,
    pickerTracked: !!(open && picker && open.picker === picker),
    anchorInBetaRow: !!(anchor && betaRow && (betaRow === anchor || betaRow.contains(anchor))),
    anchorConnected: !!(anchor && anchor.isConnected !== false),
    sessionPinned: !!(session && session.pinned === true),
    sessionProjectId: session ? (session.project_id === undefined ? null : session.project_id) : 'no-session',
  };
}
"""


def _run(page, js, arg=None):
    if arg is None:
        return page.evaluate(js)
    return page.evaluate(js, arg)


def _capture_row(page, sid):
    return page.evaluate(
        "(sid) => { window.__sweepRowBefore = document.querySelector('.session-item[data-sid=\"' + sid + '\"]'); return !!window.__sweepRowBefore; }",
        sid,
    )


def _state(page, sid, beta_sid):
    return _run(page, STATE_JS, {"sid": sid, "betaSid": beta_sid})


def _close_picker(page):
    if page.evaluate("!!document.querySelector('.project-picker')"):
        page.keyboard.press("Escape")
        page.wait_for_timeout(kb.SETTLE_MS)


def _wait_painted(page, sid, beta_sid, timeout_ms=6000):
    """Wait until the completion repaint has rebuilt A's row while B's picker stays."""
    import time as _time
    end = _time.time() + timeout_ms / 1000.0
    st = _state(page, sid, beta_sid)
    while _time.time() < end:
        if st["rowsRebuilt"] and st["pickerOpen"] and st["pickerTracked"]:
            return st
        page.wait_for_timeout(50)
        st = _state(page, sid, beta_sid)
    return st


def check(browser, seed, fail):
    """Run the four completion scenarios on one page."""
    ctx, page, errors = kb._new_page(browser, viewport={"width": 1440, "height": 900})
    if page is None:
        fail(f"page did not initialize: {errors}")
        return
    try:
        if not _run(page, HOLD_WRAPPER_JS):
            fail("the page's api() could not be wrapped to hold a response")
            return
        seed = page.evaluate(kb.SEED_JS, {"projects": kb.PROJECTS})
        page.wait_for_timeout(kb.SETTLE_MS)
        alpha, beta = seed["alpha"], seed["beta"]

        # 1) Pin: hold the pin POST, open B's picker, release.
        if _run(page, "(m) => window.__sweepArm(m)", "/api/session/pin") is not True:
            fail("pin: could not arm the hold")
            return
        clicked = _run(page, MENU_CLICK_JS, {"sid": alpha, "key": "session_pin"})
        if clicked != "clicked":
            fail(f"pin: menu click failed ({clicked})")
            return
        if not kb._wait_until(page, "!!(window.__sweepHold && window.__sweepHold.pending)", 5000):
            fail("pin: the pin POST was not held")
            return
        problem = kb._open_single_picker(page, beta)
        if problem:
            fail(f"pin: opening B's picker failed ({problem})")
            return
        if not _capture_row(page, alpha):
            fail("pin: A's row was not found before the release")
            return
        if not _run(page, "() => window.__sweepRelease()"):
            fail("pin: no held response to release")
            return
        st = _wait_painted(page, alpha, beta)
        if not (st["rowsRebuilt"] and st["pickerOpen"] and st["pickerTracked"] and st["anchorInBetaRow"] and st["sessionPinned"]):
            fail(f"pin: completion not painted under the newer picker: {st}")
            return
        print("OK  sweep pin — A's pinned row repainted the moment the response landed, B's picker kept and re-anchored")
        _close_picker(page)

        # 2) Title regeneration: hold the regenerate POST, release with a controlled title.
        if _run(page, "(m) => window.__sweepArm(m)", "/api/session/title/regenerate") is not True:
            fail("title: could not arm the hold")
            return
        clicked = _run(page, MENU_CLICK_JS, {"sid": alpha, "key": "session_title_regenerate"})
        if clicked != "clicked":
            fail(f"title: menu click failed ({clicked})")
            return
        if not kb._wait_until(page, "!!(window.__sweepHold && window.__sweepHold.pending)", 5000):
            fail("title: the regenerate POST was not held")
            return
        problem = kb._open_single_picker(page, beta)
        if problem:
            fail(f"title: opening B's picker failed ({problem})")
            return
        if not _capture_row(page, alpha):
            fail("title: A's row was not found before the release")
            return
        if not _run(page, "(v) => window.__sweepRelease(v)", {"title": "Sweep title 6713"}):
            fail("title: no held response to release")
            return
        st = _wait_painted(page, alpha, beta)
        if not (st["rowsRebuilt"] and st["pickerOpen"] and st["pickerTracked"] and "Sweep title 6713" in st["rowTitle"]):
            fail(f"title: regenerated title not painted under the newer picker: {st}")
            return
        print("OK  sweep title — the regenerated title painted the moment the response landed, B's picker kept")
        _close_picker(page)

        # 3) Picker move/unassign: hold A's move POST, open B's picker, release.
        problem = kb._open_single_picker(page, alpha)
        if problem:
            fail(f"move: opening A's picker failed ({problem})")
            return
        if _run(page, "(m) => window.__sweepArm(m)", "/api/session/move") is not True:
            fail("move: could not arm the hold")
            return
        clicked = _run(page, MOVE_NONE_CLICK_JS)
        if clicked != "clicked":
            fail(f"move: 'No project' click failed ({clicked})")
            return
        if not kb._wait_until(page, "!!(window.__sweepHold && window.__sweepHold.pending)", 5000):
            fail("move: the move POST was not held")
            return
        problem = kb._open_single_picker(page, beta)
        if problem:
            fail(f"move: opening B's picker failed ({problem})")
            return
        if not _capture_row(page, alpha):
            fail("move: A's row was not found before the release")
            return
        if not _run(page, "() => window.__sweepRelease()"):
            fail("move: no held response to release")
            return
        st = _wait_painted(page, alpha, beta)
        if not (st["rowsRebuilt"] and st["pickerOpen"] and st["pickerTracked"] and st["sessionProjectId"] is None):
            fail(f"move: the unassigned row not painted under the newer picker: {st}")
            return
        print("OK  sweep move — the unassign repainted the moment the response landed, B's picker kept")
        _close_picker(page)

        # 4) Archive: hold the archive POST, open B's picker, release.
        if _run(page, "(m) => window.__sweepArm(m)", "/api/session/archive") is not True:
            fail("archive: could not arm the hold")
            return
        clicked = _run(page, MENU_CLICK_JS, {"sid": alpha, "key": "session_archive"})
        if clicked != "clicked":
            fail(f"archive: menu click failed ({clicked})")
            return
        if not kb._wait_until(page, "!!(window.__sweepHold && window.__sweepHold.pending)", 5000):
            fail("archive: the archive POST was not held")
            return
        problem = kb._open_single_picker(page, beta)
        if problem:
            fail(f"archive: opening B's picker failed ({problem})")
            return
        if not _capture_row(page, alpha):
            fail("archive: A's row was not found before the release")
            return
        if not _run(page, "() => window.__sweepRelease()"):
            fail("archive: no held response to release")
            return
        st = _wait_painted(page, alpha, beta)
        if not (st["pickerOpen"] and st["pickerTracked"] and st["anchorInBetaRow"]):
            fail(f"archive: B's picker was not kept through the completion: {st}")
            return
        if page.evaluate("(sid) => !!document.querySelector('.session-item[data-sid=\"' + sid + '\"]')", alpha):
            fail("archive: the archived row is still on screen (completion deferred behind the picker)")
            return
        print("OK  sweep archive — the archived row left the sidebar the moment the response landed, B's picker kept")
        _close_picker(page)
        _run(page, "() => window.__sweepDisarm()")

        for err in errors:
            fail(f"pageerror: {err}")
    finally:
        ctx.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(HERE)
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-picker-sweep-")
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
        **( {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {} ),
    )
    try:
        if not kb._wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        failures = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            check(browser, None, failures.append)
            browser.close()

        if failures:
            print("\nPICKER COMPLETION SWEEP FAILED:", file=sys.stderr)
            print("\n".join(f"  [sweep] {f}" for f in failures), file=sys.stderr)
            return 1
        print("\nPICKER COMPLETION SWEEP PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
