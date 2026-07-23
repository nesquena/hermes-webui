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
    assert "_clearComposerDraft(S.session.session_id,text" not in busy_body
    try_steer_body = _block(COMMANDS_JS, "async function _trySteer(", "\nasync function cmdTitle")
    assert "_clearComposerDraft(ownerSid,_steerRestoreText(originalMsg,explicitSteer),pendingFilesSnapshot)" in try_steer_body, (
        "delivered steer must clear the captured owner draft with the submitted payload signature"
    )


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
