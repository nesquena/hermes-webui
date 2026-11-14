"""#8643, behaviour 1 — Regenerate / Undo / Edit during a running turn.

While `S.busy`, the message action bar still painted Regenerate and Undo on the
last assistant row and Edit on the last user row, but every one of those
actions returns immediately on `S.busy` (`editMessage`, `regenerateResponse`,
`submitEdit`), so the tap did nothing and said nothing. On a phone the footer
is at full opacity without a hover (`.msg-foot .msg-actions{opacity:1}`), so a
button that cannot work was one tap away at all times.

The maintainer's ask: hide (or disable with a tooltip) Regenerate, Undo and
Edit while `S.busy`, and offer Regenerate only on the turn's final assistant
message.

Coverage, in the repo's usual split:

* ``tests/browser_8643_busy_message_actions.py`` drives the real page: a
  persisted turn with an interim answer and a final answer, loaded through the
  normal session path, then re-rendered with ``S.busy`` on and off — and then
  the review reproduction for behaviour 2, a regeneration whose
  ``/api/chat/start`` is refused: the production ``startRegeneration()`` with
  only the api-adapter boundary substituted, asserting the restored transcript
  gets its idle action bar back, that a forced session-HTML cache restore keeps
  it, and that a busy-written cache entry is never served to the idle view.
* a node harness runs the production ``startRegeneration()`` source under
  recording stubs, pinning the render-vs-busy ordering itself (the optimistic
  render paints the running-turn state; the restore render paints idle) with
  no browser involved.
* the source contract below pins the gate itself, so the busy flag and the
  turn-final reuse cannot be dropped without a browser being involved, plus
  the four render-vs-busy ordering sites and the busy term in the render cache
  signature.

If playwright or a browser is unavailable the driver exits 2 and this module
reports a skip rather than a pass — the gate must never go green having
checked nothing (the #7676 lesson).

Undo is only covered at the source: its ``undoBtn`` const in
``renderMessages()`` is never inserted into ``footHtml``, so no Undo button
reaches the DOM in any state (the busy gate on it is still pinned below, so it
is correct the day someone wires it up).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
DRIVER = Path(__file__).resolve().parent / "browser_8643_busy_message_actions.py"


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


# The action-bar block of renderMessages(), from the display-only flag to the
# copy button that follows the three gated ones.
ACTION_BAR = _between(UI_JS, "const isDisplayOnlyRow=!!(m&&m._displayOnlyRow);", "const copyBtn  =")


def _run_driver() -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
    env.setdefault("TMPDIR", "/tmp")
    return subprocess.run(
        [sys.executable, str(DRIVER)],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_regenerate_undo_and_edit_are_gated_on_busy():
    """All three read the same busy flag the Worklog fold reads."""
    assert "const isBusy=!!S.busy;" in ACTION_BAR, (
        "renderMessages() must derive one busy flag for the action bar"
    )
    assert "const undoBtn  = (isLastAssistant&&!isBusy) ?" in ACTION_BAR
    assert "const editBtn  = isEditableUser ? ((isBusy||isDisplayOnlyRow) ? '' :" in ACTION_BAR
    # Every one of the three, and nothing else, is behind it.
    for name in ("undoBtn", "editBtn", "retryBtn"):
        line = next(
            raw for raw in ACTION_BAR.splitlines() if raw.strip().startswith(f"const {name}")
        )
        assert "isBusy" in line, f"{name} is not gated on the running turn: {line.strip()}"


def test_regenerate_reuses_the_existing_turn_final_predicate():
    """Regenerate is offered on the turn's final assistant message only.

    ``isTurnFinalAssistant`` is the line the Worklog fold
    (``messageBelongsInWorklog``) and the compression-recovery payload already
    draw between an interim answer and the turn's final one; the button must
    not re-derive "final" of its own.
    """
    assert (
        "const isTurnFinalAssistant=!isUser&&(!nextRendered||!nextRendered.m||nextRendered.m.role!=='assistant');"
        in UI_JS
    ), "the shared turn-final predicate moved; re-point this gate at the new one"
    assert "const retryBtn = (isLastAssistant&&isTurnFinalAssistant&&!isBusy) ?" in ACTION_BAR
    assert "isTurnFinalAssistant" not in next(
        raw for raw in ACTION_BAR.splitlines() if raw.strip().startswith("const undoBtn")
    ), "Undo must stay on the row it had, not move with the turn-final rule"


def test_busy_turn_paints_no_dead_action_buttons():
    """The real page, with a real running turn's rows on screen."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed")
    result = _run_driver()
    if result.returncode == 2:
        pytest.skip(f"browser gate could not run: {result.stderr.strip()[-500:]}")
    assert result.returncode == 0, (
        f"driver failed ({result.returncode})\n"
        f"---- stdout ----\n{result.stdout}\n---- stderr ----\n{result.stderr}"
    )
    assert "RUNNING-TURN ACTION BUTTONS GATE PASSED" in result.stdout
    for line in (
        "idle — Regenerate only on the turn's final assistant row",
        "busy — Regenerate and Edit are gone while the turn runs",
        "idle again — both buttons return once the turn is over",
    ):
        assert f"OK  {line}" in result.stdout, line


def test_rejected_regeneration_restores_the_action_buttons():
    """A regeneration refused at /api/chat/start must leave the idle action bar.

    The review reproduction: the production ``startRegeneration()`` restores the
    transcript after the start is refused, but its restore render used to run
    while ``S.busy`` was still true, so the busy predicate omitted Edit and
    Regenerate — and ``setBusy(false)`` only updates the composer, never those
    actions. The busy-derived HTML also landed in the session cache without
    busy in its signature, so a cache restore kept the buttons missing on an
    idle transcript. The driver substitutes ONLY the ``/api/chat/start``
    API-adapter boundary; everything else is the real page and the real
    production function.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed")
    result = _run_driver()
    if result.returncode == 2:
        pytest.skip(f"browser gate could not run: {result.stderr.strip()[-500:]}")
    assert result.returncode == 0, (
        f"driver failed ({result.returncode})\n"
        f"---- stdout ----\n{result.stdout}\n---- stderr ----\n{result.stderr}"
    )
    assert "RUNNING-TURN ACTION BUTTONS GATE PASSED" in result.stdout
    for line in (
        "rejected regeneration — Edit and Regenerate return once"
        " /api/chat/start refuses",
        "cache restore after a refused regeneration keeps both buttons",
        "a busy-written cache entry is never served to the idle transcript",
        "the refused regeneration paints no buttons while the turn runs",
    ):
        assert f"OK  {line}" in result.stdout, line


# ── render-vs-busy ordering, driven through the real startRegeneration() ────
# The browser gate above proves the DOM outcome; this harness proves the
# ORDERING with no browser involved, by running the production
# startRegeneration() source under stubs that record the busy flag every
# renderMessages() actually saw. It discriminates the bug on its own: before
# the fix the restore render ran at S.busy===true.
NODE_START_REGENERATION_SCRIPT = """
const result = {renderBusy: [], busy: [], thinking: 0};
let S = {session: {session_id: 's1', regeneration_revision: 'rev-1'}, busy: false,
         messages: [{role: 'user', content: 'prompt'}, {role: 'assistant', content: 'answer'}]};
const INFLIGHT = {};
function renderMessages() { result.renderBusy.push(S.busy); }
function setBusy(v) { result.busy.push(v); S.busy = v; }
function ensureLiveWorklogShell() { result.thinking++; }
function appendThinking() { result.thinking++; }
function removeThinking() { result.thinking--; }
function setComposerStatus() {}
function clearInflightState() {}
function markInflight() {}
function saveInflightState() {}
function showLiveRunStatus() {}
function updateSendBtn() {}
function renderSessionList() {}
function applySessionTitleUpdate() {}
function attachLiveStream() {}
async function api(_path, _options) { throw new Error('typed rejection'); }
%(function_source)s
(async () => {
  try { await startRegeneration('s1', 'rev-1'); }
  catch (error) { result.error = error.message; }
  result.messages = S.messages.map(m => m.role);
  process.stdout.write(JSON.stringify(result));
})();
"""


def test_rejected_regeneration_renders_the_restored_transcript_idle():
    """The restore render must run at idle; the optimistic render at busy.

    Both directions of the ordering are pinned here, because both are real
    state transitions the production code makes: entering the regeneration
    (idle → busy) and restoring it after a refused start (busy → idle). A
    render that paints the OLD state's action bar leaves dead buttons on
    screen until some unrelated re-render, which is the review's P2.
    """
    import json
    import subprocess

    source = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    start = source.index("async function startRegeneration(")
    end = source.index("\nconst LIVE_STREAMS=", start)
    script = NODE_START_REGENERATION_SCRIPT % {"function_source": source[start:end]}
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=str(REPO),
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )
    result = json.loads(completed.stdout)
    assert result["error"] == "typed rejection", "the harness rejection never fired"
    assert result["messages"] == ["user", "assistant"], (
        "the refused regeneration must restore the full local transcript"
    )
    assert result["renderBusy"], "startRegeneration() never rendered"
    assert result["renderBusy"][0] is True, (
        "the optimistic regeneration render ran at S.busy="
        f"{result['renderBusy'][0]!r}: the pending user row keeps a dead Edit"
        " until the stream attaches"
    )
    assert result["renderBusy"][-1] is False, (
        "the restored transcript was painted at S.busy="
        f"{result['renderBusy'][-1]!r}: the busy predicate omits Edit and"
        " Regenerate from an idle transcript (the review's P2)"
    )


def test_entry_and_terminal_renders_follow_the_busy_transition():
    """Every render-vs-busy ordering site paints the state it is entering.

    The reviewer asked for the other entry/terminal orderings to be checked,
    not only the regeneration catch. send() and startRegeneration() each have
    an entry (idle → busy) and a failure terminal (busy → idle); in all four
    the flag must move before the rebuild, so availability follows the real
    state transition instead of the previous render.
    """
    messages = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    regeneration = _between(
        messages, "async function startRegeneration(", "\nconst LIVE_STREAMS="
    )
    assert "setBusy(true);renderMessages();" in regeneration, (
        "startRegeneration() must set busy before its optimistic render"
    )
    assert "removeThinking();setBusy(false);renderMessages();setComposerStatus('');" in regeneration, (
        "the refused-regeneration restore must clear busy before rebuilding"
    )
    assert "removeThinking();renderMessages();setBusy(false);" not in regeneration
    send_body = _between(messages, "async function send(", "\nasync function startRegeneration(")
    assert "S.messages.push(userMsg);setBusy(true);renderMessages();" in send_body, (
        "send() must set busy before its optimistic render"
    )
    assert (
        "_queueDrainSid=activeSid;setBusy(false);renderMessages();"
        "setComposerStatus(`Error: ${errMsg}`);" in send_body
    ), "the send() failure terminal must clear busy before rebuilding the error row"
    assert "_queueDrainSid=activeSid;renderMessages();setBusy(false);" not in send_body


def test_render_cache_signature_covers_the_busy_flag():
    """The session-HTML cache must distinguish busy from idle renders.

    The action bar is derived from S.busy, so the same messages render a
    different footer while a turn runs. A cache signature that ignores busy
    lets a busy-painted entry satisfy an idle read (and vice versa), which is
    how the missing buttons survived a session restore.
    """
    signature = _between(
        UI_JS, "function _messageRenderCacheSignature(){", "function _clipCliToolSnippet("
    )
    assert "add('busy');add(S.busy?1:0);" in signature, (
        "the render cache signature must fold the busy flag in"
    )
