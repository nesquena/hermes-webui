"""Regression test for the messages.js stale-stream `source` scope bug.

Surfaced by the scope_undef_gate (scripts/scope_undef_gate.py) during the #3696
review: `_bailOutOfTerminalEventsFromStaleStream` is declared at brace depth 2
inside `attachLiveStream` (whose params are activeSid/streamId/uploaded/options —
no `source`), yet its body called `_closeSource(source)` referencing a `source`
that is NOT in its lexical scope. All call sites live inside `_wireSSE(source)`,
but JS scoping is lexical not dynamic, so when the helper runs it would throw
`ReferenceError: source is not defined` on the stale-stream terminal-event path
(`_ownsActiveStreamOrBackground()` false → user back in an active session whose
old stream finalizes late). Same class as #3696.

Fix: thread `source` as an explicit parameter
(`_bailOutOfTerminalEventsFromStaleStream(source)`) and pass it at every call
site, instead of relying on (broken) scope resolution. This test locks that the
declaration takes the param and no bare-call site remains.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

MESSAGES_JS = (Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_bailout_helper_takes_source_param():
    """The helper must declare a `source` parameter (not rely on an out-of-scope
    closure variable)."""
    m = re.search(r"function\s+_bailOutOfTerminalEventsFromStaleStream\s*\(([^)]*)\)", MESSAGES_JS)
    assert m, "_bailOutOfTerminalEventsFromStaleStream declaration not found"
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    assert "source" in params, (
        "_bailOutOfTerminalEventsFromStaleStream must take `source` as a parameter — "
        "it calls _closeSource(source) but is declared inside attachLiveStream (no "
        "`source` in scope), so a bare reference throws ReferenceError on the "
        f"stale-stream path. Current params: {params}"
    )


def test_no_bare_bailout_call_sites_remain():
    """Every call site must pass `source` — a bare `()` call would leave the helper's
    `source` undefined again."""
    bare = re.findall(r"_bailOutOfTerminalEventsFromStaleStream\(\s*\)", MESSAGES_JS)
    assert not bare, (
        f"Found {len(bare)} bare _bailOutOfTerminalEventsFromStaleStream() call site(s) "
        "with no argument — each must pass `source` so the helper can close the right "
        "stream. A bare call reintroduces the ReferenceError."
    )


def test_bailout_call_sites_pass_source():
    """Positive check: the call sites pass `source`."""
    calls = re.findall(r"_bailOutOfTerminalEventsFromStaleStream\(\s*source\s*\)", MESSAGES_JS)
    assert len(calls) >= 5, (
        f"expected >=5 call sites passing `source`, found {len(calls)} — if the SSE "
        "terminal-event wiring changed, update this count, but every call must still "
        "pass source."
    )


def test_stale_terminal_bailout_is_a_behavioral_noop_for_newer_owner():
    assert NODE, "node is required for stale-stream behavior tests"
    helper = _function_source("_bailOutOfTerminalEventsFromStaleStream")
    script = f"""
const activeSid='session-new';
const streamId='stream-old';
const INFLIGHT={{'session-new':{{streamId:'stream-new'}}}};
const LIVE_STREAMS={{'session-new':{{streamId:'stream-new',source:{{id:'new'}}}}}};
let cleanups=0;
let closes=[];
function _scheduleAnchorRegistryCleanup(){{ cleanups+=1; }}
function _closeSource(source){{ closes.push(source.id); }}
function _ownsActiveStreamOrBackground(){{ return false; }}
{helper}
const result=_bailOutOfTerminalEventsFromStaleStream({{id:'old'}});
console.log(JSON.stringify({{
  result,
  cleanups,
  closes,
  inflight:INFLIGHT['session-new'],
  live:LIVE_STREAMS['session-new'],
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "result": True,
        "cleanups": 0,
        "closes": ["old"],
        "inflight": {"streamId": "stream-new"},
        "live": {"streamId": "stream-new", "source": {"id": "new"}},
    }


def _function_source(name: str) -> str:
    start = MESSAGES_JS.index(f"function {name}")
    brace = MESSAGES_JS.index("{", start)
    depth = 0
    for idx in range(brace, len(MESSAGES_JS)):
        if MESSAGES_JS[idx] == "{":
            depth += 1
        elif MESSAGES_JS[idx] == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[start : idx + 1]
    raise AssertionError(f"{name} did not close")
