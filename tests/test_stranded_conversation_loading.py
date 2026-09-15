"""Node-harness behavior tests for stranded conversation loading settlement.

The in-flight latch helpers (``_conversationLoadingAgeMs`` /
``_sessionLoadInFlightFor``), the escape hatch
(``_settleStrandedConversationLoading``), the expiry armer
(``_armStrandedConversationLoadingTimer``), and the same-session force-reload
re-arm (``_restampStrandedPlaceholderForReload``) are extracted verbatim from
``static/sessions.js`` and exercised under node with hand-rolled DOM shims
(no JSDOM). Every assertion is on observable behavior — helper return values,
innerHTML writes, dataset re-stamps, scheduled-callback delays, and
retry-click dispatch — plus one narrow source-structure pin where a behavior
harness cannot reach production wiring (arm deadline).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    """Return the full function source for ``name`` from a single js file.

    Brace-depth tracking handles nested blocks and avoids fragile substring
    matching in the large, hand-formatted source file.
    """
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name} not found in sessions.js"

    brace_start = source.find("{", start)
    assert brace_start >= 0, f"function {name} is missing '{{'"

    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False

    for index in range(brace_start, len(source)):
        ch = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]

    raise AssertionError(f"Could not extract function {name}")


def _extract_const(source: str, name: str) -> str:
    """Return the full ``const name = ...;`` declaration from a single js file.

    The value pattern tolerates semicolons inside single-quoted JS string
    literals (e.g. ``'&amp;'``) by scanning to the first semicolon that is
    not inside a string.
    """
    start = source.find(f"const {name}")
    assert start >= 0, f"{name} not found in sessions.js"
    in_string = None
    escaped = False
    for index in range(start, len(source)):
        ch = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            continue
        if ch == ";":
            return source[start : index + 1]
    raise AssertionError(f"{name} declaration not terminated in sessions.js")


SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC = _extract_const(
    SESSIONS_SRC, "_SESSION_LOAD_IN_FLIGHT_MAX_MS"
)
RETRY_ESCAPES_SRC = _extract_const(SESSIONS_SRC, "_RETRY_ESCAPES")
CONVERSATION_LOADING_AGE_MS_SRC = _extract_function(
    SESSIONS_SRC, "_conversationLoadingAgeMs"
)
SESSION_LOAD_IN_FLIGHT_FOR_SRC = _extract_function(
    SESSIONS_SRC, "_sessionLoadInFlightFor"
)
SETTLE_STRANDED_CONVERSATION_LOADING_SRC = _extract_function(
    SESSIONS_SRC, "_settleStrandedConversationLoading"
)
ARM_STRANDED_TIMER_SRC = _extract_function(
    SESSIONS_SRC, "_armStrandedConversationLoadingTimer"
)
RESTAMP_STRANDED_PLACEHOLDER_SRC = _extract_function(
    SESSIONS_SRC, "_restampStrandedPlaceholderForReload"
)


_NODE_SCRIPT = r"""
const M = { loadCalls: [] };

function makeMsgInner({ text, stamp }) {
  const el = {
    dataset: {},
    _innerHTML: '',
    _textContent: String(text),
    _retryHandler: null,
  };
  if (stamp !== undefined && stamp !== null) {
    el.dataset.conversationLoadingSince = String(stamp);
  }
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) { this._innerHTML = String(v); this._textContent = ''; },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._textContent; },
    set(v) { this._textContent = String(v); this._innerHTML = ''; },
  });
  el.querySelector = (sel) => {
    if (sel === '#conversationLoadRetry') {
      return {
        addEventListener: (type, fn) => { if (type === 'click') el._retryHandler = fn; },
      };
    }
    return null;
  };
  return el;
}

function installEnv({ text, stamp, loadingSid, sessionId, generation }) {
  const msgInner = makeMsgInner({ text, stamp });
  globalThis.$ = (id) => (id === 'msgInner' ? msgInner : null);
  globalThis._loadingSessionId = loadingSid;
  if (generation !== undefined) globalThis._loadSessionGeneration = generation;
  else delete globalThis._loadSessionGeneration;
  globalThis.S = { session: sessionId === null ? null : { session_id: sessionId } };
  globalThis.loadSession = (sid, opts) => { M.loadCalls.push({ sid, opts }); };
  // The settle helper renders through t() in production; default to the
  // English fallback strings so assertions stay behavioral, not locale-bound.
  globalThis.t = (key) =>
    key === 'conversation_load_failed'
      ? 'Couldn\u2019t load this conversation.'
      : key === 'conversation_load_retry'
        ? 'Retry'
        : key;
  return msgInner;
}

__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__
__RETRY_ESCAPES_SRC__
__CONVERSATION_LOADING_AGE_MS_SRC__
__SESSION_LOAD_IN_FLIGHT_FOR_SRC__
__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__

function runLatchScenarios() {
  const results = {};
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: 'sid-a', sessionId: 'sid-other' });
    results.fresh = _sessionLoadInFlightFor('sid-a');
  }
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: 'sid-a', sessionId: 'sid-other' });
    results.stale = _sessionLoadInFlightFor('sid-a');
  }
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: 'sid-b', sessionId: 'sid-other' });
    results.otherSid = _sessionLoadInFlightFor('sid-a');
  }
  {
    installEnv({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: null, sessionId: 'sid-other' });
    results.nullSid = _sessionLoadInFlightFor('sid-a');
  }
  return results;
}

function runSettleScenario({
  text,
  stamp,
  loadingSid,
  sessionId,
  settleSid,
  expectedStamp,
  expectedGeneration,
  generation,
}) {
  M.loadCalls.length = 0;
  const inner = installEnv({ text, stamp, loadingSid, sessionId, generation });
  // The placeholder's live stamp is set by installEnv via makeMsgInner when
  // `stamp` is provided. The timer path passes expectedStamp separately —
  // do NOT overwrite the dataset with it; the whole point of the
  // stale-timer guard is that expectedStamp may differ from the live stamp.
  _settleStrandedConversationLoading(settleSid, expectedStamp, expectedGeneration);
  const wroteRetry = inner._innerHTML.indexOf('conversationLoadRetry') !== -1;
  if (inner._retryHandler) inner._retryHandler();
  return {
    wroteRetry,
    textContentAfter: inner._textContent,
    loadCalls: M.loadCalls.slice(),
  };
}

const results = {
  latch: runLatchScenarios(),
  settle: {
    clearedLatch: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: null, sessionId: 'sid-other', settleSid: 'sid-a' }),
    staleLatch: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: 'sid-a', sessionId: 'sid-other', settleSid: 'sid-a' }),
    freshInflight: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now(), loadingSid: 'sid-a', sessionId: 'sid-other', settleSid: 'sid-a' }),
    sameSession: runSettleScenario({ text: 'Loading conversation...', stamp: null, loadingSid: null, sessionId: 'sid-a', settleSid: 'sid-a' }),
    noLoadingText: runSettleScenario({ text: 'Already rendered transcript', stamp: null, loadingSid: null, sessionId: 'sid-other', settleSid: 'sid-a' }),
    // Overlapping-timer race: A stamps live t1, B re-stamps the placeholder to a
    // newer stamp and owns the latch. A's timer fires with expectedStamp=t0
    // (stale, the original stamp A wrote), which now differs from the live t1
    // B overwrote → stamp guard bails, pane stays untouched (still B's Loading text).
    staleTimerSuperseded: runSettleScenario({ text: 'Loading conversation...', stamp: 1100, loadingSid: 'sid-b', sessionId: 'sid-other', settleSid: 'sid-a', expectedStamp: 1000 }),
    // Current-load timer fires with expectedStamp matching the live stamp, and the
    // load is stranded (past max-age / no live latch) → Retry must be written.
    // One captured scalar feeds both the live stamp and the expected stamp so
    // a clock tick between two Date.now() calls cannot flake the exact guard.
    liveTimerWritesRetry: (() => {
      const strandedStamp = Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000);
      return runSettleScenario({ text: 'Loading conversation...', stamp: strandedStamp, loadingSid: 'sid-a', sessionId: 'sid-other', settleSid: 'sid-a', expectedStamp: strandedStamp });
    })(),
    // Owning load still running (live latch for the same sid+generation):
    // the messages fetch may still resolve — including a valid EMPTY
    // transcript — so the timer must stand down, never write Retry. One
    // captured scalar feeds the live stamp, the expected stamp, and the
    // fresh-now clock so no tick can flake the exact guards.
    owningLoadRunning: (() => {
      const liveStamp = Date.now();
      return runSettleScenario({ text: 'Loading conversation...', stamp: liveStamp, loadingSid: 'sid-a', sessionId: 'sid-a', settleSid: 'sid-a', expectedStamp: liveStamp, expectedGeneration: 7, generation: 7 });
    })(),
    // Superseded attempt (captured generation behind the live one): the
    // predecessor must stand down even when the stamp still matches, so a
    // same-session reload that inherited a stale DOM stamp is never settled
    // by its own predecessor.
    supersededGenerationStandsDown: runSettleScenario({ text: 'Loading conversation...', stamp: 5000, loadingSid: 'sid-a', sessionId: 'sid-a', settleSid: 'sid-a', expectedStamp: 5000, expectedGeneration: 7, generation: 8 }),
  },
};

console.log(JSON.stringify(results));
"""


def _build_script() -> str:
    return (
        _NODE_SCRIPT.replace(
            "__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__", SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC
        )
        .replace("__RETRY_ESCAPES_SRC__", RETRY_ESCAPES_SRC)
        .replace("__CONVERSATION_LOADING_AGE_MS_SRC__", CONVERSATION_LOADING_AGE_MS_SRC)
        .replace("__SESSION_LOAD_IN_FLIGHT_FOR_SRC__", SESSION_LOAD_IN_FLIGHT_FOR_SRC)
        .replace(
            "__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__",
            SETTLE_STRANDED_CONVERSATION_LOADING_SRC,
        )
    )


def _run_node(script: str) -> dict:
    assert NODE is not None, "node is required"
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=str(REPO),
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"node subprocess failed:\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    output_lines = [
        line.strip() for line in completed.stdout.splitlines() if line.strip()
    ]
    assert output_lines, (
        f"node produced no parseable output\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    return json.loads(output_lines[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_fresh_latch_reports_in_flight():
    body = _run_node(_build_script())
    assert body["latch"]["fresh"] is True, (
        "a fresh conversationLoadingSince stamp for the loading session must "
        "report the latch as in-flight"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_latch_expires():
    body = _run_node(_build_script())
    assert body["latch"]["stale"] is False, (
        "a stamp older than _SESSION_LOAD_IN_FLIGHT_MAX_MS must expire the latch"
    )
    assert body["latch"]["otherSid"] is False, (
        "a fresh stamp must not report in-flight when _loadingSessionId points "
        "at a different session"
    )
    assert body["latch"]["nullSid"] is False, (
        "a fresh stamp must not report in-flight when _loadingSessionId is null"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settle_writes_retry_only_when_stranded():
    body = _run_node(_build_script())
    settle = body["settle"]

    for label in ("clearedLatch", "staleLatch"):
        case = settle[label]
        assert case["wroteRetry"] is True, (
            f"{label}: no load owns the pane, so the retry escape hatch must be written"
        )
        assert case["loadCalls"] == [{"sid": "sid-a", "opts": {"force": True}}], (
            f"{label}: clicking Retry must call loadSession(sid, {{force: true}})"
        )

    for label in (
        "freshInflight",
        "sameSession",
        "noLoadingText",
        "owningLoadRunning",
        "supersededGenerationStandsDown",
    ):
        case = settle[label]
        assert case["wroteRetry"] is False, (
            f"{label}: the pane must be left untouched (no retry written)"
        )
        assert case["loadCalls"] == [], f"{label}: no Retry handler may be installed"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_overlapping_timer_does_not_clobber_newer_load():
    """A's timer fires with a stale stamp (B re-stamped the placeholder) → pane untouched."""
    body = _run_node(_build_script())
    case = body["settle"]["staleTimerSuperseded"]
    assert case["wroteRetry"] is False, (
        "a stale expectedStamp must not overwrite a newer load's placeholder"
    )
    assert case["textContentAfter"].find("Loading conversation") != -1, (
        "the pane must still show the newer load's Loading text"
    )
    assert case["loadCalls"] == [], "Retry must not fire for a superseded timer"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_timer_with_matching_stamp_writes_retry():
    """B's timer fires with expectedStamp matching the live stamp → stranded → Retry written."""
    body = _run_node(_build_script())
    case = body["settle"]["liveTimerWritesRetry"]
    assert case["wroteRetry"] is True, (
        "a current-load timer whose stamp still matches must write Retry"
    )
    assert case["loadCalls"] == [{"sid": "sid-a", "opts": {"force": True}}], (
        "clicking Retry must call loadSession(sid, {force: true})"
    )


# ── Fake-clock tests: drive the REAL scheduled callback (#7553 review) ───────
# The scenarios above call _settleStrandedConversationLoading directly with
# crafted stamps, which only proves settlement *after* expiration — not that
# production ever invokes settlement at the right time. These drive the real
# callback captured from _armStrandedConversationLoadingTimer under a fake
# clock: no Retry at 4s, Retry at the expiry boundary, stale callbacks leave
# newer placeholders untouched, and metadata-without-messages still settles.

_NODE_TIMER_SCRIPT = r"""
const M2 = { loadCalls: [] };
let nowMs = 1000000;
Date.now = () => nowMs;
const pendingTimers = [];
let timerSeq = 0;
globalThis.setTimeout = (cb, ms) => {
  timerSeq += 1;
  pendingTimers.push({ id: timerSeq, cb, due: nowMs + Number(ms), delay: Number(ms) });
  return timerSeq;
};
globalThis.clearTimeout = (id) => {
  const i = pendingTimers.findIndex((t) => t.id === id);
  if (i >= 0) pendingTimers.splice(i, 1);
};
function advance(ms) {
  nowMs += ms;
  const fire = [];
  for (let i = pendingTimers.length - 1; i >= 0; i--) {
    if (pendingTimers[i].due <= nowMs) fire.push(pendingTimers.splice(i, 1)[0]);
  }
  fire.sort((a, b) => a.due - b.due).forEach((t) => t.cb());
}

function makeTimerInner(stamp) {
  const el = {
    dataset: { conversationLoadingSince: String(stamp) },
    _html: '',
    _text: 'Loading conversation...',
    _retry: null,
    querySelector(sel) {
      if (sel === '#conversationLoadRetry') {
        return { addEventListener: (t, f) => { if (t === 'click') el._retry = f; } };
      }
      return null;
    },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) {
      this._html = String(v);
      if (this._html.indexOf('conversationLoadRetry') !== -1) this._text = '';
    },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; },
    set(v) { this._text = String(v); },
  });
  return el;
}

let timerInner = null;
function installTimerEnv({ stamp, loadingSid, generation, sessionId, messages }) {
  timerInner = makeTimerInner(stamp);
  globalThis.$ = (id) => (id === 'msgInner' ? timerInner : null);
  globalThis._loadingSessionId = loadingSid;
  globalThis._loadSessionGeneration = generation;
  globalThis.S = { session: sessionId === null ? null : { session_id: sessionId } };
  if (messages !== undefined) globalThis.S.messages = messages;
  globalThis.loadSession = (sid, opts) => { M2.loadCalls.push({ sid, opts }); };
  // The settle helper renders through t() in production; default to the
  // English fallback strings so assertions stay behavioral, not locale-bound.
  globalThis.t = (key) =>
    key === 'conversation_load_failed'
      ? 'Couldn\u2019t load this conversation.'
      : key === 'conversation_load_retry'
        ? 'Retry'
        : key;
  M2.loadCalls.length = 0;
  pendingTimers.length = 0;
}

__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__
__RETRY_ESCAPES_SRC__
__CONVERSATION_LOADING_AGE_MS_SRC__
__SESSION_LOAD_IN_FLIGHT_FOR_SRC__
__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__
__ARM_STRANDED_TIMER_SRC__
__RESTAMP_STRANDED_PLACEHOLDER_SRC__

function retryWritten() {
  return timerInner._html.indexOf('conversationLoadRetry') !== -1;
}

const T0 = 1000000;
const timerResults = {};

// A: the arm path schedules the real callback at the expiry deadline, and
// reports the latch window the settle path enforces, so the test can prove
// the two halves share one lifecycle without naming production constants.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-other', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  timerResults.armedDelay = pendingTimers.length ? pendingTimers[pendingTimers.length - 1].delay : null;
  timerResults.latchWindowMs = (typeof _SESSION_LOAD_IN_FLIGHT_MAX_MS === 'number')
    ? _SESSION_LOAD_IN_FLIGHT_MAX_MS
    : null;
}

// B: no Retry at 4s; Retry appears at the expiry boundary via the real callback.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-other', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  advance(4000);
  timerResults.noRetryAt4s = !retryWritten();
  advance(16000);
  timerResults.retryAtExpiry = retryWritten();
  if (timerInner._retry) timerInner._retry();
  timerResults.retryLoadCalls = M2.loadCalls.slice();
}

// C: a stale scheduled callback (lost clearTimeout) leaves the newer placeholder alone,
// while the newer load's own callback still settles at its expiry.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-other', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  const staleCb = pendingTimers[0].cb;
  // Load B takes over: re-stamps the placeholder, bumps the generation, owns the latch.
  timerInner.dataset.conversationLoadingSince = String(T0 + 5000);
  globalThis._loadingSessionId = 'sid-b';
  globalThis._loadSessionGeneration = 2;
  _armStrandedConversationLoadingTimer('sid-b', T0 + 5000, 2);
  staleCb();
  timerResults.staleUntouched = !retryWritten()
    && timerInner._text.indexOf('Loading conversation') !== -1;
  advance(25000);
  timerResults.newerSettlesAtOwnExpiry = retryWritten();
}

// D: owning attempt stranded past the latch window with Loading text still
// on screen (messages fetch never resolved nor rendered): the expiry
// callback must settle to Retry. The stamp is older than the latch window
// while the latch still names the sid, so the live-latch guard has expired
// and the pane is genuinely stranded.
{
  const strandedStamp = T0 - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000);
  installTimerEnv({ stamp: strandedStamp, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [] });
  nowMs = T0;
  _settleStrandedConversationLoading('sid-a', strandedStamp, 1);
  timerResults.metaWithoutMessagesSettles = retryWritten();
}

// D2: owning load STILL running when its own timer fires. The arm-then-advance
// shape cannot produce this (advancing to the deadline expires the latch by
// construction), so drive the settle directly with a fresh live stamp: the
// messages fetch may yet resolve — including a valid EMPTY transcript whose
// empty-state render also runs before the latch clears — so the timer must
// stand down, never converting the pending load into a Retry error.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [] });
  nowMs = T0;
  _settleStrandedConversationLoading('sid-a', T0, 1);
  timerResults.owningLoadRunningStandsDown = !retryWritten();
}

// E: render already replaced the placeholder (no Loading text): stand down
// regardless of transcript shape.
{
  installTimerEnv({ stamp: T0, loadingSid: null, generation: 2, sessionId: 'sid-a', messages: [{ role: 'user', content: 'hi' }] });
  timerInner._text = 'Already rendered transcript';
  timerInner._html = '<div>transcript</div>';
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  advance(20000);
  timerResults.metaWithTranscriptStandsDown = !retryWritten();
}

// E2: superseded predecessor (captured generation behind live) over a
// matching stamp: stand down — the newer same-session attempt owns the pane
// even though the DOM stamp was inherited, not re-stamped.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 2, sessionId: 'sid-a', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  advance(20000);
  timerResults.supersededGenerationStandsDown = !retryWritten();
}

// F: same-session force reload (Retry click) that inherits the visible
// Loading placeholder must re-stamp it and re-arm the expiry timer with the
// new generation — driven through the REAL production re-arm branch
// (_restampStrandedPlaceholderForReload, the exact function loadSession's
// preamble now calls), not a hand simulation. The original timer then stands
// down at its expiry (stamp+generation mismatch) and the new timer settles.
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [] });
  nowMs = T0;
  _armStrandedConversationLoadingTimer('sid-a', T0, 1);
  // Same-session force reload at T0+5000 via the production branch: bump the
  // generation the way loadSession's preamble does, then re-stamp + re-arm.
  nowMs = T0 + 5000;
  globalThis._loadSessionGeneration = 2;
  timerResults.restampActed = _restampStrandedPlaceholderForReload('sid-a', 2, true);
  timerResults.restampMovedStamp = timerInner.dataset.conversationLoadingSince === String(T0 + 5000);
  // Advance past the original expiry (T0+20000): original callback stands down.
  advance(15000); // nowMs = T0+20000
  timerResults.sameSessionForceReloadOriginalStandsDown = !retryWritten();
  // Advance to the new expiry (T0+25000): new callback settles → Retry.
  advance(5000); // nowMs = T0+25000
  timerResults.sameSessionForceReloadNewSettles = retryWritten();
}

// G: the production re-arm branch refreshes the stamp on every same-session
// force reload (attempt-scoped age authority) but arms a timer ONLY for a
// still-visible Loading placeholder. Rendered panes refresh the stamp (so a
// stale inherited timestamp can never read expired mid-fetch) and skip the
// timer; Retry panes do the same (no live attempt to protect yet — the new
// loadSession preamble that called the helper is the live attempt).
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [{ role: 'user', content: 'hi' }] });
  timerInner._text = 'Already rendered transcript';
  timerInner._html = '<div>transcript</div>';
  nowMs = T0 + 5000;
  globalThis._loadSessionGeneration = 2;
  const actedRendered = _restampStrandedPlaceholderForReload('sid-a', 2, true);
  timerResults.restampRefreshesRenderedStamp = timerInner.dataset.conversationLoadingSince === String(T0 + 5000);
  timerResults.restampSkipsRendered = actedRendered === false && pendingTimers.length === 0;
}
{
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [] });
  timerInner._text = '';
  timerInner._html = '<div>retry pane conversationLoadRetry</div>';
  nowMs = T0 + 5000;
  globalThis._loadSessionGeneration = 2;
  const actedRetry = _restampStrandedPlaceholderForReload('sid-a', 2, true);
  timerResults.restampRefreshesRetryStamp = timerInner.dataset.conversationLoadingSince === String(T0 + 5000);
  timerResults.restampSkipsRetry = actedRetry === false && pendingTimers.length === 0;
}
{
  // Non-force same-session navigation over a Loading placeholder: no re-arm
  // (the cross-session first-paint arm path owns that case).
  installTimerEnv({ stamp: T0, loadingSid: 'sid-a', generation: 1, sessionId: 'sid-a', messages: [] });
  nowMs = T0 + 5000;
  globalThis._loadSessionGeneration = 2;
  const actedNonForce = _restampStrandedPlaceholderForReload('sid-a', 2, false);
  timerResults.restampSkipsNonForce = actedNonForce === false && pendingTimers.length === 0
    && timerInner.dataset.conversationLoadingSince === String(T0);
}

console.log(JSON.stringify(timerResults));
"""


def _build_timer_script() -> str:
    return (
        _NODE_TIMER_SCRIPT.replace(
            "__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__", SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC
        )
        .replace("__RETRY_ESCAPES_SRC__", RETRY_ESCAPES_SRC)
        .replace("__CONVERSATION_LOADING_AGE_MS_SRC__", CONVERSATION_LOADING_AGE_MS_SRC)
        .replace("__SESSION_LOAD_IN_FLIGHT_FOR_SRC__", SESSION_LOAD_IN_FLIGHT_FOR_SRC)
        .replace(
            "__SETTLE_STRANDED_CONVERSATION_LOADING_SRC__",
            SETTLE_STRANDED_CONVERSATION_LOADING_SRC,
        )
        .replace("__ARM_STRANDED_TIMER_SRC__", ARM_STRANDED_TIMER_SRC)
        .replace(
            "__RESTAMP_STRANDED_PLACEHOLDER_SRC__", RESTAMP_STRANDED_PLACEHOLDER_SRC
        )
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_expiry_timer_armed_at_deadline():
    """The arm path must schedule the real callback at the expiry deadline.

    Purely behavioral: the fake clock records the delay the REAL extracted
    armer schedules, and the boundary pair (silent at 4s, Retry at 20s)
    proves the callback cannot race the live latch — the reviewed flaw where
    a 4s callback fired while the 20s latch still owned the pane. The
    armed delay must equal the latch window the settle path reads, so the
    two halves cannot drift apart.
    """
    body = _run_node(_build_timer_script())
    assert body["armedDelay"] == body["latchWindowMs"], (
        "the scheduled callback must fire exactly at the latch-expiry "
        "boundary the settle path enforces"
    )
    assert body["armedDelay"] == 20000, (
        "the real scheduled callback must fire at the 20s expiry deadline"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_no_retry_before_expiry_retry_at_expiry():
    """Drive the real scheduled callback: silent at 4s, Retry at 20s."""
    body = _run_node(_build_timer_script())
    assert body["noRetryAt4s"] is True, "no Retry may appear before the deadline"
    assert body["retryAtExpiry"] is True, "Retry must appear at the expiry boundary"
    assert body["retryLoadCalls"] == [{"sid": "sid-a", "opts": {"force": True}}], (
        "clicking Retry must call loadSession(sid, {force: true})"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_scheduled_callback_leaves_newer_placeholder():
    """A superseded load's captured callback must not settle the newer load."""
    body = _run_node(_build_timer_script())
    assert body["staleUntouched"] is True, (
        "a stale stamp+generation must leave the newer placeholder untouched"
    )
    assert body["newerSettlesAtOwnExpiry"] is True, (
        "the newer load's own callback must still settle at its expiry"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_abandoned_attempt_settles_while_running_attempt_stands_down():
    """Only an attempt that ended without rendering settles to Retry.

    The latch is attempt ownership: a cleared latch means the owning attempt
    ended without rendering (settle), while a live latch for the same
    sid+generation means the messages fetch may still resolve — including a
    valid EMPTY transcript, whose empty-state render also runs before the
    latch clears — so the timer must stand down, never manufacturing Retry.
    """
    body = _run_node(_build_timer_script())
    assert body["metaWithoutMessagesSettles"] is True, (
        "an attempt that ended without rendering must still settle to Retry"
    )
    assert body["owningLoadRunningStandsDown"] is True, (
        "a still-running owning load must stand the timer down (its empty "
        "transcript is valid, not a failure)"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_rendered_pane_and_superseded_generation_stand_down():
    """A replaced placeholder or a superseded generation never settles."""
    body = _run_node(_build_timer_script())
    assert body["metaWithTranscriptStandsDown"] is True, (
        "a replaced (rendered) placeholder must stand the settle down"
    )
    assert body["supersededGenerationStandsDown"] is True, (
        "a superseded predecessor must stand down even when the stamp "
        "still matches (same-session reload inherited the DOM stamp)"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_session_force_reload_rearms_through_production_branch():
    """A Retry-click force reload over Loading re-stamps + re-arms (production branch).

    Drives the REAL production re-arm function that loadSession's preamble
    calls (``_restampStrandedPlaceholderForReload``) under the fake clock:
    the re-stamp must move the placeholder stamp, the original timer must
    stand down at its expiry (stamp+generation mismatch), and the re-armed
    timer must settle at its own expiry. Gutting the helper breaks this
    without any source-text inspection.
    """
    body = _run_node(_build_timer_script())
    assert body["restampActed"] is True, (
        "the production re-arm branch must act on a same-session force "
        "reload over a visible Loading placeholder"
    )
    assert body["restampMovedStamp"] is True, (
        "the re-arm must re-stamp the placeholder to the new attempt's time"
    )
    assert body["sameSessionForceReloadOriginalStandsDown"] is True, (
        "the original timer must stand down at its expiry (stamp+generation "
        "mismatch from the re-stamped, re-armed same-session reload)"
    )
    assert body["sameSessionForceReloadNewSettles"] is True, (
        "the re-armed timer must settle at its own expiry → Retry appears"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_rearm_skips_rendered_retry_and_non_force_panes():
    """The production re-arm branch arms a timer only for a Loading pane.

    Every same-session force reload refreshes the stamp (attempt-scoped age
    authority, so a stale inherited timestamp can never read expired
    mid-fetch); only a visible Loading placeholder additionally arms the
    expiry timer.
    """
    body = _run_node(_build_timer_script())
    assert body["restampRefreshesRenderedStamp"] is True, (
        "a reload over a rendered pane must refresh the stamp"
    )
    assert body["restampSkipsRendered"] is True, (
        "a rendered transcript must never re-arm a settlement timer"
    )
    assert body["restampRefreshesRetryStamp"] is True, (
        "a reload over a Retry pane must refresh the stamp"
    )
    assert body["restampSkipsRetry"] is True, (
        "an already-settled Retry pane must never re-arm a settlement timer"
    )
    assert body["restampSkipsNonForce"] is True, (
        "a non-force navigation must not re-arm (first-paint path owns it) "
        "and must leave the live stamp untouched"
    )
