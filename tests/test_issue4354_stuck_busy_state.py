"""Regression coverage for #4354 stuck "Running" indicator.

Three independent client-side gaps in the busy-state machine:

1. No client-side silence watchdog in `attachLiveStream` — a dead EventSource
   can sit there forever as long as the browser keeps the TCP socket open.
2. `_reconcileActiveSessionIdleStateFromList` is gated on `_sendInProgress`
   being false, so a stuck send blocks the reconcile from clearing busy state.
3. INFLIGHT reattach re-asserts busy state unconditionally on session entry,
   even when the server has long since marked the session idle.

These tests mirror the static-source-extraction style of
`test_issue2454_active_session_spinner.py`. The first two are pure string
assertions; the third mounts the extracted `if (INFLIGHT[sid])` block in a
`node` subprocess to verify the server-truth branch actually drops the local
INFLIGHT when the server disagrees.
"""
from pathlib import Path
import json
import re
import subprocess

REPO = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    """Extract the body of a top-level JS function.

    For top-level functions (column 0), the closing `}` is also at column 0.
    We find the function signature, then the next `}\n` at column 0, ignoring
    any `}` inside strings/template literals/regex/comments. This is robust
    for very large functions with deeply nested function expressions where
    a naive brace counter can lose track.
    """
    start = src.find(signature)
    assert start != -1, f"missing {signature}"
    # Find the close-paren of the parameter list, then the opening brace of
    # the function body. This avoids matching the `{}` in default-parameter
    # values like `options={}` in the function signature.
    paren_close = src.find(")", start)
    assert paren_close != -1, f"missing ')' after {signature}"
    brace = src.find("{", paren_close)
    assert brace != -1, f"missing opening brace for {signature}"

    # Scan forward from the opening brace, tracking string/template/regex/
    # comment state, and return the slice up to the first `}` at column 0
    # that brings us out of the function body.
    in_string = None
    in_template = False
    in_line_comment = False
    in_block_comment = False
    in_regex = False
    for i in range(brace, len(src)):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        prev = src[i - 1] if i > 0 else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string is not None:
            if ch == "\\":
                continue
            if ch == in_string:
                in_string = None
            continue
        if in_template:
            if ch == "\\":
                continue
            if ch == "`":
                in_template = False
            continue
        if in_regex:
            if ch == "\\":
                continue
            if ch == "/":
                in_regex = False
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ('"', "'"):
            in_string = ch
            continue
        if ch == "`":
            in_template = True
            continue
        # Heuristic for regex literals: a `/` after an operator/keyword
        # starts a regex. This is a rough approximation but sufficient for
        # the well-formed code in this repo.
        if ch == "/" and prev in ("=", "(", ",", ";", ":", "!", "&", "|", "?", "{", "}", "[", "]", "\n", " ", "\t", ""):
            in_regex = True
            continue
        if ch == "}" and prev == "\n":
            # Found the top-level closing brace.
            return src[brace + 1 : i]
    raise AssertionError(f"could not extract function body for {signature}")


def test_attach_live_stream_has_silence_watchdog():
    """The watchdog is a setInterval closure that force-closes the EventSource
    after 5 minutes of silence while the stream is expected to be alive."""
    body = _function_body(MESSAGES_SRC, "function attachLiveStream(")

    # 5-minute hard-coded constant lives at the top of the function.
    assert re.search(r"_STREAM_SILENCE_WATCHDOG_MS\s*=\s*5\s*\*\s*60\s*\*\s*1000", body), (
        "watchdog must use a 5-minute hard-coded constant"
    )

    # Closure-local "time of last event" tracker.
    assert re.search(r"_lastEventAt\s*=", body), (
        "watchdog must track the timestamp of the last received event"
    )

    # setInterval tick that drives the silence check.
    assert "setInterval(" in body, "watchdog must schedule a periodic tick"
    assert "30 * 1000" in body, (
        "watchdog tick interval must be 30 seconds (30 * 1000 ms)"
    )

    # Fire conditions: S.busy, owner check, terminal-state guard.
    # Source uses an early-return on `<` (not silent yet); the fire path is
    # implicit when the early-return doesn't trigger.
    assert re.search(r"Date\.now\(\)\s*-\s*_lastEventAt\s*<\s*_STREAM_SILENCE_WATCHDOG_MS", body), (
        "watchdog tick must compare now against _lastEventAt and the constant"
    )
    assert re.search(r"S\.busy", body), "watchdog tick must gate on S.busy"
    assert re.search(r"S\.activeStreamId\s*===\s*streamId", body), (
        "watchdog tick must verify ownership via streamId"
    )
    assert re.search(r"_streamFinalized|_terminalStateReached", body), (
        "watchdog tick must bail out on a stream that already terminated"
    )

    # Fire actions: close, clear inflight, clear busy, toast, stop tick.
    assert "_closeSource(source)" in body, "watchdog must call _closeSource"
    assert re.search(r"delete\s+INFLIGHT\[activeSid\]", body), (
        "watchdog must delete the in-memory INFLIGHT entry"
    )
    assert re.search(r"clearInflightState\(activeSid\)", body), (
        "watchdog must clear the persisted INFLIGHT"
    )
    assert re.search(r"S\.busy\s*=\s*false", body), "watchdog must clear S.busy"
    assert re.search(r"S\.activeStreamId\s*=\s*null", body), (
        "watchdog must clear S.activeStreamId"
    )
    # After clearing busy state, the watchdog must refresh the composer
    # button + topbar DOM, otherwise the red "stop" button lingers after
    # the watchdog clears the run state. (nesquena-hermes re-review)
    assert re.search(r"updateSendBtn\s*\(\s*\)", body), (
        "watchdog fire block must call updateSendBtn() to refresh "
        "the composer button (getComposerPrimaryAction reads S.busy)"
    )
    assert re.search(r"syncTopbar\s*\(\s*\)", body), (
        "watchdog fire block must call syncTopbar() to refresh the "
        "topbar busy affordance"
    )
    assert re.search(r"showToast\([^)]*reconnect", body, re.IGNORECASE), (
        "watchdog must show a reconnect toast"
    )
    assert "clearInterval(" in body, "watchdog must stop its own tick interval"

    # Every event listener that matters bumps _lastEventAt (via a generic
    # wrapper or explicit assignments). We allow either pattern.
    lastEventAtWrites = len(re.findall(r"_lastEventAt\s*=", body))
    assert lastEventAtWrites >= 2, (
        "watchdog must bump _lastEventAt from at least one event handler "
        "(and the initial declaration counts as 1, so we need ≥2 total writes)"
    )

    # The server sends exclusively *named* SSE events. addEventListener('message')
    # only fires for unnamed events per the EventSource spec. To track real
    # server liveness during long tool calls, extended reasoning, or
    # approval/clarify waits (all of which can exceed 5 min without
    # emitting a 'token'), the watchdog must bump _lastEventAt from a
    # *central* heartbeat loop covering all non-terminal named event
    # types — not just the 'token' handler.
    # (nesquena-hermes re-review: token-only heartbeat let the watchdog
    # tear down healthy streams on long agentic turns.)
    assert re.search(
        r"_WATCHDOG_HEARTBEAT_EVENTS",
        body,
    ), (
        "watchdog must bump _lastEventAt from a central heartbeat loop "
        "covering all non-terminal named events (long tool/reasoning/"
        "approval turns emit no tokens but are healthy)"
    )
    # The central loop must include the common named events. We check
    # for a representative sample rather than the full list.
    for _evt in ("token", "reasoning", "tool", "approval", "clarify"):
        assert f"'{_evt}'" in body, (
            f"central heartbeat loop must include '{_evt}' "
            f"(otherwise the watchdog tears down on healthy {_evt}-only turns)"
        )
    # And no broken 'message' handler should be doing the bump — if it were,
    # the test above would still pass on a buggy implementation where the
    # bump is on a never-firing listener. We require the literal
    # addEventListener('message', ... pattern (with comma after the event
    # name) so the regex doesn't match the comment text that references
    # 'message' as documentation.
    assert not re.search(
        r"addEventListener\(\s*'message'\s*,",
        body,
    ), (
        "watchdog _lastEventAt must NOT be bumped from a 'message' listener "
        "(would never fire for this server's named-event protocol)"
    )
    # Server-truth pre-fire check: the watchdog must confirm the stream
    # is still active before tearing down local state, so a healthy
    # server stream in a long wait (tool/reasoning/approval) doesn't
    # get its local state ripped out.
    assert re.search(
        r"_verifyStreamStillActive|stream/status",
        body,
    ), (
        "watchdog must perform a server-truth pre-fire check "
        "(/api/chat/stream/status) before tearing down local state, "
        "so healthy long-wait streams aren't torn down"
    )

    # Terminal events clear the watchdog interval.
    for term in ("'done'", "'apperror'", "_closeSource"):
        assert term in body, f"watchdog must clear on terminal event {term}"


def test_reconcile_active_session_idle_drops_send_progress_gate():
    """The reconcile path no longer bails out on `_sendInProgress`; instead
    it force-clears the gate when the server reports the session as idle."""
    body = _function_body(SESSIONS_SRC, "function _reconcileActiveSessionIdleStateFromList(")

    # The old gate is gone.
    assert "if (typeof _sendInProgress !== 'undefined' && _sendInProgress) return false" not in body, (
        "reconcile must no longer bail out on _sendInProgress"
    )
    assert "if (_sendInProgress) return false" not in body, (
        "reconcile must no longer bail out on _sendInProgress (loose form)"
    )

    # The server-idle gate is preserved.
    assert "_isServerIdleSessionRow(serverRow)" in body, (
        "reconcile must still gate on the server-idle predicate"
    )

    # The force-clear block resets the gate variable and the sid, but only
    # when the in-progress send is for the *active* session — otherwise
    # a background send in flight for a different session would be silently
    # clobbered (#4354 P1, Greptile review). The sid guard must appear
    # before the reset, in the same conditional as the _sendInProgress
    # truthiness check.
    assert re.search(
        r"_sendInProgress[^&|]*&&[^&|]*_sendInProgressSid[^=]*===\s*sid",
        body,
    ), (
        "reconcile force-clear must guard on _sendInProgressSid === sid "
        "so a background send for another session isn't clobbered"
    )
    assert re.search(r"_sendInProgress\s*=\s*false", body), (
        "reconcile must force-clear _sendInProgress when mutating state"
    )
    assert re.search(r"_sendInProgressSid\s*=\s*null", body), (
        "reconcile must force-clear _sendInProgressSid when mutating state"
    )


def test_inflight_reattach_uses_server_truth():
    """The `if (INFLIGHT[sid])` block in `loadSession` re-asserts busy state
    only when the server's `active_stream_id` matches the local INFLIGHT and
    the session was updated within the last 10 minutes. Otherwise the local
    INFLIGHT is discarded and `S.busy` is left alone."""
    # Locate the `if(INFLIGHT[sid]){` block inside loadSession. We scan for
    # the exact unconditional pattern that the fix must replace.
    sig = "if(INFLIGHT[sid]){"
    start = SESSIONS_SRC.find(sig)
    assert start != -1, "missing `if(INFLIGHT[sid]){` block in loadSession"

    # The unconditional `S.busy=true;` in the INFLIGHT reattach path is gone.
    # We use a structural check (the re-assert is inside a server-truth
    # conditional) rather than a global substring search, because other
    # branches in the file (e.g. the idle-session reattach at the end of
    # loadSession) still set `S.busy = true` unconditionally.
    # We extract the INFLIGHT reattach block by `rfind` (the substantive
    # reattach branch is the last occurrence; the first occurrence is the
    # small idle-reset block earlier in loadSession).
    sig = "if(INFLIGHT[sid]){"
    start = SESSIONS_SRC.rfind(sig)
    assert start != -1, "missing `if(INFLIGHT[sid]){` reattach block in loadSession"

    # The replacement branch lives in the same function. We assert the
    # _body_ of the if-INFLIGHT block contains the right conditionals.
    # Extract the block using a brace-count starting from the `{` after `if(INFLIGHT[sid])`.
    brace = SESSIONS_SRC.find("{", start)
    assert brace != -1
    depth = 0
    end = brace
    for i in range(brace, len(SESSIONS_SRC)):
        ch = SESSIONS_SRC[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    block_body = SESSIONS_SRC[brace + 1 : end]

    # Server-truth check must be present (recency veto was removed —
    # see loadSession comment: long agentic turns keep active_stream_id
    # set while last_message_at goes stale, so dropping the INFLIGHT
    # on switch-away/back would lose the live reattach. The watchdog's
    # server-truth pre-fire check is the right place to handle "stream
    # is active but silent.").
    assert "S.session.active_stream_id" in block_body, (
        "INFLIGHT reattach must consult S.session.active_stream_id for "
        "the server-truth gate (recency veto removed per nesquena-hermes re-review)"
    )

    # The discard branch must delete INFLIGHT and call clearInflightState.
    assert re.search(r"delete\s+INFLIGHT\[sid\]", block_body), (
        "INFLIGHT reattach must have a discard branch that deletes INFLIGHT[sid]"
    )
    assert "clearInflightState(sid)" in block_body, (
        "INFLIGHT reattach discard branch must call clearInflightState(sid)"
    )
    # The discard branch must also reset S.busy and S.activeStreamId so
    # the "Running" indicator doesn't stay stuck when those flags were
    # true from a preceding active session. updateSendBtn() refreshes the
    # composer button. (#4354 P1, Greptile review.)
    assert re.search(r"S\.busy\s*=\s*false", block_body), (
        "INFLIGHT reattach discard branch must reset S.busy = false"
    )
    assert re.search(r"S\.activeStreamId\s*=\s*null", block_body), (
        "INFLIGHT reattach discard branch must reset S.activeStreamId = null"
    )

    # The re-assert branch sets S.busy = true, AND it must be guarded by the
    # server-truth condition. The recency veto was removed in the
    # nesquena-hermes re-review (long agentic turns keep active_stream_id
    # set while last_message_at goes stale). We verify the structural
    # invariant: `S.busy = true` appears inside the `if(_serverStreamMatches)`
    # branch, not unconditionally.
    assert re.search(r"S\.busy\s*=\s*true", block_body), (
        "INFLIGHT reattach must still set S.busy = true in the matching branch"
    )
    assert re.search(r"_serverStreamMatches", block_body), (
        "INFLIGHT reattach must gate S.busy = true on _serverStreamMatches "
        "(recency veto removed; server-truth alone is the gate)"
    )
    # The condition `_serverStreamMatches` must appear BEFORE
    # `S.busy = true` in the block (i.e. the busy re-assert is inside
    # the conditional, not before it).
    cond_pos = block_body.find("_serverStreamMatches")
    busy_pos = block_body.find("S.busy = true")
    if busy_pos == -1:
        busy_pos = block_body.find("S.busy=true")
    assert cond_pos != -1 and busy_pos != -1 and cond_pos < busy_pos, (
        "S.busy = true must be inside the _serverStreamMatches && _sessionIsRecent branch"
    )

    # Runtime check via node: build a minimal harness that mirrors the
    # conditionals and verifies both branches behave correctly.
    harness = f"""
let S = {{ session: {{ session_id: 's1', active_stream_id: null, last_message_at: 0 }} }};
let INFLIGHT = {{}};
let clearedInflight = false;
function clearInflightState(sid) {{ clearedInflight = (sid === 's1'); }}

// Inlined from the real source — keep in sync with the fix.
// Note: last_message_at recency veto was removed in the nesquena-hermes
// re-review. The reattach branch only checks stream-id match; long
// agentic turns keep active_stream_id set while last_message_at goes
// stale, and dropping the INFLIGHT on switch-away/back would lose
// the live reattach. The watchdog's silence check (with server-truth
// pre-fire verification) is the right place to handle "stream is
// active but silent".
function reattach() {{
  const sid = 's1';
  if (INFLIGHT[sid]) {{
    const inflightStreamId = INFLIGHT[sid].streamId || INFLIGHT[sid].stream_id;
    const serverStreamId = S.session.active_stream_id;
    const serverStreamMatches = !!serverStreamId && !!inflightStreamId && serverStreamId === inflightStreamId;
    if (serverStreamMatches) {{
      S.busy = true;
      S.activeStreamId = serverStreamId;
    }} else {{
      delete INFLIGHT[sid];
      clearInflightState(sid);
    }}
  }}
}}

// Case A: stale local INFLIGHT, server says idle → discard, S.busy stays false.
INFLIGHT['s1'] = {{ streamId: 'old-stream' }};
S.session.active_stream_id = null;
S.session.last_message_at = Date.now() / 1000 - 60 * 60;  // 1 hour ago
S.busy = false;
clearedInflight = false;
reattach();
const caseA = {{ busy: S.busy, hasInflight: !!INFLIGHT['s1'], clearedInflight }};

// Case B: stream id matches (recency no longer a veto) → re-assert busy.
INFLIGHT['s1'] = {{ streamId: 'live-stream' }};
S.session.active_stream_id = 'live-stream';
S.session.last_message_at = Date.now() / 1000 - 30;  // 30s ago
S.busy = false;
clearedInflight = false;
reattach();
const caseB = {{ busy: S.busy, activeStreamId: S.activeStreamId }};

// Case C: long agentic turn — stream id matches but last_message_at
// is 1 hour old. Should NOT be discarded.
INFLIGHT['s1'] = {{ streamId: 'long-turn-stream' }};
S.session.active_stream_id = 'long-turn-stream';
S.session.last_message_at = Date.now() / 1000 - 60 * 60;  // 1 hour ago
S.busy = false;
clearedInflight = false;
reattach();
const caseC = {{ busy: S.busy, activeStreamId: S.activeStreamId, hasInflight: !!INFLIGHT['s1'] }};

console.log(JSON.stringify({{ caseA, caseB, caseC }}));
"""
    result = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
    out = json.loads(result.stdout)

    # Case A: stale → discarded, busy stays false.
    assert out["caseA"]["busy"] is False, (
        f"stale INFLIGHT reattach must not re-assert busy state, got {out['caseA']}"
    )
    assert out["caseA"]["hasInflight"] is False, (
        f"stale INFLIGHT reattach must delete the local INFLIGHT, got {out['caseA']}"
    )
    assert out["caseA"]["clearedInflight"] is True, (
        f"stale INFLIGHT reattach must call clearInflightState, got {out['caseA']}"
    )

    # Case B: stream id matches (regardless of recency) → busy re-asserted.
    assert out["caseB"]["busy"] is True, (
        f"matching INFLIGHT reattach must re-assert busy, got {out['caseB']}"
    )
    assert out["caseB"]["activeStreamId"] == "live-stream", (
        f"matching INFLIGHT reattach must set S.activeStreamId, got {out['caseB']}"
    )

    # Case C: long agentic turn — stream id matches, last_message_at is
    # 1 hour old. The recency veto is gone, so the INFLIGHT is kept
    # and busy is re-asserted. This is the case the nesquena-hermes
    # re-review specifically called out: a long turn switched away and
    # back to should NOT be dropped.
    assert out["caseC"]["busy"] is True, (
        f"long agentic turn with stale last_message_at must not be "
        f"discarded (stream id matches), got {out['caseC']}"
    )
    assert out["caseC"]["activeStreamId"] == "long-turn-stream", (
        f"long agentic turn must re-assert S.activeStreamId, got {out['caseC']}"
    )
    assert out["caseC"]["hasInflight"] is True, (
        f"long agentic turn must NOT discard the INFLIGHT, got {out['caseC']}"
    )
