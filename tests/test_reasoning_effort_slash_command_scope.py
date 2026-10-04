"""Regression coverage for the ``/reasoning <effort>`` slash command scope (#6809 review).

Blocker 2 (``static/commands.js``)
    The composer chip POST carries ``_reasoningEffortContext()`` so its write
    lands on the active session. The ``/reasoning high`` slash command did not,
    so it wrote the profile-global default while its toast claimed the new value
    applied here. A session holding a persisted override kept its old effort and
    the UI lied about it.

    The command also needs the chip's staleness guard: a POST dispatched from
    session A can resolve after the user switches to session B, and applying it
    there poisons B's chip and cache.

    One behaviour must NOT change. With no active session there is no override
    to scope to, so the command keeps writing the profile-global default. That
    is what ``/reasoning`` does before the first chat starts.

These tests drive the REAL ``cmdReasoning()`` effort branch — extracted verbatim
from ``static/commands.js`` and executed under node against the REAL
``_reasoningEffortContext`` / ``_reasoningEffortQuery`` /
``_reasoningDispatchIsCurrent`` helpers from ``static/ui.js`` — rather than
re-implementing the guard and asserting on the copy.
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


def _cmd_reasoning_effort_block() -> str:
    """Slice the real ``if(EFFORTS.includes(arg)){ ... }`` branch out of cmdReasoning()."""
    body = _function_source(COMMANDS_JS, "cmdReasoning")
    start = body.index("if(EFFORTS.includes(arg)){")
    return _balanced_block(body, start)


# Shared preamble: the REAL context/query/guard helpers from static/ui.js plus the
# minimum app state the extracted branch touches. A deferred api() lets each
# scenario choose whether the session changes before the response resolves.
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


def _run_cmd_reasoning(*, session_id: str | None = "A", switch_to=None, fail=False) -> dict:
    """Dispatch the REAL cmdReasoning() effort branch."""
    script = textwrap.dedent(
        """
        %(preamble)s
        const BRAIN = '\\uD83E\\uDDE0';
        const arg = 'high';
        const EFFORTS = ['none','minimal','low','medium','high','xhigh','max'];
        S.session = %(session)s;

        // Run the REAL cmdReasoning() effort branch verbatim.
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
        "block": _cmd_reasoning_effort_block(),
        "session": json.dumps({"session_id": session_id} if session_id else None),
        "switch_to": json.dumps(switch_to),
        "settle": (
            "_pendingReject(new Error('boom'));"
            if fail
            else "_pendingResolve({ reasoning_effort: 'high' });"
        ),
    }
    return _run_node(script)


def test_slash_reasoning_writes_the_active_session():
    """``/reasoning high`` must scope the write to the session, not the profile."""
    out = _run_cmd_reasoning(session_id="A")
    assert len(out["calls"]) == 1
    assert out["calls"][0]["path"] == "/api/reasoning"
    body = out["calls"][0]["body"]
    assert body["effort"] == "high"
    assert body.get("session_id") == "A", (
        "/reasoning <effort> POSTed without a session_id, so it wrote the "
        "profile-global default while the toast claimed the value applied to "
        "this session (#6809 review blocker 2)"
    )


def test_slash_reasoning_with_no_session_still_writes_the_global():
    """No active session means no override to scope to — the global write is correct."""
    out = _run_cmd_reasoning(session_id=None)
    assert len(out["calls"]) == 1
    body = out["calls"][0]["body"]
    assert body["effort"] == "high"
    assert "session_id" not in body, (
        "with no active session the command must keep writing the profile-global "
        "default; inventing a session_id here would break /reasoning before the "
        "first chat starts"
    )


def test_slash_reasoning_with_no_session_still_toasts_and_updates_the_chip():
    """The no-session path must stay fully functional, not just correctly scoped."""
    out = _run_cmd_reasoning(session_id=None)
    assert out["chipWrites"] == ["high"], (
        "the no-session /reasoning path must still update the chip after the "
        "global write succeeds"
    )
    assert any("Reasoning effort: high" in t for t in out["toasts"]), out["toasts"]


def test_slash_reasoning_applies_when_session_is_unchanged():
    """Control: an unswitched ``/reasoning high`` still toasts and updates the chip."""
    out = _run_cmd_reasoning(session_id="A")
    assert out["chipWrites"] == ["high"]
    assert any("Reasoning effort: high" in t for t in out["toasts"]), out["toasts"]


def test_slash_reasoning_discards_response_after_session_switch():
    """The chip POST's staleness guard must cover the slash-command path too."""
    out = _run_cmd_reasoning(session_id="A", switch_to="B")
    assert out["chipWrites"] == [], (
        "a /reasoning POST dispatched from session A resolved after a switch to "
        "session B and wrote B's chip"
    )
    assert out["toasts"] == [], (
        "a stale /reasoning response must be discarded silently; a toast naming "
        "an effort for a session the user already left is misinformation"
    )


def test_slash_reasoning_discards_failure_toast_after_session_switch():
    """The guard covers the rejection path, not just success."""
    out = _run_cmd_reasoning(session_id="A", switch_to="B", fail=True)
    assert out["toasts"] == [], (
        "a stale /reasoning failure must not raise a toast on the session the "
        "user switched to"
    )


def test_slash_reasoning_takes_a_sequence_number_before_dispatch():
    """The command shares the chip's dispatch counter so either can supersede the other."""
    out = _run_cmd_reasoning(session_id="A")
    assert out["seq"] == 1, (
        "/reasoning must increment the shared _reasoningFetchSeq before its "
        "request so a later dispatch supersedes it even when the key is identical"
    )


def test_slash_reasoning_source_carries_the_context_helper():
    """Static backstop: the effort branch must build its body from the shared helper."""
    block = _cmd_reasoning_effort_block()
    assert "_reasoningEffortContext()" in block, (
        "the /reasoning effort branch must include _reasoningEffortContext() in "
        "its POST body so the write lands on the session, not the profile"
    )
