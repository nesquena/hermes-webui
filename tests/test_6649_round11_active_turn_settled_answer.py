"""#6649 greptile round-11 — two P1 findings on the settled-answer recovery path.

Finding 1 (static/sessions.js:4207, "Current turn appears twice"):
The server marks the active turn's user row with ``_active_turn_user`` even
when that turn ALREADY has saved assistant output.  The marker branch bailed
out on ``_sawCompletedAnswer`` before ever comparing turn identity, so the
base row (marked user + saved answer) and the re-supplied inflight user were
treated as different turns: recovery appended the user and the live reply
again and the current turn rendered twice.

Finding 2 (static/sessions.js:4747, "Saved answer disappears"):
``_supersededBySettled`` survives the recovery that sets it.  A second
recovery over the SAME inflight row now sees ``liveText === persistedText``,
preparation returns true, ``loadSession`` drops the saved assistant, and the
merge filters the flagged live row — the turn is left with no response at all.

Both tests drive the real extracted components in the literal loadSession
order (prepare -> conditional drop -> merge).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _node_env() -> dict:
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
    return env


def _run_node(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, check=False, env=_node_env()
    )


def _helper_block() -> str:
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1, "sessions.js helper block moved"
    block = SESSIONS_JS[start:end]
    # _hasInflightTailUserDuplicate sits above _prepareRunningLiveTail but is
    # not part of the contiguous helper block.
    marker = "function _hasInflightTailUserDuplicate("
    hstart = SESSIONS_JS.find(marker)
    assert hstart != -1
    depth = 0
    i = SESSIONS_JS.find("){", hstart) + 1
    while i < len(SESSIONS_JS):
        if SESSIONS_JS[i] == "{":
            depth += 1
        elif SESSIONS_JS[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return SESSIONS_JS[hstart:i + 1] + "\n" + block


def test_active_turn_marker_with_saved_answer_is_a_duplicate():
    """A marked current-turn user whose answer is already saved must still
    dedupe against the re-supplied inflight row (#6649 P1, sessions.js:4207)."""
    script = f"""
const assert = require('assert');
{_helper_block()}

// Marked user + saved answer already in the transcript.
const base = [
  {{ role:'user', content:'deploy it', timestamp:100, _active_turn_user:true }},
  {{ role:'assistant', content:'Deployed.', timestamp:101 }},
];

// Same turn re-supplied from the inflight cache (server marker present).
const marked = {{ role:'user', content:'deploy it', timestamp:100, _active_turn_user:true }};
assert.strictEqual(_hasInflightTailUserDuplicate(base, marked), true,
  'Marked current-turn user with a saved answer must dedupe (current turn appeared twice)');

// The inflight copy may carry the stamp but not the marker.
const unmarked = {{ role:'user', content:'deploy it', timestamp:100 }};
assert.strictEqual(_hasInflightTailUserDuplicate(base, unmarked), true,
  'Precision-matched timestamp must establish identity before the settled-answer veto');

// Whole-second drift is still a DIFFERENT turn (rapid repeated prompt).
const drifted = {{ role:'user', content:'deploy it', timestamp:105 }};
assert.strictEqual(_hasInflightTailUserDuplicate(base, drifted), false,
  'A genuinely different turn must stay separate');

// Different text under the same marker is not a duplicate either.
const otherText = {{ role:'user', content:'roll it back', timestamp:100 }};
assert.strictEqual(_hasInflightTailUserDuplicate(base, otherText), false,
  'Marker identity still requires the same prompt text');

// No identity at all: the completed answer stays a settled-turn boundary.
const anonymous = {{ role:'user', content:'deploy it' }};
assert.strictEqual(_hasInflightTailUserDuplicate(base, anonymous), false,
  'Without identity a completed answer still separates the turns');
"""
    out = _run_node(script)
    assert out.returncode == 0, out.stderr


def test_second_recovery_keeps_the_saved_answer():
    """Two recoveries over the same inflight row must not drop the only
    response (#6649 P1, sessions.js:4747)."""
    script = f"""
const assert = require('assert');
{_helper_block()}

function drive(base, inflight) {{
  const prepared = _prepareRunningLiveTail(base, inflight, {{}});
  let rows = base;
  if (prepared) rows = _dropCurrentTurnAssistantMessages(rows);
  rows = _mergeInflightTailMessages(rows, inflight);
  return {{ prepared: prepared, rows: rows.map(function (m) {{ return String(m.role) + ':' + String(m.content); }}) }};
}}

const base = [
  {{ role:'user', content:'hello' }},
  {{ role:'assistant', content:'authoritative settled answer' }},
];
const inflight = [
  {{ role:'user', content:'hello' }},
  {{ role:'assistant', _live:true, content:'stale divergent partial' }},
];
const live = inflight[1];

const first = drive(base, inflight);
assert.strictEqual(first.prepared, false,
  'First recovery: a divergent partial must not take over');
assert.ok(live._supersededBySettled, 'First recovery flags the stale live row');
assert.deepStrictEqual(first.rows, ['user:hello', 'assistant:authoritative settled answer'],
  'First recovery must keep exactly the saved answer, got ' + JSON.stringify(first.rows));

// Second recovery over the SAME inflight row: content now equals the saved
// text. Returning true here drops the saved assistant AND filters the flagged
// live row, leaving no response at all.
const second = drive(base, inflight);
assert.strictEqual(second.prepared, false,
  'Second recovery must not report the live row as a safe replacement');
assert.deepStrictEqual(second.rows, ['user:hello', 'assistant:authoritative settled answer'],
  'The saved answer must survive repeated recoveries, got ' + JSON.stringify(second.rows));
assert.strictEqual(live._live, true, 'The live row keeps its live flag');

// A genuinely resumed turn clears the stale marker: the live row is the truth.
live.content = 'authoritative settled answer\\n\\nand more';
const third = drive(base, inflight);
assert.strictEqual(third.prepared, true,
  'A resumed live turn must be allowed to take over');
assert.ok(!live._supersededBySettled,
  'A resumed live row must not stay flagged as superseded');
assert.deepStrictEqual(third.rows, ['user:hello', 'assistant:authoritative settled answer\\n\\nand more'],
  'Full resumed text must survive, got ' + JSON.stringify(third.rows));
"""
    out = _run_node(script)
    assert out.returncode == 0, out.stderr
