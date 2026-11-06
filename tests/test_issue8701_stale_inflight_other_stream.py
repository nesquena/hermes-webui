"""#8701 item 1: a stale in-memory INFLIGHT from ANOTHER stream must not show the running turn's prompt twice.

Shape (the round-4 matrix scenario `r4-inflight-other-stream-activity-only`): the saved transcript holds only the
running turn's activity, the server names `stream-1` as active with `pending_user_message` and
`pending_started_at`, there is no journal snapshot, and the tab still holds an INFLIGHT entry for `stream-0` (it
missed `done` and survived the sidebar purge and the idle delete). `_inflightTurnOwnerStart` returns 0 for that
snapshot on purpose (#8697): its turn is not proven. The tail merge then placed the stale prompt unstamped and the
pending merge added the server's copy, so `.575` through master showed two prompts where v0.52.544 shows one.

`loadSession()` now tells the tail merge when the snapshot names another stream, and the merge does not place
that snapshot's prompt; the pending merge supplies the running turn's prompt. Proven turns (same stream) and
snapshots without a stream id are unchanged. These tests drive the real `loadSession`, `_ensureMessagesLoaded`
and merge helpers from static/sessions.js and the real pending-prompt producer from static/ui.js.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

PROMPT = "pending wording"
_ABSENT = object()

_UI_PRODUCER = (
    "_pendingCurrentTailUserMessage",
    "_messageTimestampSeconds",
    "_activeTurnTokenMatches",
    "_pendingActiveTurnUserMessage",
    "getPendingSessionMessage",
    "_isContextCompactionMessage",
    "_isContextCompactionText",
    "msgContent",
    "_displayOnlyIndexState",
    "_markDisplayOnlyRow",
    "_displayOnlyRawIndices",
    "_displayOnlyCountBefore",
    "_displayOnlyRowAt",
    "_messageSessionIndexForRawIdx",
    "_messageSessionIndexBase",
)
# Defined outside the `_messageComparableText` .. `// Load older messages` slice on some trees.
_SESSIONS_OPTIONAL = (
    "_inflightHasVisibleLiveState",
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

_STUBS = """
const assert=require('assert');
var S={session:{session_id:'s1'},messages:[],busy:true};
let _oldestIdx=0; let _loadingOlder=false; let _yoloEnabled=false; let reads=0;
let _loadingSessionId='s1'; let _loadSessionGeneration=0; let _pendingCarryForwardSnapshot=null;
let _sameSessionForceReloadHint=null; let _messagesTruncated=false; let _msgLimitMax=500;
const _MSG_LIMIT_MAX=500;
var INFLIGHT={}; var window={};
var document={getElementById:()=>null};
var localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
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
function setBusy(value){S.busy=value;}
function showToast(){}
"""


def _function_source(src: str, name: str) -> str:
    # A helper missing from an older tree yields nothing, so a run against that tree fails on behaviour (or a
    # ReferenceError naming the helper) rather than at extraction.
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


def _prelude() -> str:
    start = SESSIONS_JS.index("function _messageComparableText")
    helpers = SESSIONS_JS[start : SESSIONS_JS.index("// Load older messages", start)]
    parts = [_STUBS, _const_source(UI_JS, "_PENDING_ACTIVE_TURN_TS_EPSILON")]
    parts += [_function_source(UI_JS, n) for n in _UI_PRODUCER]
    if "function _messageSessionIndexBase(" not in UI_JS:
        parts.append("function _messageSessionIndexBase(){return _oldestIdx;}")
    parts.append(helpers)
    parts += [_function_source(SESSIONS_JS, n) for n in _SESSIONS_OPTIONAL if ("function " + n + "(") not in helpers]
    parts += [_function_source(SESSIONS_JS, "loadSession"), _function_source(SESSIONS_JS, "_ensureMessagesLoaded")]
    parts.append(
        "const view=(rows)=>rows.map(m=>m?({role:m.role,content:String(m.content),ts:m._ts===undefined?null:m._ts,"
        "live:!!m._live,displayOnly:!!m._displayOnlyRow,"
        "owner:m._recoveredTurnStart===undefined?null:m._recoveredTurnStart}):null);"
    )
    return "\n".join(parts)


def _node(script: str) -> dict:
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False, cwd=ROOT, timeout=60)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


def _load_passes(
    saved: list,
    inflight_messages: list,
    stream_id,
    passes: int = 3,
    fail_after: int = 1,
    offset: int = 0,
    message_count: int = 2,
    pending=PROMPT,
) -> dict:
    """Run `loadSession('s1',{force:true})` `passes` times over a running `stream-1` turn with no journal snapshot.

    `stream_id` is the INFLIGHT snapshot's own stream (None: the snapshot names none). The message fetch fails after
    `fail_after` reads, so later passes keep and re-merge the cached transcript, as `loadSession()` does in the field.
    `pending` is the server's `pending_user_message` (`_ABSENT`: the field is not sent).
    """
    session = {
        "session_id": "s1",
        "active_stream_id": "stream-1",
        "pending_started_at": 100,
        "pending_user_message": pending,
        "tool_calls": [],
        "last_usage": {},
        "message_count": message_count,
    }
    if pending is _ABSENT:
        del session["pending_user_message"]
    snapshot = {"messages": inflight_messages, "toolCalls": []}
    if stream_id is not None:
        snapshot["streamId"] = stream_id
    body = f"""
function api(url){{
  const session={json.dumps(session)};
  if(url.includes('messages=1')){{
    reads++;
    if(reads>{int(fail_after)}) return Promise.reject(new Error('simulated message-fetch outage'));
    session.messages={json.dumps(saved)};
    session._messages_offset={int(offset)};
    session._messages_truncated={json.dumps(offset > 0)};
  }}
  return Promise.resolve({{session}});
}}
INFLIGHT.s1={json.dumps(snapshot)};
const staleUser=INFLIGHT.s1.messages.find(m=>m&&m.role==='user');
(async()=>{{
  const counts=[];
  for(let pass=0;pass<{int(passes)};pass++){{
    await loadSession('s1',{{force:true}});
    assert.equal(_loadingSessionId,null);
    counts.push(S.messages.filter(m=>m&&m.role==='user'&&m.content==={json.dumps(PROMPT)}).length);
  }}
  process.stdout.write(JSON.stringify({{counts,rows:view(S.messages),
    inflightRowShown:!!staleUser&&S.messages.includes(staleUser)}}));
}})().catch(e=>{{console.error(String(e&&e.stack||e));process.exit(1);}});
"""
    return _node(_prelude() + body)


def _u(content, ts, **extra):
    return {"role": "user", "content": content, "_ts": ts, **extra}


_ACTIVITY = [
    {"role": "assistant", "content": "Saved activity before reload", "_ts": 101},
    {"role": "tool", "content": "tool output", "_ts": 102},
]
_LIVE = {"role": "assistant", "content": "partial", "_live": True}


def _shape(rows):
    return [(r["role"], r["content"], r["displayOnly"], r["owner"]) for r in rows]


def test_stale_inflight_from_another_stream_shows_the_prompt_once():
    """Item 1: one prompt per pass (544 shows one; .575 through master showed two). The single prompt is the
    running turn's, stamped for its server start and placed in front of the turn's saved activity; the stale
    snapshot's own row is not shown."""
    result = _load_passes(_ACTIVITY, [_u(PROMPT, 99.9, _pending=True), _LIVE], "stream-0")
    assert result["counts"] == [1, 1, 1], result
    assert result["inflightRowShown"] is False, result
    assert _shape(result["rows"]) == [
        ("user", PROMPT, True, 100),
        ("tool", "tool output", False, None),
        ("assistant", "partial", False, None),
    ], result
    assert result["rows"][0]["ts"] == 100, "the server's pending prompt, not the stale snapshot's client-clock row"


def test_stale_inflight_from_another_stream_does_not_show_its_older_prompt():
    """The realistic stale entry carries an EARLIER turn's words: only the running turn's prompt is shown."""
    result = _load_passes(_ACTIVITY, [_u("an older question", 50, _pending=True), _LIVE], "stream-0")
    assert result["counts"] == [1, 1, 1], result
    users = [r for r in result["rows"] if r["role"] == "user"]
    assert [(u["content"], u["owner"]) for u in users] == [(PROMPT, 100)], result


@pytest.mark.parametrize("pending", [_ABSENT, None, "", "   "], ids=["absent", "null", "empty", "blank"])
def test_stale_inflight_from_another_stream_without_a_server_prompt_still_shows_it_once(pending):
    """When the server names another stream but supplies no `pending_user_message`, the pending merge adds nothing,
    so skipping the snapshot's prompt would leave the running turn with no prompt at all. The snapshot's row stays,
    as on master and v0.52.544: exactly one copy per pass (display-only, at its client `_ts`)."""
    result = _load_passes(_ACTIVITY, [_u(PROMPT, 99.9, _pending=True), _LIVE], "stream-0", pending=pending)
    assert result["counts"] == [1, 1, 1], result
    assert result["inflightRowShown"] is True, result
    first = _load_passes(_ACTIVITY, [_u(PROMPT, 99.9, _pending=True), _LIVE], "stream-0", passes=1, pending=pending)
    users = [r for r in first["rows"] if r["role"] == "user"]
    assert [(u["content"], u["ts"], u["displayOnly"]) for u in users] == [(PROMPT, 99.9, True)], first


def test_proven_turn_still_shows_one_prompt_at_the_turn_start():
    """#8697 behaviour for a proven turn (the snapshot names the active stream) is unchanged: one copy, the
    snapshot's own row (keeps its client `_ts`), stamped for the server start, in front of the activity."""
    first = _load_passes(_ACTIVITY, [_u(PROMPT, 99.9, _pending=True), _LIVE], "stream-1", passes=1)
    assert first["counts"] == [1], first
    assert first["inflightRowShown"] is True, first
    assert _shape(first["rows"]) == [
        ("user", PROMPT, True, 100),
        ("assistant", "Saved activity before reload", False, None),
        ("tool", "tool output", False, None),
        ("assistant", "partial", False, None),
    ], first
    assert first["rows"][0]["ts"] == 99.9, first
    result = _load_passes(_ACTIVITY, [_u(PROMPT, 99.9, _pending=True), _LIVE], "stream-1")
    assert result["counts"] == [1, 1, 1], result
    assert result["inflightRowShown"] is True, result
    rows = result["rows"]
    assert rows[0]["role"] == "user" and rows[0]["content"] == PROMPT, result
    assert rows[0]["displayOnly"] is True and rows[0]["owner"] == 100 and rows[0]["ts"] == 99.9, result
    assert rows[-1]["live"] is True, result


def test_snapshot_without_a_stream_id_is_unchanged():
    """Out of scope (#8701 item 3): a snapshot naming no stream keeps the #8697 placement and stamp."""
    result = _load_passes(_ACTIVITY, [_u(PROMPT, 99.9, _pending=True), _LIVE], None)
    assert result["counts"] == [1, 1, 1], result
    assert result["inflightRowShown"] is True, result
    assert result["rows"][0]["owner"] == 100 and result["rows"][0]["ts"] == 99.9, result


def test_windowed_stale_inflight_from_another_stream_shows_the_prompt_once():
    """The windowed variant already showed one copy; it still does, in the same place (after the previous turn's
    question, in front of the current turn's activity). The copy is now the server's pending prompt, not the stale
    snapshot's row."""
    saved = [
        _u("previous question", 90),
        {"role": "assistant", "content": "previous answer", "_ts": 99},
        {"role": "assistant", "content": "current activity", "_ts": 101},
        {"role": "tool", "content": "tool output", "_ts": 102},
    ]
    first = _load_passes(
        saved, [_u(PROMPT, 98, _pending=True), _LIVE], "stream-0", passes=1, offset=31, message_count=35
    )
    assert [(r["role"], r["content"], r["displayOnly"]) for r in first["rows"]] == [
        ("user", "previous question", False),
        ("user", PROMPT, True),
        ("tool", "tool output", False),
        ("assistant", "partial", False),
    ], first
    result = _load_passes(
        saved, [_u(PROMPT, 98, _pending=True), _LIVE], "stream-0", offset=31, message_count=35
    )
    assert result["counts"] == [1, 1, 1], result
    assert result["inflightRowShown"] is False, result


_NEW_PROMPT = "JUST SENT"


def _send_during_metadata(variant: str) -> dict:
    """Run one `loadSession('s1',{force:true})` whose metadata response is delayed while this tab sends a prompt.

    The metadata (read before the send) names `stream-1` with its pending prompt. While it is in transit `stream-1`
    ends and a send establishes `stream-2`: the server, `S.session`, `S.activeStreamId` and INFLIGHT now describe the
    new turn. `variant` is how INFLIGHT changes during the wait:

    - `absent-replaced`: no entry at start; the send creates one on `stream-2` (Codex round-1 probe);
    - `replaced`: a stale `stream-0` entry at start; the send replaces it with a new object on `stream-2`;
    - `replaced-same-stream-id`: as `replaced`, but the new object names the start entry's stream id;
    - `stream-reassigned`: the start entry itself is re-pointed to `stream-2` and given the new turn's rows.
    """
    body = f"""
const NEW={json.dumps(_NEW_PROMPT)};
const VARIANT={json.dumps(variant)};
const metadataSession={{session_id:'s1',active_stream_id:'stream-1',pending_started_at:100,
  pending_user_message:'running prompt',tool_calls:[],last_usage:{{}},message_count:2}};
const sentSession={{...metadataSession,active_stream_id:'stream-2',pending_started_at:200,
  pending_user_message:NEW,messages:[{{role:'user',content:'history',_ts:10}},
    {{role:'assistant',content:'historical answer',_ts:11}}]}};
let deliverMetadata=null;
function api(url){{
  if(url.includes('messages=0')) return new Promise(resolve=>{{
    deliverMetadata=()=>resolve({{session:JSON.parse(JSON.stringify(metadataSession))}});
  }});
  if(url.includes('messages=1')) return Promise.resolve({{session:JSON.parse(JSON.stringify(sentSession))}});
  throw new Error('unexpected '+url);
}}
S.session=JSON.parse(JSON.stringify(metadataSession));
S.activeStreamId='stream-1';
if(VARIANT!=='absent-replaced'){{
  INFLIGHT.s1={{streamId:'stream-0',toolCalls:[],messages:[{{role:'user',content:'stale prompt',_ts:50,_pending:true}},
    {{role:'assistant',content:'stale partial',_live:true}}]}};
}}
const atStart=INFLIGHT.s1;
const users=(rows)=>rows.filter(m=>m&&m.role==='user'&&m.content===NEW).length;
(async()=>{{
  const loading=loadSession('s1',{{force:true}});
  assert.equal(typeof deliverMetadata,'function');
  // sendMessage/attachLiveStream publish the new turn after /api/chat/start returns stream-2.
  S.session={{...sentSession}};
  S.messages=[{{role:'user',content:NEW,_ts:199.9,_pending:true}},
    {{role:'assistant',content:'new partial answer',_live:true}}];
  S.busy=true;
  S.activeStreamId='stream-2';
  if(VARIANT==='stream-reassigned'){{
    INFLIGHT.s1.streamId='stream-2';
    INFLIGHT.s1.messages=S.messages.slice();
  }}else{{
    INFLIGHT.s1={{streamId:VARIANT==='replaced-same-stream-id'?'stream-0':'stream-2',
      messages:S.messages.slice(),toolCalls:[]}};
  }}
  const sent=INFLIGHT.s1;
  assert.equal(sent===atStart,VARIANT==='stream-reassigned');
  deliverMetadata();
  await loading;
  process.stdout.write(JSON.stringify({{shown:users(S.messages),cached:users(INFLIGHT.s1.messages),
    sameEntry:INFLIGHT.s1===sent,rows:view(S.messages),cachedRows:view(INFLIGHT.s1.messages)}}));
}})().catch(e=>{{console.error(String(e&&e.stack||e));process.exit(1);}});
"""
    return _node(_prelude() + body)


@pytest.mark.parametrize(
    "variant", ["absent-replaced", "replaced", "replaced-same-stream-id", "stream-reassigned"]
)
def test_prompt_sent_while_metadata_is_in_flight_is_kept(variant):
    """Codex round 1 (CORE): the metadata names `stream-1`, and a send establishes `stream-2` before it arrives.
    The INFLIGHT entry then holds the NEW turn, not a stale one, so its prompt must not be suppressed: it is shown
    exactly once and stays in INFLIGHT's rows (the pending-merge write-back must not drop it from the cache). The
    other-stream suppression applies only to the entry `loadSession()` started with, still on the same stream."""
    result = _send_during_metadata(variant)
    assert result["sameEntry"] is True, result
    assert result["shown"] == 1, result
    assert result["cached"] == 1, result


def test_tail_merge_skips_only_the_prompt_of_another_streams_snapshot():
    """Unit level: with the other-stream flag the merge keeps every non-prompt tail row and does not place the
    prompt; without it (omitted, false) the merge is unchanged."""
    script = (
        _prelude()
        + """
const base=()=>[{role:'assistant',content:'Saved activity before reload',_ts:101},
  {role:'tool',content:'tool output',_ts:102}];
const tail=()=>[{role:'user',content:'pending wording',_ts:99.9,_pending:true},
  {role:'assistant',content:'partial',_live:true}];
process.stdout.write(JSON.stringify({
  other:view(_mergeInflightTailMessages(base(),tail(),0,true)),
  omitted:view(_mergeInflightTailMessages(base(),tail(),0)),
  off:view(_mergeInflightTailMessages(base(),tail(),0,false)),
}));
"""
    )
    result = _node(script)
    assert [(r["role"], r["content"]) for r in result["other"]] == [
        ("assistant", "Saved activity before reload"),
        ("tool", "tool output"),
        ("assistant", "partial"),
    ], result
    assert result["omitted"] == result["off"], result
    assert [(r["role"], r["displayOnly"]) for r in result["off"]][0] == ("user", True), result
    # Only the prompt (the tail's last user row) is skipped: every other tail row takes the unchanged path.
    script = (
        _prelude()
        + """
const base=()=>[{role:'assistant',content:'Saved activity before reload',_ts:101},
  {role:'tool',content:'tool output',_ts:102}];
const tail=()=>[{role:'user',content:'first ask',_ts:99.9,_pending:true},
  {role:'assistant',content:'partial',_live:true},{role:'user',content:'follow-up',_ts:103}];
process.stdout.write(JSON.stringify({
  other:view(_mergeInflightTailMessages(base(),tail(),0,true)),
  off:view(_mergeInflightTailMessages(base(),tail(),0,false)),
}));
"""
    )
    result = _node(script)
    assert [r for r in result["off"] if r["content"] != "follow-up"] == result["other"], result
    assert any(r["content"] == "first ask" for r in result["other"]), result


def test_inflight_names_other_stream_truth_table():
    script = (
        _prelude()
        + """
process.stdout.write(JSON.stringify([
  _inflightNamesOtherStream({streamId:'stream-0'},'stream-1'),
  _inflightNamesOtherStream({streamId:' stream-0 '},' stream-1'),
  _inflightNamesOtherStream({streamId:'stream-1'},'stream-1'),
  _inflightNamesOtherStream({streamId:' stream-1 '},'stream-1'),
  _inflightNamesOtherStream({},'stream-1'),
  _inflightNamesOtherStream({streamId:null},'stream-1'),
  _inflightNamesOtherStream({streamId:'stream-0'},''),
  _inflightNamesOtherStream({streamId:'stream-0'},null),
  _inflightNamesOtherStream(null,'stream-1'),
]));
"""
    )
    assert _node(script) == [True, True, False, False, False, False, False, False, False]
