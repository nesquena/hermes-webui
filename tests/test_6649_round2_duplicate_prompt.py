"""#6649 review 2026-10-06 — the duplicate-prompt finding, with the REAL
pending resolver composed in.

The re-gate found one MUST-FIX and one SHOULD-FIX, both in
``static/sessions.js``:

**MUST-FIX — the current prompt is duplicated when the transcript already
holds it.** ``_assistantOnlySnapshotUserTurn()`` called
``getPendingSessionMessage(session, null)``. Passing ``null`` means the
resolver never looks at the loaded transcript (it falls back to
``session.messages``, which for the assistant-only recovery snapshot does not
hold it), so it always built a NEW pending user row. The only thing then
stopping the duplicate was ``_hasInflightTailUserDuplicate()``'s strict
equality, which needs matching ids or exact ``timestamp`` equality — and misses
the identity rules master already uses for the active turn in ``static/ui.js``:
the 1e-6 ``_PENDING_ACTIVE_TURN_TS_EPSILON``, the ``_active_turn_user`` marker
and the ``_active_turn_token``.

All three producer shapes below showed the prompt TWICE on that head and once
on master. The fix passes the inflight rows into the real resolver, so its own
identity rules (strict tail scan + ``_pendingActiveTurnUserMessage``) decide.

**SHOULD-FIX — the one-side-timestamp rule was broader than the court asked.**
``if(_aTs||_bTs) return false;`` treated an asymmetric stamp as "different
turns" even when no completed assistant answer sits between the two rows. A
transcript ending in an unstamped ``go`` plus a local stamped ``go`` with a live
tail produced two rows; master shows one. The rule now only splits the pair
when a completed answer separates them.

Unlike the round-1 file, these tests do NOT stub
``getPendingSessionMessage`` — the whole point of the finding is that the stub
could not see it. The real resolver is extracted from ``static/ui.js`` together
with the helpers it closes over, exactly as ``static/index.html`` loads them.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _node_env() -> dict:
    """Strip Hermes's injected NODE_CHANNEL_FD before spawning node.

    The agent runtime sets NODE_CHANNEL_FD in this process's environment; an
    inherited fd makes the child node abort with SIGABRT (rc -6) as it shuts
    down its IPC channel, which turns a passing harness into a crash. The env
    is copied so the parent's environment is untouched.
    """
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
    return env


def _run_node(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        check=False,
        env=_node_env(),
    )


def _function_decl(src: str, name: str):
    marker = f"function {name}("
    start = src.find(marker)
    if start == -1:
        return None
    brace = src.find("){", start)
    if brace == -1:
        return None
    brace += 1
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[start:i]


def _reattach_helper_src() -> str:
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    return SESSIONS_JS[start:end]


def _pending_resolver_src() -> str:
    """The REAL pending resolver and everything it closes over, from ui.js."""
    start = UI_JS.find("const _PENDING_ACTIVE_TURN_TS_EPSILON")
    assert start != -1, "the epsilon constant is gone from static/ui.js"
    end = UI_JS.find("async function checkInflightOnBoot", start)
    assert end != -1
    block = UI_JS[start:end]
    # The resolver closes over helpers that live in either file; collect the
    # ones that are plain function declarations. Anything declared another way
    # (a const arrow, a re-export) is provided by the inline fallbacks in the
    # harness, which mirror the production semantics for the shapes under test.
    for name, src in (
        ("_sameTranscriptMessage", SESSIONS_JS),
        ("_currentTailUserMessage", SESSIONS_JS),
        ("_pendingCurrentTailUserMessage", UI_JS),
        ("_isContextCompactionMessage", UI_JS),
        ("_isContextCompactionText", UI_JS),
        ("_messageTimestampSeconds", UI_JS),
        ("_activeTurnTokenMatches", UI_JS),
        ("_pendingActiveTurnUserMessage", UI_JS),
    ):
        decl = _function_decl(src, name)
        if decl is not None:
            block = decl + "\n" + block
    return block


_FALLBACK_HELPERS = """
// Minimal stand-ins for helpers that are not plain function declarations in
// the production files (const arrows / re-exports). They mirror the
// production semantics for the shapes these tests drive.
if (typeof msgContent !== 'function') {
  var msgContent = function (m) {
    if (!m) return '';
    if (Array.isArray(m.content)) {
      return m.content
        .filter(function (p) { return p && p.type === 'text'; })
        .map(function (p) { return String(p.text || ''); })
        .join('');
    }
    return m.content == null ? '' : String(m.content);
  };
}
"""


def _run(script: str) -> None:
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _harness(session_js: str, body: str) -> str:
    return f"""
const assert = require('assert');
{_reattach_helper_src()}
{_FALLBACK_HELPERS}
{_pending_resolver_src()}
{_function_decl(SESSIONS_JS, '_hasInflightTailUserDuplicate')}
{_function_decl(SESSIONS_JS, '_prepareRunningLiveTail')}
{session_js}
{body}
"""


# The three producer shapes the reviewer listed, driven through the REAL
# _prepareRunningLiveTail -> _assistantOnlySnapshotUserTurn composition.
_SHAPE_DRIFTED = """
  const session = {
    active_stream_id: 'stream-1',
    pending_started_at: 1000.0000005,
    pending_user_message: 'current work',
    messages: [
      { role: 'assistant', content: 'older answer' },
    ],
  };
  const inflight = [
    { role: 'assistant', content: 'partial', _live: true },
  ];
  const out = _prepareRunningLiveTail([
    { role: 'user', content: 'older', _ts: 900 },
    { role: 'assistant', content: 'older answer' },
    { role: 'user', content: 'current work', _ts: 1000.0000005 },
  ], inflight, session);
  const injected = inflight.filter(m => m.role === 'user' && m.content === 'current work').length;
  console.log(JSON.stringify({ out, injected }));
"""

_SHAPE_MARKER_NO_TS = """
  const session = {
    active_stream_id: 'stream-1',
    pending_started_at: 1000.5,
    pending_user_message: 'current work',
    messages: [
      { role: 'assistant', content: 'older answer' },
    ],
  };
  const inflight = [
    { role: 'assistant', content: 'partial', _live: true },
  ];
  const out = _prepareRunningLiveTail([
    { role: 'user', content: 'older', _ts: 900 },
    { role: 'assistant', content: 'older answer' },
    { role: 'user', content: 'current work' },
  ], inflight, session);
  const injected = inflight.filter(m => m.role === 'user' && m.content === 'current work').length;
  console.log(JSON.stringify({ out, injected }));
"""

_SHAPE_TRANSCRIPT_ENDS_AT_PROMPT = """
  const session = {
    active_stream_id: 'stream-1',
    pending_started_at: 1000.5,
    pending_user_message: 'current work',
    messages: [
      { role: 'assistant', content: 'older answer' },
    ],
  };
  const inflight = [
    { role: 'assistant', content: 'partial', _live: true },
  ];
  const out = _prepareRunningLiveTail([
    { role: 'user', content: 'older', _ts: 900 },
    { role: 'assistant', content: 'older answer' },
    { role: 'user', content: 'current work', _ts: 1000.5 },
  ], inflight, session);
  const injected = inflight.filter(m => m.role === 'user' && m.content === 'current work').length;
  console.log(JSON.stringify({ out, injected }));
"""


@pytest.mark.parametrize(
    "shape_name,body",
    [
        ("drifted timestamp + _active_turn_user + partial assistant", _SHAPE_DRIFTED),
        ("_active_turn_user with no timestamp + partial assistant", _SHAPE_MARKER_NO_TS),
        ("transcript ends at the current prompt", _SHAPE_TRANSCRIPT_ENDS_AT_PROMPT),
    ],
)
def test_the_current_prompt_is_not_duplicated(shape_name, body):
    """MUST-FIX: the prompt the transcript already holds must not be
    materialized a second time on the session-switch-during-stream path."""
    out = _run_node(_harness("", body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["injected"] == 0, (
        f"the current prompt was re-injected for shape {shape_name!r}: "
        f"{payload['injected']} rows injected into the inflight tail, but the "
        "loaded transcript already holds this prompt (master shows 1)"
    )
    assert payload["out"] is True, (
        "the live tail must still reconcile when the prompt is already "
        "present — the guard skips the recovery, it does not abandon the turn"
    )


def test_an_unstamped_repeat_with_a_live_tail_is_one_row():
    """SHOULD-FIX: a transcript ending in an UNSTAMPED `go` plus a local
    STAMPED `go` with a live tail must collapse to one row — the asymmetric
    stamp is not evidence of different turns when no completed answer sits
    between them."""
    body = """
  const existing = { role: 'user', content: 'go' };
  const candidate = { role: 'user', content: 'go', _ts: 1234.5 };
  const dup = _hasInflightTailUserDuplicate([existing], candidate);
  console.log(JSON.stringify({ dup }));
"""
    out = _run_node(_harness("", body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["dup"] is True, (
        "an unstamped repeat with a live tail must dedupe to one row; the "
        "asymmetric-stamp rule split it (#6649 SHOULD-FIX)"
    )


def test_an_unstamped_repeat_across_a_completed_answer_stays_two_rows():
    """Control: the narrowed rule must not open the leak it was closing — an
    old unstamped `go` separated from the current stamped `go` by a COMPLETED
    answer is still a different turn.

    The candidate is the row about to be materialized, so it is NOT part of
    the scanned list: the list holds the settled turn (unstamped `go` + its
    completed answer) and the candidate is the new stamped `go`.
    """
    body = """
  const existing = { role: 'user', content: 'go' };
  const candidate = { role: 'user', content: 'go', _ts: 1234.5 };
  const list = [
    { role: 'user', content: 'go' },
    { role: 'assistant', content: 'previous answer' },
  ];
  const dup = _hasInflightTailUserDuplicate(list, candidate);
  console.log(JSON.stringify({ dup }));
"""
    out = _run_node(_harness("", body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["dup"] is False, (
        "an old unstamped repeat across a completed answer must stay a "
        "separate turn, or the current live reply is hidden (#6649 control)"
    )


def test_a_whole_second_drift_still_does_not_match():
    """Control: the epsilon is precision-only. A rapid repeat ~1 s later is
    ambiguous and must NOT be deduped, on either side of the narrowing."""
    body = """
  const existing = { role: 'user', content: 'go', _ts: 1000.0 };
  const candidate = { role: 'user', content: 'go', _ts: 1001.0 };
  const dup = _hasInflightTailUserDuplicate([existing], candidate);
  console.log(JSON.stringify({ dup }));
"""
    out = _run_node(_harness("", body))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["dup"] is False, (
        "a whole-second drift must not match — a rapid double-send lands "
        "~1 s after the previous turn (#6649 control)"
    )
