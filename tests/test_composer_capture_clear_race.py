"""Regression coverage for the composer capture-and-clear race (salvage of #4750).

Bug (salvaged from #4750, credit @harryazj): ``send()`` in ``static/messages.js``
captured the composer text early but only wiped the textarea
(``$('msg').value=''``) LATE — after ``await uploadPendingFiles()`` and the
forced-skill-directive await. ``send()`` is re-entrant: while a send is in
flight, a second invocation (e.g. an interrupt-mode / ``busy_input_mode`` drain,
or a fast second Enter) hits the ``if (_sendInProgress)`` guard at the top and
re-reads the LIVE composer via ``_composerTextWithPendingSelections()``. Because
the textarea still held the original text during the async upload window, the
re-entrant guard read the stale DOM and QUEUED the same message again ->
double-submit.

Fix: capture the composer value and clear the textarea IMMEDIATELY after capture,
BEFORE any await. Once cleared, a re-entrant read sees an empty composer and the
guard's ``if(_text && _targetSid)`` short-circuits -> no duplicate queue.

This module verifies BOTH:
  1. (static) the capture+wipe is ordered before the upload await in send(), and
  2. (behavioral, via node's ``vm``) the REAL re-entrancy guard block extracted
     from send() does NOT re-queue when the composer was already cleared, and
     WOULD have re-queued had the composer still held the stale text.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static ordering assertions
# ---------------------------------------------------------------------------

def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[brace + 1 : i - 1]


def test_composer_captured_and_cleared_before_upload_await():
    """The fix: capture + textarea wipe must run before uploadPendingFiles()."""
    body = _function_body(MESSAGES_JS, "send")

    capture_idx = body.index("const _submittedDraftTextForClear=$('msg').value||'';")
    # The wipe sits immediately after the capture.
    wipe_rel = body.index("$('msg').value='';autoResize();", capture_idx)
    assert wipe_rel - capture_idx < 120, "the textarea wipe must sit immediately after the capture"

    upload_idx = body.index("uploaded=await uploadPendingFiles(")
    directive_await_idx = body.index("const _directivePayload = await _pending.promise;")

    assert capture_idx < upload_idx, (
        "composer capture+clear must happen BEFORE the uploadPendingFiles() await "
        "(salvage of #4750 — closes the re-entrant double-send race)"
    )
    assert capture_idx < directive_await_idx, (
        "composer capture+clear must happen BEFORE the forced-skill-directive await too"
    )


def test_reentrancy_guard_reads_live_composer():
    """Sanity: the in-flight guard reads the LIVE composer (why the clear must be early)."""
    body = _function_body(MESSAGES_JS, "send")
    guard_idx = body.index("if (_sendInProgress) {")
    guard_block = body[guard_idx : body.index("_sendInProgress = true;", guard_idx)]
    assert "_composerTextWithPendingSelections().trim()" in guard_block, (
        "the re-entrant guard reads the live composer, so the composer must be "
        "cleared before the first await to avoid a stale double-send"
    )
    assert "queueSessionMessage(" in guard_block, (
        "the re-entrant guard queues the message it reads from the live composer"
    )


# ---------------------------------------------------------------------------
# Behavioral test — run the REAL re-entrancy guard against a cleared composer
# ---------------------------------------------------------------------------

def _extract_reentrancy_guard() -> str:
    """Slice the real `if (_sendInProgress) { ... }` guard block out of send()."""
    body = _function_body(MESSAGES_JS, "send")
    start = body.index("if (_sendInProgress) {")
    # Balance braces from the guard's opening brace to its close.
    brace = body.index("{", start)
    depth = 1
    i = brace + 1
    while depth and i < len(body):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
        i += 1
    return body[start:i]


def _run_reentrant_guard_in_node(composer_value: str):
    """Execute the REAL re-entrancy guard with a given live-composer value.

    Returns the list of queueSessionMessage calls the guard made. A non-empty
    list means the guard re-queued (double-submit); an empty list means it
    short-circuited on an empty composer (the fix's post-clear state).
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    helper = _function_body(MESSAGES_JS, "_composerTextWithPendingSelections")
    guard = _extract_reentrancy_guard()

    harness = textwrap.dedent(
        """
        const queued = [];
        const state = { input: { value: %(composer_value)s } };
        const $ = (id) => (id === 'msg' ? state.input : null);
        global.document = { getElementById: (id) => $(id) };
        // No pending inline selections in this scenario.
        const _pendingSelections = [];
        function _formatSelectedTextReplyQuote(t){ return t; }
        // Real helper the guard uses to read the live composer.
        function _composerTextWithPendingSelections(){%(helper)s}

        // Minimal in-flight state: a send is already running for sid-1.
        let _sendInProgress = true;
        let _sendInProgressSid = 'sid-1';
        const S = { session: { session_id: 'sid-1' }, pendingFiles: [], activeProfile: 'default' };

        // Stubs the guard branch touches.
        function _chatPayloadModelState(){ return { model: 'm', model_provider: 'p' }; }
        function queueSessionMessage(sid, payload){ queued.push({ sid, payload }); }
        function _clearComposerAfterQueuedSelectionSend(){ state.input.value = ''; }
        function _clearComposerDraft(){}
        function updateQueueBadge(){}
        function renderTray(){}
        function showToast(){}

        // Run the REAL guard block verbatim.
        (function () {
          %(guard)s
        })();

        console.log(JSON.stringify({ queued, composerAfter: state.input.value }));
        """
    ) % {
        "composer_value": json.dumps(composer_value),
        "helper": helper,
        "guard": guard,
    }

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_reentrant_send_does_not_requeue_after_composer_cleared():
    """With the fix, the composer is EMPTY when the re-entrant guard runs, so it
    must NOT queue a duplicate of the stale text."""
    out = _run_reentrant_guard_in_node("")
    assert out["queued"] == [], (
        "a re-entrant send must not re-queue anything once the composer has been "
        "cleared by the in-flight send (salvage of #4750)"
    )


def test_reentrant_send_would_double_submit_if_composer_not_cleared():
    """Sanity / bug demonstration: had the composer NOT been cleared before the
    async window (the pre-fix behaviour), the re-entrant guard reads the stale
    DOM text and queues a DUPLICATE — exactly the double-send this fix prevents."""
    out = _run_reentrant_guard_in_node("hello world")
    assert len(out["queued"]) == 1, (
        "the stale-composer scenario must reproduce the double-submit the fix removes"
    )
    assert out["queued"][0]["payload"]["text"] == "hello world"
    assert out["queued"][0]["sid"] == "sid-1"
