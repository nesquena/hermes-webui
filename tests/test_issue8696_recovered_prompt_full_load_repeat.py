"""#8696: on a zero-offset (full) load, repeated pending-prompt recovery must reuse the recovered copy.

`exp-v0.52.605` (#8632) made the full-load branch of `_mergePendingSessionMessage` place the recovered prompt by
turn ownership: in front of the first persisted row at or after `pending_started_at`. When that persisted current-turn
`user` row's text differs from `pending_user_message`, the tail-duplicate check cannot see the copy an earlier pass
placed. Every re-merge over the same array then stacked another visible prompt. `loadSession()` re-merges like this
after each failed message fetch, giving 1 -> 2 -> 3 copies, where v0.52.544 and .602 keep one.

These tests drive the real merge, ownership and reuse helpers from static/sessions.js and the real pending-prompt
producer from static/ui.js.
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
    "_markRecoveredPromptRow",
    "_existingRecoveredPromptIndex",
    "_mergePendingSessionMessage",
)
_UI_PRODUCER = (
    "_pendingCurrentTailUserMessage",
    "_messageTimestampSeconds",
    "_activeTurnTokenMatches",
    "_pendingActiveTurnUserMessage",
    "getPendingSessionMessage",
)


def _function_source(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
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
    start = src.index("const " + name + "=")
    return src[start : src.index(";", start) + 1]


def _run(body: str) -> dict:
    script = "\n".join(
        [
            "const assert=require('assert');",
            "var S={session:{session_id:'s1'},messages:[]};",
            "let _oldestIdx=0;",
            _const_source(UI_JS, "_PENDING_ACTIVE_TURN_TS_EPSILON"),
            _const_source(SESSIONS_JS, "_RECOVERED_PROMPT_TS_EPSILON"),
            *(_function_source(UI_JS, n) for n in _UI_PRODUCER),
            *(_function_source(SESSIONS_JS, n) for n in _SESSIONS_HELPERS),
            "const view=(rows)=>rows.map(m=>({role:m.role,content:m.content,ts:m._ts,live:!!m._live,"
            "displayOnly:!!m._displayOnlyRow}));",
            body,
        ]
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False, cwd=ROOT)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


_DISAGREEING_TURN = """
const session={session_id:'s1',active_stream_id:'stream-1',pending_started_at:100,
  pending_user_message:'pending wording'};
S.session=session;
const messages=[
  {role:'user',content:'canonical wording',_ts:100},
  {role:'assistant',content:'progress',_ts:101},
  {role:'assistant',content:'partial',_live:true},
];
"""


def test_repeated_full_load_recovery_keeps_one_prompt_when_turn_text_differs():
    result = _run(
        _DISAGREEING_TURN
        + """
const merged=[]; const counts=[];
for(let pass=0;pass<3;pass++){
  merged.push(_mergePendingSessionMessage(session,messages));
  counts.push(messages.filter(m=>m.role==='user'&&m.content==='pending wording').length);
}
process.stdout.write(JSON.stringify({merged,counts,rows:view(messages)}));
"""
    )
    assert result["counts"] == [1, 1, 1], result
    assert result["merged"] == [True, False, False], "later passes reuse the copy in place"
    assert result["rows"] == [
        {"role": "user", "content": "pending wording", "ts": 100, "live": False, "displayOnly": True},
        {"role": "user", "content": "canonical wording", "ts": 100, "live": False, "displayOnly": False},
        {"role": "assistant", "content": "progress", "ts": 101, "live": False, "displayOnly": False},
        {"role": "assistant", "content": "partial", "live": True, "displayOnly": False},
    ]


def test_a_displaced_recovered_copy_is_moved_to_the_turn_start_not_duplicated():
    """A display-only copy left at the wrong index (here in front of an earlier turn) moves to the current turn's
    start. Copies directly before the live row stay put on every tag (the tail-duplicate check), and a copy after the
    canonical row is adopted by the producer itself, so this layout is the one that reaches the move."""
    result = _run(
        """
const session={session_id:'s1',active_stream_id:'stream-1',pending_started_at:100,
  pending_user_message:'pending wording'};
S.session=session;
const stray=_markRecoveredPromptRow({role:'user',content:'pending wording',_ts:100,_pending:true});
const messages=[
  stray,
  {role:'user',content:'earlier prompt',_ts:50},
  {role:'assistant',content:'earlier answer',_ts:51},
  {role:'user',content:'canonical wording',_ts:100},
  {role:'assistant',content:'progress',_ts:101},
  {role:'assistant',content:'partial',_live:true},
];
const merged=_mergePendingSessionMessage(session,messages);
const again=_mergePendingSessionMessage(session,messages);
process.stdout.write(JSON.stringify({merged,again,sameObject:messages[2]===stray,rows:view(messages)}));
"""
    )
    assert result["merged"] is True and result["again"] is False, result
    assert result["sameObject"] is True, "the existing copy is moved, not replaced"
    assert [(r["content"], r["displayOnly"]) for r in result["rows"]] == [
        ("earlier prompt", False),
        ("earlier answer", False),
        ("pending wording", True),
        ("canonical wording", False),
        ("progress", False),
        ("partial", False),
    ]


def test_an_earlier_canonical_turn_with_the_same_text_is_left_alone():
    """Reuse is limited to display-only copies: a persisted earlier turn that repeats the prompt is not moved."""
    result = _run(
        """
const session={session_id:'s1',active_stream_id:'stream-1',pending_started_at:100,
  pending_user_message:'pending wording'};
S.session=session;
const earlier={role:'user',content:'pending wording',_ts:50};
const messages=[
  earlier,
  {role:'assistant',content:'earlier answer',_ts:51},
  {role:'user',content:'canonical wording',_ts:100},
  {role:'assistant',content:'progress',_ts:101},
  {role:'assistant',content:'partial',_live:true},
];
const merged=[_mergePendingSessionMessage(session,messages),_mergePendingSessionMessage(session,messages),
  _mergePendingSessionMessage(session,messages)];
process.stdout.write(JSON.stringify({merged,earlierFirst:messages[0]===earlier,rows:view(messages)}));
"""
    )
    assert result["merged"] == [True, False, False], result
    assert result["earlierFirst"] is True
    assert [(r["content"], r["displayOnly"]) for r in result["rows"]] == [
        ("pending wording", False),
        ("earlier answer", False),
        ("pending wording", True),
        ("canonical wording", False),
        ("progress", False),
        ("partial", False),
    ]
