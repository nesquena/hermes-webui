"""Re-gate regression tests for #7882 (greptile inline P2 findings).

Round 5 (greptile 2026-10-10, static/sessions.js
`upsertActiveSessionForLocalTurn`): after an idle transcript reload,
`_ensureMessagesLoaded` (and the sibling reload paths
`_ensureAllMessagesLoaded` and the gateway CLI refresh) set
`_visibleBaselineLocal` WITHOUT `_visibleBaselineServer`. The next local
send skips the pair initialization because the local baseline already
exists, so the bump computes
`visible_message_count = undefined + optimisticVisible` → NaN, the NaN
comparison loses `Math.max`, and the sidebar falls back to the RAW count —
counting hidden delegation wakeups again.
Fix: every reload path re-baselines BOTH halves together, and the bump
self-heals a missing server half from the local baseline.

Round 6 (greptile 2026-10-11, same file):
1. `_loadOlderMessages` advanced the local baseline only while `S.busy`.
   The pair is sticky across sends, so after IDLE pagination the next
   send's bump (localVisible − stale baseline) counted every prepended
   row a second time: 30 older rows + 1 send → visible +31 instead of +1.
   Fix: prepends advance the local baseline in BOTH states via
   `_advanceVisibleBaselineForServerKnownRows` (server-known rows must
   never land inside a send's bump); the server half stays anchored
   because the server total already includes them.
2. The gateway-refresh source-pin test asserted assignment strings
   anywhere in sessions.js — removing the gateway assignments still
   passed via the other reload paths. Fix: the gateway transcript
   replacement is now an executable unit
   (`_applyGatewayCliImportRefresh`, extracted from startGatewaySSE's
   inline .then callback) and is driven here against the real updater,
   asserting the next send's visible total.
"""
import json
import shutil
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _run_node_vm(source: str) -> str:
    if NODE_BIN is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        import subprocess

        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


_VM_COMMON = r"""
function extractFunc(name) {
  const start = SESSIONS_JS.indexOf('function ' + name + '(');
  if (start < 0) throw Error(name + ' missing');
  let i = SESSIONS_JS.indexOf('{', start) + 1, depth = 1;
  while (depth && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}
const _allSessions = [];
const t = (k) => k;
function renderSessionListFromCache() {}
function closeSessionActionMenu() {}
const document = {createElement: () => ({style: {}, dataset: {}})};
"""


def _updater_body() -> str:
    start = SESSIONS_SRC.index("function upsertActiveSessionForLocalTurn")
    end = SESSIONS_SRC.index("function _sessionRowsWithActiveEphemeralSession", start)
    return SESSIONS_SRC[start:end]


def _fn(name: str) -> str:
    marker = f"function {name}("
    start = SESSIONS_SRC.index(marker)
    i = SESSIONS_SRC.index("{", start) + 1
    depth = 1
    while depth and i < len(SESSIONS_SRC):
        if SESSIONS_SRC[i] == "{":
            depth += 1
        elif SESSIONS_SRC[i] == "}":
            depth -= 1
        i += 1
    return SESSIONS_SRC[start:i]


# ── Round 5: NaN-poisoned bump after an idle transcript load ───────────────


@_node_tests
def test_send_after_reload_only_local_baseline_does_not_go_nan():
    """The exact round-5 greptile reproduction: a reload path set ONLY the
    local baseline (simulated by the test pre-seeding the pair the way
    `_ensureMessagesLoaded` used to). The next send must still produce a
    finite visible count that excludes hidden wakeups — never NaN, which
    the sidebar falls back from to the raw count."""
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + r"""
const S = {
  session: {
    session_id: 'sid-nan-7882',
    title: 'Chat with wakeup',
    message_count: 4,             // server raw total: 3 visible + 1 hidden
    visible_message_count: 3,     // server visible total
    // Reload-path shape: local half set, server half left undefined —
    // exactly what _ensureMessagesLoaded produced before the fix.
    _visibleBaselineLocal: 3,
  },
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'a1'},
    {role: 'user', content: 'q2'},
    {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
     _source: 'delegation_wakeup'},
    {role: 'user', content: 'my new question'},
  ],
  activeProfile: 'default',
};

"""
        + _updater_body()
        + r"""

upsertActiveSessionForLocalTurn({messageCount: 5, localVisibleBeforePush: 3});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    assert result["visible"] == result["visible"], (
        "Visible count went NaN — an undefined _visibleBaselineServer "
        f"poisoned the bump and the sidebar would fall back to the raw "
        f"count, got {result}"
    )
    assert result["raw"] == 5
    # 3 visible + 1 send = 4: the hidden wakeup stays excluded.
    assert result["visible"] == 4, (
        f"Visible count must stay 4 (3 visible + 1 send), got {result}"
    )


@_node_tests
def test_send_after_full_reload_uses_fresh_server_baseline():
    """After a real idle reload (both baselines set together), a send bumps
    the SERVER visible total by exactly its own rows — the fresh server
    half from the reload response is authoritative, so the wakeup that the
    reload absorbed stays excluded."""
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + r"""
const S = {
  session: {
    session_id: 'sid-reload-7882',
    title: 'Reloaded chat',
    message_count: 4,
    visible_message_count: 3,     // fresh value from the reload response
    // Both halves, as _ensureMessagesLoaded now sets them: the server
    // half adopts the reload's authoritative visible total.
    _visibleBaselineLocal: 3,
    _visibleBaselineServer: 3,
  },
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'a1'},
    {role: 'user', content: 'q2'},
    {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
     _source: 'delegation_wakeup'},
    {role: 'user', content: 'my new question'},
  ],
  activeProfile: 'default',
};

"""
        + _updater_body()
        + r"""

upsertActiveSessionForLocalTurn({messageCount: 5, localVisibleBeforePush: 3});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    # 3 visible + 1 send = 4 — no NaN, no raw-count leak.
    assert result["visible"] == 4, (
        f"Send after reload must bump exactly its own rows (3+1=4), got {result}"
    )


# ── Round 6 finding 1: idle pagination double-counts the next send ─────────


@_node_tests
def test_send_after_idle_pagination_bumps_only_its_own_rows():
    """The exact round-6 greptile reproduction: a paginated conversation
    (server visible total 100, 30 loaded visible rows), the user loads 30
    older rows while IDLE, then sends one message. The sticky baseline pair
    must have advanced with the prepends (server-known rows), so the send
    bumps the visible total by exactly 1 → 101. Without the fix the bump is
    30+1=31 and the sidebar reports 131 until the next server refresh."""
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + _fn("_advanceVisibleBaselineForServerKnownRows")
        + _updater_body()
        + r"""
const S = {
  session: {
    session_id: 'sid-pag-7882',
    title: 'Long chat',
    message_count: 100,
    visible_message_count: 100,   // server visible total (no wakeups here)
    // Sticky pair from the last send, BEFORE pagination: 30 loaded visible
    // rows, server total 100.
    _visibleBaselineLocal: 30,
    _visibleBaselineServer: 100,
  },
  // 30 loaded visible rows.
  messages: Array.from({length: 30}, (_, i) => ({role: 'user', content: 'm' + i})),
  activeProfile: 'default',
};

// Idle pagination: _loadOlderMessages prepends 30 older VISIBLE rows to the
// transcript AND advances the sticky pair's local half by the same count
// (production call shape — same sid, count of visible prepended rows).
S.messages = Array.from({length: 30}, (_, i) => ({role: 'user', content: 'old' + i}))
  .concat(S.messages);
_advanceVisibleBaselineForServerKnownRows('sid-pag-7882', 30);

// The transcript tail is now 60 visible rows; the user sends one message.
S.messages.push({role: 'user', content: 'my new question'});
upsertActiveSessionForLocalTurn({messageCount: 61, localVisibleBeforePush: 60});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
  baselineLocal: S.session._visibleBaselineLocal,
  baselineServer: S.session._visibleBaselineServer,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    assert result["visible"] == 101, (
        "Send after idle pagination must bump exactly its own row "
        f"(100+1=101) — the prepended older rows were counted twice, got "
        f"{result}"
    )


@_node_tests
def test_busy_send_baseline_survives_full_reload_midflight():
    """A full-transcript reload (msg_limit=all) landing while a send is in
    flight only adds server-known rows: the in-flight send's local baseline
    must advance by the replace's visible-row delta, keeping the bump scoped
    to the send's own rows (same rule as idle pagination)."""
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + _fn("_advanceVisibleBaselineForServerKnownRows")
        + _updater_body()
        + r"""
const S = {
  session: {
    session_id: 'sid-busy-7882',
    title: 'Chat',
    message_count: 30,
    visible_message_count: 29,
    // The in-flight send snapshotted 30 loaded visible rows / server 29.
    _visibleBaselineLocal: 30,
    _visibleBaselineServer: 29,
  },
  // 30 loaded rows + the optimistic user turn pushed below = 31 visible.
  messages: Array.from({length: 30}, (_, i) => ({role: 'user', content: 'm' + i})),
  activeProfile: 'default',
};

// The send pushes its optimistic user turn BEFORE the reload lands.
S.messages.push({role: 'user', content: 'in-flight question'});

// _ensureAllMessagesLoaded replaces the transcript with the server's full
// view: the 31 local rows plus one older row the client had not loaded
// (the send's own rows are server-known by now). Visible delta = +1.
const beforeLen = S.messages.length;
S.messages = [{role: 'user', content: 'older-0'}, ...S.messages];
_advanceVisibleBaselineForServerKnownRows('sid-busy-7882',
  (S.messages.filter(m => m && m.role && m._source !== 'delegation_wakeup').length)
  - (beforeLen));

// Next updater pass of the same send: the bump must stay 1 row — the bump is
// (localVisible 32 − advanced baseline 31) = 1, server half 29 + 1 = 30.
upsertActiveSessionForLocalTurn({messageCount: 32, localVisibleBeforePush: 31});

console.log(JSON.stringify({
  visible: S.session.visible_message_count,
  baselineLocal: S.session._visibleBaselineLocal,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    # 29 visible + 1 send row = 30: the reload's extra older row is NOT
    # re-counted by the in-flight send's bump.
    assert result["visible"] == 30, (
        "In-flight send after a full reload must bump only its own row "
        f"(29+1=30), got {result}"
    )


# ── Round 6 finding 2: the gateway refresh is executed, not string-matched ─


@_node_tests
def test_gateway_refresh_rebaselines_both_halves_and_next_send():
    """Round-6 greptile P2: the old test asserted assignment strings anywhere
    in sessions.js — removing the gateway assignments still passed. This
    drives the extracted production gateway-refresh unit
    (`_applyGatewayCliImportRefresh`, the exact code startGatewaySSE's
    import_cli .then callback now delegates to) against the real updater:
    the refresh must re-baseline BOTH halves from its response, with the
    server half taken from the response's authoritative
    visible_message_count, and the next send must bump exactly one row."""
    gateway_refresh = _fn("_applyGatewayCliImportRefresh")
    prefix_match = _fn("_isCliImportRefreshPrefixMatch")
    normalize = _fn("_normalizeMessageForCliImportComparison")
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + "const window = {};\n"
        + "function renderMessages() {}\n"
        + normalize
        + prefix_match
        + gateway_refresh
        + _updater_body()
        + r"""
const SID = 'sid-gw-7882';
const S = {
  session: {
    session_id: SID,
    title: 'CLI chat',
    message_count: 6,
    visible_message_count: 5,     // pre-refresh sidebar value
  },
  // Local tail: 5 rows, one of them a hidden delegation wakeup.
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'a1'},
    {role: 'user', content: 'q2'},
    {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
     _source: 'delegation_wakeup'},
    {role: 'assistant', content: 'a2'},
  ],
  activeProfile: 'default',
};

// Gateway SSE import_cli response: the merged transcript grew by one
// visible assistant reply. The server's visible_message_count (7) is
// deliberately higher than the client's local derivation (6 = 7 rows minus
// the hidden wakeup): the server count is the authority the sticky pair
// must adopt — mirroring the local count strands the sidebar at the stale
// server total on the next send.
_applyGatewayCliImportRefresh(SID, {
  session: {
    session_id: SID,
    visible_message_count: 7,
    messages: [
      {role: 'user', content: 'q1'},
      {role: 'assistant', content: 'a1'},
      {role: 'user', content: 'q2'},
      {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
       _source: 'delegation_wakeup'},
      {role: 'assistant', content: 'a2'},
      {role: 'assistant', content: 'a3'},
      {role: 'user', content: 'q3 from CLI'},
    ],
  },
});

const afterRefresh = {
  baselineLocal: S.session._visibleBaselineLocal,
  baselineServer: S.session._visibleBaselineServer,
};

// Next local send: one visible row. The bump rides the refreshed pair.
S.messages.push({role: 'user', content: 'my new question'});
upsertActiveSessionForLocalTurn({messageCount: 8, localVisibleBeforePush: 7});

console.log(JSON.stringify({
  afterRefresh,
  visible: S.session.visible_message_count,
  messageCount: S.session.message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    refreshed = result["afterRefresh"]
    # Both halves exist after the refresh (no NaN poison for the next send).
    assert isinstance(refreshed["baselineLocal"], (int, float)), (
        f"Gateway refresh left no local baseline half: {refreshed}"
    )
    assert isinstance(refreshed["baselineServer"], (int, float)), (
        f"Gateway refresh left no server baseline half: {refreshed}"
    )
    # The server half adopts the RESPONSE's authoritative visible total
    # (7), not the client's local derivation (6).
    assert refreshed["baselineServer"] == 7, (
        "Gateway refresh must take the server half from the import "
        f"response's visible_message_count (7), got {refreshed}"
    )
    # Next send bumps exactly its own row: 7 + 1 = 8.
    assert result["visible"] == 8, (
        f"Send after gateway refresh must bump exactly one row (7+1=8), got {result}"
    )
    assert result["messageCount"] == 8


@_node_tests
def test_gateway_refresh_rejects_divergent_prefix():
    """The extracted refresh keeps the inline version's guards: a response
    whose rows diverge from the local prefix is rejected without touching
    the transcript or the baseline pair."""
    gateway_refresh = _fn("_applyGatewayCliImportRefresh")
    prefix_match = _fn("_isCliImportRefreshPrefixMatch")
    normalize = _fn("_normalizeMessageForCliImportComparison")
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + "const window = {};\n"
        + "function renderMessages() {}\n"
        + normalize
        + prefix_match
        + gateway_refresh
        + r"""
const SID = 'sid-gw-diverge';
const S = {
  session: {session_id: SID, message_count: 2, visible_message_count: 2},
  messages: [{role: 'user', content: 'local q1'}, {role: 'assistant', content: 'local a1'}],
};
_applyGatewayCliImportRefresh(SID, {
  session: {
    session_id: SID,
    visible_message_count: 5,
    messages: [{role: 'user', content: 'DIVERGED'}, {role: 'assistant', content: 'x'}],
  },
});
console.log(JSON.stringify({
  len: S.messages.length,
  first: S.messages[0].content,
  baselineLocal: S.session._visibleBaselineLocal === undefined ? null : S.session._visibleBaselineLocal,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    assert result["len"] == 2 and result["first"] == "local q1", (
        f"Divergent prefix must be rejected, transcript mutated: {result}"
    )
    assert result["baselineLocal"] is None, (
        f"Rejected refresh must not re-baseline the pair: {result}"
    )


# ── Source pins: companions to the executed tests above ────────────────────
# The round-5 pins stay narrow (per-function, both halves); the round-6 pins
# tie the executed helper to its production call sites.


def test_ensure_messages_loaded_rebaselines_both_halves():
    """_ensureMessagesLoaded must reset _visibleBaselineServer alongside
    _visibleBaselineLocal when idle — the local-only baseline was the bug."""
    fn = _fn("_ensureMessagesLoaded")
    assert "_visibleBaselineLocal=" in fn
    assert "_visibleBaselineServer=" in fn, (
        "_ensureMessagesLoaded re-baselines only the local half — the next "
        "send's bump goes NaN and the sidebar counts hidden wakeups again"
    )


def test_ensure_all_messages_loaded_rebaselines_both_halves():
    """_ensureAllMessagesLoaded must reset both halves together too, and its
    in-flight-send branch must advance the baseline via the shared helper."""
    fn = _fn("_ensureAllMessagesLoaded")
    assert "_visibleBaselineLocal=" in fn
    assert "_visibleBaselineServer=" in fn, (
        "_ensureAllMessagesLoaded re-baselines only the local half"
    )
    assert "_advanceVisibleBaselineForServerKnownRows(" in fn, (
        "_ensureAllMessagesLoaded busy branch must advance the in-flight "
        "send's baseline for the rows the wholesale replace added"
    )


def test_load_older_messages_advances_baseline_in_both_states():
    """_loadOlderMessages must route prepend accounting through the shared
    helper with NO busy gate — idle pagination was the round-6 double-count."""
    fn = _fn("_loadOlderMessages")
    assert "_advanceVisibleBaselineForServerKnownRows(" in fn, (
        "_loadOlderMessages must advance the sticky baseline for prepended "
        "server-known rows (idle or busy) or the next send counts them twice"
    )


def test_start_gateway_sse_delegates_to_extracted_refresh():
    """startGatewaySSE's import_cli callback must delegate to the extracted,
    unit-testable refresh — the inline closure was the round-6
    test-oracle gap."""
    fn = _fn("startGatewaySSE")
    assert "_applyGatewayCliImportRefresh(" in fn, (
        "startGatewaySSE must delegate the import_cli refresh to "
        "_applyGatewayCliImportRefresh so the transcript replacement stays "
        "executable as a unit"
    )
