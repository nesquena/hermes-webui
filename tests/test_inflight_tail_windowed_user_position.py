"""Windowed transcripts must place the current turn's prompt by turn ownership.

Repro (WebUI, live session opened while `msg_limit` paging is active):

  * the first page is a TAIL window of the transcript, so it can start in the
    middle of the running turn and carry no `user` row at all;
  * `_mergeInflightTailMessages()` pulls the live tail in and, because that
    tail starts at the turn's `user` row, pushed the prompt onto the END of
    the window;
  * `renderMessages()` then painted the orphaned window rows as a standalone
    assistant turn above the user bubble — the reader sees "messages above my
    first message", and the prompt sits after its own progress narration.

A user-free window alone does NOT prove the rows belong to the running turn:
with deferred session saving the last window can be entirely the PREVIOUS
completed turn while the new prompt is still unpersisted. Placement therefore
follows turn/start ownership (`pending_started_at` vs persisted `_ts`, plus
`_live` rows) instead of the shape of the window.

A prompt recovered from outside the canonical transcript — a windowed tail or
a full (zero-offset) load whose prompt is still unpersisted — is display-only:
it has no canonical index, so it must never shift the absolute index of the
real rows that fork/edit/regenerate and the older-page cursor are computed
from. The display-only marker is what keeps saved row 0 at absolute 0
(keep_count 1) when the recovered prompt leads a zero-offset transcript.
"""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
UI_JS_PATH = ROOT / "static" / "ui.js"
COMMANDS_JS_PATH = ROOT / "static" / "commands.js"
NODE = shutil.which("node")


def _helper_src() -> str:
    src = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    start = src.find("function _messageComparableText")
    end = src.find("// Load older messages", start)
    assert start != -1 and end != -1, "sessions.js helper slice markers not found"
    return src[start:end]


def _function(src: str, name: str) -> str:
    marker = "function " + name + "("
    plain = src.find(marker)
    async_at = src.find("async " + marker)
    candidates = [p for p in (plain, async_at) if p != -1]
    assert candidates, f"{name} not found"
    start = min(candidates)
    brace = src.index("{", start)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated function {name}")


def _function_optional(src: str, name: str) -> str:
    try:
        return _function(src, name)
    except AssertionError:
        return ""


def _index_helpers() -> str:
    src = UI_JS_PATH.read_text(encoding="utf-8")
    helpers = (
        "_displayOnlyIndexState",
        "_markDisplayOnlyRow",
        "_displayOnlyRawIndices",
        "_displayOnlyCountBefore",
        "_displayOnlyRowAt",
        "_messageSessionIndexBase",
        "_messageSessionIndexForRawIdx",
        "_messageRawIdxForSessionIndex",
        "_canonicalRawIdxForSessionIndex",
        "_rowCurrentRawIdx",
    )
    parts = [chunk for chunk in (_function_optional(src, n) for n in helpers) if chunk]
    if "_canonicalRawIdxForSessionIndex" not in "\n".join(parts):
        # Base blob has no canonical helper; keep the RED control semantic
        # (fork payload 33 instead of 32) instead of failing at extraction.
        parts.append(
            "function _canonicalRawIdxForSessionIndex(i){return Number(i)-_messageSessionIndexBase();}"
        )
    return "\n".join(parts)


def _fork_caller() -> str:
    return _function(COMMANDS_JS_PATH.read_text(encoding="utf-8"), "forkFromMessage")


def _run(script: str) -> str:
    assert NODE, "node is required for this test"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def test_windowed_tail_keeps_prompt_above_activity_rows():
    """A tail window that starts mid-turn must not swallow the prompt to the end."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 16;
{helper_src}

// First page = transcript[16:], which is inside the running turn: activity
// rows only, no user row. The live tail still starts at that turn's prompt.
const base = [
  {{role:'assistant', content:'icons generated, starting the first build'}},
  {{role:'tool', content:'{{"status":"success"}}'}},
  {{role:'assistant', content:'build failed in 11 places'}},
];
const inflight = [
  {{role:'user', content:'build me a todo app with a sticky notification'}},
  {{role:'assistant', _live:true, content:'now running the JVM tests'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.deepStrictEqual(
  merged.map(m => m.role),
  ['user', 'assistant', 'tool', 'assistant', 'assistant'],
  'prompt must stay in front of the loaded window rows'
);
assert.strictEqual(merged[0].content, 'build me a todo app with a sticky notification');
assert.strictEqual(merged.filter(m => m.role === 'user').length, 1);
assert.strictEqual(merged[merged.length - 1]._live, true);
"""
    _run(script)


def test_full_transcript_still_dedupes_prompt_in_place():
    """Full transcripts keep the prompt where it already is (no duplicate, no move).

    Real callers run `_dropCurrentTurnAssistantMessages(S.messages)` first, so a
    full transcript reaches the merge with its current-turn assistant rows
    already removed — the tail user row is then the last row.
    """
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 0;
{helper_src}

const base = [
  {{role:'user', content:'go'}},
];
const inflight = [
  {{role:'user', content:'go'}},
  {{role:'assistant', _live:true, content:'first progress'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.deepStrictEqual(merged.map(m => m.role), ['user', 'assistant']);
assert.strictEqual(merged[0].content, 'go');
assert.strictEqual(merged[merged.length - 1]._live, true);
"""
    _run(script)


def test_windowed_tail_places_pending_prompt_before_the_window():
    """The pending-user recovery path needs the same windowed placement rule.

    `_mergeInflightTailMessages()` alone is not enough: the live restore also
    runs `_mergePendingSessionMessage()`, which splices the pending prompt in
    front of the live tail. With a mid-turn window that leaves the prompt after
    its own activity rows, so it must go in front of the window instead.
    """
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 120;
{helper_src}

function getPendingSessionMessage(session, messages) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {{role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true}};
}}

const live = {{role:'assistant', _live:true, content:'running'}};
const messages = [
  {{role:'assistant', content:'icons generated'}},
  {{role:'tool', content:'{{"status":"success"}}'}},
  live,
];
const changed = _mergePendingSessionMessage({{pending_user_message:'build me a todo app', pending_started_at:1}}, messages);
assert.strictEqual(changed, true);
assert.deepStrictEqual(
  messages.map(m => m.role),
  ['user', 'assistant', 'tool', 'assistant'],
  'pending prompt must lead the windowed transcript'
);
assert.strictEqual(messages[0].content, 'build me a todo app');
assert.strictEqual(messages[messages.length - 1]._live, true);
"""
    _run(script)


def test_window_with_an_earlier_turn_keeps_pending_prompt_before_live():
    """When the window already holds a turn, the pending prompt still goes before the live tail."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 40;
{helper_src}

function getPendingSessionMessage(session, messages) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {{role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true}};
}}

const live = {{role:'assistant', _live:true, content:'running'}};
const messages = [
  {{role:'user', content:'earlier turn'}},
  {{role:'assistant', content:'earlier answer'}},
  live,
];
const changed = _mergePendingSessionMessage({{pending_user_message:'current turn', pending_started_at:3}}, messages);
assert.strictEqual(changed, true);
assert.deepStrictEqual(
  messages.map(m => m.role),
  ['user', 'assistant', 'user', 'assistant'],
  'pending prompt belongs after the earlier turn, directly before the live tail'
);
assert.strictEqual(messages[0].content, 'earlier turn');
assert.strictEqual(messages[2].content, 'current turn');
"""
    _run(script)


def test_previous_turn_window_does_not_claim_the_new_prompt():
    """A window made only of the previous turn must not put the new prompt on top.

    Deferred session saving keeps the new prompt out of the persisted
    transcript while the tail window still ends at the previous completed turn:
    every row has `_ts` before `pending_started_at`, so none of them belongs to
    the running turn. The prompt belongs AFTER those rows.
    """
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 31;
{helper_src}

// Persisted window: 30 rows of the PREVIOUS turn (timestamps 32..61), while
// the new turn starts at 100 and its prompt is still unsaved.
const base = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'previous turn activity ' + i,
  _ts: 32 + i,
}}));
const inflight = [
  {{role:'user', content:'brand new prompt', _ts:100}},
  {{role:'assistant', _live:true, content:'working on it'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(merged[0].role, 'assistant', 'previous-turn activity stays above the new prompt');
assert.strictEqual(merged[merged.length - 1]._live, true);
assert.strictEqual(merged[merged.length - 2].content, 'brand new prompt');
assert.strictEqual(merged.filter(m => m.role === 'user').length, 1);
"""
    _run(script)


def test_previous_turn_window_keeps_pending_prompt_after_older_rows():
    """Pending recovery must use the same ownership rule for the previous-turn window."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 31;
{helper_src}

function getPendingSessionMessage(session, messages) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {{role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true}};
}}

const messages = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'previous turn activity ' + i,
  _ts: 32 + i,
}}));
messages.push({{role:'assistant', _live:true, content:'working on it'}});
const changed = _mergePendingSessionMessage({{pending_user_message:'brand new prompt', pending_started_at:100}}, messages);
assert.strictEqual(changed, true);
assert.strictEqual(messages[0].role, 'assistant');
assert.strictEqual(messages[messages.length - 1]._live, true);
assert.strictEqual(messages[messages.length - 2].content, 'brand new prompt');
assert.strictEqual(messages.filter(m => m.role === 'user').length, 1);
"""
    _run(script)


def test_current_turn_window_still_places_prompt_first():
    """Timestamps at/after the turn start prove the window is the running turn's suffix."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 31;
{helper_src}

const base = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'current turn activity ' + i,
  _ts: 100 + i,
}}));
const inflight = [
  {{role:'user', content:'current prompt', _ts:100}},
  {{role:'assistant', _live:true, content:'working on it'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(merged[0].content, 'current prompt');
assert.strictEqual(merged[0]._displayOnlyRow, true);
assert.strictEqual(merged.filter(m => m.role === 'user').length, 1);
"""
    _run(script)


def _fork_harness(index_helpers: str, helper_src: str, fork_src: str, oldest_idx: int = 31, busy: bool = False) -> str:
    """Shared node prelude: real index helpers + real merge helpers + real fork caller."""
    return f"""
const assert = require('assert');
var S = {{session:{{session_id:'s1', pending_started_at:100}}, messages:[], busy:{'true' if busy else 'false'}}};
let _oldestIdx = {oldest_idx};
{index_helpers}
{helper_src}
const forkPayloads = [];
function api(url, opts) {{
  forkPayloads.push(JSON.parse(opts.body));
  return Promise.resolve({{session_id:'forked'}});
}}
async function loadSession() {{}}
async function renderSessionList() {{}}
async function _ensureAllMessagesLoaded() {{}}
function showToast() {{}}
function t(k) {{ return k; }}
function _isReadOnlySession() {{ return false; }}
function _isBranchableReadOnlySession() {{ return false; }}
{fork_src}
function rowFor(rawIdx) {{
  // Mirror the real renderer (ui.js line ~21986): display-only rows carry
  // data-display-only='1', which forkFromMessage reads before any index math.
  const m = S.messages[rawIdx] || null;
  return {{dataset:{{msgIdx:String(rawIdx), sessionMsgIdx:String(_messageSessionIndexForRawIdx(rawIdx)), displayOnly:(m && m._displayOnlyRow) ? '1' : undefined}}}};
}}
async function forkAt(rawIdx) {{
  await forkFromMessage(rawIdx + 1, {{closest:() => rowFor(rawIdx)}});
  return forkPayloads[forkPayloads.length - 1].keep_count;
}}
const windowRows = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'current turn activity ' + i,
  _ts: 100 + i,
}}));
const currentPrompt = {{role:'user', content:'current prompt', _ts:100, _pending:true}};
"""


def test_recovered_prompt_does_not_shift_canonical_window_indices():
    """Both recovery sources must keep `keep_count` on the canonical window row.

    The window begins at absolute index 31. Inserting a recovered prompt at the
    head must not make canonical row 31 look like absolute row 32 — the real
    fork caller would then send 33 instead of 32.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    script = (
        _fork_harness(index_helpers, helper_src, fork_src)
        + """
function getPendingSessionMessage(session, messages) {
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true};
}

(async () => {
  // Source A: INFLIGHT live-tail recovery.
  S.messages = _mergeInflightTailMessages(windowRows, [
    currentPrompt,
    {role:'assistant', _live:true, content:'working on it'},
  ]);
  assert.strictEqual(S.messages[0].content, 'current prompt');
  assert.strictEqual(S.messages[1].content, 'current turn activity 0');
  assert.strictEqual(_messageSessionIndexForRawIdx(0), null, 'display-only rows have no canonical index');
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 31);
  const inflightKeep = await forkAt(1);

  // Source B: pending-user recovery on a prompt-free window.
  S.messages = windowRows.slice();
  const changed = _mergePendingSessionMessage({pending_user_message:'current prompt', pending_started_at:100}, S.messages);
  assert.strictEqual(changed, true);
  assert.strictEqual(S.messages[0].content, 'current prompt');
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 31);
  const pendingKeep = await forkAt(1);

  console.log(JSON.stringify({inflightKeep, pendingKeep}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    assert '"inflightKeep":32' in out, out
    assert '"pendingKeep":32' in out, out


def test_fork_payload_survives_older_page_load():
    """Paging into the canonical prompt must drop the display copy and keep indices exact.

    Runs the real `_loadOlderMessages()` against a stubbed transport. The older
    page contains the canonical user row (absolute 10), so the display-only
    prompt must be reconciled away rather than duplicated; fork on absolute 31
    still sends 32 afterwards.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    bump_generation = _function(SESSIONS_JS_PATH.read_text(encoding="utf-8"), "_bumpMessagesGeneration")
    load_older = _function(SESSIONS_JS_PATH.read_text(encoding="utf-8"), "_loadOlderMessages")
    script = (
        _fork_harness(index_helpers, helper_src, fork_src)
        + f"""
let _messagesTruncated = true;
let _loadingOlder = false;
let _loadingSessionId = 's1';
let _msgLimitMax = 500;
let _messageRenderWindowSize = 50;
let _messagesGeneration = 0;
const _INITIAL_MSG_LIMIT = 30;
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
var window = {{}};
{bump_generation}

// Full canonical transcript: row 10 is the still-running turn's user prompt.
const transcript = Array.from({{length:61}}, (_, i) => ({{
  role: i === 10 ? 'user' : (i % 4 === 1 ? 'tool' : 'assistant'),
  content: i === 10 ? 'current prompt' : 'row ' + i,
  _ts: i,
}}));
function page(rows, offset, truncated) {{
  return {{session:{{messages:rows, _messages_offset:offset, _messages_truncated:truncated, tool_calls:[]}}}};
}}
function api(url, opts) {{
  if (opts && opts.method === 'POST') {{
    forkPayloads.push(JSON.parse(opts.body));
    return Promise.resolve({{session_id:'forked'}});
  }}
  if (url.indexOf('msg_before=31') !== -1) return Promise.resolve(page(transcript.slice(1, 31), 1, true));
  return Promise.resolve(page(transcript.slice(0, 61), 0, false));
}}
function $() {{ return null; }}
function renderMessages() {{}}
function _syncToolCallsForLoadedMessages() {{}}
function _advanceVisibleBaselineForServerKnownRows() {{}}
function _currentMessageRenderWindowSize() {{ return 50; }}
function msgContent(m) {{ return String((m && m.content) || ''); }}
{load_older}

(async () => {{
  // The running turn starts at absolute row 10 (ts 10), so the window rows
  // (ts 100+) and the recovered prompt share that turn start.
  currentPrompt._ts = 10;
  S.session.pending_started_at = 10;
  S.messages = _mergeInflightTailMessages(windowRows, [
    currentPrompt,
    {{role:'assistant', _live:true, content:'working on it'}},
  ]);
  const before = await forkAt(1);
  await _loadOlderMessages();
  assert.strictEqual(S.messages.filter(m => m.role === 'user' && m.content === 'current prompt').length, 1,
    'the canonical row replaces the display-only copy');
  const canonicalIdx = S.messages.findIndex(m => m.role === 'user' && m.content === 'current prompt');
  assert.strictEqual(canonicalIdx, 9);
  const windowStart = S.messages.findIndex(m => m.content === 'current turn activity 0');
  assert.strictEqual(_oldestIdx + windowStart, 31, 'absolute index of the window head is preserved');
  const after = await forkAt(windowStart);
  console.log(JSON.stringify({{before, after, messages: S.messages.length}}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    )
    out = _run(script)
    assert '"before":32' in out, out
    assert '"after":32' in out, out


def test_busy_guard_forks_saved_row_after_recovered_prefix():
    """S.busy=true: a saved canonical row right after a recovered prefix still forks.

    `_rowCurrentRawIdx()` returns the CANONICAL window-relative index
    (display-only rows excluded). A busy guard that indexes `S.messages` with
    that canonical count lands on the recovered display-only prompt, which
    carries `_pending`, so the fork of a committed row is refused even though
    the correct keep_count is computable. Both recovery sources must fork the
    window head (canonical 31) with keep_count 32 while S.busy is true.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    script = (
        _fork_harness(index_helpers, helper_src, fork_src, busy=True)
        + """
function getPendingSessionMessage(session, messages) {
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true};
}
async function forkAttempt(rawIdx) {
  try { return await forkAt(rawIdx); } catch (e) { return null; }
}

(async () => {
  // Source A: local-INFLIGHT live-tail recovery — the recovered prompt leads
  // the window, the 30 saved canonical rows follow, the live row trails.
  S.messages = _mergeInflightTailMessages(windowRows, [
    currentPrompt,
    {role:'assistant', _live:true, content:'working on it'},
  ]);
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 31);
  const inflightKeep = await forkAttempt(1);
  assert.strictEqual(inflightKeep, 32,
    'INFLIGHT recovery: the saved row after the recovered prompt must fork with 32, not be misread as the pending prompt');

  // Source B: prompt-free server-journal recovery (pending prompt spliced
  // into the loaded window by _mergePendingSessionMessage).
  S.messages = windowRows.slice();
  const changed = _mergePendingSessionMessage({pending_user_message:'current prompt', pending_started_at:100}, S.messages);
  assert.strictEqual(changed, true);
  S.messages.push({role:'assistant', _live:true, content:'working on it'});
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 31);
  const pendingKeep = await forkAttempt(1);
  assert.strictEqual(pendingKeep, 32,
    'server-journal recovery: the saved row after the recovered prompt must fork with 32');

  console.log(JSON.stringify({inflightKeep, pendingKeep}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    assert '"inflightKeep":32' in out, out
    assert '"pendingKeep":32' in out, out


def test_busy_guard_refuses_live_row():
    """S.busy=true: forking the live row itself must be refused, not branched.

    With a recovered display-only prefix, the live row's canonical index is
    smaller than its real array position: `S.messages[canonical-1]` is the
    last SAVED row and the last-row check compares a canonical count against
    the display array length — both miss the live row, so the branch API is
    called. The guard must read the real current array position instead.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    script = (
        _fork_harness(index_helpers, helper_src, fork_src, busy=True)
        + """
function getPendingSessionMessage(session, messages) {
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true};
}
async function forkAttempt(rawIdx) {
  try { return await forkAt(rawIdx); } catch (e) { return null; }
}

(async () => {
  // Source A: local-INFLIGHT live-tail recovery.
  S.messages = _mergeInflightTailMessages(windowRows, [
    currentPrompt,
    {role:'assistant', _live:true, content:'working on it'},
  ]);
  const inflightCalls = forkPayloads.length;
  await forkAttempt(S.messages.length - 1);
  assert.strictEqual(forkPayloads.length, inflightCalls,
    'INFLIGHT recovery: the live row must not reach /api/session/branch');

  // Source B: prompt-free server-journal recovery.
  S.messages = windowRows.slice();
  _mergePendingSessionMessage({pending_user_message:'current prompt', pending_started_at:100}, S.messages);
  S.messages.push({role:'assistant', _live:true, content:'working on it'});
  const pendingCalls = forkPayloads.length;
  await forkAttempt(S.messages.length - 1);
  assert.strictEqual(forkPayloads.length, pendingCalls,
    'server-journal recovery: the live row must not reach /api/session/branch');

  console.log(JSON.stringify({liveRefused: true}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    assert '"liveRefused":true' in out, out


# ── Zero-offset (full transcript) recovered-prompt regressions ────────────
# The non-windowed branch (offset 0) also recovers an unpersisted current-turn
# prompt ahead of the server's activity rows. Without the display-only marker
# the prompt counts as a real array member: saved row 0 (array position 1)
# maps to absolute position 1 and the real fork caller sends keep_count 2
# instead of 1. These tests compose the real merge, the real index helpers,
# the real fork caller and the real reload path with a stubbed transport.

_ZERO_OFFSET_FIXTURES = """
const savedRows = [
  {role:'assistant', content:'saved answer 0', _ts:10},
  {role:'tool', content:'{"status":"success"}', _ts:11},
];
const inflightTail = [
  {role:'user', content:'current prompt', _ts:10, _pending:true},
  {role:'assistant', _live:true, content:'working on it'},
];
"""


def _ensure_loaded_scaffold() -> str:
    """Module-state + stubs required to run the real `_ensureMessagesLoaded`."""
    return """
let _loadingSessionId = 's1';
let _loadSessionGeneration = 0;
let _pendingCarryForwardSnapshot = null;
let _sameSessionForceReloadHint = null;
let _messagesTruncated = false;
let _msgLimitMax = 500;
const _MSG_LIMIT_MAX = 500;
function _messageReloadLimitForSession() { return 30; }
function _clearSameSessionForceReloadHint() {}
function clearLiveToolCards() {}
function _syncToolCallsForLoadedMessages() {}
function _isSessionActivelyViewedForList() { return true; }
function _setSessionViewedCount() {}
var INFLIGHT = {};
var window = {};
"""


def test_zero_offset_recovered_prompt_keeps_canonical_index():
    """Full-transcript recovery: saved row 0 must stay canonical 0 (keep_count 1)."""
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    script = (
        _fork_harness(index_helpers, helper_src, fork_src, oldest_idx=0)
        + _ZERO_OFFSET_FIXTURES
        + """
async function forkAttempt(rawIdx) {
  try { return await forkAt(rawIdx); } catch (e) { return null; }
}

(async () => {
  // Real caller chain: prepare the live tail, drop current-turn assistant
  // rows, then merge.
  S.messages = savedRows.slice();
  const prepared = _prepareRunningLiveTail(S.messages, inflightTail);
  if (prepared) S.messages = _dropCurrentTurnAssistantMessages(S.messages);
  S.messages = _mergeInflightTailMessages(S.messages, inflightTail);
  assert.deepStrictEqual(S.messages.map(m => m.role), ['user','assistant','tool','assistant']);
  assert.strictEqual(S.messages[0].content, 'current prompt');
  // The recovered prompt is display-only: no canonical index of its own, and
  // the saved rows keep their absolute positions (0, 1) instead of 1, 2.
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  assert.strictEqual(_messageSessionIndexForRawIdx(0), null,
    'display-only rows have no canonical index');
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 0,
    'saved row 0 must stay canonical 0, not shift to 1');
  const idleKeep = await forkAt(1);
  assert.strictEqual(idleKeep, 1,
    'idle fork of saved row 0 must send keep_count 1, not 2');
  // The recovered prompt itself refuses the branch (renderer marks the row
  // data-display-only; forkFromMessage returns before any index math).
  const callsBefore = forkPayloads.length;
  await forkAttempt(0);
  assert.strictEqual(forkPayloads.length, callsBefore,
    'the recovered display-only prompt must not reach /api/session/branch');
  console.log(JSON.stringify({idleKeep}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    assert '"idleKeep":1' in out, out


def test_zero_offset_busy_guard_forks_saved_row_and_refuses_live():
    """S.busy=true: saved row 0 still forks keep_count 1; live row refuses."""
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    script = (
        _fork_harness(index_helpers, helper_src, fork_src, oldest_idx=0, busy=True)
        + _ZERO_OFFSET_FIXTURES
        + """
async function forkAttempt(rawIdx) {
  try { return await forkAt(rawIdx); } catch (e) { return null; }
}

(async () => {
  S.messages = _mergeInflightTailMessages(savedRows.slice(), inflightTail);
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 0);
  const busyKeep = await forkAttempt(1);
  assert.strictEqual(busyKeep, 1,
    'busy fork of saved row 0 must send keep_count 1, not 2');
  const promptCalls = forkPayloads.length;
  await forkAttempt(0);
  assert.strictEqual(forkPayloads.length, promptCalls,
    'recovered display-only prompt must refuse the branch while busy');
  const liveCalls = forkPayloads.length;
  await forkAttempt(S.messages.length - 1);
  assert.strictEqual(forkPayloads.length, liveCalls,
    'the live row must refuse the branch while busy');
  console.log(JSON.stringify({busyKeep, refusals:true}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    assert '"busyKeep":1' in out, out
    assert '"refusals":true' in out, out


def test_zero_offset_canonical_prompt_stays_unmarked():
    """Control: an already-canonical prompt is deduped in place and unmarked."""
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    script = (
        _fork_harness(index_helpers, helper_src, fork_src, oldest_idx=0)
        + """
// Full transcript already carries the canonical prompt; the INFLIGHT tail
// mirrors it. Real callers drop current-turn assistant rows first, so the
// tail user row is the last row and dedupe keeps the canonical copy in place.
let base = [
  {role:'user', content:'saved prompt', _ts:10},
  {role:'assistant', content:'saved answer', _ts:11},
];
const inflight = [
  {role:'user', content:'saved prompt', _ts:10, _pending:true},
  {role:'assistant', _live:true, content:'working on it'},
];
const dropped = _dropCurrentTurnAssistantMessages(base);
assert.deepStrictEqual(dropped.map(m => m.role), ['user']);
S.messages = _mergeInflightTailMessages(dropped, inflight);
assert.deepStrictEqual(S.messages.map(m => m.role), ['user','assistant']);
assert.strictEqual(S.messages[0].content, 'saved prompt');
assert.strictEqual(S.messages[0]._displayOnlyRow, undefined,
  'the canonical prompt must stay unmarked');
assert.strictEqual(S.messages.filter(m => m.role === 'user').length, 1);
assert.strictEqual(_messageSessionIndexForRawIdx(0), 0);

(async () => {
  const keep = await forkAt(0);
  assert.strictEqual(keep, 1);
  console.log(JSON.stringify({keep}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    assert '"keep":1' in out, out


def test_zero_offset_repeated_recovery_keeps_one_prompt():
    """A second recovery pass against the carried array must not stack a prompt.

    The real reload can early-return (messages populated + a
    regeneration_revision), so the second pass merges against the array that
    already holds the recovered prompt. The real prepare/drop step clears the
    current-turn assistant rows that would otherwise block the tail-user
    duplicate scan, so exactly one prompt survives with exact indices.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    ensure = _function(SESSIONS_JS_PATH.read_text(encoding="utf-8"), "_ensureMessagesLoaded")
    script = (
        _fork_harness(index_helpers, helper_src, fork_src, oldest_idx=0)
        + _ZERO_OFFSET_FIXTURES
        + _ensure_loaded_scaffold()
        + f"""
{ensure}
let ensureCalls = 0;
function api(url, opts) {{
  if (opts && opts.method === 'POST') {{
    forkPayloads.push(JSON.parse(opts.body));
    return Promise.resolve({{session_id:'forked'}});
  }}
  ensureCalls++;
  return Promise.resolve({{session:{{messages:[], _messages_offset:0, _messages_truncated:false, _msg_limit_max:500, tool_calls:[], message_count:0, visible_message_count:0, last_usage:{{}}}}}});
}}
function recoverPass() {{
  return _ensureMessagesLoaded('s1', {{}}).then(() => {{
    const prepared = _prepareRunningLiveTail(S.messages, inflightTail);
    if (prepared) S.messages = _dropCurrentTurnAssistantMessages(S.messages);
    S.messages = _mergeInflightTailMessages(S.messages, inflightTail);
  }});
}}
(async () => {{
  S.session.regeneration_revision = 'r1';
  S.messages = _mergeInflightTailMessages(savedRows.slice(), inflightTail);
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  await recoverPass();
  assert.strictEqual(ensureCalls, 0,
    'reload must early-return; the second pass merged against the carried array');
  assert.strictEqual(S.messages.filter(m => m.role === 'user').length, 1,
    'repeated recovery must keep exactly one prompt');
  assert.strictEqual(S.messages[0].content, 'current prompt');
  assert.strictEqual(_messageSessionIndexForRawIdx(0), null);
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 0,
    'saved row 0 must stay canonical 0 after the second pass');
  const keep = await forkAt(1);
  assert.strictEqual(keep, 1);
  console.log(JSON.stringify({{keep}}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    )
    out = _run(script)
    assert '"keep":1' in out, out


def test_zero_offset_canonical_reload_drops_display_copy():
    """Once the turn settles, a full reload replaces the display copy with the
    canonical prompt and indices are correct again.

    Drives the real `_ensureMessagesLoaded()` against a stubbed transport: the
    reload response carries the now-persisted prompt, so the wholesale replace
    drops the display-only row instead of duplicating it.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    ensure = _function(SESSIONS_JS_PATH.read_text(encoding="utf-8"), "_ensureMessagesLoaded")
    script = (
        _fork_harness(index_helpers, helper_src, fork_src, oldest_idx=0)
        + _ZERO_OFFSET_FIXTURES
        + _ensure_loaded_scaffold()
        + f"""
{ensure}
function api(url, opts) {{
  if (opts && opts.method === 'POST') {{
    forkPayloads.push(JSON.parse(opts.body));
    return Promise.resolve({{session_id:'forked'}});
  }}
  // The turn settled: the canonical transcript now includes the prompt.
  return Promise.resolve({{session:{{messages:[
    {{role:'user', content:'current prompt', _ts:10}},
    {{role:'assistant', content:'saved answer 0', _ts:11}},
    {{role:'tool', content:'{{"status":"success"}}', _ts:12}},
  ], _messages_offset:0, _messages_truncated:false, _msg_limit_max:500,
     tool_calls:[], message_count:3, visible_message_count:3, last_usage:{{}}}}}});
}}
(async () => {{
  S.messages = _mergeInflightTailMessages(savedRows.slice(), inflightTail);
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  assert.strictEqual(S.messages.filter(m => m.role === 'user').length, 1);
  await _ensureMessagesLoaded('s1', {{force:true}});
  assert.deepStrictEqual(S.messages.map(m => m.role), ['user','assistant','tool']);
  assert.strictEqual(S.messages.filter(m => m.role === 'user' && m.content === 'current prompt').length, 1);
  assert.strictEqual(S.messages[0]._displayOnlyRow, undefined,
    'the canonical reload must replace the display copy, not duplicate it');
  assert.strictEqual(_oldestIdx, 0);
  assert.strictEqual(_messageSessionIndexForRawIdx(0), 0);
  const keep = await forkAt(0);
  assert.strictEqual(keep, 1);
  console.log(JSON.stringify({{keep, len: S.messages.length}}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    )
    out = _run(script)
    assert '"keep":1' in out, out


def test_missing_pending_started_at_still_leads_the_turn():
    """A Date.now() fallback stamp must not become the turn start.

    `getPendingSessionMessage` stamps a freshly materialized pending row with
    `session.pending_started_at || Date.now()/1000`. When the session carries no
    `pending_started_at` (reload / recovery before the server re-stamps the
    turn), reading that fallback back as the turn start makes "now" the start:
    every loaded row then looks OLDER than the turn, so the recovered prompt
    was spliced after all of them — the reported "there is content above my
    first message" order, with the window's activity above the question.

    Only the server-issued `pending_started_at` proves ownership. Without it
    placement must fall back to structure: a window carrying no `user` row means
    the prompt IS the running turn's first row, so it leads the window.
    """
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 12;
{helper_src}

function getPendingSessionMessage(session, messages) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  // ui.js: the new row's `_ts` falls back to "now" when the session has no
  // pending_started_at.
  return {{role:'user', content:text, _ts:(session && session.pending_started_at) || Date.now()/1000, _pending:true}};
}}

const T0 = 1793245868;  // the turn really started here
const messages = Array.from({{length:30}}, (_, i) => ({{
  role: i % 2 ? 'assistant' : 'tool',
  content: 'current turn activity ' + i,
  _ts: T0 + 12 + i,
}}));
messages.push({{role:'assistant', _live:true, content:'working on it'}});

const changed = _mergePendingSessionMessage(
  {{pending_user_message:'brand new prompt'}},  // no pending_started_at
  messages
);
assert.strictEqual(changed, true);
const userIdx = messages.findIndex(m => m.role === 'user');
assert.strictEqual(userIdx, 0, 'prompt must lead the window instead of sinking below its own activity');
assert.strictEqual(messages[messages.length - 1]._live, true);
assert.strictEqual(messages.filter(m => m.role === 'user').length, 1);
"""
    _run(script)
