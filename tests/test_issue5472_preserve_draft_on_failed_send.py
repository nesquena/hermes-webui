"""Regression coverage for #5472 — preserve the composer draft when a send fails.

Bug: when a provider/background error aborts a send, ``send()`` in
``static/messages.js`` has already cleared the composer (``$('msg').value=''``),
the persisted draft (``_clearComposerDraft``), AND the staged files
(``uploadPendingFiles()`` sets ``S.pendingFiles=[]``) at send time — before the
turn is durably accepted by ``/api/chat/start``. On a start-time throw the turn
is never persisted, so the user loses the entire typed message + attachments and
must retype.

Fix: ``send()`` snapshots the ORIGINAL typed text + staged files BEFORE slash
rewrites (/moa, bundles) mutate the payload and BEFORE the upload drains
``S.pendingFiles``. On a start-time throw,
``_restoreComposerDraftAfterFailedSend(text, files, sid)`` restores that exact
snapshot, re-stages the files, and re-persists the draft. It is session-aware
(never pollutes a different session's visible composer) and never clobbers a new
message the user began typing during the async window.

This module verifies BOTH:
  1. (static) the snapshot capture + wiring into the send-error path, and
  2. (behavioral, via node's ``vm``) the helper's branching logic, including the
     three Codex-caught edges: original-vs-mutated payload, dropped attachments,
     and cross-session composer pollution.
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


def test_helper_has_new_three_arg_signature_and_guards():
    body = _helper_body()
    assert "function _restoreComposerDraftAfterFailedSend(draftText, filesSnapshot, sid)" in body
    # No-op when there is nothing to restore (no text AND no staged files).
    assert "if(!restore&&!files.length) return false;" in body
    # Session-aware: never mutate a different session's visible composer.
    assert "const visibleSid=(S.session&&S.session.session_id)||null;" in body
    assert "if(sid&&visibleSid&&sid!==visibleSid) return false;" in body
    # Never clobber a message the user began typing during the async window.
    assert "if(String(inp.value||'').trim()) return false;" in body
    # Restores text and re-stages files.
    assert "inp.value=restore;" in body
    assert "S.pendingFiles=files;" in body


def test_send_captures_immutable_snapshot_before_rewrites_and_upload():
    # The snapshot must be captured right after the post-flush trim, BEFORE the
    # busy branch / slash-command rewrites and BEFORE uploadPendingFiles().
    snap_idx = MESSAGES_JS.find("const _failedSendDraftText=text;")
    files_idx = MESSAGES_JS.find(
        "const _failedSendFilesSnapshot=Array.isArray(S.pendingFiles)?[...S.pendingFiles]:[];"
    )
    moa_idx = MESSAGES_JS.find("text=_moaArgs;")
    upload_idx = MESSAGES_JS.find("uploaded=await uploadPendingFiles();")
    assert snap_idx != -1 and files_idx != -1, "send() must snapshot text + files for #5472"
    assert moa_idx != -1 and upload_idx != -1
    # Snapshot happens before both the /moa rewrite and the upload drain.
    assert snap_idx < moa_idx, "text snapshot must precede the /moa rewrite of `text`"
    assert files_idx < upload_idx, "files snapshot must precede uploadPendingFiles() drain"


def test_error_branch_restores_original_snapshot_not_mutated_payload():
    start = MESSAGES_JS.find("S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});")
    assert start != -1, "the /api/chat/start error branch must still push an Error turn"
    window = MESSAGES_JS[start:start + 900]
    assert (
        "_restoreComposerDraftAfterFailedSend(_failedSendDraftText, _failedSendFilesSnapshot, activeSid);"
        in window
    ), "the send-error path must restore the ORIGINAL captured snapshot (not `text`)"


def test_send_still_clears_composer_on_the_happy_path():
    assert "$('msg').value='';autoResize();" in MESSAGES_JS
    assert "if (activeSid && typeof _clearComposerDraft === 'function') _clearComposerDraft(activeSid);" in MESSAGES_JS


# ---------------------------------------------------------------------------
# Behavioral test — actually execute the helper in a JS sandbox
# ---------------------------------------------------------------------------

def _run_helper_in_node(draft_text, files_snapshot, initial_input, visible_sid, sid="sid-1"):
    """Execute _restoreComposerDraftAfterFailedSend in a node vm sandbox."""
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    body = _helper_body()
    harness = textwrap.dedent(
        """
        const state = {
          input: {value: %(initial_input)s, resized: false},
          pendingFiles: [],
          trayRendered: false,
          saved: null,
          sendBtnUpdated: false,
        };
        const $ = (id) => (id === 'msg' ? state.input : null);
        const S = {pendingFiles: state.pendingFiles, session: %(session)s};
        function autoResize(){ state.input.resized = true; }
        function updateSendBtn(){ state.sendBtnUpdated = true; }
        function renderTray(){ state.trayRendered = true; }
        function _saveComposerDraftNow(sid, text, files){ state.saved = {sid, text, files}; }

        %(helper)s

        const ret = _restoreComposerDraftAfterFailedSend(%(draft_text)s, %(files)s, %(sid)s);
        console.log(JSON.stringify({
          ret,
          inputValue: state.input.value,
          resized: state.input.resized,
          sendBtnUpdated: state.sendBtnUpdated,
          trayRendered: state.trayRendered,
          pendingFiles: S.pendingFiles,
          saved: state.saved,
        }));
        """
    ) % {
        "initial_input": json.dumps(initial_input),
        "session": json.dumps({"session_id": visible_sid} if visible_sid else None),
        "helper": body,
        "draft_text": json.dumps(draft_text),
        "files": json.dumps(files_snapshot),
        "sid": json.dumps(sid),
    }
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_restores_typed_text_into_empty_composer():
    out = _run_helper_in_node("my long message", [], "", visible_sid="sid-1")
    assert out["ret"] is True
    assert out["inputValue"] == "my long message"
    assert out["resized"] is True and out["sendBtnUpdated"] is True
    # Draft persisted for reload (text only — File objects aren't serializable).
    assert out["saved"] == {"sid": "sid-1", "text": "my long message", "files": []}


def test_restores_original_text_not_mutated_moa_payload():
    # The snapshot passed in is the user's ORIGINAL "/moa summarize this", even
    # though send() would have rewritten `text` to just "summarize this".
    out = _run_helper_in_node("/moa summarize this", [], "", visible_sid="sid-1")
    assert out["ret"] is True
    assert out["inputValue"] == "/moa summarize this"


def test_restages_attachments_that_upload_already_drained():
    files = [{"name": "a.pdf"}, {"name": "b.png"}]
    out = _run_helper_in_node("look at these", files, "", visible_sid="sid-1")
    assert out["ret"] is True
    assert out["pendingFiles"] == files
    assert out["trayRendered"] is True


def test_restores_when_only_staged_files_remain():
    files = [{"name": "a.pdf"}]
    out = _run_helper_in_node("", files, "", visible_sid="sid-1")
    assert out["ret"] is True
    assert out["pendingFiles"] == files


def test_does_not_clobber_a_new_in_progress_draft():
    out = _run_helper_in_node("original failed", [], "something new", visible_sid="sid-1")
    assert out["ret"] is False
    assert out["inputValue"] == "something new"


def test_does_not_pollute_a_different_visible_session():
    # The failed send belongs to sid-1, but the user has switched to sid-2. The
    # visible composer must NOT be touched — but the draft is still persisted for
    # sid-1 so it survives a switch-back / reload.
    out = _run_helper_in_node("failed on old session", [], "", visible_sid="sid-2")
    assert out["ret"] is False
    assert out["inputValue"] == ""
    assert out["pendingFiles"] == []
    assert out["saved"] == {"sid": "sid-1", "text": "failed on old session", "files": []}


def test_noop_when_nothing_to_restore():
    out = _run_helper_in_node("", [], "", visible_sid="sid-1")
    assert out["ret"] is False
    assert out["saved"] is None
