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
    assert "function _restoreComposerDraftAfterFailedSend(draftText, filesSnapshot, sid, clearPromise)" in body
    # No-op when there is nothing to restore (no text AND no staged files).
    assert "if(!restore&&!files.length) return false;" in body
    # Session-aware: never mutate a different session's visible composer.
    assert "const visibleSid=(S.session&&S.session.session_id)||null;" in body
    assert "const belongsToVisible=!(sid&&visibleSid&&sid!==visibleSid);" in body
    # Never clobber a message the user began typing during the async window.
    assert "if(inp && !String(inp.value||'').trim()){" in body
    # Restores text and re-stages files.
    assert "inp.value=restore;" in body
    assert "S.pendingFiles=files;" in body
    # The deferred persist is stale-aware: re-reads the LIVE composer when the
    # failed session is still visible (Codex #5488 catch), rather than the
    # captured snapshot.
    assert "const stillVisible=(S.session&&S.session.session_id)===sid;" in body
    assert "const liveText=inp?String(inp.value||''):restore;" in body


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
    window = MESSAGES_JS[start:start + 1100]
    assert (
        "_restoreComposerDraftAfterFailedSend(_failedSendDraftText, _failedSendFilesSnapshot, activeSid, _composerDraftClearPromise);"
        in window
    ), "the send-error path must restore the ORIGINAL captured snapshot (not `text`)"


def test_send_still_clears_composer_on_the_happy_path():
    # Anchor to the MAIN-path clear specifically (the send-time composer wipe +
    # persisted-draft clear), not a bare `$('msg').value=''` string that also
    # appears at ~13 other sites (slash returns, bundle-error paths). This
    # assertion must actually guard the main send path's clear. (Opus #5484 NIT.)
    main_clear = (
        "if (activeSid && typeof _clearComposerDraft === 'function') "
        "_composerDraftClearPromise=_clearComposerDraft(activeSid,_submittedDraftTextForClear,_submittedDraftFilesForClear);"
    )
    assert main_clear in MESSAGES_JS, "main send path must clear the persisted draft at send time"
    # The composer textarea wipe must sit immediately above that clear.
    clear_idx = MESSAGES_JS.find(main_clear)
    window_before = MESSAGES_JS[clear_idx - 700:clear_idx]
    assert "const _submittedDraftTextForClear=$('msg').value||'';" in window_before
    assert "const _submittedDraftFilesForClear=Array.isArray(_failedSendFilesSnapshot)?[..._failedSendFilesSnapshot]:[];" in window_before
    assert "$('msg').value='';autoResize();" in window_before, (
        "the main-path composer wipe must precede the persisted-draft clear"
    )


def test_restore_persist_chains_after_the_clear_promise():
    # NIT 1: the restore's re-persist must be ordered AFTER the send-time clear
    # POST resolves (avoids an HTTP/2 reorder leaving the server draft empty).
    body = _helper_body()
    assert "clearPromise" in body, "helper must accept the clear promise for ordering"
    assert "clearPromise.then(_persist,_persist)" in body, (
        "re-persist must chain off the clear promise (both fulfill and reject → persist)"
    )
    # And send() must pass the captured clear promise into the restore call.
    assert "let _composerDraftClearPromise=null;" in MESSAGES_JS
    call = (
        "_restoreComposerDraftAfterFailedSend(_failedSendDraftText, "
        "_failedSendFilesSnapshot, activeSid, _composerDraftClearPromise);"
    )
    assert call in MESSAGES_JS, "send() must pass the clear promise into the restore helper"


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


def test_persist_is_ordered_after_the_clear_promise():
    """Behavioral: the re-persist must run AFTER the clear promise resolves.

    Simulates the send-time clear POST as a promise that records the persist
    order. The re-persist must observe that the clear has already resolved.
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _helper_body()
    harness = textwrap.dedent(
        """
        const order = [];
        const state = {input: {value: ""}, pendingFiles: []};
        const $ = (id) => (id === 'msg' ? state.input : null);
        const S = {pendingFiles: state.pendingFiles, session: {session_id: 'sid-1'}};
        function autoResize(){}
        function updateSendBtn(){}
        function renderTray(){}
        function _saveComposerDraftNow(sid, text, files){ order.push('persist:' + text); }

        %(helper)s

        // Clear POST resolves on a microtask; record its completion first.
        const clearPromise = Promise.resolve().then(() => { order.push('clear'); });
        _restoreComposerDraftAfterFailedSend('hello', [], 'sid-1', clearPromise);
        // Flush microtasks, then report ordering.
        Promise.resolve().then(() => Promise.resolve()).then(() => {
          console.log(JSON.stringify({order}));
        });
        """
    ) % {"helper": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["order"] == ["clear", "persist:hello"], (
        f"persist must run after the clear resolves, got {out['order']}"
    )


def test_persist_still_fires_when_clear_promise_absent():
    """Fallback: with no clear promise, the persist happens immediately (sync)."""
    out = _run_helper_in_node("no clear promise", [], "", visible_sid="sid-1")
    assert out["ret"] is True
    assert out["saved"] == {"sid": "sid-1", "text": "no clear promise", "files": []}


def test_deferred_persist_captures_a_post_restore_edit_not_the_stale_snapshot():
    """Codex #5488 regression: if the user edits the restored composer before the
    deferred persist fires, the persist must save the EDITED text — not clobber it
    with the original failed-send snapshot."""
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _helper_body()
    harness = textwrap.dedent(
        """
        let saved = null;
        const state = {input: {value: ""}, pendingFiles: []};
        const $ = (id) => (id === 'msg' ? state.input : null);
        const S = {pendingFiles: state.pendingFiles, session: {session_id: 'sid-1'}};
        function autoResize(){}
        function updateSendBtn(){}
        function renderTray(){}
        function _saveComposerDraftNow(sid, text, files){ saved = {sid, text}; }

        %(helper)s

        // Clear POST settles on a microtask; the deferred persist runs after it.
        const clearPromise = Promise.resolve();
        _restoreComposerDraftAfterFailedSend('original', [], 'sid-1', clearPromise);
        // Synchronously the composer shows the restored text...
        const afterRestore = state.input.value;
        // ...then the user edits it BEFORE the deferred persist fires.
        state.input.value = 'edited after restore';
        Promise.resolve().then(() => Promise.resolve()).then(() => {
          console.log(JSON.stringify({afterRestore, saved}));
        });
        """
    ) % {"helper": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["afterRestore"] == "original", "composer should show the restored text synchronously"
    assert out["saved"] == {"sid": "sid-1", "text": "edited after restore"}, (
        f"deferred persist must save the LIVE edited text, not the stale snapshot; got {out['saved']}"
    )


def test_deferred_persist_skips_when_user_switched_away_after_restore():
    """Codex #5488 regression: if we restored the visible session then the user
    switched to a different session before the deferred persist fires, the stale
    persist must be SKIPPED (the session-switch save path already saved it)."""
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _helper_body()
    harness = textwrap.dedent(
        """
        let saveCalls = [];
        const state = {input: {value: ""}, pendingFiles: []};
        const $ = (id) => (id === 'msg' ? state.input : null);
        const S = {pendingFiles: state.pendingFiles, session: {session_id: 'sid-1'}};
        function autoResize(){}
        function updateSendBtn(){}
        function renderTray(){}
        function _saveComposerDraftNow(sid, text, files){ saveCalls.push({sid, text}); }

        %(helper)s

        const clearPromise = Promise.resolve();
        _restoreComposerDraftAfterFailedSend('original', [], 'sid-1', clearPromise);
        // User switches to a different session before the deferred persist fires.
        S.session = {session_id: 'sid-2'};
        state.input.value = 'draft for sid-2';
        Promise.resolve().then(() => Promise.resolve()).then(() => {
          console.log(JSON.stringify({saveCalls}));
        });
        """
    ) % {"helper": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    # No persist for sid-1 with the stale 'original' text, and crucially no write
    # of sid-2's live composer under sid-1 (that would corrupt sid-1's draft).
    assert out["saveCalls"] == [], (
        f"deferred persist must skip entirely after a switch-away; got {out['saveCalls']}"
    )


def test_background_failure_persists_snapshot_since_no_live_composer():
    """A failed send for a NON-visible session (background) has no live composer
    to read, so the deferred persist saves the captured snapshot for that sid."""
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _helper_body()
    harness = textwrap.dedent(
        """
        let saveCalls = [];
        const state = {input: {value: "visible session draft"}, pendingFiles: []};
        const $ = (id) => (id === 'msg' ? state.input : null);
        // The visible session is sid-2; the failed send was for sid-1 (background).
        const S = {pendingFiles: state.pendingFiles, session: {session_id: 'sid-2'}};
        function autoResize(){}
        function updateSendBtn(){}
        function renderTray(){}
        function _saveComposerDraftNow(sid, text, files){ saveCalls.push({sid, text}); }

        %(helper)s

        const ret = _restoreComposerDraftAfterFailedSend('bg failed msg', [], 'sid-1', null);
        Promise.resolve().then(() => {
          console.log(JSON.stringify({ret, saveCalls, visibleUntouched: state.input.value}));
        });
        """
    ) % {"helper": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["ret"] is False, "a background (non-visible) failure must not report a visible restore"
    assert out["visibleUntouched"] == "visible session draft", "the visible composer must be untouched"
    assert out["saveCalls"] == [{"sid": "sid-1", "text": "bg failed msg"}], (
        f"background failure must persist the snapshot for its own sid; got {out['saveCalls']}"
    )
