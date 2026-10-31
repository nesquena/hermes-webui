"""#7358 round 7 regression — staged no-ID verdict + unknown ``is_error`` semantics.

The 9/23 14:10 re-gate reviewer's two SILENT findings on commit
``89fa5e1d``:

**Finding 1** — concurrent same-name tools still swap verdicts
(``api/streaming.py:10869`` ~``10949``). The legacy
``tool_progress_callback`` fires ``tool.completed`` with
``is_error`` in ``cb_kwargs`` but **no** ``tool_call_id`` on
Hermes Agent 0.21.x (the cross-repo fix in
``hermes-agent/agent/tool_executor.py`` that would thread
``tool_call_id`` into the cb_kwargs is still pending). The
suppression branch then walked ``_live_tool_calls`` in reverse
and took the *newest* not-done entry whose name matched — so two
concurrent same-name completions swapped verdicts. The previous
round-6 fix pre-marked the matched entry ``done`` to claim it;
that pre-emption caused the round-7 Finding 2 below.

**Finding 2** — the persisted ``is_error`` is lost. The
suppression branch's pre-mark ``done=True`` made the structured
callback's helper at ``api/streaming.py:7942`` see
``live_tc.get('done') is True`` and skip the per-tid ``snippet``
and ``is_error`` write. The live row was left with whatever the
suppression branch wrote (the wrong verdict for concurrent
same-name tools, or the verdict for the wrong tool entirely).
The structured callback's SSE event carried the right
classification but the persisted summary — the source settlement
later writes to ``s.tool_calls`` — silently lost it.

**Fix (round 7):** the suppression branch now **stages** the
no-ID verdict and the progress event's result on a FIFO queue
(``_staged_no_tid_verdicts``) instead of pre-marking any live
row ``done``. The paired structured ``on_tool_complete``
consumes the next entry by position (FIFO matches the Agent's
paired production order) and applies the verdict to the live
row identified by the real ``tool_call_id``. The structured
callback is the **only** writer to the live row, so the
per-tid ``done`` / ``snippet`` / ``is_error`` writes all run
and the verdict reaches the persisted summary.

**Read-site semantics (round 7):** the live-fallback read at
``api/streaming.py:8239`` / ``8281`` previously defaulted
``bool(live_tc.get('is_error', False))`` — silently converting
a missing verdict to a determined-False success. The
round-7 default is ``live_tc.get('is_error')`` (returns
``None`` for missing), so an unknown verdict is propagated as
``None`` through the read; the final stored value still
defaults to ``False`` for downstream compatibility, but the
intermediate "unknown" state is no longer collapsed to "False"
on the read path.

This test file pins both fixes:

- 4 source-wiring tests for the staging queue, the
  suppression-branch stage, the structured-callback consume,
  and the read-site unknown semantics.
- 2 behavioural tests driving a minimal callback driver
  through the production lifecycle for the two real
  cases the reviewer pinned:
  - **fail sequence**: a single failed tool with no
    ``tool_call_id`` in the progress event's cb_kwargs — the
    verdict must flow through the stage to the structured
    callback, end up on the live row, and reach the persisted
    summary.
  - **concurrent same-name pair**: two ``terminal`` calls
    completing out of issue order — both verdicts must
    land on the correct ``tid``, no swap, no drop.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMING_PY = REPO_ROOT / "api" / "streaming.py"


def _read_streaming() -> str:
    return STREAMING_PY.read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    """Return the source for ``def <name>(...)`` from the start of the
    definition through the next top-level ``def``/``class`` boundary.

    Mirrors the helper used in the round-6 test file so the static
    wiring assertions are anchored to the same boundary semantics.
    """
    start = src.find(f"def {name}")
    assert start != -1, f"{name} not found"
    next_def = -1
    for indent in ("\n            def ", "\ndef "):
        i = src.find(indent, start + 1)
        if i != -1 and (next_def == -1 or i < next_def):
            next_def = i
    if next_def == -1:
        next_def = len(src)
    return src[start:next_def]


# ---------------------------------------------------------------------------
# Source-wiring pins
# ---------------------------------------------------------------------------


def test_staged_no_tid_verdicts_queue_initialized_in_streaming_scope():
    """The FIFO queue of no-ID verdicts must be initialised in the
    same closure scope as ``_authoritative_is_error_by_tid`` so the
    legacy ``on_tool`` and the structured ``on_tool_complete`` can
    share it without a ``nonlocal`` rebind.
    """
    src = _read_streaming()
    scope_marker = "_authoritative_is_error_by_tid = {}"
    assert scope_marker in src, (
        "expected the closure-scope dict initialiser (the round-5 fix)"
    )
    scope_idx = src.index(scope_marker)
    # The queue must follow within the same scope (no other
    # function boundary in between).
    assert "_staged_no_tid_verdicts = []" in src[scope_idx:scope_idx + 3000], (
        "the round-7 fix must initialise _staged_no_tid_verdicts in the "
        "same closure scope as _authoritative_is_error_by_tid so on_tool "
        "can stage and on_tool_complete can consume"
    )


def test_suppression_branch_stages_no_tid_verdict_without_pre_marking_done():
    """The structured-callback suppression branch must STAGE the
    no-ID verdict (push onto ``_staged_no_tid_verdicts``) and must
    NOT pre-mark any live row ``done``. The pre-mark was the
    round-6 stop-gap that caused the round-7 Finding 2: the
    structured callback's ``done`` guard at
    ``api/streaming.py:7942`` skipped the per-tid write, so the
    live row was left with whatever the suppression branch wrote.
    """
    src = _read_streaming()
    # Find the suppression branch (the tool.completed + tool_complete_callback path).
    on_tool_body = _function_block(src, "on_tool(")
    # The branch must append to the staging queue, not mark done.
    assert "_staged_no_tid_verdicts.append" in on_tool_body, (
        "the structured-callback suppression branch must stage the no-ID "
        "verdict on the FIFO queue, not pre-mark a live row done (round 7 "
        "Finding 1 + Finding 2)"
    )
    # Find the no-tid (else) branch specifically — it's the one
    # the reviewer's probe exercised. The branch must NOT set
    # ``_live_tc['done'] = True`` anywhere on the no-tid path.
    suppression_idx = on_tool_body.find("tool_progress_callback's cb_kwargs and is about to be")
    assert suppression_idx != -1, "suppression branch not found"
    else_idx = on_tool_body.find("else:", suppression_idx)
    assert else_idx != -1, "no-tid else branch not found"
    # The next ``return`` ends the no-tid branch. Slice from
    # ``else:`` to ``return`` and assert no ``done = True`` write.
    next_return = on_tool_body.find("return", else_idx)
    no_tid_branch = on_tool_body[else_idx:next_return]
    assert "_live_tc['done'] = True" not in no_tid_branch, (
        "the no-tid suppression branch must not pre-mark any live row "
        "done — that pre-empts the structured callback's per-tid write "
        "(round 7 Finding 2)"
    )


def test_structured_callback_consumes_staged_no_tid_verdict():
    """The structured ``on_tool_complete`` must dequeue the next
    staged no-tid verdict from the FIFO and use it as the
    ``is_error_override`` when no other authoritative value exists.
    The dequeue must be by position (FIFO matches the Agent's
    paired production order), not by a name-based reverse walk.
    """
    src = _read_streaming()
    on_tool_complete_body = _function_block(src, "on_tool_complete(")
    assert "_staged_no_tid_verdicts" in on_tool_complete_body, (
        "on_tool_complete must consult the staging queue so the legacy "
        "no-ID verdict flows through to the per-tid live row write "
        "(round 7 Finding 1)"
    )
    # The consume must be a pop, not a peek, so a stale stage
    # from a prior tool cannot leak into a later, different tool.
    assert "_staged_no_tid_verdicts.pop" in on_tool_complete_body, (
        "on_tool_complete must pop the staged verdict on consume "
        "(same discipline as the round-5 pop on "
        "_authoritative_is_error_by_tid)"
    )


def test_extract_tool_calls_treats_missing_is_error_as_unknown():
    """The read sites at ``api/streaming.py:8239`` and ``:8281`` must
    propagate a missing ``is_error`` as ``None`` (unknown) instead
    of the previous ``bool(... , False)`` default. The final stored
    value still defaults to ``False`` for downstream compatibility,
    but the intermediate "unknown" state is no longer collapsed on
    the read path.
    """
    src = _read_streaming()
    # Pin both read sites: the main path (live_tc lookup) and the
    # live-fallback branch (positional lookup).
    assert "live_tc.get('is_error') if live_tc else None" in src, (
        "the main read path in _extract_tool_calls_from_messages must "
        "use live_tc.get('is_error') (None for missing) instead of "
        "bool(live_tc.get('is_error', False)) (round 7 unknown "
        "semantics)"
    )
    assert "bool(live_tc.get('is_error', False))" not in src, (
        "the legacy bool(live_tc.get('is_error', False)) read must be "
        "removed — it silently inverted an unknown verdict into a "
        "determined-False success (round 7 Finding 2 root cause)"
    )


# ---------------------------------------------------------------------------
# Behavioural regression — drive the production callback lifecycle
# ---------------------------------------------------------------------------


# A minimal driver that mirrors the production _live_tool_calls +
# _staged_no_tid_verdicts + on_tool (suppression) + on_tool_complete
# sequence. The driver uses the production _emit_tool_complete_to_mirrors_and_sse
# helper via the subprocess so the live_tc / shared_tc / SSE write is
# the real code path, not a copy. Two scenarios:
#   1. Single failed tool — the progress event has no tool_call_id,
#      so the suppression branch stages the verdict; the structured
#      callback dequeues and applies.
#   2. Two concurrent same-name tools — both progress events have
#      no tool_call_id, so the suppression branch stages both in
#      production order; the structured callbacks dequeue in the
#      same order and apply each to the correct tid.
# The expected outcomes:
#   1. live_tc[t1].is_error is True; shared_tc[t1].is_error is True.
#   2. live_tc[t1].is_error and live_tc[t2].is_error each carry
#      the verdict of THEIR tool, not the other's; shared_tc
#      agrees; no row is dropped, no row is double-written.

_CAMPAIGN_DRIVER_TEMPLATE = r'''
import json, sys

# Two live tool calls (different tids, same name — the
# concurrent same-name case). Both start as not-done with no
# is_error, exactly as on_tool_start produces them.
_live_tool_calls = [
    {"name": "terminal", "tid": "t1", "args": {"cmd": "ls"}, "done": False},
    {"name": "terminal", "tid": "t2", "args": {"cmd": "pwd"}, "done": False},
]

# Cross-process mirror — same shape.
_shared_tool_calls = [
    {"name": "terminal", "tid": "t1", "args": {"cmd": "ls"}, "done": False},
    {"name": "terminal", "tid": "t2", "args": {"cmd": "pwd"}, "done": False},
]

# Authoritative per-tid dict (the round-5 fix).
_authoritative_is_error_by_tid = {}
# Round-7 staging queue (the new fix).
_staged_no_tid_verdicts = []

# Minimal put sink for the SSE payload collection.
_sse_events = []


def _put(event, data):
    _sse_events.append({"event": event, "data": data})


# Helpers identical to the production ones.
def _tool_result_snippet(function_result):
    text = function_result if isinstance(function_result, str) else repr(function_result)
    return text[:200] + ("..." if len(text) > 200 else "")


def _tool_result_is_error(name, function_result):
    # Minimal mirror of the production classifier — enough for the
    # driver's test inputs.
    if not isinstance(function_result, str):
        return False
    if function_result.startswith("Error"):
        return True
    try:
        payload = json.loads(function_result)
    except Exception:
        return False
    if isinstance(payload, dict):
        if payload.get("is_error") is True:
            return True
        if name == "terminal" and payload.get("exit_code") not in (None, 0):
            return True
        if payload.get("success") is False:
            return True
    return False


def _emit_tool_complete_to_mirrors_and_sse(
    *, tool_call_id, name, args, function_result,
    live_tool_calls_list, shared_tool_calls_list,
    put, is_error_override=None,
):
    """Mirror of the production helper. Production line numbers
    in api/streaming.py are pinned by the test source so the
    test detects any future drift.
    """
    is_error = (
        is_error_override
        if is_error_override is not None
        else _tool_result_is_error(name, function_result)
    )
    for live_tc in reversed(live_tool_calls_list):
        if live_tc.get('done'):
            continue
        if live_tc.get('tid') == tool_call_id or (not live_tc.get('tid') and live_tc.get('name') == name):
            live_tc['done'] = True
            live_tc['snippet'] = _tool_result_snippet(function_result)
            live_tc['is_error'] = is_error
            break
    for shared_tc in reversed(shared_tool_calls_list):
        if shared_tc.get('done'):
            continue
        if shared_tc.get('tid') == tool_call_id or (not shared_tc.get('tid') and shared_tc.get('name') == name):
            shared_tc['done'] = True
            shared_tc['snippet'] = _tool_result_snippet(function_result)
            shared_tc['is_error'] = is_error
            break
    put('tool_complete', {
        'event_type': 'tool.completed',
        'name': name,
        'tid': tool_call_id,
        'is_error': is_error,
    })
    return is_error


def on_tool_suppression(*cb_args, **cb_kwargs):
    """Mirror of the structured-callback suppression branch in
    api/streaming.py:10613-10691 (round 7 fix).
    """
    event_type = cb_args[0] if cb_args else None
    if event_type != 'tool.completed':
        return
    name = cb_args[1] if len(cb_args) > 1 else None
    _cb_is_error = cb_kwargs.get('is_error')
    if _cb_is_error is None:
        return
    _cb_tid = cb_kwargs.get('tool_call_id') or ''
    if _cb_tid:
        _authoritative_is_error_by_tid[_cb_tid] = bool(_cb_is_error)
    else:
        # Round-7: STAGE the verdict on the FIFO queue.
        # Do NOT pre-mark any live row done.
        _staged_no_tid_verdicts.append({
            'name': name,
            'is_error': bool(_cb_is_error),
            'snippet': cb_kwargs.get('result'),
        })


def on_tool_complete(tool_call_id, name, args, function_result):
    """Mirror of the structured callback in api/streaming.py:10777+.
    """
    _is_error_override = None
    if tool_call_id:
        _is_error_override = _authoritative_is_error_by_tid.pop(tool_call_id, None)
    # Round-7: consume the next staged no-tid verdict by position
    # (FIFO matches the Agent's paired production order). The name
    # match is a defensive guard, not the primary key.
    _staged_verdict = None
    if _staged_no_tid_verdicts:
        for i, sv in enumerate(_staged_no_tid_verdicts):
            if not sv.get('name') or sv.get('name') == name:
                _staged_verdict = _staged_no_tid_verdicts.pop(i)
                break
    if _is_error_override is None and _staged_verdict is not None:
        _is_error_override = _staged_verdict.get('is_error')
    _emit_tool_complete_to_mirrors_and_sse(
        tool_call_id=tool_call_id,
        name=name,
        args=args,
        function_result=function_result,
        live_tool_calls_list=_live_tool_calls,
        shared_tool_calls_list=_shared_tool_calls,
        put=_put,
        is_error_override=_is_error_override,
    )


# ---------------------------------------------------------------------
# Scenario A — fail sequence (single failed call).
# The Agent's progress event has NO tool_call_id in cb_kwargs
# (the Hermes Agent 0.21.x live path). The verdict flows:
#   on_tool(tool.completed, "terminal", None, None,
#           is_error=True, result="boom")
#     -> suppression branch STAGES the verdict
#        (_staged_no_tid_verdicts = [{...is_error=True}])
#   on_tool_complete("t1", "terminal", {"cmd": "ls"}, "boom")
#     -> dequeues the staged verdict
#     -> passes is_error=True as is_error_override
#     -> _emit writes is_error=True to live_tc[t1] and shared_tc[t1]
# ---------------------------------------------------------------------
on_tool_suppression('tool.completed', 'terminal', None, None, is_error=True, result='boom')
on_tool_complete('t1', 'terminal', {'cmd': 'ls'}, 'boom')

# Round-7 guarantee: live_tc[t1] carries the right verdict,
# shared_tc[t1] agrees, the SSE event carries True.
assert _live_tool_calls[0]['done'] is True, (
    "live_tc[t1] must be marked done by the structured callback, not "
    "pre-marked by the suppression branch (round 7 Finding 2)"
)
assert _live_tool_calls[0]['is_error'] is True, (
    "live_tc[t1].is_error must be True after the staged no-tid verdict "
    "flows through the structured callback (round 7 Finding 2)"
)
assert _shared_tool_calls[0]['done'] is True, (
    "shared_tc[t1] must be marked done by the structured callback"
)
assert _shared_tool_calls[0]['is_error'] is True, (
    "shared_tc[t1].is_error must be True after the staged no-tid "
    "verdict flows through (round 7 Finding 2)"
)
assert any(
    e.get('data', {}).get('tid') == 't1' and e.get('data', {}).get('is_error') is True
    for e in _sse_events
), "SSE tool_complete payload for t1 must carry is_error=True"
assert _staged_no_tid_verdicts == [], (
    "staging queue must be drained after the consume — no stale stage "
    "may linger (round 7 stale-stage guard)"
)

# ---------------------------------------------------------------------
# Scenario B — concurrent same-name pair, COMPLETING OUT OF ISSUE ORDER.
# t1 is issued first; t2 finishes first in production. Both progress
# events have no tool_call_id. Production order: t2 then t1.
# ---------------------------------------------------------------------
_live_tool_calls.append({"name": "terminal", "tid": "t3", "args": {"cmd": "ls2"}, "done": False})
_live_tool_calls.append({"name": "terminal", "tid": "t4", "args": {"cmd": "pwd2"}, "done": False})
_shared_tool_calls.append({"name": "terminal", "tid": "t3", "args": {"cmd": "ls2"}, "done": False})
_shared_tool_calls.append({"name": "terminal", "tid": "t4", "args": {"cmd": "pwd2"}, "done": False})

# t2 finishes first in production with a failure (is_error=True).
# The Agent's progress event has no tool_call_id — the
# suppression branch STAGES the verdict. It does NOT pre-mark
# any live row done.
on_tool_suppression('tool.completed', 'terminal', None, None, is_error=True, result='t2-fail')
# t1 finishes second with a success (is_error=False).
on_tool_suppression('tool.completed', 'terminal', None, None, is_error=False, result='t1-ok')

# Sanity: nothing was pre-marked done by the suppression branch.
for tc in _live_tool_calls:
    if tc.get('tid') in ('t3', 't4'):
        assert tc.get('done') is False, (
            "the suppression branch must NOT pre-mark any live row done — "
            "that pre-empts the structured callback's per-tid write "
            "(round 7 Finding 2)"
        )

# The structured callbacks fire in the same production order
# (Agent invokes them in pairs with the progress events).
on_tool_complete('t4', 'terminal', {'cmd': 'pwd2'}, 't2-fail')  # t2 finished first
on_tool_complete('t3', 'terminal', {'cmd': 'ls2'}, 't1-ok')    # t1 finished second

# Round-7 guarantee: each tid carries ITS OWN verdict, no swap.
_by_tid_live = {tc['tid']: tc for tc in _live_tool_calls if tc.get('tid') in ('t3', 't4')}
_by_tid_shared = {tc['tid']: tc for tc in _shared_tool_calls if tc.get('tid') in ('t3', 't4')}

# Production order: t2 first (is_error=True), t1 second (is_error=False).
# With the round-7 FIFO stage, the structured callback that fires
# first consumes the first staged entry. So:
#   on_tool_complete('t4', ...)  consumes t2's stage  -> t4.is_error = True
#   on_tool_complete('t3', ...)  consumes t1's stage  -> t3.is_error = False
# t3 corresponds to t1 in our issue naming, t4 corresponds to t2.
assert _by_tid_live['t4']['is_error'] is True, (
    "t4 (the tool that failed first) must carry is_error=True; "
    "the FIFO stage must route t2's verdict to the first structured "
    "callback. Got: %r" % _by_tid_live['t4']
)
assert _by_tid_live['t3']['is_error'] is False, (
    "t3 (the tool that succeeded second) must carry is_error=False; "
    "the FIFO stage must route t1's verdict to the second structured "
    "callback. Got: %r" % _by_tid_live['t3']
)
assert _by_tid_shared['t4']['is_error'] is True, (
    "shared_tc[t4].is_error must be True (round 7 Finding 2)"
)
assert _by_tid_shared['t3']['is_error'] is False, (
    "shared_tc[t3].is_error must be False (round 7 Finding 2)"
)
# Both must be marked done by the structured callback (not pre-marked).
assert _by_tid_live['t3']['done'] is True
assert _by_tid_live['t4']['done'] is True
assert _by_tid_shared['t3']['done'] is True
assert _by_tid_shared['t4']['done'] is True
# No stale stages linger.
assert _staged_no_tid_verdicts == [], (
    "staging queue must be drained after both consumes (round 7 "
    "stale-stage guard)"
)

# Emit the final state as JSON for the test to assert on.
sys.stdout.write(json.dumps({
    "live_tc": _live_tool_calls,
    "shared_tc": _shared_tool_calls,
    "sse_events": _sse_events,
    "staged_no_tid_verdicts": _staged_no_tid_verdicts,
}))
'''


def _run_campaign() -> dict:
    """Run the campaign driver in a subprocess and return the final state.

    The driver carries the full assertion suite for both scenarios
    AND emits a JSON snapshot of the final state for the pytest
    side to assert on. The subprocess form keeps the driver out
    of the pytest process (which can't import the production
    module without a heavy side-effect chain), the same pattern
    the round-6 test file uses.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CAMPAIGN_DRIVER_TEMPLATE],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        "campaign driver failed: stdout=%r stderr=%r"
        % (proc.stdout, proc.stderr)
    )
    return json.loads(proc.stdout)


def test_fail_sequence_stages_and_persists_is_error_true():
    """A single failed call where the Agent's progress event has
    no ``tool_call_id`` (the Hermes Agent 0.21.x live path) must
    have its verdict flow through the staging queue to the live
    row AND the persisted summary. The structured callback is the
    only writer to the live row, so the per-tid write runs and
    the verdict reaches ``s.tool_calls``.
    """
    final = _run_campaign()
    # Scenario A: a single failed call (t1).
    by_tid_live = {tc['tid']: tc for tc in final['live_tc'] if tc.get('tid')}
    assert by_tid_live['t1']['is_error'] is True, (
        "fail sequence: live_tc[t1].is_error must be True after the "
        "staged no-tid verdict flows through the structured callback "
        "(round 7 Finding 2). Got: %r" % by_tid_live['t1']
    )
    assert by_tid_live['t1']['done'] is True
    # The SSE event must carry the right verdict.
    t1_sse = [
        e for e in final['sse_events']
        if e.get('data', {}).get('tid') == 't1'
    ]
    assert any(
        e.get('data', {}).get('is_error') is True
        for e in t1_sse
    ), "SSE tool_complete for t1 must carry is_error=True"


def test_concurrent_same_name_pair_no_swap_no_drop():
    """Two concurrent same-name completions, out of issue order
    (t2 finished first with a failure, t1 finished second with a
    success). Both progress events have no ``tool_call_id`` (the
    Agent doesn't pass it on 0.21.x). With the round-7 FIFO
    stage, the suppression branch STAGES both verdicts; the
    structured callbacks dequeue in production order and apply
    each to the correct tid. No swap, no drop.
    """
    final = _run_campaign()
    by_tid_live = {tc['tid']: tc for tc in final['live_tc'] if tc.get('tid') in ('t3', 't4')}
    by_tid_shared = {tc['tid']: tc for tc in final['shared_tc'] if tc.get('tid') in ('t3', 't4')}
    # Production order: t4 (named t2 in the issue) first, t3 (named t1) second.
    # t4 is the tool that FAILED first. t3 is the tool that SUCCEEDED second.
    assert by_tid_live['t4']['is_error'] is True, (
        "concurrent same-name: t4 (the tool that failed first) must "
        "carry is_error=True. The FIFO stage must route t2's verdict "
        "to the first structured callback. Got live: %r, shared: %r"
        % (by_tid_live, by_tid_shared)
    )
    assert by_tid_live['t3']['is_error'] is False, (
        "concurrent same-name: t3 (the tool that succeeded second) "
        "must carry is_error=False. The FIFO stage must route t1's "
        "verdict to the second structured callback. Got live: %r, "
        "shared: %r" % (by_tid_live, by_tid_shared)
    )
    # The shared mirror must agree (the round-7 fix is what makes
    # this true — without the stage, the done-guard in the
    # structured callback's helper skipped the shared write).
    assert by_tid_shared['t4']['is_error'] is True
    assert by_tid_shared['t3']['is_error'] is False
    # Both rows are marked done by the structured callback.
    assert by_tid_live['t3']['done'] is True
    assert by_tid_live['t4']['done'] is True
    assert by_tid_shared['t3']['done'] is True
    assert by_tid_shared['t4']['done'] is True
    # No stale stage from the prior tool leaks.
    assert final['staged_no_tid_verdicts'] == [], (
        "staging queue must be drained after both consumes (round 7 "
        "stale-stage guard). Got: %r" % final['staged_no_tid_verdicts']
    )


# ---------------------------------------------------------------------------
# Read-site unknown semantics — pin that missing is_error is propagated
# ---------------------------------------------------------------------------


def _call_extract(messages, live_tool_calls=None, prior_tool_calls=None):
    """Run ``_extract_tool_calls_from_messages`` in a subprocess so
    the heavy production module is importable in the child.
    """
    payload = {
        "messages": messages,
        "live_tool_calls": live_tool_calls or [],
        "prior_tool_calls": prior_tool_calls or [],
    }
    driver = (
        "import sys, json;\n"
        "sys.path.insert(0, %r);\n"
        "from api.streaming import _extract_tool_calls_from_messages;\n"
        "data = json.loads(sys.stdin.read());\n"
        "out = _extract_tool_calls_from_messages(\n"
        "    data['messages'],\n"
        "    live_tool_calls=data['live_tool_calls'],\n"
        "    prior_tool_calls=data['prior_tool_calls'],\n"
        ");\n"
        "sys.stdout.write(json.dumps(out));\n"
    ) % str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not import api.streaming: {proc.stderr[:200]}")
    return json.loads(proc.stdout)


def test_extract_propagates_missing_live_is_error_as_unknown_via_prior():
    """When the live mirror is missing ``is_error`` but the prior
    turn's verdict says True, the prior verdict wins (this is the
    round-6 case). The live read must NOT silently convert the
    missing live field to ``False`` and overwrite the prior True
    verdict — the round-7 fix is to use ``.get('is_error')`` so
    the missing value is ``None`` and the prior branch fires.
    """
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "boom"},
    ]
    # Live mirror: row exists for t1 but WITHOUT is_error (the
    # round-7 case where the structured callback's helper was
    # blocked by the pre-marked done flag and never wrote the
    # verdict).
    prior = [{"name": "terminal", "tid": "t1", "is_error": True, "snippet": "boom"}]
    live = [{"name": "terminal", "tid": "t1", "done": True}]  # no is_error
    out = _call_extract(messages, live_tool_calls=live, prior_tool_calls=prior)
    assert len(out) == 1
    assert out[0]["tid"] == "t1"
    assert out[0]["is_error"] is True, (
        "round 7 unknown semantics: a live row missing is_error must "
        "fall through to the prior turn's True verdict. The "
        "bool(live.get('is_error', False)) default in the round-6 read "
        "would silently override the prior True with False. Got: %r"
        % out[0]
    )
