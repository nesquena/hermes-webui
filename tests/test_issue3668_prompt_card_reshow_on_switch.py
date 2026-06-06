"""Regression tests for #3668 — approval/clarify cards must re-show on switch-back.

Reported symptom (#3668, 2026-06-05): when a user switches away from a session
that is blocked waiting on a `clarify` (or `approval`) prompt and then switches
back, the card appears to vanish — making the agent look permanently stuck with
no prompt to answer.

The reporter's root-cause analysis cited `sessions.js` lines 715-718, where
`loadSession()` unconditionally hides the clarify/approval cards at the *top* of
a session switch. That part is real — but it is only the teardown half. The
*re-show* half ships in the same `loadSession()` further down and was missed:

  1. Pending prompts are cached per-session in the in-memory maps
     `_clarifyPendingBySession` / `_approvalPendingBySession` (NOT cleared on
     switch — only the visible card is hidden).
  2. `_renderPendingPromptsForActiveSession()` runs near the end of every
     `loadSession()` and re-dispatches the cached pending prompt for the
     now-active session to the show helper (and hides any other session's card
     without clearing its cache).
  3. For the live/blocked case, `loadSession()` also re-arms
     `startClarifyPolling(sid)` / `startApprovalPolling(sid)`, whose SSE
     `initial` event re-fetches server state and calls
     `showClarifyForSession` / `showApprovalForSession`. This covers the
     uncached path (e.g. a fresh page reload where the in-memory map is empty).

This machinery shipped in v0.51.19 (PR #1829, "keep approval and clarify prompts
session-owned"), a month before #3668 was filed, and was verified live: loading
a clarify-blocked session, switching away, and switching back re-shows the card
with the correct question.

These tests lock the behavioral invariant so it cannot silently regress:

* `test_clarify_card_reshows_on_switch_back` / `test_approval_..._reshows_...`
  run the REAL extracted JS functions through node and drive the exact
  switch-away → switch-back sequence, asserting the card is re-shown for the
  returning session while the other session's card was hidden and neither
  session's pending cache was discarded.
* The source-invariant tests assert the polling re-arm + SSE `initial` re-fetch
  wiring (the uncached / fresh-reload path) stays in place.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
MESSAGES_JS = MESSAGES_JS_PATH.read_text(encoding="utf-8")
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ── node driver ─────────────────────────────────────────────────────────────
# Extracts the REAL render/cache helpers from messages.js by brace-matching,
# stubs only the leaf DOM-touching show/hide helpers (recording their calls and
# mirroring the _clarifySessionId / _approvalSessionId bookkeeping the real ones
# perform), then drives a clarify+approval prompt through:
#     show on A  →  switch to B  →  switch back to A
# and prints the recorded call log + surviving cache state as JSON.

_DRIVER = r"""
const fs = require('fs');
// node -e shifts argv: with `node -e SCRIPT FILE`, FILE is argv[1].
const src = fs.readFileSync(process.argv[1], 'utf8');

function extractFunc(name){
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length){ if(src[i]==='{')depth++; else if(src[i]==='}')depth--; i++; }
  return src.slice(start, i);
}

// ── module-scope state the extracted functions close over ──
let _clarifyPendingBySession = new Map();
let _approvalPendingBySession = new Map();
let _clarifySessionId = null;
let _approvalSessionId = null;
let S = { session: null };

// ── recorded calls ──
const calls = [];

// ── leaf stubs: record + mirror the real bookkeeping ──
// real showClarifyCard sets _clarifySessionId; real hideClarifyCard nulls it.
function showClarifyCard(pending){
  const sid = (pending && pending._session_id) || (S.session && S.session.session_id) || null;
  _clarifySessionId = sid;
  calls.push({fn:'showClarifyCard', sid, q:(pending && pending.question) || null});
}
function hideClarifyCard(force, reason){
  _clarifySessionId = null;
  calls.push({fn:'hideClarifyCard', force:!!force, reason:reason||null});
}
function showApprovalCard(pending, count){
  const sid = (pending && pending._session_id) || (S.session && S.session.session_id) || null;
  _approvalSessionId = sid;
  calls.push({fn:'showApprovalCard', sid, count:count||null});
}
function hideApprovalCard(force){
  _approvalSessionId = null;
  calls.push({fn:'hideApprovalCard', force:!!force});
}

// ── real functions under test ──
eval(extractFunc('_promptActiveSessionId'));
eval(extractFunc('_clarifyPromptBelongsToActiveSession'));
eval(extractFunc('_rememberClarifyPending'));
eval(extractFunc('_clearClarifyPendingForSession'));
eval(extractFunc('_renderPendingClarifyForActiveSession'));
eval(extractFunc('_approvalPromptBelongsToActiveSession'));
eval(extractFunc('_rememberApprovalPending'));
eval(extractFunc('_clearApprovalPendingForSession'));
eval(extractFunc('_renderPendingApprovalForActiveSession'));
eval(extractFunc('_renderPendingPromptsForActiveSession'));

const SIDA = 'sessA', SIDB = 'sessB';

// 1) Agent on A asks a clarify AND hits an approval gate while A is active.
S.session = { session_id: SIDA };
_rememberClarifyPending({ question: 'Which deploy target?', _session_id: SIDA });
_rememberApprovalPending({ command: 'rm -rf build', _session_id: SIDA }, 1);
_renderPendingPromptsForActiveSession();
const afterShowA = calls.slice();

// 2) Switch AWAY to B (no pending prompts on B).
calls.length = 0;
S.session = { session_id: SIDB };
_renderPendingPromptsForActiveSession();
const afterSwitchB = calls.slice();
const cacheSurvivedClarify = _clarifyPendingBySession.has(SIDA);
const cacheSurvivedApproval = _approvalPendingBySession.has(SIDA);

// 3) Switch BACK to A — the #3668 scenario.
calls.length = 0;
S.session = { session_id: SIDA };
_renderPendingPromptsForActiveSession();
const afterSwitchBackA = calls.slice();

console.log(JSON.stringify({
  afterShowA, afterSwitchB, afterSwitchBackA,
  cacheSurvivedClarify, cacheSurvivedApproval,
  finalClarifyOwner: _clarifySessionId,
  finalApprovalOwner: _approvalSessionId,
}));
"""


def _run_driver():
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(MESSAGES_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed:\n{proc.stderr}"
    import json

    return json.loads(proc.stdout.strip().splitlines()[-1])


def _shown(calls, fn, sid):
    return any(c["fn"] == fn and c.get("sid") == sid for c in calls)


def _hidden(calls, fn):
    return any(c["fn"] == fn and c.get("force") for c in calls)


def test_clarify_card_reshows_on_switch_back():
    """Switch away from a clarify-blocked session, switch back → card re-shows."""
    r = _run_driver()
    # Initially shown on A.
    assert _shown(r["afterShowA"], "showClarifyCard", "sessA"), \
        "clarify card should show for A when A is active and has a pending clarify"
    # Switching to B hides A's card but does NOT discard A's cached pending.
    assert _hidden(r["afterSwitchB"], "hideClarifyCard"), \
        "A's clarify card should be hidden when switching to B"
    assert not _shown(r["afterSwitchB"], "showClarifyCard", "sessA"), \
        "A's clarify card must not re-show while B is active"
    assert r["cacheSurvivedClarify"], \
        "A's pending clarify must survive the switch (cache not cleared on hide)"
    # The core #3668 assertion: switching BACK to A re-shows A's clarify card.
    assert _shown(r["afterSwitchBackA"], "showClarifyCard", "sessA"), \
        "#3668: clarify card MUST re-show when switching back to the blocked session"
    assert r["finalClarifyOwner"] == "sessA"


def test_approval_card_reshows_on_switch_back():
    """Same invariant for the approval card (the more-severe #3668 sub-case)."""
    r = _run_driver()
    assert _shown(r["afterShowA"], "showApprovalCard", "sessA"), \
        "approval card should show for A when A is active and has a pending approval"
    assert _hidden(r["afterSwitchB"], "hideApprovalCard"), \
        "A's approval card should be hidden when switching to B"
    assert not _shown(r["afterSwitchB"], "showApprovalCard", "sessA"), \
        "A's approval card must not re-show while B is active"
    assert r["cacheSurvivedApproval"], \
        "A's pending approval must survive the switch (cache not cleared on hide)"
    assert _shown(r["afterSwitchBackA"], "showApprovalCard", "sessA"), \
        "#3668: approval card MUST re-show when switching back to the blocked session"
    assert r["finalApprovalOwner"] == "sessA"


def test_load_session_rearms_prompt_polling_for_uncached_path():
    """loadSession must restart clarify/approval polling so the SSE `initial`
    event re-fetches and re-shows a pending prompt even when the in-memory cache
    is empty (the fresh-page-reload path)."""
    # Both branches of loadSession that handle an active stream re-arm polling.
    assert SESSIONS_JS.count("startClarifyPolling(sid)") >= 1, \
        "loadSession must re-arm clarify polling on switch-back to a live session"
    assert SESSIONS_JS.count("startApprovalPolling(sid)") >= 1, \
        "loadSession must re-arm approval polling on switch-back to a live session"
    # And loadSession explicitly re-renders cached prompts for the new session.
    assert "_renderPendingPromptsForActiveSession();" in SESSIONS_JS


def test_sse_initial_event_reshows_pending_prompt():
    """The clarify/approval SSE `initial` event (fired on every (re)connect)
    must dispatch a pending prompt to the session-owned show helper — this is
    what re-shows the card when polling re-arms on switch-back."""
    # initial handlers exist and route through the session-owned helpers.
    assert "showClarifyForSession(sid" in MESSAGES_JS
    assert "showApprovalForSession(sid" in MESSAGES_JS
    # the 'initial' SSE event specifically is wired (not only the live event).
    assert "addEventListener('initial'" in MESSAGES_JS


def test_close_live_stream_snapshots_turn_before_teardown():
    """#3668 'stays gone' variant: switching away from a streaming session during
    a quiet window (mid tool-exec / silent thinking, between content SSE events)
    must still preserve the live thinking/tool content on switch-back.

    The per-event snapshot (snapshotLiveTurn) only fires on content/tool_complete
    events, so closeLiveStream() — the switch-away teardown — must capture a DOM
    snapshot BEFORE closing the source. Otherwise restoreLiveTurnHtmlForSession()
    finds no/stale snapshot and loadSession()'s fallback rebuilds with an EMPTY
    appendThinking(), permanently losing the streamed content (only the elapsed
    clock survives — the reported signature).
    """
    # Extract the closeLiveStream body and assert the snapshot precedes teardown.
    start = MESSAGES_JS.index("function closeLiveStream(")
    brace = MESSAGES_JS.index("{", start)
    depth = 0
    body = ""
    for i in range(brace, len(MESSAGES_JS)):
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
            if depth == 0:
                body = MESSAGES_JS[brace + 1 : i]
                break
    assert body, "closeLiveStream body not found"
    snap_idx = body.find("snapshotLiveTurnHtmlForSession(sessionId)")
    close_idx = body.find("live.source.close()")
    delete_idx = body.find("delete LIVE_STREAMS[sessionId]")
    assert snap_idx != -1, (
        "closeLiveStream() must snapshot the live turn (snapshotLiveTurnHtmlForSession) "
        "before tearing the stream down, or switch-away during a quiet window loses content (#3668)."
    )
    assert close_idx != -1 and snap_idx < close_idx, (
        "the snapshot must be taken BEFORE live.source.close()."
    )
    assert delete_idx != -1 and snap_idx < delete_idx, (
        "the snapshot must be taken BEFORE LIVE_STREAMS teardown."
    )
