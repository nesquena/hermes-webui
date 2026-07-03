"""Regression coverage for #5472 — preserve the composer draft when a send fails.

Bug: when a provider/background error aborts a send, ``send()`` in
``static/messages.js`` has already cleared the composer (``$('msg').value=''``)
and the persisted draft (``_clearComposerDraft``) at send time — before the turn
is durably accepted by ``/api/chat/start``. On a start-time throw the turn is
never persisted server-side, so the user loses the entire typed message and must
retype it.

Fix: a new ``_restoreComposerDraftAfterFailedSend(text, sid)`` helper puts the
typed text back into the composer, keeps staged files in ``S.pendingFiles``, and
re-persists the draft so it also survives a reload. It is called from the
``/api/chat/start`` throw handler's general-error branch.

This module verifies BOTH:
  1. (static) the helper exists and is wired into the send-error path with the
     right guards, and
  2. (behavioral, via node's ``vm``) the helper's actual branching logic —
     restores text, keeps a new in-progress draft from being clobbered, no-ops
     on an empty draft with no files, and re-persists via _saveComposerDraftNow.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static wiring assertions
# ---------------------------------------------------------------------------

def _helper_body() -> str:
    start = MESSAGES_JS.find("function _restoreComposerDraftAfterFailedSend(")
    assert start != -1, "the _restoreComposerDraftAfterFailedSend helper must exist"
    end = MESSAGES_JS.find("\nasync function send(", start)
    assert end != -1, "helper must be defined immediately before send()"
    return MESSAGES_JS[start:end]


def test_helper_exists_with_expected_guards():
    body = _helper_body()
    # Reads the live composer element.
    assert "const inp=$('msg')" in body
    # No-op when there is nothing to restore (no text AND no staged files).
    assert "if(!restore&&!hasFiles) return false;" in body
    # Never clobber a message the user began typing during the async window.
    assert "if(String(inp.value||'').trim()) return false;" in body
    # Restores the text into the composer and re-persists the draft.
    assert "inp.value=restore;" in body
    assert "_saveComposerDraftNow(sid, restore, S.pendingFiles?[...S.pendingFiles]:[])" in body


def test_helper_is_wired_into_chat_start_error_path():
    # The general-error branch of the /api/chat/start catch pushes an Error
    # assistant turn, then must call the restore helper with the original text.
    start = MESSAGES_JS.find("S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});")
    assert start != -1, "the /api/chat/start error branch must still push an Error turn"
    window = MESSAGES_JS[start:start + 800]
    assert "_restoreComposerDraftAfterFailedSend(text, activeSid);" in window, (
        "the send-error path must restore the composer draft after a failed send"
    )


def test_send_still_clears_composer_on_the_happy_path():
    # Guard against a regression that would leave the composer populated on a
    # successful send: the send path must still clear + clear-draft up front.
    assert "$('msg').value='';autoResize();" in MESSAGES_JS
    assert "if (activeSid && typeof _clearComposerDraft === 'function') _clearComposerDraft(activeSid);" in MESSAGES_JS


# ---------------------------------------------------------------------------
# Behavioral test — actually execute the helper in a JS sandbox
# ---------------------------------------------------------------------------

def _run_helper_in_node(draft_text, initial_input, pending_files):
    """Execute _restoreComposerDraftAfterFailedSend in a node vm sandbox and
    return the resulting state as a dict."""
    node = shutil.which("node")
    if not node:  # pragma: no cover - environment without node
        pytest.skip("node not available")

    # Extract the helper source verbatim so the test exercises the shipped code.
    body = _helper_body()

    harness = textwrap.dedent(
        """
        const state = {
          input: {value: %(initial_input)s, resized: false},
          pendingFiles: %(pending_files)s,
          saved: null,
          sendBtnUpdated: false,
        };
        const $ = (id) => (id === 'msg' ? state.input : null);
        const S = {pendingFiles: state.pendingFiles};
        function autoResize(){ state.input.resized = true; }
        function updateSendBtn(){ state.sendBtnUpdated = true; }
        function _saveComposerDraftNow(sid, text, files){ state.saved = {sid, text, files}; }

        %(helper)s

        const ret = _restoreComposerDraftAfterFailedSend(%(draft_text)s, 'sid-1');
        console.log(JSON.stringify({
          ret,
          inputValue: state.input.value,
          resized: state.input.resized,
          sendBtnUpdated: state.sendBtnUpdated,
          saved: state.saved,
        }));
        """
    ) % {
        "initial_input": json.dumps(initial_input),
        "pending_files": json.dumps(pending_files),
        "helper": body,
        "draft_text": json.dumps(draft_text),
    }

    proc = subprocess.run(
        [node, "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_restores_typed_text_into_empty_composer():
    out = _run_helper_in_node(draft_text="my long message", initial_input="", pending_files=[])
    assert out["ret"] is True
    assert out["inputValue"] == "my long message"
    assert out["resized"] is True
    assert out["sendBtnUpdated"] is True
    # Re-persisted so the restored draft survives a reload.
    assert out["saved"] == {"sid": "sid-1", "text": "my long message", "files": []}


def test_does_not_clobber_a_new_in_progress_draft():
    # The user started typing a NEW message during the async send window — the
    # restore must not overwrite it.
    out = _run_helper_in_node(
        draft_text="original failed message",
        initial_input="something new I am typing",
        pending_files=[],
    )
    assert out["ret"] is False
    assert out["inputValue"] == "something new I am typing"
    assert out["saved"] is None


def test_noop_when_nothing_to_restore():
    out = _run_helper_in_node(draft_text="", initial_input="", pending_files=[])
    assert out["ret"] is False
    assert out["saved"] is None


def test_restores_when_only_staged_files_remain():
    # Empty text but staged attachments — still a recoverable send, so re-persist.
    out = _run_helper_in_node(
        draft_text="",
        initial_input="",
        pending_files=[{"name": "a.pdf", "path": "/w/a.pdf"}],
    )
    assert out["ret"] is True
    assert out["saved"]["files"] == [{"name": "a.pdf", "path": "/w/a.pdf"}]
