"""Fail-closed coverage for the ``/reasoning <effort>`` ownership helpers (#6809 review round 4).

Blocker (``static/commands.js``, effort branch)
    The effort branch reached its ownership helpers through ``typeof`` fallbacks::

        const ctx=(typeof _reasoningEffortContext==='function')?_reasoningEffortContext():{};
        const key=(typeof _reasoningEffortQuery==='function')?_reasoningEffortQuery():'';
        const seq=(typeof _reasoningFetchSeq==='undefined')?null:++_reasoningFetchSeq;
        const current=function(){
          if(seq===null||typeof _reasoningDispatchIsCurrent!=='function') return true;
          ...
        };

    Every one of those fallbacks failed OPEN into the exact defect this change
    set exists to close. Without ``_reasoningEffortContext`` the command sent a
    bare ``{effort}`` and mutated the PROFILE-GLOBAL default. Without the
    sequence counter or the predicate, ``current()`` returned ``true``
    unconditionally and the command applied a superseded chip write and toast.

    ``static/index.html`` loads ``ui.js`` (line 1775) before ``commands.js``
    (line 1779), both ``defer``, so ``ui.js`` runs to completion before this
    handler can exist. No legitimate load order needs those fallbacks. They only
    covered dependency failure or bundle skew, and in both cases a higher-scope
    mutation is worse than no mutation.

    The prior slash-command suite injected all three helpers in every scenario,
    so all eight tests passed without ever entering a fallback. That is the gap
    these tests close.

Each test drives the REAL effort branch, sliced verbatim out of
``static/commands.js``, under node with ONE required helper omitted from the
environment. The assertion is that nothing mutates: no ``/api/reasoning``
request, no chip write, and no toast claiming an effort was saved.
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
INDEX_HTML = ROOT.joinpath("static", "index.html").read_text(encoding="utf-8")

NODE_TIMEOUT = 30

# The ui.js symbols the effort branch owns. Each is required; each is omitted in
# turn below. ``_reasoningFetchSeq`` is a mutable counter rather than a function,
# so it carries its own source line.
REQUIRED_HELPERS = (
    "_reasoningEffortContext",
    "_reasoningEffortQuery",
    "_reasoningDispatchIsCurrent",
    "_reasoningFetchSeq",
)


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
    return _balanced_block(src, src.index(f"function {name}("))


def _cmd_reasoning_effort_block() -> str:
    """Slice the real ``if(EFFORTS.includes(arg)){ ... }`` branch out of cmdReasoning()."""
    body = _function_source(COMMANDS_JS, "cmdReasoning")
    return _balanced_block(body, body.index("if(EFFORTS.includes(arg)){"))


def _helper_source(name: str) -> str:
    if name == "_reasoningFetchSeq":
        return "let _reasoningFetchSeq = 0;"
    return _function_source(UI_JS, name)


def _preamble(omit: str | None, *, stub_query: bool = False) -> str:
    """Recording harness plus the REAL ui.js helpers, minus ``omit``.

    An omitted name is simply never declared, so the production block's direct
    call raises a ReferenceError exactly as a failed ``ui.js`` load would.

    ``stub_query`` swaps the real ``_reasoningEffortQuery`` for a self-contained
    stub. The real one calls ``_reasoningEffortContext()`` internally
    (ui.js:5177), so with the context helper omitted the throw would come from
    the query line and would not prove the branch's own direct
    ``_reasoningEffortContext()`` call fails closed. The stub isolates that.
    """
    parts = []
    for name in REQUIRED_HELPERS:
        if name == omit:
            continue
        if name == "_reasoningEffortQuery" and stub_query:
            parts.append("function _reasoningEffortQuery(){ return '?session_id=A'; }")
        else:
            parts.append(_helper_source(name))
    return textwrap.dedent(
        """
        const calls = [];
        const toasts = [];
        const chipWrites = [];

        let _profileTransitionReasoningContext = null;
        const S = { session: { session_id: 'A' }, activeProfile: 'default' };
        const $ = () => null;                   // no modelSelect in this harness
        const _modelStateForSelect = () => ({});

        %(helpers)s

        let _pendingResolve = null;
        let _pendingReject = null;
        function api(path, opts) {
          calls.push({ path, body: opts && opts.body ? JSON.parse(opts.body) : null });
          return new Promise((res, rej) => { _pendingResolve = res; _pendingReject = rej; });
        }
        function showToast(msg) { toasts.push(String(msg)); }
        function _applyReasoningChip(eff, meta) { chipWrites.push(eff); }
        """
    ) % {"helpers": "\n".join(parts)}


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
    omit: str | None = None,
    session_id: str | None = "A",
    stub_query: bool = False,
) -> dict:
    """Run the REAL effort branch with ``omit`` absent from the environment.

    ``threw`` records whether the branch let an exception escape the handler.
    ``messages.js:1494`` calls the handler with no try/catch inside an async
    ``send()`` that ``ui.js:8418`` invokes unawaited, so an escaping throw
    strands the composer with no user-visible feedback.

    ``stub_query`` replaces ``_reasoningEffortQuery`` with a self-contained stub.
    The real one calls ``_reasoningEffortContext()`` internally (ui.js:5177), so
    omitting the context helper otherwise throws from the query line and masks
    whether the branch's OWN ``_reasoningEffortContext()`` call fails closed.
    """
    script = textwrap.dedent(
        """
        %(preamble)s
        const BRAIN = '\\uD83E\\uDDE0';
        const arg = 'high';
        const EFFORTS = ['none','minimal','low','medium','high','xhigh','max'];
        S.session = %(session)s;

        let threw = null;
        try {
          // The REAL cmdReasoning() effort branch, verbatim.
          (function () {
            %(block)s
          })();
        } catch (e) {
          threw = (e && e.name) || 'Error';
        }

        // Resolve any in-flight request so a fail-open POST gets the chance to
        // write the chip and toast. A fail-closed branch never dispatched one.
        if (_pendingResolve) _pendingResolve({ reasoning_effort: 'high' });

        setTimeout(() => {
          console.log(JSON.stringify({ calls, toasts, chipWrites, threw }));
        }, 0);
        """
    ) % {
        "preamble": _preamble(omit, stub_query=stub_query),
        "block": _cmd_reasoning_effort_block(),
        "session": json.dumps({"session_id": session_id} if session_id else None),
    }
    return _run_node(script)


# ── Fail-closed: one required helper missing, nothing may mutate ──────────────


@pytest.mark.parametrize("helper", REQUIRED_HELPERS)
def test_missing_ownership_helper_sends_no_request(helper):
    """A missing ownership helper must abort BEFORE any /api/reasoning write."""
    out = _dispatch(omit=helper)
    assert out["calls"] == [], (
        f"with {helper} unavailable the effort branch still POSTed "
        f"{out['calls']!r}. The old `typeof` fallback substituted an empty "
        "context, so this request carried no session_id and mutated the "
        "PROFILE-GLOBAL default — a higher-scope write than the user asked for, "
        "and the exact defect #6809 exists to close. Dependency failure must "
        "fail closed."
    )


@pytest.mark.parametrize("helper", REQUIRED_HELPERS)
def test_missing_ownership_helper_writes_no_chip(helper):
    """A missing ownership helper must never reach _applyReasoningChip()."""
    out = _dispatch(omit=helper)
    assert out["chipWrites"] == [], (
        f"with {helper} unavailable the effort branch wrote the chip "
        f"{out['chipWrites']!r}. With no sequence counter or predicate the old "
        "current() returned true unconditionally, so a superseded response "
        "still poisoned the chip."
    )


@pytest.mark.parametrize("helper", REQUIRED_HELPERS)
def test_missing_ownership_helper_claims_no_saved_effort(helper):
    """No toast may claim the effort was saved when ownership is unavailable."""
    out = _dispatch(omit=helper)
    liars = [t for t in out["toasts"] if "saved" in t or "Reasoning effort:" in t]
    assert liars == [], (
        f"with {helper} unavailable the effort branch raised {liars!r}. A toast "
        "asserting the effort applied here, when the write could not be scoped "
        "to this session, is misinformation."
    )


@pytest.mark.parametrize("helper", REQUIRED_HELPERS)
def test_missing_ownership_helper_reports_instead_of_throwing(helper):
    """The branch reports the internal failure rather than stranding the composer.

    ``messages.js:1494`` runs ``_cmd.fn(_parsedCmd.args)`` with no try/catch,
    inside an async ``send()`` that ``ui.js:8418`` calls unawaited. An escaping
    ReferenceError would skip the composer clear and the dropdown hide and
    surface only as an unhandled rejection — the user sees nothing at all. The
    ``/pet`` handler at ``messages.js:1505`` already establishes catch-and-report
    for a failing command handler.
    """
    out = _dispatch(omit=helper)
    assert out["threw"] is None, (
        f"with {helper} unavailable the effort branch threw {out['threw']}, "
        "which aborts send() before it clears the composer and hides the "
        "command dropdown"
    )
    assert any("unavailable" in t for t in out["toasts"]), (
        "the user must be told the command failed internally; silence looks "
        f"identical to success. toasts={out['toasts']!r}"
    )


# ── Fail-closed, context helper isolated from the query helper ────────────────
#
# The real _reasoningEffortQuery() calls _reasoningEffortContext() internally
# (ui.js:5177). With the context helper omitted, the throw therefore comes from
# the query line, which proves nothing about the branch's OWN direct
# _reasoningEffortContext() call. These tests stub the query helper so the only
# reference to the missing context helper is the production block's own.


def test_missing_context_helper_alone_sends_no_request():
    """The branch's own _reasoningEffortContext() call must fail closed.

    This is the fallback the maintainer named first: without it the branch POSTed
    a bare ``{effort}`` and mutated the PROFILE-GLOBAL default. The unscoped
    write is worse than no write, so a missing context helper must send nothing.
    """
    out = _dispatch(omit="_reasoningEffortContext", stub_query=True)
    assert out["calls"] == [], (
        "with only _reasoningEffortContext unavailable the effort branch POSTed "
        f"{out['calls']!r}. Under the old `typeof` fallback that request carried "
        "no session_id, so it silently mutated the profile-global default while "
        "the toast claimed the value applied to this session."
    )
    assert out["chipWrites"] == []
    assert not [t for t in out["toasts"] if "saved" in t]


def test_missing_context_helper_alone_reports_and_does_not_throw():
    """Isolated context failure still reports to the user without throwing."""
    out = _dispatch(omit="_reasoningEffortContext", stub_query=True)
    assert out["threw"] is None, out["threw"]
    assert any("unavailable" in t for t in out["toasts"]), out["toasts"]


# ── Positive controls: the fix must not break the working paths ───────────────


def test_all_helpers_present_still_writes_the_session():
    """Control: with every helper available the scoped write still happens."""
    out = _dispatch()
    assert out["threw"] is None
    assert len(out["calls"]) == 1, out["calls"]
    assert out["calls"][0]["body"]["session_id"] == "A"
    assert out["chipWrites"] == ["high"]


def test_no_active_session_still_writes_the_profile_global():
    """Control: no session means no override to scope to, so the global write is right.

    This must hold through ``_reasoningEffortContext()`` omitting ``session_id``
    on its own — never through a missing-helper fallback. That distinction is
    the whole point of making the helpers mandatory.
    """
    out = _dispatch(session_id=None)
    assert out["threw"] is None
    assert len(out["calls"]) == 1, out["calls"]
    body = out["calls"][0]["body"]
    assert body["effort"] == "high"
    assert "session_id" not in body, (
        "with no active session the command must keep writing the "
        f"profile-global default; got {body!r}"
    )
    assert out["chipWrites"] == ["high"]
    assert any("Reasoning effort: high" in t for t in out["toasts"]), out["toasts"]


# ── Source contract: the fallbacks must not come back ─────────────────────────


@pytest.mark.parametrize("helper", REQUIRED_HELPERS)
def test_effort_branch_calls_each_helper_directly(helper):
    """Pin the source contract: direct calls, no ``typeof`` fallback."""
    block = _cmd_reasoning_effort_block()
    assert f"typeof {helper}" not in block, (
        f"the effort branch guards {helper} with `typeof` again. That fallback "
        "fails OPEN: the substituted value drives either a profile-global "
        "mutation or an unconditional late chip write."
    )


def test_index_html_loads_ui_before_commands():
    """The mandatory-helper decision rests on this load order — pin it.

    Both tags are ``defer``, so execution follows document order and ``ui.js``
    completes before ``commands.js`` runs. If a future change reorders these or
    makes ``commands.js`` load independently, the direct calls above stop being
    safe and this test must fail loudly rather than let a real load-order bug
    reach users.
    """
    ui = INDEX_HTML.index('src="static/ui.js')
    commands = INDEX_HTML.index('src="static/commands.js')
    assert ui < commands, "ui.js must load before commands.js"
    for name in ("ui.js", "commands.js"):
        tag_start = INDEX_HTML.index(f'src="static/{name}')
        tag = INDEX_HTML[INDEX_HTML.rindex("<script", 0, tag_start):
                         INDEX_HTML.index(">", tag_start) + 1]
        assert "defer" in tag, f"{name} must stay deferred for ordered execution: {tag}"
        assert "async" not in tag, f"{name} must not be async — that breaks order: {tag}"
