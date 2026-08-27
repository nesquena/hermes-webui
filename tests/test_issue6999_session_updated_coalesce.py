"""#6999 re-gate -- `session-updated` frames must COALESCE, not be dropped.

The first #6999 iteration made the messages.js `session-updated` handler
return immediately when the visibility-recovery probe owned the shared
``_activeSessionExternalRefreshInFlight`` guard. That dropped the update
entirely: a real-callback schedule emitted a larger count while the refresh
owned the guard and zero loads happened; only a manually repeated event after
release caused a load. Production does not guarantee that second event.

Fix (reviewer spec): latch the MAXIMUM pending count per SID while the guard
is held, and in the owner's ``finally`` run ONE guarded follow-up when local
state is still behind. The follow-up re-enters
``refreshActiveSessionIfExternallyUpdated`` so every OOM guard (busy, stream,
loading, hidden) still applies, and it only force-reloads when the metadata
probe actually observes the count change.

Scenarios covered: metadata-read (probe ran before the write landed),
same-SID load (already caught up -> no second load), switch-away (latched for
the old SID -> no follow-up), duplicate events (max latched -> ONE follow-up).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(src: str, name: str) -> str:
    """Return the full ``function name(...) {...}`` source from ``src``."""
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    params = src.find("(", start)
    assert params != -1, f"{name} has no parameter list"
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1, f"{name} parameter list did not close"
    brace = src.find("{", close)
    assert brace != -1, f"{name} has no body"
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


def test_pending_count_latch_and_drain_helpers_exist():
    assert "function _latchSessionUpdatedPendingCount(sid, count)" in SESSIONS_JS
    assert "function _drainSessionUpdatedPendingCount()" in SESSIONS_JS
    assert "let _pendingSessionUpdatedCounts = null;" in SESSIONS_JS
    # The owner's finally must drain the latch (coalesce, never discard).
    owner = _function_source(SESSIONS_JS, "refreshActiveSessionIfExternallyUpdated")
    assert "_drainSessionUpdatedPendingCount();" in owner
    assert "_activeSessionExternalRefreshInFlight = false;" in owner
    # The drain must re-enter the guarded refresh (single follow-up).
    drain = _function_source(SESSIONS_JS, "_drainSessionUpdatedPendingCount")
    assert "refreshActiveSessionIfExternallyUpdated('session-updated')" in drain
    assert "_pendingSessionUpdatedCounts = null;" in drain
    assert "localCount >= latched" in drain, "caught-up local state must skip the follow-up"


def test_messages_handler_latches_instead_of_dropping_when_guard_held():
    start = MESSAGES_JS.index("es.addEventListener('session-updated', e => {")
    end = MESSAGES_JS.index("\n    });", start) + len("\n    });")
    handler = MESSAGES_JS[start:end]
    assert "_coalesceSessionUpdatedWhileRefreshHeld(sid, serverCount)" in handler, (
        "the handler must route the frame through the coalesce helper while the guard is held"
    )
    # The old bare-return must be gone from this handler.
    assert "_activeSessionExternalRefreshInFlight) return;" not in handler


def _run_node(script: str) -> dict:
    assert NODE, "node is required for the coalesce harness"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_COALESCE_NODE_BODY = r"""
const record = [];
let _pendingSessionUpdatedCounts = null;
const S = {session: {session_id: 's1', message_count: 3}, messages: []};
global.refreshActiveSessionIfExternallyUpdated = async (reason) => { record.push(['followup', reason]); };
global.S = S;
"""


def _coalesce_script(body: str) -> str:
    fns = "\n\n".join(
        [
            _function_source(SESSIONS_JS, "_latchSessionUpdatedPendingCount"),
            _function_source(SESSIONS_JS, "_drainSessionUpdatedPendingCount"),
        ]
    )
    return f"{_COALESCE_NODE_BODY}\n{fns}\n{body}"


def test_latch_takes_max_count_and_drain_runs_one_followup():
    script = _coalesce_script(
        r"""
(async () => {
  // Duplicate events while the guard is held: 3, then 5, then 4.
  _latchSessionUpdatedPendingCount('s1', 3);
  _latchSessionUpdatedPendingCount('s1', 5);
  _latchSessionUpdatedPendingCount('s1', 4);
  _drainSessionUpdatedPendingCount();
  // One follow-up only (coalesced), with the max latched count.
  console.log(JSON.stringify({record}));
})();
"""
    )
    out = _run_node(script)
    assert out["record"] == [["followup", "session-updated"]], out["record"]


def test_drain_skips_when_local_state_already_caught_up():
    script = _coalesce_script(
        r"""
(async () => {
  // A same-SID load during the refresh window already caught us up.
  S.session.message_count = 6;
  _latchSessionUpdatedPendingCount('s1', 6);
  _drainSessionUpdatedPendingCount();
  console.log(JSON.stringify({record}));
})();
"""
    )
    out = _run_node(script)
    assert out["record"] == [], out["record"]


def test_drain_skips_after_switch_away():
    script = _coalesce_script(
        r"""
(async () => {
  // User switched to another session while the refresh held the guard:
  // the latched count belongs to the OLD sid, so no follow-up may fire.
  S.session.session_id = 's2';
  _latchSessionUpdatedPendingCount('s1', 9);
  _drainSessionUpdatedPendingCount();
  console.log(JSON.stringify({record}));
})();
"""
    )
    out = _run_node(script)
    assert out["record"] == [], out["record"]


def test_drain_clears_latch_and_handles_multiple_sids():
    script = _coalesce_script(
        r"""
(async () => {
  // Events for another SID must not leak into the current session's follow-up.
  _latchSessionUpdatedPendingCount('s2', 50);
  S.session.message_count = 3;
  _latchSessionUpdatedPendingCount('s1', 8);
  _drainSessionUpdatedPendingCount();
  const afterDrain = _pendingSessionUpdatedCounts;
  console.log(JSON.stringify({record, afterDrain}));
})();
"""
    )
    out = _run_node(script)
    assert out["record"] == [["followup", "session-updated"]], out["record"]
    assert out["afterDrain"] is None, "the latch must be cleared after draining"


def test_coalesce_helper_latches_when_guard_held_and_passes_through_when_free():
    fns = "\n\n".join(
        [
            _function_source(SESSIONS_JS, "_latchSessionUpdatedPendingCount"),
            _function_source(SESSIONS_JS, "_coalesceSessionUpdatedWhileRefreshHeld"),
        ]
    )
    script = f"""
{_COALESCE_NODE_BODY}
let _activeSessionExternalRefreshInFlight = true;
{fns}
const heldResult = _coalesceSessionUpdatedWhileRefreshHeld('s1', 7);
const latchedWhileHeld = _pendingSessionUpdatedCounts && _pendingSessionUpdatedCounts['s1'];
_activeSessionExternalRefreshInFlight = false;
const freeResult = _coalesceSessionUpdatedWhileRefreshHeld('s1', 8);
const latchedWhileFree = _pendingSessionUpdatedCounts && _pendingSessionUpdatedCounts['s1'];
console.log(JSON.stringify({{heldResult, latchedWhileHeld, freeResult, latchedWhileFree}}));
"""
    out = _run_node(script)
    assert out["heldResult"] is True
    assert out["latchedWhileHeld"] == 7, "the guard-held frame must be latched"
    assert out["freeResult"] is False, "a free guard must pass through to the direct-load path"
    assert out["latchedWhileFree"] == 7, "a free-guard frame must NOT latch"


def test_messages_handler_latches_when_guard_held_and_loads_when_free():
    start = MESSAGES_JS.index("es.addEventListener('session-updated', e => {")
    # The handler body is the try/catch block before the handler's own
    # closing "    });" (4-space indent, at end of the addEventListener call).
    end = MESSAGES_JS.index("\n    });", start)
    body = MESSAGES_JS[start + len("es.addEventListener('session-updated', e => {") : end]
    script = f"""
const record = [];
const sid = 's1';
const S = {{session: {{session_id: 's1', message_count: 5}}, messages: [], activeStreamId: null}};
let _activeSessionExternalRefreshInFlight = true;
const _coalesceSessionUpdatedWhileRefreshHeld = (s, c) => {{
  if (!_activeSessionExternalRefreshInFlight) return false;
  record.push(['latch', s, c]);
  return true;
}};
const _isSessionCurrentPane = () => true;
const loadSession = (sid, opts) => record.push(['load', sid, opts]);
global.S = S;
const handler = (e) => {{
{body}
}};
// Guard held: the update must be latched, never dropped, and NOT loaded now.
handler({{data: JSON.stringify({{session_id: 's1', message_count: 9}})}});
const afterHeld = record.slice();
// Guard released: the same frame must load directly (unchanged behavior).
_activeSessionExternalRefreshInFlight = false;
handler({{data: JSON.stringify({{session_id: 's1', message_count: 9}})}});
console.log(JSON.stringify({{afterHeld, full: record}}));
"""
    out = _run_node(script)
    assert out["afterHeld"] == [["latch", "s1", 9]], out["afterHeld"]
    assert out["full"] == [
        ["latch", "s1", 9],
        ["load", "s1", {"force": True, "externalRefreshReason": "session-updated", "keepStaleUntilLoaded": True}],
    ], out["full"]
