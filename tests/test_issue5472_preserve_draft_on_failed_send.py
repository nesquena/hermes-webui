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
import tempfile
import textwrap
from pathlib import Path

import pytest
from tests.js_source_extract import extract_function

ROOT = Path(__file__).parents[1]
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
SEND_SRC = extract_function(MESSAGES_JS, "send", "async function")
RECOVERY_START = MESSAGES_JS.index("const _submittedPayloadRecovery")
RECOVERY_SRC = MESSAGES_JS[RECOVERY_START : MESSAGES_JS.index("async function send(", RECOVERY_START)]
DRAFT_HELPERS_START = SESSIONS_JS.index("function _composerDraftFileSignature")
DRAFT_HELPERS_SRC = SESSIONS_JS[
    DRAFT_HELPERS_START : SESSIONS_JS.index("function _composerDraftPayloadSignatureForSid", DRAFT_HELPERS_START)
]
RESTORE_DRAFT_START = SESSIONS_JS.index("function _restoreComposerDraft(")
RESTORE_DRAFT_SRC = SESSIONS_JS[
    RESTORE_DRAFT_START : SESSIONS_JS.index("// Clear the saved draft", RESTORE_DRAFT_START)
]
LOAD_PROJECTION_START = SESSIONS_JS.index("// Sync context usage indicator from session data")
LOAD_PROJECTION_SRC = SESSIONS_JS[
    LOAD_PROJECTION_START : SESSIONS_JS.index("// ── Cross-channel handoff hint", LOAD_PROJECTION_START)
]


# ---------------------------------------------------------------------------
# Static wiring assertions
# ---------------------------------------------------------------------------

def _helper_body() -> str:
    start = MESSAGES_JS.find("function _restoreComposerDraftAfterFailedSend(")
    assert start != -1, "the _restoreComposerDraftAfterFailedSend helper must exist"
    end = MESSAGES_JS.find("\nasync function send(", start)
    assert end != -1, "helper must be defined immediately before send()"
    return MESSAGES_JS[start:end]


def test_helper_has_recovery_signature_and_guards():
    body = _helper_body()
    assert "function _restoreComposerDraftAfterFailedSend(draftText, filesSnapshot, sid, clearPromise, options)" in body
    # No-op when there is nothing to restore (no text AND no staged files).
    assert "if(!restore&&!files.length) return false;" in body
    # Session-aware: never mutate a different session's visible composer.
    assert "const visibleSid=(S.session&&S.session.session_id)||null;" in body
    assert "const belongsToVisible=!(sid&&visibleSid&&sid!==visibleSid);" in body
    # Never clobber a message the user began typing during the async window.
    assert "if(inp && !pendingNavigation && !newerFiles && (!currentText||matchingHydratedText||allowFileProjection)){" in body
    # Restores text and re-stages files.
    assert "inp.value=preserveVisibleText?currentRawText:restore;" in body
    assert "S.pendingFiles=files;" in body
    assert "const allowMatchingText=!!(options&&options.allowMatchingText);" in body
    assert "const allowFileProjection=!!(options&&options.allowFileProjection);" in body
    assert "const preserveVisibleText=!!(options&&options.preserveVisibleText);" in body
    assert "{allowMatchingText:true,preserveVisibleText:true}" in MESSAGES_JS
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
    upload_idx = MESSAGES_JS.find("uploaded=await uploadPendingFiles(")
    assert snap_idx != -1 and files_idx != -1, "send() must snapshot text + files for #5472"
    assert moa_idx != -1 and upload_idx != -1
    # Snapshot happens before both the /moa rewrite and the upload drain.
    assert snap_idx < moa_idx, "text snapshot must precede the /moa rewrite of `text`"
    assert files_idx < upload_idx, "files snapshot must precede uploadPendingFiles() drain"


def test_error_branch_restores_original_snapshot_not_mutated_payload():
    start = MESSAGES_JS.find("S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});")
    assert start != -1, "the /api/chat/start error branch must still push an Error turn"
    window = MESSAGES_JS[start:start + 1100]
    assert "_releaseSubmittedPayload();" in window, (
        "the send-error path must restore the ORIGINAL captured snapshot through custody"
    )


def test_send_still_clears_composer_on_the_happy_path():
    # Anchor to the MAIN-path persisted-draft clear specifically (not a bare
    # `$('msg').value=''` string that also appears at ~13 other sites — slash
    # returns, bundle-error paths). This assertion must actually guard the main
    # send path's clear.
    main_clear = (
        "if (activeSid && typeof _clearComposerDraft === 'function') "
        "_composerDraftClearPromise=_clearComposerDraft(activeSid,_submittedDraftTextForClear,_submittedDraftFilesForClear);"
    )
    assert main_clear in MESSAGES_JS, "main send path must clear the persisted draft at send time"

    # Salvage of #4750: the composer textarea capture + wipe was moved UP to run
    # immediately after capture (right after `_sendInProgressSid=activeSid;`) and
    # BEFORE `uploadPendingFiles()` / the forced-skill-directive await — so a
    # re-entrant/interrupt-mode send during the async window can't re-read the
    # still-populated DOM and double-submit. Verify that ordering here.
    capture = "const _submittedDraftTextForClear=$('msg').value||'';"
    assert capture in MESSAGES_JS, "send() must still capture the send-time draft text"
    capture_idx = MESSAGES_JS.index(capture)
    # The textarea wipe sits immediately after the capture (same 120-char window).
    window_after_capture = MESSAGES_JS[capture_idx : capture_idx + 120]
    assert "$('msg').value='';autoResize();" in window_after_capture, (
        "the composer textarea wipe must sit immediately after the capture"
    )
    upload_idx = MESSAGES_JS.index("uploaded=await uploadPendingFiles(")
    clear_idx = MESSAGES_JS.index(main_clear)
    # THE FIX: capture+wipe happen before the upload await (closes the race)...
    assert capture_idx < upload_idx, (
        "composer must be captured+cleared BEFORE the uploadPendingFiles() await "
        "so a re-entrant send can't re-read stale DOM text (salvage of #4750)"
    )
    # ...and the captured text is still what feeds the persisted-draft clear.
    assert capture_idx < clear_idx, (
        "the captured send-time draft text must precede the persisted-draft clear"
    )
    # The files snapshot (#5912 gate fix) is taken from S.pendingFiles BEFORE the
    # upload await and feeds the persisted-draft clear which now runs before the await.
    assert (
        "const _submittedDraftFilesForClear=[..._submittedFiles];"
        in MESSAGES_JS
    ), "the files snapshot for the persisted-draft clear must be captured pre-await"
    # The persisted-draft clear must run BEFORE the upload await (not after it),
    # so a draft typed during the upload window is not clobbered.
    assert clear_idx < upload_idx, (
        "the persisted-draft clear must precede the uploadPendingFiles() await (#5912)"
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
    assert "files:[..._submittedFiles]" in MESSAGES_JS
    assert "clearPromise:_composerDraftClearPromise" in MESSAGES_JS
    assert "_restoreComposerDraftAfterFailedSend(\n      _submittedPayloadCustody.draftText" in MESSAGES_JS


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


def _run_reload_projection_in_node(stage: str, conflict: str | None = None):
    """Compose the real send, draft hydration, and owner projection order."""
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    recovery_setup = textwrap.dedent(
        f"""
        const fileA1 = {{id: 'a1', name: 'a.pdf', size: 1, lastModified: 11}};
        const fileA2 = {{id: 'a2', name: 'b.png', size: 2, lastModified: 22}};
        const fileB = {{id: 'b1', name: 'new.txt', size: 3, lastModified: 33}};
        const fileCollision = {{id: 'b2', name: 'a.pdf', size: 1, lastModified: 99}};
        let savedDrafts = [];
        let trayRenders = 0;
        {DRAFT_HELPERS_SRC}
        {RECOVERY_SRC}
        {RESTORE_DRAFT_SRC}
        let observedRestoreReport = 'unset';
        const _realRestoreComposerDraft = _restoreComposerDraft;
        _restoreComposerDraft = (...args) => {{
          observedRestoreReport = _realRestoreComposerDraft(...args);
          return observedRestoreReport;
        }};
        const _realProjectSubmittedPayloadForOwner = projectSubmittedPayloadForOwner;
        projectSubmittedPayloadForOwner = (sid, acceptedDraft) => {{
          if ({json.dumps(conflict)} === 'off_pane') {{
            S.session = {{session_id: 'session-b', workspace: '/ws', model: 'model', profile: 'default'}};
            input.value = 'B draft';
            S.pendingFiles = [fileB];
          }}
          return _realProjectSubmittedPayloadForOwner(sid, acceptedDraft);
        }};
        """
    )
    conflict_text_setup = """
        const conflictDraft = {text: 'newer text', files: []};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: false,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    conflict_files_setup = """
        const conflictDraft = {text: 'hello', files: _composerDraftFilesForPersist([fileB])};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [fileB];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: false,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    whitespace_setup = """
        const conflictDraft = {text: '   ', files: []};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
           projected: false,
          inputValue: input.value,
           pendingFileIds: S.pendingFiles.map(file => file.id),
           pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
           recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
           trayRenders,
           sendBtnUpdates,
           savedDrafts,
           acceptedReport: observedRestoreReport,
         }));
    """
    hydrated_b_setup = """
        const conflictDraft = {text: 'hello', files: _composerDraftFilesForPersist([fileB])};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: false,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          acceptedDraftFiles: S.session.composer_draft.files,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    empty_setup = """
        const conflictDraft = {text: '', files: []};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: false,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    newer_text_setup = """
        const conflictDraft = {text: 'newer text', files: _composerDraftFilesForPersist([fileA1, fileA2])};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: true,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    file_only_setup = """
        const conflictDraft = {text: '', files: _composerDraftFilesForPersist([fileA1, fileA2])};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: true,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    echo_setup = """
        const conflictDraft = {text: 'hello', files: []};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: true,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    rejected_setup = """
        const conflictDraft = {text: 'server draft', files: _composerDraftFilesForPersist([fileB])};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = 'hello';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: true,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    no_draft_setup = """
        S.session = {session_id: 'session-a'};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: true,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    off_pane_setup = """
        const conflictDraft = {text: 'hello', files: _composerDraftFilesForPersist([fileA1, fileA2])};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: false,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    collision_setup = """
        const conflictDraft = {text: 'hello', files: _composerDraftFilesForPersist([fileCollision])};
        S.session = {session_id: 'session-a', composer_draft: conflictDraft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          acceptedReport: observedRestoreReport,
        }));
    """
    late_acceptance_setup = """
        S.session = {session_id: 'session-a', composer_draft: {text: '', files: []}};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        S.session = {session_id: 'session-b', workspace: '/ws', model: 'model', profile: 'default'};
        input.value = 'B draft';
        S.pendingFiles = [fileB];
    """
    late_post_setup = """
        console.log(JSON.stringify({
          bSnapshot,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          savedDrafts,
        }));
    """
    normal_setup = """
        const draft = {text: 'hello', files: _composerDraftFilesForPersist([fileA1, fileA2])};
        S.session = {session_id: 'session-a', composer_draft: draft};
        input.value = '';
        S.pendingFiles = [];
        _loadingSessionId = 'session-a';
        await loadSession('session-a');
        console.log(JSON.stringify({
          bSnapshot,
          projected: true,
          inputValue: input.value,
          pendingFileIds: S.pendingFiles.map(file => file.id),
          pendingFileRefs: [S.pendingFiles[0] === fileA1, S.pendingFiles[1] === fileA2],
          recoveryOutstanding: _submittedPayloadRecovery.has('session-a'),
          trayRenders,
          sendBtnUpdates,
          savedDrafts,
        }));
    """
    post_setup = {
        "text": conflict_text_setup,
        "files": conflict_files_setup,
        "whitespace": whitespace_setup,
        "hydrated_b": hydrated_b_setup,
        "empty": empty_setup,
        "newer_text": newer_text_setup,
        "file_only": file_only_setup,
        "echo": echo_setup,
        "suppressed": rejected_setup,
        "preserve_active": rejected_setup,
        "no_draft": no_draft_setup,
        "off_pane": off_pane_setup,
        "collision": collision_setup,
        "late_empty": late_post_setup,
    }.get(conflict, normal_setup)
    pre_release_setup = late_acceptance_setup if conflict == "late_empty" else ""
    harness = textwrap.dedent(
        f"""
        let _sendInProgress = false;
        let _sendInProgressSid = null;
        let _pendingPickMatch = null;
        let _pendingSelections = [];
        let _forcedSkillDirectivePending = null;
        let _queueDrainSid = null;
        let _approvalSessionId = null;
        let _clarifySessionId = null;
        let _loadingSessionId = null;
        let resolveStart = null;
        let resolveUpload = null;
        let resolveDirective = null;
        let startCalls = 0;
        let sendBtnUpdates = 0;
        const input = {{value: 'hello', style: {{}}}};
        const S = {{
          session: {{session_id: 'session-a', workspace: '/ws', model: 'model', profile: 'default'}},
          messages: [], pendingFiles: [], pendingSelections: [], toolCalls: [],
          busy: false, activeStreamId: null, activeProfile: 'default',
        }};
        const window = {{_defaultMessageMode: 'steer'}};
        const document = {{querySelector() {{return null;}}}};
        const localStorage = {{setItem() {{}}, removeItem() {{}}, getItem() {{return null;}}}};
        const INFLIGHT = {{}};
        function $(id) {{return id === 'msg' ? input : null;}}
        function _isSessionCurrentPane(sid) {{return !!S.session && S.session.session_id === sid;}}
        async function _ensureSessionOwner() {{return S.session && S.session.session_id;}}
        function _composerTextWithPendingSelections() {{return input.value;}}
        function _flushSelectionBlocksToComposer() {{}}
        function _clearStaleBusyStateBeforeSend() {{return false;}}
        function _clearComposerAfterQueuedSelectionSend() {{}}
        function _chatPayloadModelState() {{return {{model: 'model', model_provider: null}};}}
        function _dismissHandoffHint() {{}}
        function _bumpMessagesGeneration() {{return 1;}}
        function _runOptionalPreStartUiStep(_label, fn) {{if (typeof fn === 'function') fn();}}
        function _runOptionalPostStartUiStep(_label, fn) {{if (typeof fn === 'function') fn();}}
        function _clearPendingSessionModel() {{}}
        function _clearComposerDraft() {{return Promise.resolve();}}
        function _clearOptimisticSessionStreaming() {{}}
        function clearOptimisticSessionStreaming() {{}}
        function _fetchYoloState() {{}}
        function updateSendBtn() {{sendBtnUpdates += 1;}}
        function setComposerStatus() {{}}
        function setStatus() {{}}
        function setBusy(value) {{S.busy = !!value;}}
        function renderMessages() {{}}
        function renderTray() {{trayRenders += 1;}}
        function autoResize() {{}}
        function hideCmdDropdown() {{}}
        function clearLiveToolCards() {{}}
        function ensureLiveWorklogShell() {{}}
        function appendThinking() {{}}
        function upsertActiveSessionForLocalTurn() {{}}
        function markInflight() {{}}
        function saveInflightState() {{}}
        function startApprovalPolling() {{}}
        function startClarifyPolling() {{}}
        function stopApprovalPolling() {{}}
        function stopClarifyPolling() {{}}
        function hideApprovalCard() {{}}
        function hideClarifyCard() {{}}
        function removeThinking() {{}}
        function showToast() {{}}
        function syncTopbar() {{}}
        function updateQueueBadge() {{}}
        function queueSessionMessage() {{}}
        function startApprovalPolling() {{}}
        function resumeManualCompressionForSession() {{}}
        function _deferWorkspaceRefreshForSession() {{}}
        function _acknowledgeSessionVisit() {{}}
        function t(key) {{return key;}}
        function _composerDraftHasPayload(text, files) {{return !!String(text || '').trim() || (Array.isArray(files) && files.length > 0);}}
         function _isComposerDraftRestoreSuppressed() {{return {json.dumps(conflict)} === 'suppressed';}}
        function _clearComposerDraftRestoreSuppression() {{}}
        function _saveComposerDraftNow(sid, text, files) {{
          savedDrafts.push({{
            sid,
            text,
            fileIds: (files || []).map(file => file && file.id || file && file.name || file),
            persistedFiles: _composerDraftFilesForPersist(files || []),
          }});
        }}
        async function uploadPendingFiles() {{
          if ({json.dumps(stage)} === 'upload') return new Promise(resolve => {{resolveUpload = resolve;}});
          return [];
        }}
        function attachLiveStream() {{}}
        function api(url) {{
          if (String(url) !== '/api/chat/start') throw new Error('unexpected API request: ' + String(url));
          startCalls += 1;
          return new Promise((resolve, reject) => {{
            resolveStart = () => {{
              if ({json.dumps(stage)} === 'chat_start_error') reject(new Error('start failed'));
              else resolve({{stream_id: 'stream-a'}});
            }};
          }});
        }}
        {recovery_setup}
        async function loadSession(sid) {{
           const opts = {{preserveActiveInput: {json.dumps(conflict)} === 'preserve_active'}};
          const currentSid = 'session-b';
          const force = true;
          const sameSessionForceReload = false;
          let activeStreamId = null;
          const _isCurrentLoad = () => true;
          {LOAD_PROJECTION_SRC}
        }}
        {SEND_SRC}
        (async () => {{
          if ({json.dumps(stage)} === 'directive') {{
            _forcedSkillDirectivePending = {{sessionId: 'session-a', promise: new Promise(resolve => {{resolveDirective = resolve;}})}};
          }}
          S.pendingFiles = [fileA1, fileA2];
          const sendPromise = send();
          if ({json.dumps(stage)} === 'upload') {{
            for (let i = 0; i < 200 && !resolveUpload; i++) await new Promise(resolve => setTimeout(resolve, 0));
          }} else if ({json.dumps(stage)} === 'directive') {{
            for (let i = 0; i < 20; i++) await new Promise(resolve => setTimeout(resolve, 0));
          }} else {{
            for (let i = 0; i < 200 && !resolveStart; i++) await new Promise(resolve => setTimeout(resolve, 0));
          }}
           S.session = {{session_id: 'session-b', workspace: '/ws', model: 'model', profile: 'default'}};
          input.value = 'B draft';
          S.pendingFiles = [fileB];
          S.busy = false;
          S.activeStreamId = null;
          {pre_release_setup}
          if ({json.dumps(stage)} === 'upload') resolveUpload([]);
          else if ({json.dumps(stage)} === 'directive') resolveDirective({{directive: 'forced'}});
          else resolveStart();
          await sendPromise;
          const bSnapshot = {{sessionId: S.session.session_id, inputValue: input.value, pendingFileIds: S.pendingFiles.map(file => file.id)}};
          {post_setup}
        }})().catch(err => {{console.error(err.stack || String(err)); process.exit(1);}});
        """
    )
    with tempfile.TemporaryDirectory(prefix="webui-5472-reload-") as temp_dir:
        script_path = Path(temp_dir) / "case.js"
        script_path.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [node, str(script_path)], capture_output=True, text=True, timeout=30
        )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


@pytest.mark.parametrize("stage", ["upload", "directive", "chat_start_error"])
def test_failed_send_reload_restages_exact_files_after_server_draft_hydration(stage):
    out = _run_reload_projection_in_node(stage)
    assert out["projected"] is True
    assert out["inputValue"] == "hello"
    assert out["pendingFileIds"] == ["a1", "a2"]
    assert out["pendingFileRefs"] == [True, True]
    assert out["recoveryOutstanding"] is False
    assert out["trayRenders"] >= 2
    assert out["sendBtnUpdates"] >= 1
    assert out["savedDrafts"][-1] == {
        "sid": "session-a",
        "text": "hello",
        "fileIds": ["a1", "a2"],
        "persistedFiles": [
            {"name": "a.pdf", "path": "", "size": 1, "type": "", "lastModified": 11},
            {"name": "b.png", "path": "", "size": 2, "type": "", "lastModified": 22},
        ],
    }
    assert out["bSnapshot"] == {"sessionId": "session-b", "inputValue": "B draft", "pendingFileIds": ["b1"]}


@pytest.mark.parametrize("conflict", ["text", "files"])
def test_failed_send_reload_preserves_newer_text_or_files(conflict):
    out = _run_reload_projection_in_node("upload", conflict=conflict)
    assert out["projected"] is False
    assert out["inputValue"] == ("newer text" if conflict == "text" else "hello")
    assert out["pendingFileIds"] == ([] if conflict == "text" else ["b1"])
    assert out["recoveryOutstanding"] is False


def test_failed_send_reload_preserves_hydrated_whitespace_when_restaging_files():
    out = _run_reload_projection_in_node("upload", conflict="whitespace")
    assert out["projected"] is False
    assert out["inputValue"] == "   "
    assert out["pendingFileIds"] == []
    assert out["recoveryOutstanding"] is False
    assert out["acceptedReport"] == {"text": "   ", "files": []}


@pytest.mark.parametrize("conflict", ["hydrated_b", "empty", "whitespace", "newer_text"])
def test_failed_send_reload_accepted_draft_owns_projection(conflict):
    out = _run_reload_projection_in_node("upload", conflict=conflict)
    assert out["recoveryOutstanding"] is False
    assert out["acceptedReport"]["text"] == {
        "hydrated_b": "hello",
        "empty": "",
        "whitespace": "   ",
        "newer_text": "newer text",
    }[conflict]
    if conflict == "hydrated_b":
        assert out["inputValue"] == "hello"
        assert out["pendingFileIds"] == []
        assert out["acceptedDraftFiles"][0]["name"] == "new.txt"
    elif conflict == "empty":
        assert out["inputValue"] == ""
        assert out["pendingFileIds"] == []
    elif conflict == "whitespace":
        assert out["inputValue"] == "   "
        assert out["pendingFileIds"] == []
    else:
        assert out["inputValue"] == "newer text"
        assert out["pendingFileIds"] == ["a1", "a2"]
        assert out["pendingFileRefs"] == [True, True]


def test_failed_send_reload_retires_custody_for_same_name_with_newer_file_metadata():
    out = _run_reload_projection_in_node("upload", conflict="collision")
    assert out["inputValue"] == "hello"
    assert out["pendingFileIds"] == []
    assert out["pendingFileRefs"] == [False]
    assert out["recoveryOutstanding"] is False
    assert out["acceptedReport"]["files"][0]["lastModified"] == 99


def test_failed_send_reload_file_only_draft_restages_matching_files_without_text():
    out = _run_reload_projection_in_node("upload", conflict="file_only")
    assert out["inputValue"] == ""
    assert out["pendingFileIds"] == ["a1", "a2"]
    assert out["pendingFileRefs"] == [True, True]
    assert out["acceptedReport"] == {
        "text": "",
        "files": [
            {"name": "a.pdf", "path": "", "size": 1, "type": "", "lastModified": 11},
            {"name": "b.png", "path": "", "size": 2, "type": "", "lastModified": 22},
        ],
    }


def test_failed_send_reload_metadata_less_echo_restages_matching_files():
    out = _run_reload_projection_in_node("upload", conflict="echo")
    assert out["inputValue"] == "hello"
    assert out["pendingFileIds"] == ["a1", "a2"]
    assert out["pendingFileRefs"] == [True, True]
    assert out["recoveryOutstanding"] is False


@pytest.mark.parametrize("conflict", ["suppressed", "preserve_active"])
def test_failed_send_reload_rejected_hydration_keeps_legacy_recovery(conflict):
    out = _run_reload_projection_in_node("upload", conflict=conflict)
    assert out["acceptedReport"] is None
    assert out["inputValue"] == "hello"
    assert out["pendingFileIds"] == ["a1", "a2"]
    assert out["pendingFileRefs"] == [True, True]
    assert out["recoveryOutstanding"] is False


def test_failed_send_reload_no_draft_keeps_legacy_recovery():
    out = _run_reload_projection_in_node("upload", conflict="no_draft")
    assert out["acceptedReport"] == "unset"
    assert out["inputValue"] == "hello"
    assert out["pendingFileIds"] == ["a1", "a2"]
    assert out["pendingFileRefs"] == [True, True]


def test_failed_send_reload_off_pane_keeps_owner_custody_and_visible_pane():
    out = _run_reload_projection_in_node("upload", conflict="off_pane")
    assert out["acceptedReport"]["text"] == "hello"
    assert out["inputValue"] == "B draft"
    assert out["pendingFileIds"] == ["b1"]
    assert out["recoveryOutstanding"] is True


def test_failed_send_late_rejection_does_not_restore_accepted_empty_draft():
    out = _run_reload_projection_in_node("chat_start_error", conflict="late_empty")
    assert out["inputValue"] == "B draft"
    assert out["pendingFileIds"] == ["b1"]
    assert out["recoveryOutstanding"] is False
    assert out["savedDrafts"] == []


def test_load_session_hydrates_server_text_before_owner_projection():
    load_body = SESSIONS_JS[SESSIONS_JS.index("async function loadSession") :]
    assert load_body.index("_restoreComposerDraft(_draft") < load_body.index(
        "projectSubmittedPayloadForOwner(sid, _acceptedDraft)"
    )
