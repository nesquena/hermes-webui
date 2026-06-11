"""Regression tests for #3993 — self-heal stuck session loads on non-404 failures.

A session load that fails with a non-404, non-401 error (400 / 403 / 500 /
network) during boot used to leave a stale session id in localStorage + URL, so
every subsequent refresh retried the same dead session and the WebUI could not
load ANY chat. loadSession() now calls _clearStuckSessionOnBoot(sid, currentSid)
which clears the stale id ONLY when no session is currently on screen
(!currentSid) — never when the user is already viewing a healthy session (a
500/network blip there may be transient, #4028), and never on a click into a
*different* dead session (localStorage/URL still point at the live one, #2782).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_clear_stuck_session_helper_exists_and_is_wired():
    """The non-404 failure branch must delegate to _clearStuckSessionOnBoot."""
    js = _read(SESSIONS_JS)
    marker = "function _clearStuckSessionOnBoot(sid, currentSid){"
    assert marker in js
    # Wired into the non-404 error branch.
    assert "_clearStuckSessionOnBoot(sid, currentSid);" in js
    # Guarded on the boot condition (no active session on screen).
    helper = js[js.index(marker): js.index(marker) + 260]
    assert "if(!currentSid){" in helper
    assert "localStorage.removeItem('hermes-webui-session')" in helper
    assert "history.replaceState" in helper


def _run_helper(current_sid_js: str) -> dict:
    """Run _clearStuckSessionOnBoot in node with a fake localStorage/history and
    report whether each was cleared."""
    assert NODE, "node is required"
    js = _read(SESSIONS_JS)
    start = js.index("function _clearStuckSessionOnBoot(sid, currentSid){")
    end = js.index("\n}", start) + 2
    helper_src = js[start:end]
    script = f"""
let removed=false, replaced=false;
const localStorage = {{ removeItem(k){{ if(k==='hermes-webui-session') removed=true; }} }};
const history = {{ replaceState(){{ replaced=true; }} }};
function _appRootPath(){{ return '/'; }}
{helper_src}
_clearStuckSessionOnBoot('dead-sid', {current_sid_js});
process.stdout.write(JSON.stringify({{removed, replaced}}));
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


def test_clears_stale_session_on_boot_failure():
    """No active session (boot) + a failed load → clear the stale id so the next
    boot doesn't retry the dead session."""
    data = _run_helper("null")
    assert data["removed"] is True
    assert data["replaced"] is True


def test_does_not_clear_when_viewing_a_healthy_session():
    """An active session on screen (currentSid set) → do NOT wipe localStorage/URL;
    the failure may be transient and the live session must survive (#2782/#4028)."""
    data = _run_helper("'live-session-123'")
    assert data["removed"] is False
    assert data["replaced"] is False


def test_stale_load_guard_present_before_self_heal():
    """A superseded in-flight load (a newer loadSession started during the await)
    must bail BEFORE any self-heal/DOM mutation, so a failed boot restore can't
    wipe localStorage/URL for the session the user navigated to mid-flight (Codex
    race finding). The guard re-arms the active stream and returns."""
    js = _read(SESSIONS_JS)
    # Anchor on the self-heal CALL (unique; the bare name also appears in the
    # helper's docstring), then look at the preceding window of the same
    # loadSession catch block for the stale-load guard.
    heal_idx = js.index("_clearStuckSessionOnBoot(sid, currentSid);")
    block = js[heal_idx - 2400: heal_idx + 60]
    guard = "if (_loadingSessionId !== sid) {"
    assert guard in block, "stale-load guard missing from the loadSession catch block"
    # The guard must come BEFORE the self-heal call (so a superseded load can't clear).
    assert block.index(guard) < block.index("_clearStuckSessionOnBoot(sid, currentSid);"), \
        "stale-load guard must precede _clearStuckSessionOnBoot"
    # And before the 404 inline clear too.
    assert block.index(guard) < block.index("localStorage.removeItem('hermes-webui-session')"), \
        "stale-load guard must precede the 404 inline self-heal"
    # It re-arms the active stream rather than leaving it torn down.
    guard_tail = block[block.index(guard): block.index(guard) + 120]
    assert "_rearmActiveSessionStream()" in guard_tail
