"""Node-harness behavior tests for stranded conversation loading settlement.

The in-flight latch helpers (``_conversationLoadingAgeMs`` /
``_sessionLoadInFlightFor``) and the escape hatch
(``_settleStrandedConversationLoading``) are extracted verbatim from
``static/sessions.js`` and exercised under node with hand-rolled DOM shims
(no JSDOM). Every assertion is on observable behavior — helper return values,
innerHTML writes, and retry-click dispatch — never on source strings.
"""

from __future__ import annotations

import json
import re
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
        if ch in ('\'', '"', "`"):
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
    """Return the full ``const name = ...;`` declaration from a single js file."""
    m = re.search(rf"const {name}\s*=\s*[^;]+;", source)
    assert m, f"{name} not found in sessions.js"
    return m.group(0)


SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC = _extract_const(
    SESSIONS_SRC, "_SESSION_LOAD_IN_FLIGHT_MAX_MS"
)
CONVERSATION_LOADING_AGE_MS_SRC = _extract_function(
    SESSIONS_SRC, "_conversationLoadingAgeMs"
)
SESSION_LOAD_IN_FLIGHT_FOR_SRC = _extract_function(
    SESSIONS_SRC, "_sessionLoadInFlightFor"
)
SETTLE_STRANDED_CONVERSATION_LOADING_SRC = _extract_function(
    SESSIONS_SRC, "_settleStrandedConversationLoading"
)


_NODE_SCRIPT = r'''
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

function installEnv({ text, stamp, loadingSid, sessionId }) {
  const msgInner = makeMsgInner({ text, stamp });
  globalThis.$ = (id) => (id === 'msgInner' ? msgInner : null);
  globalThis._loadingSessionId = loadingSid;
  globalThis.S = { session: sessionId === null ? null : { session_id: sessionId } };
  globalThis.loadSession = (sid, opts) => { M.loadCalls.push({ sid, opts }); };
  return msgInner;
}

__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__
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

function runSettleScenario({ text, stamp, loadingSid, sessionId, settleSid, expectedStamp }) {
  M.loadCalls.length = 0;
  const inner = installEnv({ text, stamp, loadingSid, sessionId });
  // The placeholder's live stamp is set by installEnv via makeMsgInner when
  // `stamp` is provided. The timer path passes expectedStamp separately —
  // do NOT overwrite the dataset with it; the whole point of the
  // stale-timer guard is that expectedStamp may differ from the live stamp.
  _settleStrandedConversationLoading(settleSid, expectedStamp);
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
    liveTimerWritesRetry: runSettleScenario({ text: 'Loading conversation...', stamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000), loadingSid: 'sid-a', sessionId: 'sid-other', settleSid: 'sid-a', expectedStamp: Date.now() - (_SESSION_LOAD_IN_FLIGHT_MAX_MS + 1000) }),
  },
};

console.log(JSON.stringify(results));
'''


def _build_script() -> str:
    return (
        _NODE_SCRIPT.replace(
            "__SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC__", SESSION_LOAD_IN_FLIGHT_MAX_MS_SRC
        )
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
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
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

    for label in ("freshInflight", "sameSession", "noLoadingText"):
        case = settle[label]
        assert case["wroteRetry"] is False, (
            f"{label}: the pane must be left untouched (no retry written)"
        )
        assert case["loadCalls"] == [], (
            f"{label}: no Retry handler may be installed"
        )


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
    assert case["loadCalls"] == [], (
        "Retry must not fire for a superseded timer"
    )


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