"""Regression coverage for stale composer_draft restoration after send."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
COMMANDS_JS = ROOT.joinpath("static", "commands.js").read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_clear_composer_draft_suppresses_same_session_stale_restore():
    """An async draft-clear POST must not allow old server draft text to repopulate #msg."""
    assert "const _composerDraftRestoreSuppressedUntilBySid = new Map();" in SESSIONS_JS
    assert "function _composerDraftPayloadSignature(text, files)" in SESSIONS_JS
    assert "function _suppressComposerDraftRestoreAfterSubmit(sid, text, files)" in SESSIONS_JS
    clear_body = _block(SESSIONS_JS, "function _clearComposerDraft(sid, text, files)", "const SESSION_VIEWED_COUNTS_KEY")
    suppress_idx = clear_body.index("_suppressComposerDraftRestoreAfterSubmit(sid, text, files);")
    post_idx = clear_body.index("api('/api/session/draft'")
    assert suppress_idx < post_idx, "restore suppression must be local and immediate before async POST"


def test_non_empty_draft_save_clears_submit_restore_suppression():
    save_body = _block(SESSIONS_JS, "function _saveComposerDraft(sid, text, files)", "function _composerDraftHasPayload")
    assert "_clearComposerDraftRestoreSuppression(sid);" in save_body
    now_body = _block(SESSIONS_JS, "function _saveComposerDraftNow(sid, text, files)", "// Restore composer draft")
    assert "_clearComposerDraftRestoreSuppression(sid);" in now_body


def test_restore_skips_suppressed_non_empty_server_draft_only():
    restore_body = _block(SESSIONS_JS, "function _restoreComposerDraft(draft, targetSid", "// Clear the saved draft")
    assert "const restoreSid = targetSid || (S.session && S.session.session_id);" in restore_body
    assert "const hasServerDraftPayload = _composerDraftHasPayload(text, files);" in restore_body
    assert "hasServerDraftPayload && _isComposerDraftRestoreSuppressed(restoreSid, text, files)" in restore_body
    assert "!hasServerDraftPayload) _clearComposerDraftRestoreSuppression(restoreSid);" in restore_body


def test_busy_send_paths_clear_persisted_composer_draft():
    helper_body = _block(MESSAGES_JS, "function _clearComposerAfterQueuedSelectionSend", "function _flushSelectionBlocksToComposer")
    assert "function _clearComposerAfterQueuedSelectionSend()" in helper_body
    assert "const sid=arguments.length?arguments[0]:(S.session&&S.session.session_id);" in helper_body
    assert "const draftText=composer?String(composer.value||''):'';" in helper_body
    assert "const draftFiles=Array.isArray(S.pendingFiles)?[...S.pendingFiles]:[];" in helper_body
    assert "_clearComposerDraft(sid,draftText,draftFiles)" in helper_body

    in_progress_body = _block(MESSAGES_JS, "if (_sendInProgress) {", "  _sendInProgress = true;")
    assert "_clearComposerAfterQueuedSelectionSend();" in in_progress_body
    assert "_clearComposerDraft(_targetSid,_text,S.pendingFiles?[...S.pendingFiles]:[])" in in_progress_body

    busy_body = _block(MESSAGES_JS, "if(S.busy||compressionRunning){", "  if(S.session&&(S.session.read_only||S.session.is_read_only))")
    assert "_clearComposerAfterQueuedSelectionSend(S.session&&S.session.session_id);" in busy_body
    assert busy_body.count("_clearComposerAfterQueuedSelectionSend(S.session&&S.session.session_id);") >= 2
    assert "_steerFinalizeComposer(_steerResult.ownerSid,text,_steerResult.files,/*explicitSteer=*/false)" in busy_body, (
        "delivered/queued steer must route composer cleanup through the shared guard so a replacement draft is preserved"
    )
    assert "_clearComposerDraft(S.session.session_id,text" not in busy_body
    # Draft clearing on the steer path lives only in the shared guard, never inline
    # in _trySteer, so the accepted-steer path can no longer wipe a replacement draft.
    try_steer_body = _block(COMMANDS_JS, "async function _trySteer(", "\nasync function cmdTitle")
    assert "_clearComposerDraft(" not in try_steer_body, "steer draft clearing must route through _steerFinalizeComposer"
    finalize_body = _block(COMMANDS_JS, "function _steerFinalizeComposer", "\nfunction _showSteerRecovery")
    assert "_clearComposerDraft(ownerSid,_steerRestoreText(msg,explicitSteer),delivered)" in finalize_body, (
        "the shared guard clears the captured owner draft with the submitted payload signature"
    )
    assert "if(!safe)return;" in finalize_body, "textarea and draft clears must be gated by the combined text+files+owner predicate"


def test_file_signature_survives_server_draft_round_trip():
    """#5471 attachment case: the signature of a just-sent text+File payload must
    MATCH the signature of the same payload after it round-trips through the server
    draft (where a live File JSON-serializes to {}). Both the persist path and the
    signature path must canonicalize files identically, or a text+attachment send
    never matches its own suppression and the stale tail repopulates.
    """
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    persist_fn = _block(
        SESSIONS_JS,
        "function _composerDraftFilesForPersist(files)",
        "function _composerDraftPayloadSignature(text, files)",
    )
    sig_fns = _block(
        SESSIONS_JS,
        "function _composerDraftFileSignature(file)",
        "function _composerDraftPayloadSignatureForSid(sid)",
    )

    harness = textwrap.dedent(
        """
        %(sig_fns)s
        %(persist_fn)s

        // A real browser File exposes name/size/type via PROTOTYPE getters that
        // JSON.stringify drops (serializes to {}). Simulate that: own props empty,
        // metadata on the prototype.
        function makeFile(name, size, type, lastModified) {
          return Object.create({ name, size, type, lastModified });
        }
        const liveFile = makeFile('report.pdf', 1234, 'application/pdf', 42);

        // THE BUG: persisting the raw File loses everything through JSON.
        const rawPersistLossy = JSON.parse(JSON.stringify([liveFile]));   // -> [{}]
        // THE FIX: canonicalize BEFORE persist so metadata survives the round-trip.
        const canonPersist = JSON.parse(JSON.stringify(_composerDraftFilesForPersist([liveFile])));

        // Signature of what the server would return in each case, vs the sent payload.
        const sentSig = _composerDraftPayloadSignature('hi', [liveFile]);
        const restoredSigLossy = _composerDraftPayloadSignature('hi', rawPersistLossy);
        const restoredSigCanon = _composerDraftPayloadSignature('hi', canonPersist);
        const otherSig = _composerDraftPayloadSignature('hi', [makeFile('notes.txt', 99, 'text/plain', 7)]);

        console.log(JSON.stringify({
          harnessOk: JSON.stringify(liveFile) === '{}',
          lossyWouldMismatch: sentSig !== restoredSigLossy,   // demonstrates the bug exists
          canonMatchesSelf: sentSig === restoredSigCanon,      // the fix
          differsFromOther: sentSig !== otherSig,
        }));
        """
    ) % {"sig_fns": sig_fns, "persist_fn": persist_fn}

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["harnessOk"] is True, "harness must simulate a File that JSON-serializes to {}"
    assert out["lossyWouldMismatch"] is True, (
        "sanity: persisting the raw File (the bug) loses metadata so the restored "
        "signature would NOT match the sent one"
    )
    assert out["canonMatchesSelf"] is True, (
        "the fix: canonicalizing files before persist makes a text+attachment send's "
        "signature match the same payload after the server draft round-trip — #5471"
    )
    assert out["differsFromOther"] is True, (
        "a genuinely different draft must NOT collide with the sent signature"
    )


def _run_steer_finalize_harness(body: str) -> None:
    """Eval the shared steer cleanup guard and run `body` against it under node."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")
    guard_src = _block(
        COMMANDS_JS,
        "function _steerComposerSafeToClear",
        "\nfunction _showSteerRecovery",
    )
    # Real signature functions (file canon + payload signature) so the persisted-draft
    # comparison is exercised exactly as production computes it. This block spans
    # _composerDraftFileSignature, _composerDraftFilesForPersist, and
    # _composerDraftPayloadSignature.
    sig_src = _block(
        SESSIONS_JS,
        "function _composerDraftFileSignature(file)",
        "function _composerDraftPayloadSignatureForSid(sid)",
    )
    script = textwrap.dedent(
        """
        const assert = require('assert');
        let draftClears = [];
        function renderTray(){}
        function autoResize(){}
        function updateSendBtn(){}
        function _clearComposerDraft(sid,text,files){draftClears.push({sid,text,files});}
        function _steerOwnerIsCurrent(sid){return !!(sid && S && S.session && S.session.session_id===sid);}
        function _steerRestoreText(msg,explicit){return explicit?('/steer '+msg):msg;}
        %(sig)s
        // Per-sid persisted drafts the guard reads for a switched-away owner. Tests
        // populate this to mimic what loadSession/_saveComposerDraftNow recorded.
        let _persistedDrafts = {};
        function _composerDraftLastPersistedForSid(sid){
          return Object.prototype.hasOwnProperty.call(_persistedDrafts, sid) ? _persistedDrafts[sid] : null;
        }
        eval(%(guard)s);
        %(body)s
        """
    ) % {
        "guard": json.dumps(guard_src),
        "sig": sig_src,
        "body": body,
    }
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"


def test_accepted_steer_replacement_text_keeps_persisted_draft():
    """#5585: an accepted local steer must not wipe the persisted draft when the
    user typed replacement text during the steer await. Previously an unguarded
    _clearComposerDraft in _trySteer cleared it; the shared _steerFinalizeComposer
    guard now skips the clear when the live composer holds replacement text."""
    _run_steer_finalize_harness(
        """
        let S = {session:{session_id:'A'}, pendingFiles:[]};
        const msgEl = {value:'my replacement'};   // typed during the steer await
        function $(id){return msgEl;}
        // Accepted local steer for owner 'A', still the live session.
        _steerFinalizeComposer('A','hint',[],false);
        assert.strictEqual(msgEl.value, 'my replacement', 'replacement text must stay in the composer');
        assert.strictEqual(draftClears.length, 0, 'persisted draft must survive replacement text');
        """
    )


def test_accepted_steer_replacement_file_keeps_persisted_draft():
    """#5585: the safe-clear predicate must consider staged files, not just text.
    An empty textarea with a newly staged replacement file must not clear the draft
    (the round-6 defect where _steerComposerAllowsDraftClear ignored files)."""
    _run_steer_finalize_harness(
        """
        let S = {session:{session_id:'A'}, pendingFiles:[{name:'new.pdf'}]}; // staged during await
        const msgEl = {value:''};   // textarea empty
        function $(id){return msgEl;}
        // Text-only steer delivered (no files submitted); a replacement file is staged.
        _steerFinalizeComposer('A','hint',[],false);
        assert.strictEqual(draftClears.length, 0, 'persisted draft must survive a replacement staged file');
        assert.strictEqual(S.pendingFiles.length, 1, 'the newly staged file must not be dropped');
        """
    )


# ── Cross-session (switched-away owner) — the round-7 class ────────────────────
# The owner's replacement content lives only in the server-persisted draft; the
# visible textarea belongs to another session. The guard must read the owner's
# persisted-draft signature, not the textarea. One test per representation x
# context cell that was fixed, plus the current-owner cells re-asserted above.

def test_cross_session_replacement_text_keeps_persisted_draft():
    """Context B, R3: send steer, type replacement, switch session, finalize.
    The owner's persisted replacement draft must NOT be wiped."""
    _run_steer_finalize_harness(
        """
        // Visible session is 'B'; owner 'A' is switched away.
        let S = {session:{session_id:'B'}, pendingFiles:[{name:'B-file.png'}]};
        const msgEl = {value:'unrelated text in session B'};
        function $(id){return msgEl;}
        // loadSession/_saveComposerDraftNow persisted A's replacement text on switch.
        _persistedDrafts['A'] = {text:'my replacement for A', files:[]};
        _steerFinalizeComposer('A','hint',[],false);
        assert.strictEqual(draftClears.length, 0, 'switched-away owner replacement draft must be preserved');
        assert.strictEqual(msgEl.value, 'unrelated text in session B', 'visible session composer untouched');
        assert.strictEqual(S.pendingFiles.length, 1, 'visible session staged files untouched');
        """
    )


def test_cross_session_replacement_file_keeps_persisted_draft():
    """Context B, R3 with files: owner persisted a draft holding a replacement file
    not in the delivered steer set. Must be preserved."""
    _run_steer_finalize_harness(
        """
        let S = {session:{session_id:'B'}, pendingFiles:[]};
        const msgEl = {value:''};
        function $(id){return msgEl;}
        // A's persisted draft (text cleared on submit) has a replacement file that
        // was staged during the await; the steer delivered no files.
        _persistedDrafts['A'] = {text:'', files:[{name:'replacement.pdf', path:'/r.pdf', size:5, type:'application/pdf'}]};
        _steerFinalizeComposer('A','hint',[],false);
        assert.strictEqual(draftClears.length, 0, 'switched-away owner replacement file draft must be preserved');
        """
    )


def test_cross_session_unedited_echo_clears_stale_draft():
    """Context B, R3 safe case: owner switched away WITHOUT editing. The composer text
    is cleared on submit, so its persisted draft is empty text with (at most) the
    delivered files — the consumed steer. That stale echo must be cleared."""
    _run_steer_finalize_harness(
        """
        const f = {name:'a.pdf', path:'/a.pdf', size:9, type:'application/pdf'};
        // Unedited file steer: persisted draft is {text:'', files:[delivered]}.
        let S = {session:{session_id:'B'}, pendingFiles:[]};
        function $(id){return {value:''};}
        _persistedDrafts['A'] = {text:'', files:[f]};
        _steerFinalizeComposer('A','hint',[f],false);
        assert.strictEqual(draftClears.length, 1, 'unedited file-steer echo (empty text) must be cleared');
        assert.strictEqual(draftClears[0].sid, 'A', 'the owner draft is the one cleared');

        // Failure-restore echo shape: persisted text equals the bare msg.
        draftClears = [];
        _persistedDrafts['A'] = {text:'hint', files:[]};
        _steerFinalizeComposer('A','hint',[],false);
        assert.strictEqual(draftClears.length, 1, 'persisted bare-msg echo must be cleared');

        // Explicit steer failure-restore echo: persisted text equals `/steer hint`.
        draftClears = [];
        _persistedDrafts['A'] = {text:'/steer hint', files:[]};
        _steerFinalizeComposer('A','hint',[],true);
        assert.strictEqual(draftClears.length, 1, 'persisted /steer-echo must be cleared');
        """
    )


def test_cross_session_no_persisted_draft_is_safe_noop():
    """Context B, R3: no recorded draft for the owner means nothing non-empty is
    persisted, so the clear is authorized (harmless no-op) and never blocked."""
    _run_steer_finalize_harness(
        """
        let S = {session:{session_id:'B'}, pendingFiles:[]};
        function $(id){return {value:''};}
        // _persistedDrafts empty -> _composerDraftLastPersistedForSid('A') is null.
        _steerFinalizeComposer('A','hint',[],false);
        assert.strictEqual(draftClears.length, 1, 'no persisted draft -> safe to clear');
        """
    )


def test_cross_session_guard_fails_closed_without_persist_helpers():
    """If the persistence helpers are unavailable, the non-current branch must fail
    closed (skip the clear) rather than run blind."""
    import json, shutil, subprocess, textwrap
    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")
    guard_src = _block(COMMANDS_JS, "function _steerComposerSafeToClear", "\nfunction _showSteerRecovery")
    script = textwrap.dedent(
        """
        const assert = require('assert');
        let draftClears = [];
        function renderTray(){}
        function _clearComposerDraft(sid,text,files){draftClears.push({sid});}
        function _steerOwnerIsCurrent(sid){return !!(sid && S && S.session && S.session.session_id===sid);}
        function _steerRestoreText(msg,explicit){return explicit?('/steer '+msg):msg;}
        // Deliberately DO NOT define the persistence helpers the guard needs.
        eval(%(guard)s);
        let S = {session:{session_id:'B'}, pendingFiles:[]};
        function $(id){return {value:''};}
        _steerFinalizeComposer('A','hint',[],false);
        assert.strictEqual(draftClears.length, 0, 'no evidence available -> skip the clear, never run blind');
        """
    ) % {"guard": json.dumps(guard_src)}
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"


def test_failure_restore_preserves_switched_away_replacement_draft():
    """Steer FAILURE path, switched-away owner: _steerRestoreDraftOnFailure must not
    overwrite the owner's replacement draft with the steer echo. Only overwrite when
    the persisted draft is the consumed steer (empty / echo)."""
    import json, shutil, subprocess, textwrap
    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")
    # Extract the failure helper + the guard it delegates to.
    guard_src = _block(COMMANDS_JS, "function _steerComposerSafeToClear", "\nfunction _steerFinalizeComposer")
    restore_src = _block(COMMANDS_JS, "async function _steerRestoreDraftOnFailure", "\n// #5459 gate")
    sig_src = _block(SESSIONS_JS, "function _composerDraftFileSignature(file)", "function _composerDraftPayloadSignatureForSid(sid)")
    script = textwrap.dedent(
        """
        const assert = require('assert');
        %(sig)s
        let _persistedDrafts = {};
        let persistCalls = [];
        function _composerDraftLastPersistedForSid(sid){
          return Object.prototype.hasOwnProperty.call(_persistedDrafts, sid) ? _persistedDrafts[sid] : null;
        }
        function _steerOwnerIsCurrent(sid){return !!(sid && S && S.session && S.session.session_id===sid);}
        function _steerRestoreText(msg,explicit){return explicit?('/steer '+msg):msg;}
        function autoResize(){}
        function renderTray(){}
        function _saveComposerDraftNow(sid,text,files){persistCalls.push({sid,text}); return Promise.resolve();}
        async function _steerPersistDraftForOwner(sid,msg,explicit,files){
          if(!sid) return;
          await _saveComposerDraftNow(sid,_steerRestoreText(msg,explicit),files);
        }
        eval(%(guard)s);
        eval(%(restore)s);
        (async()=>{
          // Owner 'A' switched away; its persisted draft is a REPLACEMENT.
          let S1 = {session:{session_id:'B'}, pendingFiles:[]};
          globalThis.S = S1;
          function $(){ return {value:''}; }
          globalThis.$ = $;
          _persistedDrafts['A'] = {text:'replacement I typed then switched', files:[]};
          await _steerRestoreDraftOnFailure('A','fixthis',true,[]);
          assert.strictEqual(persistCalls.length, 0, 'failure restore must NOT overwrite a replacement draft');

          // Owner 'A' switched away with the consumed steer (empty draft): restore echo.
          persistCalls = [];
          _persistedDrafts = {};   // nothing persisted -> safe to restore the failed steer
          await _steerRestoreDraftOnFailure('A','fixthis',true,[]);
          assert.strictEqual(persistCalls.length, 1, 'failed steer is restored when no replacement exists');
          assert.strictEqual(persistCalls[0].text, '/steer fixthis');
        })().catch(err=>{console.error(err); process.exit(1);});
        """
    ) % {"guard": json.dumps(guard_src), "restore": json.dumps(restore_src), "sig": sig_src}
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"


def test_failure_restore_preserves_current_owner_replacement_text():
    """Steer FAILURE path, current owner: do not clobber replacement text the user
    typed after submit cleared the composer."""
    import json, shutil, subprocess, textwrap
    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")
    guard_src = _block(COMMANDS_JS, "function _steerComposerSafeToClear", "\nfunction _steerFinalizeComposer")
    restore_src = _block(COMMANDS_JS, "async function _steerRestoreDraftOnFailure", "\n// #5459 gate")
    script = textwrap.dedent(
        """
        const assert = require('assert');
        function _steerOwnerIsCurrent(sid){return !!(sid && S && S.session && S.session.session_id===sid);}
        function _steerRestoreText(msg,explicit){return explicit?('/steer '+msg):msg;}
        function autoResize(){}
        function renderTray(){}
        function _composerDraftLastPersistedForSid(){return null;}
        function _composerDraftFilesForPersist(f){return Array.isArray(f)?f:[];}
        function _composerDraftFileSignature(f){return {v:String((f&&f.name)||f||'')};}
        async function _steerPersistDraftForOwner(){throw new Error('current owner must not persist');}
        eval(%(guard)s);
        eval(%(restore)s);
        (async()=>{
          globalThis.S = {session:{session_id:'A'}, pendingFiles:[]};
          // User typed a replacement after submit cleared the composer.
          const msgEl = {value:'my replacement'};
          globalThis.$ = function(){ return msgEl; };
          await _steerRestoreDraftOnFailure('A','fixthis',true,[]);
          assert.strictEqual(msgEl.value, 'my replacement', 'replacement text must not be clobbered by the failed steer');

          // Empty composer (normal failure): restore the failed steer for retry.
          msgEl.value = '';
          await _steerRestoreDraftOnFailure('A','fixthis',true,[]);
          assert.strictEqual(msgEl.value, '/steer fixthis', 'failed steer restored into an empty composer');
        })().catch(err=>{console.error(err); process.exit(1);});
        """
    ) % {"guard": json.dumps(guard_src), "restore": json.dumps(restore_src)}
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"


def test_guard_evidence_covers_effect_source_shape():
    """The non-current-owner branch no longer returns an unconditional true, the
    per-sid persisted draft (the evidence) is maintained at the single persist choke
    point, and the failure-restore path routes through one guarded helper."""
    assert "if(!_steerOwnerIsCurrent(ownerSid))return _steerPersistedDraftSafeToClear(ownerSid,msg,files);" in COMMANDS_JS
    assert "if(!_steerOwnerIsCurrent(ownerSid))return true;" not in COMMANDS_JS, (
        "the guard must not authorize a non-current-owner clear without evidence"
    )
    assert "function _steerPersistedDraftSafeToClear(ownerSid,msg,files)" in COMMANDS_JS
    assert "async function _steerRestoreDraftOnFailure(ownerSid,originalMsg,explicitSteer,filesSnapshot)" in COMMANDS_JS
    # All three failure sites route through the one helper; no inline restore blocks remain.
    trysteer_body = _block(COMMANDS_JS, "async function _trySteer(", "\nasync function cmdTitle")
    assert trysteer_body.count("_steerRestoreDraftOnFailure(ownerSid,originalMsg,explicitSteer,pendingFilesSnapshot)") == 3
    assert "inp.value=_steerRestoreText(originalMsg,explicitSteer)" not in trysteer_body, (
        "failure restore must not clobber the composer inline; route through the guarded helper"
    )
    assert "const _composerDraftLastPersistedBySid = new Map();" in SESSIONS_JS
    remember_body = _block(
        SESSIONS_JS,
        "function _rememberComposerDraftPayloadState(sid, text, files)",
        "function _composerDraftLastPersistedForSid",
    )
    assert "_composerDraftLastPersistedBySid.set(sid, { text: normalizedText, files: _composerDraftFilesForPersist(normalizedFiles) });" in remember_body
    assert "_composerDraftLastPersistedBySid.delete(sid);" in remember_body
    assert "function _composerDraftLastPersistedForSid(sid)" in SESSIONS_JS
