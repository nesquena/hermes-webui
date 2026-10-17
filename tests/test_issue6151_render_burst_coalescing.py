"""Regression coverage for issue #6151: renderMessages() full-rebuild on every update.

Issue #6151 identified two distinct O(N) costs in the transcript renderer:

  (a) default-OFF DOM virtualization  -> long sessions hold one DOM node per
      message. Already addressed by the open opt-out PR (#6155); NOT covered
      here, and deliberately not duplicated.

  (b) `renderMessages()` is an unconditional wipe-and-rebuild
      (`inner.innerHTML=''` then reconstruct every row) reached from 113
      call sites. Most of those sites fire in BURSTS for a single logical
      event -- one tool completion calls renderMessages() from ~10 places
      (messages.js 5860/6138/6143/6344/6374/6523/6587/6633/8148/8594), and the
      stream settle adds more. Every one of those is a full O(N) rebuild, so a
      single finished tool can re-render the entire transcript a dozen times.

This file covers (b): redundant renders within the same task are coalesced
into one rebuild.

Why a task-scoped coalesce is provably safe (and why it is scoped to the task
rather than tracked as long-lived "is the DOM clean" state): JS is
single-threaded, so two renderMessages() calls in the same synchronous task
cannot have any DOM mutation happen between them. If the render inputs
(signature + render window key) are identical, the rebuild would produce the
same DOM, so skipping the second one cannot change what the reader sees. Any
mutation from a DIFFERENT task necessarily lands after the coalesce window
closes, and long-lived dirty-state would instead require every out-of-band
transcript mutator to remember to invalidate it -- a missed site is a stale
transcript, which is exactly the failure mode this avoids.

The streaming regime is excluded outright: while a turn is live, tool cards
and activity scenes are appended into #msgInner directly (appendLiveToolCard,
renderLiveAnchorActivityScene, appendLiveCompressionCard) without touching
S.messages/S.toolCalls, so the signature cannot see them.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

COALESCE_FUNCS = [
    "_messageRenderCoalesceCanSkip",
    "_noteMessageRenderCompleted",
    "_resetMessageRenderCoalesce",
]


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _function_source(js: str, name: str) -> str:
    """Extract one top-level function body, string/comment aware."""
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", js)
    assert match, f"{name}() not found in static/ui.js"
    brace = js.find("{", match.end())
    assert brace != -1, f"{name}() has no body"
    depth = 1
    i = brace + 1
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while i < len(js) and depth:
        ch = js[i]
        nxt = js[i + 1] if i + 1 < len(js) else ""
        if in_line_comment:
            in_line_comment = ch != "\n"
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in "'\"`":
            in_string = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return js[match.start():i]


COALESCE_STATE_VARS = [
    "_messageRenderCoalesceSid",
    "_messageRenderCoalesceWindowKey",
    "_messageRenderCoalesceSignature",
    "_messageRenderCoalesceResetTimer",
]


def _coalesce_harness(body: str) -> str:
    """Drive the REAL coalesce helpers from static/ui.js under node."""
    js = UI_JS.read_text(encoding="utf-8")
    sources = "\n".join(_function_source(js, name) for name in COALESCE_FUNCS)
    # Pull the REAL module-level state declarations out of static/ui.js so the
    # harness exercises the shipped variables, not test-local stand-ins.
    declarations = []
    for var in COALESCE_STATE_VARS:
        match = re.search(rf"^let {re.escape(var)}\s*=\s*[^;]+;", js, re.M)
        assert match, f"{var} declaration not found in static/ui.js"
        declarations.append(match.group(0))
    return (
        "\n".join(declarations)
        + "\n"
        + sources
        + "\n"
        + """
const S = {session:{session_id:'s1'}, messages:[], toolCalls:[]};
const INFLIGHT = {};
// Minimal DOM stub: the guard reads S.session / S.activeStreamId only.
"""
        + body
    )


# ── #6151 (b): the coalescing contract ──────────────────────────────────────


def test_redundant_render_in_same_task_is_coalesced():
    """Second identical renderMessages() in one task must not rebuild."""
    out = _run_node(_coalesce_harness("""
_resetMessageRenderCoalesce();
const first = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
_noteMessageRenderCompleted('s1', 'k1', 'sig1');
const second = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
console.log(JSON.stringify({first, second}));
"""))
    result = json.loads(out)
    assert result["first"] is False, "the first render of a window must always rebuild"
    assert result["second"] is True, "identical re-render in the same task must be skipped"


def test_changed_signature_rebuilds_and_changed_window_key_rebuilds():
    """Any change to the render inputs must rebuild -- never serve a stale DOM."""
    out = _run_node(_coalesce_harness("""
_resetMessageRenderCoalesce();
_noteMessageRenderCompleted('s1', 'k1', 'sig1');
const sameSigNewKey = _messageRenderCoalesceCanSkip('s1', 'k2', 'sig1');
const newSigSameKey = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig2');
const newSession    = _messageRenderCoalesceCanSkip('s2', 'k1', 'sig1');
console.log(JSON.stringify({sameSigNewKey, newSigSameKey, newSession}));
"""))
    result = json.loads(out)
    assert result["sameSigNewKey"] is False, "a new render window (scroll) must rebuild"
    assert result["newSigSameKey"] is False, "changed message/tool state must rebuild"
    assert result["newSession"] is False, "a different session must rebuild"


def test_never_skips_while_a_stream_is_live_or_a_turn_is_inflight():
    """Live tool cards are appended straight into #msgInner without touching
    S.messages, so coalescing must be disabled for the streaming regime."""
    out = _run_node(_coalesce_harness("""
_resetMessageRenderCoalesce();
_noteMessageRenderCompleted('s1', 'k1', 'sig1');
const idle = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
INFLIGHT.s1 = {};
const inflight = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
delete INFLIGHT.s1;
S.activeStreamId = 'st1';
const streaming = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
S.activeStreamId = null;
S.busy = true;
const busy = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
S.busy = false;
console.log(JSON.stringify({idle, inflight, streaming, busy}));
"""))
    result = json.loads(out)
    assert result["idle"] is True
    assert result["inflight"] is False, "INFLIGHT[sid] renders must always rebuild"
    assert result["streaming"] is False, "an active stream renders must always rebuild"
    assert result["busy"] is False, "a busy session renders must always rebuild"


def test_coalesce_window_closes_so_a_later_task_always_rebuilds():
    """The window must not survive into a later task (scheduled reset runs)."""
    out = _run_node(_coalesce_harness("""
_resetMessageRenderCoalesce();
_noteMessageRenderCompleted('s1', 'k1', 'sig1');
const immediate = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
Promise.resolve().then(() => {}); // let any queued microtask settle
setTimeout(() => {
  const afterTask = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
  console.log(JSON.stringify({immediate, afterTask}));
}, 0);
"""))
    result = json.loads(out)
    assert result["immediate"] is True
    assert result["afterTask"] is False, "a later task must rebuild, not serve coalesced state"


def test_reset_clears_coalesce_state():
    out = _run_node(_coalesce_harness("""
_noteMessageRenderCompleted('s1', 'k1', 'sig1');
_resetMessageRenderCoalesce();
const afterReset = _messageRenderCoalesceCanSkip('s1', 'k1', 'sig1');
console.log(JSON.stringify({afterReset}));
"""))
    assert json.loads(out)["afterReset"] is False


def test_empty_signature_never_skips():
    """A missing/empty signature is treated as 'unknown' -> always rebuild."""
    out = _run_node(_coalesce_harness("""
_resetMessageRenderCoalesce();
_noteMessageRenderCompleted('s1', 'k1', '');
const blank = _messageRenderCoalesceCanSkip('s1', 'k1', '');
const blankKey = _messageRenderCoalesceCanSkip('', 'k1', 'sig1');
console.log(JSON.stringify({blank, blankKey}));
"""))
    result = json.loads(out)
    assert result["blank"] is False
    assert result["blankKey"] is False


# ── wiring: the guard is actually consulted, and state is recorded ──────────


def test_render_messages_consults_and_records_the_coalesce_guard():
    js = UI_JS.read_text(encoding="utf-8")
    start = js.index("function renderMessages(options)")
    body = js[start:]

    # The skip decision happens BEFORE the destructive wipe.
    guard = body.index("_messageRenderCoalesceCanSkip(")
    wipe = body.index("inner.innerHTML='';")
    assert guard < wipe, "the guard must be consulted before the wipe-and-rebuild"

    # The completed render is recorded after the rebuild so the next
    # same-task call can coalesce against it.
    note = body.index("_noteMessageRenderCompleted(")
    assert note > wipe, "coalesce state must be recorded after the rebuild completes"


def test_session_switch_resets_coalesce_state():
    """A session switch must never coalesce against the previous session's DOM."""
    js = UI_JS.read_text(encoding="utf-8")
    reset_fn = _function_source(js, "_resetMessageRenderWindow")
    assert "_resetMessageRenderCoalesce()" in reset_fn, (
        "switching sessions must reset the coalesce window"
    )


def test_transcript_ui_reveal_invalidates_coalesce_state():
    """_revealTransparentEarlierSteps mutates the rendered DOM in place
    (outside renderMessages) to show capped tool steps. It already invalidates
    the HTML cache for the same reason; the coalesce guard must not treat a
    following identical-signature render as redundant."""
    js = UI_JS.read_text(encoding="utf-8")
    fn = _function_source(js, "_revealTransparentEarlierSteps")
    assert "_resetMessageRenderCoalesce()" in fn, (
        "an out-of-band transcript mutation must reset the coalesce window"
    )


def test_new_helpers_are_typeof_guarded_for_standalone_render_harnesses():
    """renderMessages() is executed by test harnesses that extract the function
    on its own (no surrounding module scope), so it must never call a helper
    unguarded -- an unguarded call is a hard ReferenceError there, which is how
    the existing `_isKeepSettledWorklogOpenArmed` calls are guarded too.

    This pins the invariant for the helpers #6151 adds: each one is fail-open
    guarded so a harness lacking it renders normally instead of throwing.
    """
    js = UI_JS.read_text(encoding="utf-8")
    body = _function_source(js, "renderMessages")
    # Only the two helpers renderMessages() actually CALLS. _resetMessageRenderCoalesce
    # is invoked from _resetMessageRenderWindow / _revealTransparentEarlierSteps, which
    # renderMessages never calls.
    for helper in ("_messageRenderCoalesceCanSkip", "_noteMessageRenderCompleted"):
        assert f"typeof {helper}==='function'" in body, (
            f"renderMessages() must typeof-guard {helper}() so standalone "
            "renderMessages harnesses do not ReferenceError"
        )


def test_coalesce_helpers_are_defined_at_module_scope():
    """The helpers the guard references must actually exist in ui.js, not only
    in the tests -- otherwise the typeof guard would silently disable #6151."""
    js = UI_JS.read_text(encoding="utf-8")
    for helper in COALESCE_FUNCS:
        assert f"function {helper}(" in js, f"{helper}() is not defined in ui.js"