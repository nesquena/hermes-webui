"""Regression coverage for the reasoning-effort session-switch race (#6809 review).

Blocker 1 (chip POST, ``static/ui.js``)
    The reasoning-option click handler POSTs ``/api/reasoning`` with the active
    session in the payload. The request is asynchronous, so a POST dispatched
    from session A can resolve AFTER the user switches to session B. Without a
    staleness guard the late response writes A's effort onto B's chip and
    poisons B's cached ``_currentReasoningEffort`` — the same session-confusion
    class the per-session storage fix removes, on the async-completion path.

The fix: snapshot the dispatch key, take a sequence number, and apply the result
(chip, cache, toast) only when ``_reasoningFetchSeq`` AND
``_reasoningEffortQuery()`` still match at completion time.

These tests drive the REAL production block — extracted verbatim from
``static/ui.js`` and executed under node — rather than re-implementing the guard
and asserting on the copy.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT.joinpath("static", "ui.js").read_text(encoding="utf-8")

NODE_TIMEOUT = 30


def _balanced_block(src: str, start: int) -> str:
    """Return src[start:] up to and including the close of its first ``{`` block."""
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
    marker = f"function {name}("
    start = src.index(marker)
    return _balanced_block(src, start)


def _reasoning_option_click_block() -> str:
    """Slice the real ``if(opt){ ... }`` POST block out of the click listener."""
    anchor = UI_JS.index("const payload=Object.assign({effort:effort},_reasoningEffortContext());")
    start = UI_JS.rindex("if(opt){", 0, anchor)
    return _balanced_block(UI_JS, start)


# The shared preamble: REAL _reasoningEffortContext / _reasoningEffortQuery /
# _reasoningDispatchIsCurrent from static/ui.js, plus the minimum DOM and app
# state the extracted blocks touch. A deferred `api()` lets each scenario choose
# whether the session changes before or after the response resolves.
_PREAMBLE = """
const calls = [];
const toasts = [];
const chipWrites = [];

let _profileTransitionReasoningContext = null;
const S = { session: { session_id: 'A' }, activeProfile: 'default' };
const $ = () => null;                       // no modelSelect in this harness
const _modelStateForSelect = () => ({});

%(context_fn)s
%(query_fn)s
let _reasoningFetchSeq = 0;
%(guard_fn)s

let _pendingResolve = null;
let _pendingReject = null;
function api(path, opts) {
  calls.push({ path, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return new Promise((res, rej) => { _pendingResolve = res; _pendingReject = rej; });
}
function showToast(msg) { toasts.push(msg); }
function _applyReasoningChip(eff, meta) { chipWrites.push(eff); }
function closeReasoningDropdown() {}
function removeThinking() {}
function renderMessages() {}
"""


def _preamble() -> str:
    return _PREAMBLE % {
        "context_fn": _function_source(UI_JS, "_reasoningEffortContext"),
        "query_fn": _function_source(UI_JS, "_reasoningEffortQuery"),
        "guard_fn": _function_source(UI_JS, "_reasoningDispatchIsCurrent"),
    }


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=NODE_TIMEOUT
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _run_chip_post(*, switch_to=None, fail=False) -> dict:
    """Dispatch the REAL chip-POST block, optionally switching session mid-flight."""
    script = textwrap.dedent(
        """
        %(preamble)s
        const opt = { dataset: { effort: 'high' } };
        const effort = opt.dataset.effort;

        // Run the REAL click-handler POST block verbatim.
        (function () {
          %(block)s
        })();

        const switchTo = %(switch_to)s;
        if (switchTo) S.session = { session_id: switchTo };

        %(settle)s

        setTimeout(() => {
          console.log(JSON.stringify({ calls, toasts, chipWrites, seq: _reasoningFetchSeq }));
        }, 0);
        """
    ) % {
        "preamble": _preamble(),
        "block": _reasoning_option_click_block(),
        "switch_to": json.dumps(switch_to),
        "settle": (
            "_pendingReject(new Error('boom'));"
            if fail
            else "_pendingResolve({ reasoning_effort: 'high' });"
        ),
    }
    return _run_node(script)


# ---------------------------------------------------------------------------
# Blocker 1 — chip POST
# ---------------------------------------------------------------------------


def test_chip_post_sends_the_active_session():
    """Baseline: the POST body carries the session so the write is session-scoped."""
    out = _run_chip_post()
    assert len(out["calls"]) == 1
    assert out["calls"][0]["path"] == "/api/reasoning"
    assert out["calls"][0]["body"]["session_id"] == "A"
    assert out["calls"][0]["body"]["effort"] == "high"


def test_chip_post_applies_when_session_is_unchanged():
    """Control: with no session switch the response must apply normally."""
    out = _run_chip_post()
    assert out["chipWrites"] == ["high"], "a current response must still update the chip"
    assert any("Reasoning effort set to high" in t for t in out["toasts"]), out["toasts"]


def test_chip_post_discards_response_after_session_switch():
    """A POST from session A must not write session B's chip when it lands late."""
    out = _run_chip_post(switch_to="B")
    assert out["chipWrites"] == [], (
        "a reasoning POST dispatched from session A resolved after the user "
        "switched to session B and wrote B's chip — session-switch race (#6809 "
        "review blocker 1)"
    )
    assert out["toasts"] == [], (
        "a stale reasoning response must be discarded SILENTLY; a toast for a "
        "session the user already left is itself misinformation"
    )


def test_chip_post_discards_failure_toast_after_session_switch():
    """The staleness guard covers the rejection path too, not just success."""
    out = _run_chip_post(switch_to="B", fail=True)
    assert out["toasts"] == [], (
        "a stale reasoning POST failure must not raise a toast on the session "
        "the user switched to"
    )


def test_chip_post_takes_a_sequence_number_before_dispatch():
    """The counter must advance on dispatch so a superseded POST is rejected."""
    out = _run_chip_post()
    assert out["seq"] == 1, (
        "the chip POST must increment _reasoningFetchSeq before the request so a "
        "later dispatch supersedes it even when the session key is identical"
    )


def test_chip_post_stale_by_sequence_alone_is_discarded():
    """Same session, two dispatches: only the newest may apply.

    The key comparison cannot catch this — both dispatches share one key — so
    this is the case that proves the sequence half of the guard is load-bearing.
    """
    script = textwrap.dedent(
        """
        %(preamble)s
        const opt = { dataset: { effort: 'high' } };
        const effort = opt.dataset.effort;

        // First dispatch (the one that will be superseded).
        (function () {
          %(block)s
        })();
        const firstResolve = _pendingResolve;

        // Second dispatch for the SAME session — same _reasoningEffortQuery() key.
        (function () {
          %(block)s
        })();

        // The stale first response lands last.
        firstResolve({ reasoning_effort: 'low' });

        setTimeout(() => {
          console.log(JSON.stringify({ calls, toasts, chipWrites, seq: _reasoningFetchSeq }));
        }, 0);
        """
    ) % {"preamble": _preamble(), "block": _reasoning_option_click_block()}
    out = _run_node(script)
    assert out["seq"] == 2
    assert out["chipWrites"] == [], (
        "the superseded first POST wrote the chip with its stale 'low' value; "
        "only the most recent dispatch may apply"
    )
    assert out["toasts"] == []
