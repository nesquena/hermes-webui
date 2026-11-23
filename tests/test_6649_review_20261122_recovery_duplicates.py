"""#6649 maintainer review 2026-11-22 (pullrequestreview-6014682293) — the
two recovery duplicates the head still ships, plus their control matrix.

Blocker 1 — paginated recovery shows the current prompt twice
(``static/sessions.js``, windowed branch of ``_mergeInflightTailMessages``).
The guard there is still ``_hasCurrentTailUserDuplicate``, whose reverse
scan stops at the first settled assistant.  The new single-segment policy
keeps the saved assistant row of the current turn, so a paginated
``loadSession`` that keeps it places the prompt a second time.  Master
shows it once.  The neighbouring non-windowed branch already uses the
identity-aware ``_hasInflightTailUserDuplicate``.

Blocker 2 — multi-segment recovery with an assistant-only journal shows a
segment twice (``_prepareRunningLiveTail``, ``liveMessages.length > 1``).
When the in-flight record has no user row, ``_inflightTailTurnUser`` is
empty, so the branch returns false, the saved segments survive, and the
merge then appends the live segments as well — including a stale prefix of
a segment that is already saved.  A real-browser run kept both a
309-character saved segment and its older live prefix; master shows the
segment once.

Controls (the review's guardrails): a genuinely DIFFERENT adjacent turn
with the same words must never be swallowed, and without turn identity the
recovery must stay conservative — it keeps the saved content instead of
deduping it away.

These tests drive the real extracted source: the literal ``loadSession``
order (prepare -> conditional drop -> merge) for the unit shapes, and the
real ``loadSession`` + ``_ensureMessagesLoaded`` for the paginated shape,
exactly as ``tests/test_issue8701_stale_inflight_other_stream.py`` does.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _node_env() -> dict:
    """Strip Hermes's injected NODE_CHANNEL_FD before spawning node.

    The agent runtime sets NODE_CHANNEL_FD in this process's environment; an
    inherited fd makes the child node abort with SIGABRT (rc -6) as it shuts
    down its IPC channel.  Same guard as the other #6649 node harnesses.
    """
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
    return env


def _run_node(script: str) -> subprocess.CompletedProcess:
    # Feed the script over stdin, not argv: the prelude embeds a large slice
    # of static/sessions.js and Linux caps a single argv string at 128 KiB
    # (MAX_ARG_STRLEN).  `node -` reads the same script from stdin.
    return subprocess.run(
        [NODE, "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
        env=_node_env(),
        cwd=ROOT,
        timeout=120,
    )


def _function_source(src: str, name: str) -> str:
    marker = "function " + name + "("
    plain = src.find(marker)
    if plain == -1:
        return ""
    async_at = src.find("async " + marker)
    start = async_at if async_at != -1 and async_at < plain else plain
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unterminated function {name}")


def _const_source(src: str, name: str) -> str:
    marker = "const " + name + "="
    if marker not in src:
        return ""
    start = src.index(marker)
    return src[start : src.index(";", start) + 1]


def _helpers_slice() -> str:
    """Every sessions.js helper from _messageComparableText to the paging code."""
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1, "sessions.js helper slice markers not found"
    return SESSIONS_JS[start:end]


# The real ui.js pending-prompt producers: the witness resolvers
# _pendingCurrentTailUserMessage / _pendingActiveTurnUserMessage and the
# resolver getPendingSessionMessage, with everything they close over.
_UI_PRODUCER = (
    "_pendingCurrentTailUserMessage",
    "_messageTimestampSeconds",
    "_activeTurnTokenMatches",
    "_pendingActiveTurnUserMessage",
    "getPendingSessionMessage",
    "_isContextCompactionMessage",
    "_isContextCompactionText",
    "msgContent",
)

# Defined outside the helper slice on some trees.
_SESSIONS_OPTIONAL = (
    "_inflightHasVisibleLiveState",
    "_serverLiveSnapshotToolId",
    "_serverLiveSnapshotInflight",
    "_selectLiveRecoveryInflight",
    "_notifyRunJournalSideEffectRecoveryTruncation",
    "_renderRuntimeJournalAnchorActivityScene",
    "_projectInflightMessagesForActivityBursts",
    "_ensureInflightLiveAssistantMessage",
    "_prepareRunningLiveTail",
    "_dropCurrentTurnAssistantMessages",
    "_inflightTurnOwnerStart",
    "_inflightNamesOtherStream",
)

_MSG_CONTENT_FALLBACK = """
if (typeof msgContent !== 'function') {
  var msgContent = function (m) {
    if (!m) return '';
    if (Array.isArray(m.content)) {
      return m.content
        .filter(function (p) { return p && p.type === 'text'; })
        .map(function (p) { return String(p.text || ''); })
        .join('');
    }
    return m.content == null ? '' : String(m.content);
  };
}
"""


def _ui_producer_src() -> str:
    parts = [_const_source(UI_JS, "_PENDING_ACTIVE_TURN_TS_EPSILON")]
    parts += [_function_source(UI_JS, n) for n in _UI_PRODUCER]
    return "\n".join(p for p in parts if p)


def _load_session_stubs() -> str:
    # Same environment shape as test_issue8701_stale_inflight_other_stream.py:
    # the real loadSession needs these module-level names to exist.
    return """
const assert = require('assert');
var S = {session:{session_id:'s1'}, messages:[], busy:true};
let _oldestIdx = 0; let _loadingOlder = false; let reads = 0;
let _loadingSessionId = 's1'; let _loadSessionGeneration = 0;
let _pendingCarryForwardSnapshot = null;
let _sameSessionForceReloadHint = null; let _messagesTruncated = false;
let _msgLimitMax = 500;
const _MSG_LIMIT_MAX = 500;
var INFLIGHT = {}; var window = {};
var document = {getElementById:() => null};
var localStorage = {getItem:() => null, setItem:() => {}, removeItem:() => {}};
function $(){return null;}
function _messageReloadLimitForSession(){return 30;}
function _clearSameSessionForceReloadHint(){}
function clearLiveToolCards(){}
function _syncToolCallsForLoadedMessages(){}
function _isSessionActivelyViewedForList(){return true;}
function _setSessionViewedCount(){}
function _captureSameSessionForceReloadHint(){}
function _rearmActiveSessionStream(){}
function stopApprovalPolling(){}
function hideApprovalCard(){}
function _updateYoloPill(){}
function _resolveSessionModelForDisplaySoon(){}
function _acknowledgeSessionVisit(){}
function _setActiveSessionUrl(){}
function _isMessagingSession(){return false;}
function _hideHandoffHint(){}
function _deferWorkspaceRefreshForSession(){}
function syncTopbar(){}
function renderMessages(){}
function renderSessionList(){}
function ensureLiveWorklogShell(){}
function startApprovalPolling(){}
function setComposerStatus(){}
function setStatus(){}
function updateSendBtn(){}
function updateQueueBadge(){}
function setBusy(value){S.busy = value;}
function showToast(){}
"""


_LOAD_VIEW = (
    "const view=(rows)=>rows.map(m=>m?({role:m.role,content:String(m.content),"
    "ts:m._ts===undefined?null:m._ts,live:!!m._live,"
    "displayOnly:!!m._displayOnlyRow,"
    "owner:m._recoveredTurnStart===undefined?null:m._recoveredTurnStart}):null);"
)


def _load_prelude() -> str:
    parts = [
        _load_session_stubs(),
        _const_source(UI_JS, "_PENDING_ACTIVE_TURN_TS_EPSILON"),
    ]
    parts += [_function_source(UI_JS, n) for n in _UI_PRODUCER]
    parts.append(_helpers_slice())
    parts += [
        _function_source(SESSIONS_JS, n)
        for n in _SESSIONS_OPTIONAL
        if ("function " + n + "(") not in _helpers_slice()
    ]
    parts += [
        _function_source(SESSIONS_JS, "loadSession"),
        _function_source(SESSIONS_JS, "_ensureMessagesLoaded"),
    ]
    parts.append(_LOAD_VIEW)
    return "\n".join(p for p in parts if p)


def _chain_prelude() -> str:
    """Prelude for the literal loadSession order: prepare -> drop -> merge.

    The helper slice already carries _prepareRunningLiveTail and
    _mergeInflightTailMessages; the ui.js producers supply the real
    pending-prompt identity rules the recovery path composes with.  The
    epsilon constant rides inside _ui_producer_src() and must be evaluated
    BEFORE the sessions.js slice (a `typeof` probe on a lexically declared
    but not yet initialised binding throws ReferenceError/TDZ).
    """
    parts = [
        "const assert = require('assert');",
        "var S = {session:{session_id:'s1', active_stream_id:'stream-1'}};",
        "var INFLIGHT = {s1:{streamId:'stream-1'}};",
        "let _oldestIdx = 0;",
        _ui_producer_src(),
        _helpers_slice(),
        _MSG_CONTENT_FALLBACK,
    ]
    return "\n".join(p for p in parts if p)


# ── Blocker 1: paginated (windowed) recovery shows the prompt twice ────────

# The paginated first page is a TAIL window that starts inside the running
# turn: it carries the turn's user row AND the assistant row the server
# already saved for it.  The in-flight snapshot names the ACTIVE stream, so
# this is the proven-turn path; the saved row is kept (single-segment
# policy), and the windowed merge must dedup the prompt against it.
_PAGINATED_CURRENT_TURN = {
    "session": {
        "session_id": "s1",
        "active_stream_id": "stream-1",
        "pending_started_at": 100,
        "pending_user_message": "current prompt",
        "tool_calls": [],
        "last_usage": {},
        "message_count": 2,
    },
    "saved": [
        {"role": "user", "content": "current prompt", "timestamp": 100},
        {"role": "assistant", "content": "saved answer", "_ts": 101},
    ],
    "inflight": [
        {"role": "user", "content": "current prompt", "_ts": 99.9, "_pending": True},
        {"role": "assistant", "content": "live partial", "_live": True},
    ],
    "stream_id": "stream-1",
    "offset": 1,
}

# Control: a genuinely DIFFERENT adjacent turn repeating the same words.
# The saved window holds the PREVIOUS turn's stamped pair (msg-1 @1000); the
# running turn is a new stamped prompt (msg-2 @2000).  Identity separates
# them: both user rows must survive and the previous answer stays.
_PAGINATED_OLDER_IDENTICAL_TURN = {
    "session": {
        "session_id": "s1",
        "active_stream_id": "stream-1",
        "pending_started_at": 2000,
        "pending_user_message": "go",
        "tool_calls": [],
        "last_usage": {},
        "message_count": 2,
    },
    "saved": [
        {"role": "user", "content": "go", "timestamp": 1000, "id": "msg-1"},
        {"role": "assistant", "content": "answer", "_ts": 1001},
    ],
    "inflight": [
        {
            "role": "user",
            "content": "go",
            "_ts": 1999.9,
            "_pending": True,
            "id": "msg-2",
        },
        {"role": "assistant", "content": "answer live", "_live": True},
    ],
    "stream_id": "stream-1",
    "offset": 1,
}


def _run_load_session(shape: dict, prompt: str, passes: int = 3, fail_after: int = 99) -> dict:
    session = json.dumps(shape["session"])
    saved = json.dumps(shape["saved"])
    inflight = json.dumps(shape["inflight"])
    body = f"""
function api(url){{
  const session={session};
  if(url.includes('messages=1')){{
    reads++;
    if(reads>{int(fail_after)}) return Promise.reject(new Error('simulated message-fetch outage'));
    session.messages={saved};
    session._messages_offset={int(shape['offset'])};
    session._messages_truncated={json.dumps(shape['offset'] > 0)};
  }}
  return Promise.resolve({{session}});
}}
INFLIGHT.s1={{"messages":{inflight},"toolCalls":[],"streamId":{json.dumps(shape["stream_id"])}}};
(async()=>{{
  const counts=[];
  for(let pass=0;pass<{int(passes)};pass++){{
    await loadSession('s1',{{force:true}});
    assert.equal(_loadingSessionId,null);
    counts.push(S.messages.filter(m=>m&&m.role==='user'&&m.content==={json.dumps(prompt)}).length);
  }}
  process.stdout.write(JSON.stringify({{counts,rows:view(S.messages)}}));
}})().catch(e=>{{console.error(String(e&&e.stack||e));process.exit(1);}});
"""
    result = _run_node(_load_prelude() + "\n" + body)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_paginated_load_session_shows_the_current_prompt_once():
    """Blocker 1: a paginated loadSession that keeps the saved assistant row
    of the current turn must show the prompt once (master shows one)."""
    result = _run_load_session(_PAGINATED_CURRENT_TURN, "current prompt")
    assert result["counts"] == [1, 1, 1], (
        "the current prompt was placed twice per pass: "
        + json.dumps(result["counts"])
        + " rows=" + json.dumps(result["rows"])
    )
    users = [r for r in result["rows"] if r["role"] == "user"]
    assert len(users) == 1, result
    # The saved answer of the current turn stays the single assistant row.
    assistants = [r for r in result["rows"] if r["role"] == "assistant"]
    assert [a["content"] for a in assistants] == ["saved answer"], result


def test_paginated_load_session_keeps_an_adjacent_older_identical_turn():
    """Control: a genuinely different adjacent turn with the same words is
    never swallowed by the windowed dedup.  Both user rows survive and the
    previous turn's saved answer stays above the recovered prompt."""
    result = _run_load_session(_PAGINATED_OLDER_IDENTICAL_TURN, "go")
    assert result["counts"] == [2, 2, 2], (
        "an older identical turn was swallowed (or duplicated differently): "
        + json.dumps(result["counts"])
        + " rows=" + json.dumps(result["rows"])
    )
    assert result["rows"][0]["role"] == "user" and result["rows"][0]["content"] == "go", result
    assert any(
        r["role"] == "assistant" and r["content"] == "answer" for r in result["rows"]
    ), "the previous turn's saved answer must survive: " + json.dumps(result["rows"])


# ── Blocker 2: assistant-only multi-segment recovery repeats a segment ─────

# The real HTTP journal projection clears `messages` whenever
# last_assistant_text is present (pinned by
# test_inflight_assistant_only_recovery.py), so the recovered in-flight
# record is assistant-only; the activity-burst projection splits its single
# live accumulator into segments.  One of those segments is already saved in
# the loaded transcript, in a FULLER form (the live copy is a stale prefix).
_ASSISTANT_ONLY_TWO_SEGMENTS = """
let base = [
  {role:'user', content:'current prompt', timestamp:100},
  {role:'assistant', content:'First progress.', _ts:101},
];
const inflight = [
  {role:'assistant', _live:true, content:'First', _liveSegmentSeq:1, _activityBurstId:1},
  {role:'assistant', _live:true, content:'Second progress.', _liveSegmentSeq:2, _activityBurstId:2},
];
const session = {
  session_id:'s1', active_stream_id:'stream-1',
  pending_started_at:100, pending_user_message:'current prompt',
};
let prepared = _prepareRunningLiveTail(base, inflight, session);
assert.strictEqual(prepared, true,
  'assistant-only multi-segment recovery must authorize drop-and-replace once the loaded transcript proves the turn');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
const merged = _mergeInflightTailMessages(base, inflight);
process.stdout.write(JSON.stringify({prepared, rows:merged.map(m=>({role:m.role, content:String(m.content), live:!!m._live}))}));
"""


def test_assistant_only_multisegment_recovery_keeps_each_segment_once():
    """Blocker 2: each segment exactly once — the fuller saved copy replaces
    the stale live prefix, the additional live segment is kept."""
    result = _run_node(_chain_prelude() + "\n" + _ASSISTANT_ONLY_TWO_SEGMENTS)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    rows = payload["rows"]
    users = [r for r in rows if r["role"] == "user"]
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert len(users) == 1, rows
    assert [a["content"] for a in assistants] == ["First progress.", "Second progress."], (
        "each segment must appear exactly once, keeping the fuller saved copy: "
        + json.dumps(rows)
    )


_ASSISTANT_ONLY_PREVIOUS_TURN = """
let base = [
  {role:'user', content:'old question', timestamp:50},
  {role:'assistant', content:'First progress.', _ts:51},
];
const inflight = [
  {role:'assistant', _live:true, content:'First', _liveSegmentSeq:1, _activityBurstId:1},
  {role:'assistant', _live:true, content:'Second progress.', _liveSegmentSeq:2, _activityBurstId:2},
];
const session = {
  session_id:'s1', active_stream_id:'stream-1',
  pending_started_at:100, pending_user_message:'current prompt',
};
let prepared = _prepareRunningLiveTail(base, inflight, session);
assert.strictEqual(prepared, false,
  'no current-turn identity in the loaded transcript: stay conservative, keep the saved content');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
const merged = _mergeInflightTailMessages(base, inflight);
process.stdout.write(JSON.stringify({prepared, rows:merged.map(m=>({role:m.role, content:String(m.content), live:!!m._live}))}));
"""


def test_assistant_only_multisegment_previous_turn_is_not_swallowed():
    """Control: the saved tail belongs to a PREVIOUS turn (no identity).
    The recovery stays conservative: the saved answer is kept (never
    dropped), and the live segments are still appended."""
    result = _run_node(_chain_prelude() + "\n" + _ASSISTANT_ONLY_PREVIOUS_TURN)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    rows = payload["rows"]
    assert payload["prepared"] is False, rows
    assert any(
        r["role"] == "assistant" and r["content"] == "First progress." and not r["live"]
        for r in rows
    ), "the previous turn's saved answer must survive: " + json.dumps(rows)
    live = [r for r in rows if r["live"]]
    assert [r["content"] for r in live] == ["First", "Second progress."], rows


# ── Blocker 2 matrix: fuller copy, extension, tool splits, repeats ─────────

_ASSISTANT_ONLY_LIVE_EXTENSION = """
let base = [
  {role:'user', content:'current prompt', timestamp:100},
  {role:'assistant', content:'First progress.', _ts:101},
];
const inflight = [
  {role:'assistant', _live:true, content:'First progress.\\n\\nand more text', _liveSegmentSeq:1, _activityBurstId:1},
  {role:'assistant', _live:true, content:'Second progress.', _liveSegmentSeq:2, _activityBurstId:2},
];
const session = {
  session_id:'s1', active_stream_id:'stream-1',
  pending_started_at:100, pending_user_message:'current prompt',
};
let prepared = _prepareRunningLiveTail(base, inflight, session);
assert.strictEqual(prepared, true, 'a live strict extension is progress, not staleness');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
const merged = _mergeInflightTailMessages(base, inflight);
process.stdout.write(JSON.stringify({rows:merged.map(m=>({role:m.role, content:String(m.content), live:!!m._live}))}));
"""


def test_assistant_only_multisegment_keeps_the_fuller_live_extension():
    """When the live segment is a strict EXTENSION of the saved copy, the
    live text is progress and must not be rolled back to the saved prefix."""
    result = _run_node(_chain_prelude() + "\n" + _ASSISTANT_ONLY_LIVE_EXTENSION)
    assert result.returncode == 0, result.stderr or result.stdout
    rows = json.loads(result.stdout.strip().splitlines()[-1])["rows"]
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert [a["content"] for a in assistants] == [
        "First progress.\n\nand more text",
        "Second progress.",
    ], rows


_ASSISTANT_ONLY_TOOL_SPLIT = """
let base = [
  {role:'user', content:'current prompt', timestamp:100},
  {role:'assistant', content:'First progress.', _ts:101},
  {role:'tool', content:'{"status":"success"}', _ts:102},
  {role:'assistant', content:'Second progress.', _ts:103},
];
const inflight = [
  {role:'assistant', _live:true, content:'First', _liveSegmentSeq:1, _activityBurstId:1},
  {role:'assistant', _live:true, content:'Second progress.', _liveSegmentSeq:2, _activityBurstId:2},
];
const session = {
  session_id:'s1', active_stream_id:'stream-1',
  pending_started_at:100, pending_user_message:'current prompt',
};
let prepared = _prepareRunningLiveTail(base, inflight, session);
assert.strictEqual(prepared, true, 'tool-split saved rows reconcile segment by segment');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
const merged = _mergeInflightTailMessages(base, inflight);
process.stdout.write(JSON.stringify({rows:merged.map(m=>({role:m.role, content:String(m.content), live:!!m._live}))}));
"""


def test_assistant_only_multisegment_reconciles_tool_split_saved_rows():
    """Saved rows split at a tool call reconcile one segment at a time; the
    tool row itself is not an assistant row and survives the drop."""
    result = _run_node(_chain_prelude() + "\n" + _ASSISTANT_ONLY_TOOL_SPLIT)
    assert result.returncode == 0, result.stderr or result.stdout
    rows = json.loads(result.stdout.strip().splitlines()[-1])["rows"]
    roles = [r["role"] for r in rows]
    assert roles == ["user", "tool", "assistant", "assistant"], rows
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert [a["content"] for a in assistants] == ["First progress.", "Second progress."], rows


_ASSISTANT_ONLY_IDENTICAL = """
let base = [
  {role:'user', content:'current prompt', timestamp:100},
  {role:'assistant', content:'First progress.', _ts:101},
];
const inflight = [
  {role:'assistant', _live:true, content:'First progress.', _liveSegmentSeq:1, _activityBurstId:1},
  {role:'assistant', _live:true, content:'Second progress.', _liveSegmentSeq:2, _activityBurstId:2},
];
const session = {
  session_id:'s1', active_stream_id:'stream-1',
  pending_started_at:100, pending_user_message:'current prompt',
};
let prepared = _prepareRunningLiveTail(base, inflight, session);
assert.strictEqual(prepared, true, 'an identical live segment replaces the saved copy');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
const merged = _mergeInflightTailMessages(base, inflight);
process.stdout.write(JSON.stringify({rows:merged.map(m=>({role:m.role, content:String(m.content), live:!!m._live}))}));
"""


def test_assistant_only_multisegment_identical_segment_shows_once():
    """A live segment with the SAME text as the saved row is a true duplicate:
    it must render once, not twice."""
    result = _run_node(_chain_prelude() + "\n" + _ASSISTANT_ONLY_IDENTICAL)
    assert result.returncode == 0, result.stderr or result.stdout
    rows = json.loads(result.stdout.strip().splitlines()[-1])["rows"]
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert [a["content"] for a in assistants] == ["First progress.", "Second progress."], rows


_ASSISTANT_ONLY_SAVED_EXTRA = """
let base = [
  {role:'user', content:'current prompt', timestamp:100},
  {role:'assistant', content:'First progress.', _ts:101},
  {role:'assistant', content:'Middle saved segment.', _ts:102},
];
const inflight = [
  {role:'assistant', _live:true, content:'First', _liveSegmentSeq:1, _activityBurstId:1},
  {role:'assistant', _live:true, content:'Third live segment.', _liveSegmentSeq:2, _activityBurstId:2},
];
const session = {
  session_id:'s1', active_stream_id:'stream-1',
  pending_started_at:100, pending_user_message:'current prompt',
};
let prepared = _prepareRunningLiveTail(base, inflight, session);
assert.strictEqual(prepared, true, 'a saved-only segment is additional content and must survive');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
const merged = _mergeInflightTailMessages(base, inflight);
process.stdout.write(JSON.stringify({rows:merged.map(m=>({role:m.role, content:String(m.content), live:!!m._live}))}));
"""


def test_assistant_only_multisegment_keeps_saved_only_segment():
    """A saved segment with no live counterpart is additional content: keep
    it (once) alongside the reconciled and new live segments."""
    result = _run_node(_chain_prelude() + "\n" + _ASSISTANT_ONLY_SAVED_EXTRA)
    assert result.returncode == 0, result.stderr or result.stdout
    rows = json.loads(result.stdout.strip().splitlines()[-1])["rows"]
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert [a["content"] for a in assistants] == [
        "First progress.", "Middle saved segment.", "Third live segment.",
    ], rows


# ── Rare shape (review smaller point): recorded, not hidden ────────────────

# Two or more saved text rows for the current turn plus a single live row
# that runs them together WITHOUT blank lines (the server records split
# points at every tool call, so this needs an old cached in-flight record).
# Neither the prefix/extension branches nor the compact-equality branch
# match ('A.\\n\\nB.' vs 'A.B.'), so the live row is reconciled to the
# persisted text and flagged superseded: the saved rows stay visible and the
# newest live text is hidden until the turn finishes.  This test PINS that
# recorded behaviour — it is a known limitation, not a regression, and the
# code carries a note at the reconciliation branch.
_RARE_CONCATENATED_LIVE = """
let base = [
  {role:'user', content:'current prompt', timestamp:100},
  {role:'assistant', content:'A.', _ts:101},
  {role:'tool', content:'{"status":"success"}', _ts:102},
  {role:'assistant', content:'B.', _ts:103},
];
const inflight = [
  {role:'user', content:'current prompt', _ts:100, _pending:true},
  {role:'assistant', _live:true, content:'A.B.'},
];
const session = {
  session_id:'s1', active_stream_id:'stream-1',
  pending_started_at:100, pending_user_message:'current prompt',
};
const prepared = _prepareRunningLiveTail(base, inflight, session);
let merged;
if(prepared){ merged = _mergeInflightTailMessages(_dropCurrentTurnAssistantMessages(base), inflight); }
else { merged = _mergeInflightTailMessages(base, inflight); }
process.stdout.write(JSON.stringify({prepared, rows:merged.map(m=>({role:m.role, content:String(m.content), live:!!m._live}))}));
"""


def test_rare_concatenated_live_prefix_shape_is_recorded():
    """Rare shape from the review: two saved rows plus one live row running
    them together without blank lines.

    Recorded current behaviour (pinned so a future change is deliberate):
    the live row reconciles to the persisted text and is flagged superseded,
    the saved rows remain the visible copy, and the un-separated live text
    stays hidden until the turn finishes.  The newest live text is NOT lost
    from the transcript — it is only not shown as a second row.
    """
    result = _run_node(_chain_prelude() + "\n" + _RARE_CONCATENATED_LIVE)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    rows = payload["rows"]
    assert payload["prepared"] is False, rows
    # The saved rows are the visible copy; the concatenated live row is not
    # appended as a second assistant row.
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert [a["content"] for a in assistants] == ["A.", "B."], rows
    assert all(not a["live"] for a in assistants), rows
