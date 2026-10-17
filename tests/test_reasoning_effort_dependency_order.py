"""Ordering and unusable-dependency coverage for ``/reasoning <effort>`` (#6809 round 5).

Blocker from the 3 September re-gate at ``d8a9c33c``
    The effort branch resolved its ownership dependencies in an order that
    mutated shared state before it could fail::

        key = _reasoningEffortQuery();
        seq = ++_reasoningFetchSeq;          # <-- mutates FIRST
        const isCurrent = _reasoningDispatchIsCurrent;   # <-- may throw AFTER

    ``_reasoningFetchSeq`` is a SHARED dispatch generation. ``fetchReasoningChip()``
    and ``syncReasoningChip()`` in ``ui.js`` compare their captured sequence
    against it. Advancing it and then failing supersedes a cold in-flight chip
    fetch that captured the old value. That fetch returns early at its
    stale-generation check while ``_lastReasoningFetchKey`` stays set, so a
    same-key ``syncReasoningChip()`` short-circuits instead of retrying, and chip
    hydration is stranded. Merely invoking an unavailable command caused that.

    Two related shapes were also unguarded. A ``_reasoningDispatchIsCurrent``
    that EXISTS but is ``undefined`` or non-callable let the assignment succeed,
    so the POST went out and only the response callback threw, after server
    state changed. A ``_reasoningFetchSeq`` holding ``undefined`` produced ``NaN``
    from the prefix increment, which throws nothing and makes every later
    generation comparison false.

What the sibling module already covers, and what it missed
    ``tests/test_reasoning_effort_slash_command_fail_closed.py`` omits each
    helper entirely, which only exercises the UNDECLARED shape. It never reports
    or asserts the counter, and it assigns ``_pendingReject`` without ever
    invoking it. Its 25 passing cases therefore missed the mutation-order and
    rejection shapes above.

Method
    Every test drives the REAL effort branch, sliced verbatim out of
    ``static/commands.js``, under node. Dependencies are installed as genuine
    globals so ``++_reasoningFetchSeq`` performs a real global write the harness
    can observe. The counter is reported on every run, and asserted.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT.joinpath("static", "ui.js").read_text(encoding="utf-8")
COMMANDS_JS = ROOT.joinpath("static", "commands.js").read_text(encoding="utf-8")

NODE_TIMEOUT = 30
START_SEQ = 7


def _balanced_block(src: str, start: int) -> str:
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "unbalanced braces while slicing block"
    return src[start:i]


def _function_source(src: str, name: str) -> str:
    return _balanced_block(src, src.index(f"function {name}("))


def _effort_block() -> str:
    body = _function_source(COMMANDS_JS, "cmdReasoning")
    return _balanced_block(body, body.index("if(EFFORTS.includes(arg)){"))


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=NODE_TIMEOUT
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _dispatch(
    *,
    predicate: str = "real",
    counter: str = "int",
    settle: str = "resolve",
    cold_fetch: bool = False,
) -> dict:
    """Drive the real effort branch and report every observable effect.

    ``predicate``: ``real`` | ``undefined`` | ``noncallable`` | ``absent``
    ``counter``:   ``int`` | ``undefined`` | ``nan`` | ``absent`` | ``float``
    ``settle``:    ``resolve`` | ``reject`` | ``none``
    ``cold_fetch``: also start a cold chip fetch that captured the PRIOR
    generation, then report whether it was superseded.
    """
    if predicate == "real":
        pred_src = _function_source(UI_JS, "_reasoningDispatchIsCurrent")
    elif predicate == "undefined":
        pred_src = "var _reasoningDispatchIsCurrent = undefined;"
    elif predicate == "noncallable":
        pred_src = "var _reasoningDispatchIsCurrent = 42;"
    elif predicate == "absent":
        pred_src = ""
    else:  # pragma: no cover
        raise AssertionError(predicate)

    counter_src = {
        "int": f"var _reasoningFetchSeq = {START_SEQ};",
        "undefined": "var _reasoningFetchSeq = undefined;",
        "nan": "var _reasoningFetchSeq = NaN;",
        "float": "var _reasoningFetchSeq = 1.5;",
        "absent": "",
    }[counter]

    script = textwrap.dedent(
        """
        const calls = [];
        const toasts = [];
        const chipWrites = [];

        // Minimal ui.js environment the real helpers read.
        let _profileTransitionReasoningContext = null;
        const S = { session: { session_id: 'A' }, activeProfile: 'default' };
        const $ = () => null;
        const _modelStateForSelect = () => ({});
        let _lastReasoningFetchKey = null;

        %(counter)s
        %(predicate)s
        function _reasoningEffortContext(){ return { session_id: 'A' }; }
        function _reasoningEffortQuery(){ return '?session_id=A'; }

        let _pendingResolve = null;
        let _pendingReject = null;
        function api(path, opts) {
          calls.push({ path, body: opts && opts.body ? JSON.parse(opts.body) : null });
          return new Promise((res, rej) => { _pendingResolve = res; _pendingReject = rej; });
        }
        function showToast(msg) { toasts.push(String(msg)); }
        function _applyReasoningChip(eff, meta) { chipWrites.push(eff); }

        // A cold chip fetch that captured the generation BEFORE the command ran.
        // If the command advances the generation and then fails, this fetch is
        // wrongly superseded and chip hydration strands.
        let coldSeq = null, coldSuperseded = null;
        if (%(cold)s) {
          coldSeq = (typeof _reasoningFetchSeq === 'number') ? _reasoningFetchSeq : null;
          _lastReasoningFetchKey = '?session_id=A';
        }

        const BRAIN = '\\uD83E\\uDDE0';
        const arg = 'high';
        const EFFORTS = ['none','minimal','low','medium','high','xhigh','max'];

        let threw = null;
        try {
          // The REAL cmdReasoning() effort branch, verbatim.
          (function () {
            %(block)s
          })();
        } catch (e) {
          threw = (e && e.name) || 'Error';
        }

        const seqAfter = (typeof _reasoningFetchSeq === 'undefined')
          ? '<undeclared>'
          : (Number.isNaN(_reasoningFetchSeq) ? 'NaN' : String(_reasoningFetchSeq));

        if (coldSeq !== null) {
          // The cold fetch is superseded when the live generation moved past the
          // value it captured.
          coldSuperseded = (typeof _reasoningFetchSeq === 'number')
            && !Number.isNaN(_reasoningFetchSeq)
            && _reasoningFetchSeq !== coldSeq;
        }

        let settleThrew = null;
        try {
          if ('%(settle)s' === 'resolve' && _pendingResolve) {
            _pendingResolve({ reasoning_effort: 'high' });
          } else if ('%(settle)s' === 'reject' && _pendingReject) {
            _pendingReject(new Error('network down'));
          }
        } catch (e) { settleThrew = (e && e.name) || 'Error'; }

        setTimeout(() => {
          console.log(JSON.stringify({
            calls, toasts, chipWrites, threw, seqAfter,
            coldSeq, coldSuperseded, settleThrew,
            lastKey: _lastReasoningFetchKey,
          }));
        }, 0);
        """
    ) % {
        "counter": counter_src,
        "predicate": pred_src,
        "block": _effort_block(),
        "settle": settle,
        "cold": "true" if cold_fetch else "false",
    }
    return _run_node(script)


# ── The harness itself must be trustworthy ───────────────────────────────────


def test_control_advances_the_counter_exactly_once():
    """Positive control, and proof the harness can OBSERVE the counter.

    An earlier version of this harness passed dependencies as function
    parameters. Parameters are local bindings, so ``++_reasoningFetchSeq``
    mutated a local and the harness never saw the write. Every counter reading
    was meaningless. This test fails if that regresses.
    """
    out = _dispatch()
    assert out["threw"] is None, out
    assert out["seqAfter"] == str(START_SEQ + 1), (
        "the harness must observe the real global increment; "
        f"expected {START_SEQ + 1}, got {out['seqAfter']}"
    )
    assert len(out["calls"]) == 1, out["calls"]
    assert out["chipWrites"] == ["high"]


# ── Claim 1: no dependency failure may advance the generation ────────────────

_UNUSABLE_PREDICATES = ["absent", "undefined", "noncallable"]


@pytest.mark.parametrize("predicate", _UNUSABLE_PREDICATES)
def test_unusable_predicate_does_not_advance_the_generation(predicate):
    """The shared dispatch generation must be untouched when the branch fails.

    This is the maintainer's exact finding. Advancing it and then failing
    supersedes a cold in-flight chip fetch, which then returns early while
    ``_lastReasoningFetchKey`` stays set, so a same-key ``syncReasoningChip()``
    short-circuits and chip hydration strands.
    """
    out = _dispatch(predicate=predicate)
    assert out["seqAfter"] == str(START_SEQ), (
        f"with a {predicate} predicate the branch advanced the shared generation "
        f"{START_SEQ} -> {out['seqAfter']}. A cold in-flight chip fetch that "
        "captured the old value is now wrongly superseded."
    )


@pytest.mark.parametrize("predicate", _UNUSABLE_PREDICATES)
def test_unusable_predicate_publishes_nothing(predicate):
    """No API mutation, no chip write, and no toast claiming a saved effort."""
    out = _dispatch(predicate=predicate)
    assert out["calls"] == [], (
        f"a {predicate} predicate still POSTed {out['calls']!r}. Assignment "
        "succeeding is not evidence the helper works: the old code discovered "
        "that inside .then(), after the request had already changed server state."
    )
    assert out["chipWrites"] == [], out["chipWrites"]
    liars = [t for t in out["toasts"] if "saved" in t or "Reasoning effort:" in t]
    assert liars == [], liars


@pytest.mark.parametrize("predicate", _UNUSABLE_PREDICATES)
def test_unusable_predicate_reports_instead_of_throwing(predicate):
    out = _dispatch(predicate=predicate)
    assert out["threw"] is None, (
        f"a {predicate} predicate threw {out['threw']} out of the handler, which "
        "aborts send() before it clears the composer"
    )
    assert any("unavailable" in t for t in out["toasts"]), out["toasts"]


# ── Claim 3: an unusable counter must not dispatch ───────────────────────────


@pytest.mark.parametrize("counter", ["undefined", "nan", "absent", "float"])
def test_unusable_counter_publishes_nothing(counter):
    """``undefined`` and ``NaN`` never throw on prefix increment.

    ``++undefined`` yields ``NaN``, so the old code dispatched with a generation
    that compares false forever. ``float`` is rejected for the same reason: a
    non-integer generation cannot be compared for equality reliably.
    """
    out = _dispatch(counter=counter)
    assert out["calls"] == [], (
        f"a {counter} counter still POSTed {out['calls']!r} with seq="
        f"{out['seqAfter']}"
    )
    assert out["chipWrites"] == [], out["chipWrites"]
    assert out["threw"] is None, out["threw"]
    assert any("unavailable" in t for t in out["toasts"]), out["toasts"]


def test_undefined_counter_is_not_silently_turned_into_nan():
    """Pin the NaN shape explicitly: it must fail closed, not dispatch."""
    out = _dispatch(counter="undefined")
    assert out["seqAfter"] != "NaN", (
        "the branch incremented an undefined counter into NaN. Prefix increment "
        "throws nothing here, so every later generation comparison would be "
        "false and no response would ever be applied."
    )
    assert out["calls"] == []


# ── Claim 1, end to end: the cold-fetch interleaving ────────────────────────


def test_failed_command_does_not_supersede_a_cold_chip_fetch():
    """Production-shaped interleaving, which is the point of the whole ordering fix.

    A cold ``fetchReasoningChip()`` captures generation N. The user then invokes
    ``/reasoning high`` while the predicate is unavailable. If the command
    advances the generation to N+1 before failing, the cold fetch's response is
    discarded at its stale-generation check while ``_lastReasoningFetchKey``
    remains set, so a same-key ``syncReasoningChip()`` short-circuits and the
    chip never hydrates.
    """
    out = _dispatch(predicate="absent", cold_fetch=True)
    assert out["coldSeq"] == START_SEQ, out
    assert out["coldSuperseded"] is False, (
        "invoking an unavailable /reasoning command superseded a cold in-flight "
        f"chip fetch (generation {out['coldSeq']} -> {out['seqAfter']}). That "
        "strands chip hydration for a command that changed nothing."
    )
    assert out["calls"] == []


def test_successful_command_does_supersede_a_cold_chip_fetch():
    """Discriminating control: a SUCCESSFUL command must still supersede.

    Without this, the assertion above passes trivially for any implementation
    that never advances the generation at all.
    """
    out = _dispatch(cold_fetch=True)
    assert out["coldSeq"] == START_SEQ
    assert out["coldSuperseded"] is True, (
        "a successful command must advance the generation so a cold fetch's "
        "late response cannot overwrite the fresh value"
    )
    assert len(out["calls"]) == 1


# ── The rejection path, which the sibling module never invoked ───────────────


def test_rejected_request_reports_failure_and_writes_no_chip():
    """``_pendingReject`` is now actually invoked.

    The sibling module assigns ``_pendingReject`` and never calls it, so the
    ``.catch()`` arm of the dispatch was untested. A rejection must report the
    failure and must not write the chip.
    """
    out = _dispatch(settle="reject")
    assert out["settleThrew"] is None, out["settleThrew"]
    assert len(out["calls"]) == 1, out["calls"]
    assert out["chipWrites"] == [], (
        "a failed request must not write the chip; "
        f"got {out['chipWrites']!r}"
    )
    assert any("Failed to set effort" in t for t in out["toasts"]), out["toasts"]
    assert not [t for t in out["toasts"] if "saved" in t], out["toasts"]


def test_unsettled_request_writes_nothing_yet():
    """Control: with the promise left pending, neither arm has run."""
    out = _dispatch(settle="none")
    assert len(out["calls"]) == 1
    assert out["chipWrites"] == []
    assert not [t for t in out["toasts"] if "saved" in t]


# ── Source contract: the ordering must not regress ───────────────────────────


def test_every_dependency_is_validated_before_the_counter_mutates():
    """Pin the ORDER in source: the increment is the last thing to happen.

    A future edit that moves ``++_reasoningFetchSeq`` back above the predicate
    check reintroduces the exact defect, and every behavioural test above would
    still pass for the UNDECLARED shape because that one throws earlier.
    """
    block = _effort_block()
    inc = block.index("++_reasoningFetchSeq")
    pred_check = block.index("typeof isCurrent!=='function'")
    counter_check = block.index("Number.isSafeInteger")
    assert pred_check < inc, (
        "the predicate must be validated BEFORE the shared generation is "
        "incremented"
    )
    assert counter_check < inc, (
        "the counter must be validated BEFORE it is incremented"
    )


def test_counter_is_read_before_it_is_written():
    """The validation must read the PRIOR value, not the incremented one."""
    block = _effort_block()
    read = block.index("const prevSeq=_reasoningFetchSeq")
    inc = block.index("++_reasoningFetchSeq")
    assert read < inc, "the counter must be read before it is incremented"
