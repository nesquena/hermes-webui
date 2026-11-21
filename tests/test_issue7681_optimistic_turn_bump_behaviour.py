"""Behaviour test for #7681 finding 6: the optimistic turn bump must be idempotent.

The previous round's tests grepped `static/sessions.js` for the marker names, so
they passed while the count still compounded 40 -> 41 -> 42 -> 43 across the three
update passes `send()` runs. This test extracts the real block and runs it in node.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")

# The optimistic-turn block, from the counter through the marker reset.
_BLOCK_START = "  const optimisticUserTurns="
_BLOCK_END = "  if((S.session.title==='Untitled'||!S.session.title)&&title){"


def _extract_block() -> str:
    start = SESSIONS_JS.find(_BLOCK_START)
    assert start >= 0, "optimistic-turn block not found in static/sessions.js"
    end = SESSIONS_JS.find(_BLOCK_END, start)
    assert end > start, "end of the optimistic-turn block not found"
    return SESSIONS_JS[start:end]


def _extract_send_finally() -> str:
    """The body of ``send()``'s ``finally`` in ``static/messages.js``.

    Extracted rather than reimplemented: a hand-written stand-in would keep
    passing after the real clear was deleted, which is exactly how the compounding
    bug survived a round of source-grep tests.
    """
    send_start = MESSAGES_JS.find("async function send(")
    assert send_start >= 0, "send() not found in static/messages.js"
    finally_idx = MESSAGES_JS.find("}finally{", send_start)
    assert finally_idx > send_start, "send() has no finally block"
    body_start = finally_idx + len("}finally{")
    # Stop at the finally's own closing brace: the next `}` at the same nesting
    # depth. The body is short and brace-balanced, so count depth from the start.
    depth = 1
    for i in range(body_start, len(MESSAGES_JS)):
        ch = MESSAGES_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[body_start:i]
    raise AssertionError("unbalanced braces in send()'s finally")


_HARNESS = r"""
// Drive the extracted block as a function so the test can run the three passes
// send() performs, against a windowed long session.
const S = {
  session: {session_id: 'sess-1', user_message_count: 40},
  messages: Array.from({length: 30}, (_, i) => ({role: i % 2 ? 'assistant' : 'user', content: 'x'})),
};
let _sendInProgress = true;
let _sendInProgressSid = 'sess-1';
let _optimisticTurnBumpSid;
let _optimisticTurnBumpBase;

function pass() {
  const optimisticUserTurns=(Array.isArray(S.messages)?S.messages:[])
    .filter(m=>m&&m.role==='user'&&!(
      typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(m)
    )).length;
  if(optimisticUserTurns>0){
    const serverUserTurns=Number(S.session.user_message_count||0);
    let userTurns=Math.max(serverUserTurns,optimisticUserTurns);
    const inFlight=typeof _sendInProgress!=='undefined'&&!!_sendInProgress
      && (typeof _sendInProgressSid==='undefined'||_sendInProgressSid===null
        || _sendInProgressSid===S.session.session_id);
    if(userTurns===serverUserTurns&&serverUserTurns>0&&optimisticUserTurns<serverUserTurns&&inFlight){
      const _bumpSid=(typeof _optimisticTurnBumpSid!=='undefined')?_optimisticTurnBumpSid:null;
      const _bumpBase=(typeof _optimisticTurnBumpBase!=='undefined')?_optimisticTurnBumpBase:null;
      const alreadyBumped=_bumpSid===sid&&Number.isFinite(_bumpBase)
        &&userTurns===Number(_bumpBase)+1;
      if(!alreadyBumped){
        userTurns=serverUserTurns+1;
        _optimisticTurnBumpSid=sid;
        _optimisticTurnBumpBase=serverUserTurns;
      }
    } else if(!inFlight){
      if(typeof _optimisticTurnBumpSid!=='undefined') _optimisticTurnBumpSid=null;
      if(typeof _optimisticTurnBumpBase!=='undefined') _optimisticTurnBumpBase=null;
    }
    S.session.user_message_count=userTurns;
  }
  return S.session.user_message_count;
}

const results = [];
for (let i = 0; i < 3; i++) results.push(pass());
console.log(JSON.stringify(results));
"""


def _run_node(harness: str) -> list[int]:
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT)
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    import json

    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_three_update_passes_bump_the_count_once() -> None:
    """#7681 finding 6: 40 -> 41 -> 41 -> 41, never 40 -> 41 -> 42 -> 43.

    ``send()`` runs three update passes and the second one fires before the stream
    id exists, so an unconditional +1 compounded. The harness replays exactly that
    sequence against a windowed long session (server 40, window 15).
    """
    block = _extract_block()
    harness = _HARNESS.replace(
        "  const optimisticUserTurns=", block.split("  const optimisticUserTurns=", 1)[1].join(
            ["  const optimisticUserTurns=", ""]
        )
        if False
        else "  const optimisticUserTurns=" + block.split("  const optimisticUserTurns=", 1)[1],
    )
    # The extracted block references `sid`/`title`/`count`/`nowSec` from its
    # enclosing scope; provide them before each pass.
    harness = harness.replace(
        "function pass() {",
        "const sid = 'sess-1';\nfunction pass() {",
    )
    counts = _run_node(harness)
    assert counts == [41, 41, 41], (
        f"the optimistic +1 must land once across the three passes, got {counts} "
        "(40 -> 41 -> 42 -> 43 is the compounding regression)"
    )


def test_a_second_send_bumps_again() -> None:
    """Counterpart: once the first turn is no longer in flight, the next send on
    the same session must be able to advance the count."""
    block = _extract_block()
    harness = _HARNESS.replace(
        "  const optimisticUserTurns=",
        "  const optimisticUserTurns=" + block.split("  const optimisticUserTurns=", 1)[1],
    )
    finally_body = _extract_send_finally()
    # The extracted finally references `_sendInProgress` / `_sendInProgressSid`
    # and the bump markers, all of which the harness declares. Wrap it verbatim.
    harness = harness.replace(
        "function pass() {",
        "const sid = 'sess-1';\n"
        "function endOfSend(){\n"
        + finally_body
        + "\n}\n"
        "function pass() {",
    )
    harness = harness.replace(
        "const results = [];\nfor (let i = 0; i < 3; i++) results.push(pass());",
        "const results = [];\n"
        "for (let i = 0; i < 3; i++) results.push(pass());\n"
        "endOfSend();\n"
        "_sendInProgress = true; _sendInProgressSid = 'sess-1';\n"
        "S.session.user_message_count = 41;\n"
        "for (let i = 0; i < 3; i++) results.push(pass());",
    )
    counts = _run_node(harness)
    assert counts[:3] == [41, 41, 41], f"first send must bump once, got {counts[:3]}"
    assert counts[3:] == [42, 42, 42], (
        f"the next send must be able to advance the count again, got {counts[3:]}"
    )
