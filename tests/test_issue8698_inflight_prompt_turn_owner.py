"""#8698: a running turn's prompt recovered from INFLIGHT must belong to the server's turn, not the client clock.

`exp-v0.52.575` (#8543) made `_mergeInflightTailMessages` place the recovered prompt by turn ownership. Two load
shapes went wrong, where v0.52.544 and .574 handle both:

1. Full load whose saved transcript holds only the running turn's activity. The client prompt `_ts` trails the
   server's `pending_started_at` by ordinary latency (99.9 vs 100). The display-only copy carried no record of its
   turn, so the pending merge could not recognise it and added a second prompt.
2. A client clock behind the server: placement used the prompt's client `_ts` first, so a previous turn's answer
   stamped between the two times was treated as current-turn activity and ended up below the prompt.

`loadSession()` now passes `_inflightTurnOwnerStart(...)` (the server turn start, proven by the active stream) so the
merge places by the server start and stamps the copy for reuse. These tests drive the real merge, ownership and reuse
helpers from static/sessions.js and the real pending-prompt producer from static/ui.js.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_SESSIONS_HELPERS = (
    "_messageComparableText",
    "_stripAttachedFilesMarker",
    "_stripForcedSkillEnvelope",
    "_normalizeUserTranscriptText",
    "_sameTranscriptMessage",
    "_currentTailUserMessage",
    "_hasCurrentTailUserDuplicate",
    "_messageTimestampValue",
    "_messageTurnOwnership",
    "_isCompactionMarkerRow",
    "_currentTurnInsertIndex",
    "_recoveredPromptTargetIndex",
    "_markRecoveredPromptRow",
    "_existingRecoveredPromptIndex",
    "_recoveredPromptIdentityMatch",
    "_isRecoveredPromptTwin",
    "_recoveredPromptCopyForTurn",
    "_mergePendingSessionMessage",
    "_inflightTurnOwnerStart",
    "_mergeInflightTailMessages",
)
_UI_PRODUCER = (
    "_pendingCurrentTailUserMessage",
    "_messageTimestampSeconds",
    "_activeTurnTokenMatches",
    "_pendingActiveTurnUserMessage",
    "getPendingSessionMessage",
)


def _function_source(src: str, name: str) -> str:
    # A helper missing from an older static/sessions.js yields nothing, so a run against that tree fails on
    # behaviour (or a ReferenceError naming the helper) rather than at extraction.
    marker = "function " + name + "("
    if marker not in src:
        return ""
    start = src.index(marker)
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


def _run(body: str, oldest_idx: int = 0) -> dict:
    script = "\n".join(
        [
            "var S={session:{session_id:'s1'},messages:[]};",
            f"let _oldestIdx={int(oldest_idx)};",
            _const_source(UI_JS, "_PENDING_ACTIVE_TURN_TS_EPSILON"),
            _const_source(SESSIONS_JS, "_RECOVERED_PROMPT_TS_EPSILON"),
            *(_function_source(UI_JS, n) for n in _UI_PRODUCER),
            *(_function_source(SESSIONS_JS, n) for n in _SESSIONS_HELPERS),
            "const view=(rows)=>rows.map(m=>({role:m.role,content:m.content,ts:m._ts,live:!!m._live,"
            "displayOnly:!!m._displayOnlyRow,owner:m._recoveredTurnStart===undefined?null:m._recoveredTurnStart}));",
            body,
        ]
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False, cwd=ROOT)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


_RUNNING_TURN = """
const session={session_id:'s1',active_stream_id:'stream-1',pending_started_at:100,
  pending_user_message:PROMPT};
S.session=session;
const inflight=[{role:'user',content:PROMPT,_ts:CLIENT_TS,_pending:true},
  {role:'assistant',content:'working',_live:true}];
"""


def _running_turn(prompt: str, client_ts: float) -> str:
    return _RUNNING_TURN.replace("PROMPT", json.dumps(prompt)).replace("CLIENT_TS", repr(client_ts))


def test_activity_only_full_load_shows_the_prompt_once_above_the_activity():
    """Codex .574..575 audit finding 1, the browser `activity-only` shape: one prompt per pass, where .575-.605 give
    two (a display-only copy in front of the activity plus the pending merge's own copy). Later passes re-merge over
    the same array, as `loadSession()` does when `_ensureMessagesLoaded()` keeps the cached transcript: the copy, with
    activity after it, must not be appended again."""
    result = _run(
        _running_turn("ordinary prompt", 99.9)
        + """
let messages=[{role:'assistant',content:'Saved activity before reload',_ts:101},
  {role:'tool',content:'tool output',_ts:102}];
const counts=[];
for(let pass=0;pass<3;pass++){
  messages=_mergeInflightTailMessages(messages,inflight,100);
  _mergePendingSessionMessage(session,messages);
  counts.push(messages.filter(m=>m.role==='user'&&m.content==='ordinary prompt').length);
}
process.stdout.write(JSON.stringify({counts,rows:view(messages)}));
"""
    )
    assert result["counts"] == [1, 1, 1], result
    assert [(r["role"], r["content"], r["displayOnly"], r["owner"]) for r in result["rows"]] == [
        ("user", "ordinary prompt", True, 100),
        ("assistant", "Saved activity before reload", False, None),
        ("tool", "tool output", False, None),
        ("assistant", "working", False, None),
    ]
    assert result["rows"][0]["ts"] == 99.9, "the recovered row keeps its own timestamp"


def test_client_clock_behind_the_server_keeps_the_previous_answer_above_the_prompt():
    """Codex .574..575 audit finding 2: the prompt `_ts` (98) trails a previous answer (99) that predates the server
    turn start (100). Placement follows the server start, so the previous answer stays in its turn."""
    result = _run(
        _running_turn("current question", 98)
        + """
let messages=[
  {role:'user',content:'previous question',_ts:90},
  {role:'assistant',content:'previous answer',_ts:99},
  {role:'assistant',content:'current activity',_ts:101},
  {role:'tool',content:'tool output',_ts:102},
];
messages=_mergeInflightTailMessages(messages,inflight,100);
process.stdout.write(JSON.stringify({rows:view(messages)}));
""",
        oldest_idx=31,
    )
    assert [(r["content"], r["displayOnly"], r["owner"]) for r in result["rows"]] == [
        ("previous question", False, None),
        ("previous answer", False, None),
        ("current question", True, 100),
        ("current activity", False, None),
        ("tool output", False, None),
        ("working", False, None),
    ], result


def test_without_a_proven_turn_owner_the_inflight_merge_is_unchanged():
    """No owner start (no active stream, another stream's snapshot, or an old caller): placement by the client
    timestamp and no stamp, as before."""
    result = _run(
        _running_turn("current question", 98)
        + """
const base=()=>[
  {role:'user',content:'previous question',_ts:90},
  {role:'assistant',content:'previous answer',_ts:99},
  {role:'assistant',content:'current activity',_ts:101},
];
const rows=(owner)=>{
  const fresh=[{role:'user',content:'current question',_ts:98,_pending:true},{role:'assistant',content:'working',_live:true}];
  return view(owner===undefined?_mergeInflightTailMessages(base(),fresh):_mergeInflightTailMessages(base(),fresh,owner));
};
process.stdout.write(JSON.stringify({two:rows(undefined),zero:rows(0),bad:rows('x'),neg:rows(-5)}));
""",
        oldest_idx=31,
    )
    expected = [
        ("previous question", False, None),
        ("current question", True, None),
        ("previous answer", False, None),
        ("current activity", False, None),
        ("working", False, None),
    ]
    for key in ("two", "zero", "bad", "neg"):
        assert [(r["content"], r["displayOnly"], r["owner"]) for r in result[key]] == expected, (key, result[key])


def test_an_earlier_turns_display_copy_with_the_same_words_does_not_hide_the_current_prompt():
    """The re-merge guard reuses by turn (the same row, or a copy stamped for this turn), never by text: an earlier
    turn's display-only prompt with the same words keeps its turn, and the current prompt is still recovered."""
    result = _run(
        _running_turn("continue", 99.9)
        + """
const earlier={role:'user',content:'continue',_ts:50,_displayOnlyRow:true,_recoveredTurnStart:50};
let messages=[earlier,{role:'assistant',content:'earlier answer',_ts:51},
  {role:'assistant',content:'current activity',_ts:101}];
messages=_mergeInflightTailMessages(messages,inflight,100);
const copies=messages.filter(m=>m.role==='user'&&m.content==='continue');
process.stdout.write(JSON.stringify({count:copies.length,earlierIdx:messages.indexOf(earlier),
  earlierOwner:earlier._recoveredTurnStart,current:copies.filter(m=>m!==earlier).map(m=>m._ts)}));
"""
    )
    assert result["count"] == 2, result
    assert result["earlierIdx"] == 0 and result["earlierOwner"] == 50
    assert result["current"] == [99.9]


def test_inflight_turn_owner_start_requires_the_active_stream():
    result = _run(
        """
const s={pending_started_at:100};
process.stdout.write(JSON.stringify([
  _inflightTurnOwnerStart(s,{streamId:'stream-1'},'stream-1'),
  _inflightTurnOwnerStart(s,{},'stream-1'),
  _inflightTurnOwnerStart(s,{streamId:' stream-1 '},'stream-1'),
  _inflightTurnOwnerStart(s,{streamId:'stream-0'},'stream-1'),
  _inflightTurnOwnerStart(s,{streamId:'stream-1'},''),
  _inflightTurnOwnerStart(s,{streamId:'stream-1'},null),
  _inflightTurnOwnerStart(null,{streamId:'stream-1'},'stream-1'),
  _inflightTurnOwnerStart({},{streamId:'stream-1'},'stream-1'),
  _inflightTurnOwnerStart({pending_started_at:0},{},'stream-1'),
  _inflightTurnOwnerStart({pending_started_at:'abc'},{},'stream-1'),
  _inflightTurnOwnerStart({pending_started_at:'100.5'},{},'stream-1'),
]));
"""
    )
    assert result == [100, 100, 100, 0, 0, 0, 0, 0, 0, 0, 100.5]


def test_load_session_passes_the_proven_turn_owner_to_the_inflight_merge():
    calls = re.findall(r"_mergeInflightTailMessages\(\s*S\.messages\s*,\s*inflightMessages\s*(,[^;]*)?\)\s*;", SESSIONS_JS)
    assert calls, "loadSession no longer merges the INFLIGHT tail"
    for extra in calls:
        compact = re.sub(r"\s+", "", extra or "")
        assert compact == ",_inflightTurnOwnerStart(S.session,INFLIGHT[sid],activeStreamId)", calls
