"""Non-windowed sibling of `test_inflight_tail_windowed_user_position.py`.

The windowed branch (`_oldestIdx > 0`) already places the recovered prompt by
turn ownership. The non-windowed branch still fell through to `push()`, so a
session switch during the first reply could hand the merge a base holding this
turn's ACTIVITY rows (assistant prose + tool rows) without its `user` row — the
question then rendered BELOW its own answer until the turn settled:

    [assistant("..."), tool(...), user("What files are in this workspace?"), assistant(_live)]

Structural invariant: a merged transcript with no `user` row at all means this
prompt IS the turn's first row and must lead it.
"""
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _helper_source() -> str:
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    return SESSIONS_JS[start:end]


def _run_node(script: str) -> subprocess.CompletedProcess:
    assert NODE, "node not on PATH"
    return subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, check=False
    )


def test_activity_only_base_puts_current_prompt_first():
    """Base holds ONLY this turn's activity rows (no user). The recovered
    prompt must be the first row of the turn."""
    script = f"""
const assert = require('assert');
{_helper_source()}

const base = [
  {{role:'assistant', content:'first segment of the answer', _ts: 1005}},
  {{role:'tool', content:'tool output', _ts: 1006}},
];
const inflight = [
  {{role:'assistant', _live:true, content:'first segment of the answer', _ts: 1005}},
  {{role:'user', content:'What files are in this workspace?', _pending:true, _ts: 1000}},
];

const view = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(view[0].role, 'user',
  'current prompt must lead the turn, got: ' + view.map(m=>m.role).join(','));
assert.strictEqual(view[0].content, 'What files are in this workspace?');
assert.deepStrictEqual(view.slice(1).map(m=>m.role), ['assistant', 'tool']);
"""
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr


def test_activity_only_base_without_timestamps_puts_prompt_first():
    """Journal projection can drop timestamps entirely; the prompt must still
    lead a user-free transcript (fallback target = 0)."""
    script = f"""
const assert = require('assert');
{_helper_source()}

const base = [
  {{role:'assistant', content:'segment'}},
  {{role:'tool', content:'out'}},
];
const inflight = [
  {{role:'user', content:'question', _pending:true}},
  {{role:'assistant', _live:true, content:'segment'}},
];

const view = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(view[0].role, 'user', 'got: ' + view.map(m=>m.role).join(','));
"""
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr


def test_previous_turn_user_keeps_append_semantics():
    """Control: a transcript that already carries a user row (an earlier turn)
    keeps append semantics — the new prompt must not be yanked to the front."""
    script = f"""
const assert = require('assert');
{_helper_source()}

const base = [
  {{role:'user', content:'earlier question', _ts: 900}},
  {{role:'assistant', content:'earlier answer', _ts: 901}},
];
const inflight = [
  {{role:'user', content:'new question', _pending:true, _ts: 1000}},
  {{role:'assistant', _live:true, content:'new answer'}},
];

const view = _mergeInflightTailMessages(base, inflight);
assert.deepStrictEqual(view.map(m=>m.role), ['user','assistant','user','assistant']);
assert.strictEqual(view[2].content, 'new question');
"""
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr
