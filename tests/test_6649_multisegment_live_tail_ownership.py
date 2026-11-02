"""Regression tests: a multi-segment live tail must prove current-turn
ownership before loadSession may drop the base transcript's settled answer.

Deferred session-save leaves the base transcript at the PREVIOUS turn's
settled user+assistant while the current turn's prompt and live partial exist
only in the INFLIGHT snapshot.  ``_projectInflightMessagesForActivityBursts``
splits the live accumulator into several ``_live`` segments, so
``_prepareRunningLiveTail`` takes its ``liveMessages.length > 1`` fast path.
That fast path never proves the base's settled answer belongs to the CURRENT
turn: returning ``true`` authorizes ``_dropCurrentTurnAssistantMessages`` and
the immediately preceding reply disappears until an idle reload restores it
from disk.

The single-segment path already gates the drop on
``settledBelongsToCurrentTurn`` (verified via ``_hasInflightTailUserDuplicate``)
— the multi-segment path must obey the same rule.
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


def test_multisegment_live_tail_never_drops_previous_turn_answer():
    """Three turns, deferred save. While the third turn streams, its live tail
    is split into multiple segments. The previous (second) turn's settled
    answer must stay visible in the recovery view."""
    script = f"""
const assert = require('assert');
{_helper_source()}

// Deferred-save base: ends with the SECOND turn's settled pair. The third
// turn's prompt lives only in the inflight snapshot.
const base = [
  {{role:'user', content:'first question'}},
  {{role:'assistant', content:'first answer'}},
  {{role:'user', content:'second question'}},
  {{role:'assistant', content:'second answer', _row_id: 608781}},
];

// Multi-segment live tail for the THIRD turn (activity-burst projection).
const inflight = [
  {{role:'user', content:'third question', _pending:true, _ts: 200}},
  {{role:'assistant', _live:true, content:'segment one', _liveSegmentSeq:1, _activityBurstId:1}},
  {{role:'assistant', _live:true, content:'segment two', _liveSegmentSeq:2, _activityBurstId:2}},
];

const prepared = _prepareRunningLiveTail(base, inflight, {{
  session_id: 's1', active_stream_id: 'st1', pending_user_message: 'third question',
  pending_started_at: 200,
}});
// The drop may only run when the settled tail belongs to the CURRENT turn.
assert.strictEqual(prepared, false);

const view = _mergeInflightTailMessages(base, inflight);
// Previous turn's answer survives; the live tail still renders.
assert.ok(view.some(m => m.role === 'assistant' && m.content === 'second answer'),
  'previous turn answer must remain visible during streaming');
assert.ok(view.some(m => m._live && m.content === 'segment one'),
  'live segment must still be appended');
assert.ok(view.some(m => m._live && m.content === 'segment two'),
  'live segment 2 must still be appended');
// And the current prompt must not duplicate.
assert.strictEqual(view.filter(m => m.role === 'user' && m.content === 'third question').length, 1);
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr


def test_multisegment_live_tail_replaces_own_turn_settled_answer():
    """When the base DOES contain the current turn's user (lost done event),
    the multi-segment path must still authorize drop-and-replace: the settled
    row of the SAME turn is replaced by the live segments, never shown twice."""
    script = f"""
const assert = require('assert');
{_helper_source()}

const base = [
  {{role:'user', content:'first question'}},
  {{role:'assistant', content:'first answer'}},
  {{role:'user', content:'third question', _active_turn_user:true, timestamp: 200}},
  {{role:'assistant', content:'partial settled answer of the CURRENT turn'}},
];

const inflight = [
  {{role:'user', content:'third question', _pending:true, _ts: 200}},
  {{role:'assistant', _live:true, content:'partial settled answer of the CURRENT turn', _liveSegmentSeq:1, _activityBurstId:1}},
  {{role:'assistant', _live:true, content:'extra live tail', _liveSegmentSeq:2, _activityBurstId:2}},
];

const prepared = _prepareRunningLiveTail(base, inflight, {{
  session_id: 's1', active_stream_id: 'st1', pending_user_message: 'third question',
  pending_started_at: 200,
}});
assert.strictEqual(prepared, true);

const dropped = _dropCurrentTurnAssistantMessages(base);
const view = _mergeInflightTailMessages(dropped, inflight);
// The settled same-turn answer is replaced by the live segments — one copy.
const copies = view.filter(m =>
  m.role === 'assistant' && /CURRENT turn/.test(String(m.content)));
assert.strictEqual(copies.length, 1, 'same-turn settled answer must appear exactly once');
assert.strictEqual(copies[0]._live, true);
// Previous turn stays intact.
assert.ok(view.some(m => m.role === 'assistant' && m.content === 'first answer'));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr


def test_single_segment_ownership_gate_still_passes():
    """Sanity: the single-segment gate keeps its behavior (deferred save →
    no drop; previous answer visible). Guards against over-broadening the
    multi-segment fix into the single path."""
    script = f"""
const assert = require('assert');
{_helper_source()}

const base = [
  {{role:'user', content:'first question'}},
  {{role:'assistant', content:'first answer'}},
  {{role:'user', content:'second question'}},
  {{role:'assistant', content:'second answer'}},
];
const inflight = [
  {{role:'user', content:'third question', _pending:true, _ts: 200}},
  {{role:'assistant', _live:true, content:'live partial'}},
];
const prepared = _prepareRunningLiveTail(base, inflight, {{
  session_id: 's1', active_stream_id: 'st1', pending_user_message: 'third question',
  pending_started_at: 200,
}});
assert.strictEqual(prepared, false);
const view = _mergeInflightTailMessages(base, inflight);
assert.ok(view.some(m => m.role === 'assistant' && m.content === 'second answer'));
assert.ok(view.some(m => m._live && m.content === 'live partial'));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
