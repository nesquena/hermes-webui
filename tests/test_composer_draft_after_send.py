"""Regression coverage for stale composer_draft restoration after send."""
from pathlib import Path
import json
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
COMMANDS_JS = ROOT.joinpath("static", "commands.js").read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _draft_save_helper_block() -> str:
    start = SESSIONS_JS.find("let _draftSaveTimer = null;")
    end = SESSIONS_JS.find("function _restoreComposerDraft(draft, targetSid, opts={}) {")
    assert start != -1, "draft helper block start marker not found"
    assert end != -1, "draft helper block end marker not found"
    return SESSIONS_JS[start:end]


def _run_draft_save_helper(caller: str, api_responses: list[dict]) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    harness = textwrap.dedent(
        """
        const responses = %(responses)s;
        const filesInput = %(files)s;
        const initialSid = 'sid_old';
        const expectedPayload = {
          text: 'hello from harness',
          files: _composerDraftFilesForPersist(filesInput),
        };
        let callIndex = 0;
        const state = {
          calls: [],
          rememberCalls: [],
          warns: [],
          expectedPayload,
        };

        const originalWarn = console.warn;
        console.warn = (...args) => {
          state.warns.push(args.map((value) => String(value)).join(' '));
          if (typeof originalWarn === 'function') {
            originalWarn.apply(console, args);
          }
        };

        async function api(path, opts) {
          const body = (typeof opts.body === 'string') ? JSON.parse(opts.body) : {};
          state.calls.push(body);
          const next = responses[callIndex++];
          if (!next) {
            throw new Error('unexpected /api/session/draft call');
          }
          if (next.throw) {
            const err = new Error(next.error || 'draft draft save failed');
            if (next.status) {
              err.status = Number(next.status);
            }
            if (typeof next.body === 'string') {
              err.body = next.body;
            } else if (next.body) {
              err.body = JSON.stringify(next.body);
            }
            throw err;
          }
          if (Number(next.status) === 409) {
            const err = new Error(next.error || 'Session moved');
            err.status = 409;
            err.body = typeof next.body === 'string' ? next.body : JSON.stringify(next.body || {});
            throw err;
          }
          return Object.assign({ok: true}, next.response || {});
        }

        setTimeout = (fn, _ms) => {
          if (typeof fn === 'function') fn();
          return 0;
        };
        clearTimeout = () => {};

        %(draft_helpers)s

        const S = { session: { session_id: initialSid, profile: 'default' } };
        const _rememberComposerDraftPayloadStateOriginal = _rememberComposerDraftPayloadState;
        _rememberComposerDraftPayloadState = (sid, text, files) => {
          state.rememberCalls.push({
            sid,
            text: String(text || ''),
            files: Array.isArray(files) ? files.filter(Boolean) : [],
          });
          _rememberComposerDraftPayloadStateOriginal(sid, text, files);
        };

        (async () => {
          if ('%(caller)s' === 'immediate') {
            await _saveComposerDraftNow(initialSid, expectedPayload.text, filesInput);
          } else {
            _saveComposerDraft(initialSid, expectedPayload.text, filesInput);
            await new Promise(resolve => setImmediate(resolve));
          }
          console.log(JSON.stringify({
            calls: state.calls,
            rememberCalls: state.rememberCalls,
            knownPayloadSids: Array.from(_composerDraftKnownPayloadSessions).sort(),
            localDraft: S.session.composer_draft || null,
            expectedPayload,
            warns: state.warns,
            ok: true,
          }));
        })().catch(error => {
          const serialized = {
            ok: false,
            error: String(error && error.message ? error.message : error || ''),
            status: error && Number(error.status),
            calls: state.calls,
            rememberCalls: state.rememberCalls,
            knownPayloadSids: Array.from(_composerDraftKnownPayloadSessions).sort(),
            localDraft: S.session.composer_draft || null,
            expectedPayload,
            warns: state.warns,
          };
          console.log(JSON.stringify(serialized));
        });
        """
    ) % {
        "responses": json.dumps(api_responses),
        "files": json.dumps(
            [
                {
                    "name": "proof.txt",
                    "size": 42,
                    "type": "text/plain",
                    "path": "/tmp/proof.txt",
                    "lastModified": 123,
                    "extra": "ignored",
                },
            ]
        ),
        "draft_helpers": _draft_save_helper_block(),
        "caller": caller,
    }
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


@pytest.mark.parametrize("caller", ["debounced", "immediate"])
@pytest.mark.parametrize("scenario", ["changed-200", "structured-409"])
def test_draft_save_paths_use_authoritative_session_id_after_rotation(caller, scenario):
    if scenario == "changed-200":
        responses = [{"status": 200, "response": {"ok": True, "session_id": "sid_authoritative"}}]
    else:
        responses = [
            {
                "status": 409,
                "error": "Session moved",
                "body": {
                    "error": "Session moved",
                    "code": "session_moved",
                    "session_id": "sid_authoritative",
                },
            },
            {"status": 200, "response": {"ok": True, "session_id": "sid_authoritative"}},
        ]

    out = _run_draft_save_helper(caller, responses)
    assert out["ok"] is True

    expected_sid = "sid_authoritative"
    old_sid = "sid_old"
    if scenario == "changed-200":
        assert len(out["calls"]) == 1
        first = out["calls"][0]
        assert first["session_id"] == old_sid
    else:
        assert len(out["calls"]) == 2
        assert out["calls"][0]["session_id"] == old_sid
        assert out["calls"][1]["session_id"] == expected_sid

    first_call = out["calls"][0]
    last_call = out["calls"][-1]
    assert first_call["text"] == out["expectedPayload"]["text"]
    assert last_call["text"] == out["expectedPayload"]["text"]
    assert first_call["files"] == out["expectedPayload"]["files"]
    assert last_call["files"] == out["expectedPayload"]["files"]
    assert out["rememberCalls"][0]["sid"] == expected_sid
    assert len(out["rememberCalls"]) == 1
    assert out["knownPayloadSids"] == [expected_sid]
    if scenario == "structured-409":
        assert last_call["session_id"] == expected_sid
    else:
        assert first_call["session_id"] == old_sid


@pytest.mark.parametrize("caller", ["debounced", "immediate"])
@pytest.mark.parametrize(
    "scenario",
    [
        "second-redirect",
        "replay-failure",
        "invalid-sid",
    ],
)
def test_draft_save_paths_reject_replayed_authority_or_bad_response(caller, scenario):
    if scenario == "second-redirect":
        responses = [
            {
                "status": 409,
                "error": "Session moved",
                "body": {
                    "error": "Session moved",
                    "code": "session_moved",
                    "session_id": "sid_authoritative",
                },
            },
            {
                "status": 409,
                "error": "Session moved",
                "body": {
                    "error": "Session moved",
                    "code": "session_moved",
                    "session_id": "sid_authoritative",
                },
            },
        ]
    elif scenario == "replay-failure":
        responses = [
            {
                "status": 409,
                "error": "Session moved",
                "body": {
                    "error": "Session moved",
                    "code": "session_moved",
                    "session_id": "sid_authoritative",
                },
            },
            {
                "throw": True,
                "status": 503,
                "error": "server down",
            },
        ]
    else:
        responses = [
            {
                "status": 200,
                "response": {
                    "ok": True,
                    "session_id": "sid_!@#",
                },
            },
        ]

    out = _run_draft_save_helper(caller, responses)
    sid_old = "sid_old"
    assert out["calls"]
    assert out["rememberCalls"] == []
    for call in out["calls"]:
        assert call["text"] == out["expectedPayload"]["text"]
        assert call["files"] == out["expectedPayload"]["files"]
    if caller == "immediate":
        assert out["knownPayloadSids"] == []
    else:
        assert out["knownPayloadSids"] == [sid_old]
    if scenario == "second-redirect":
        assert len(out["calls"]) == 2
    elif scenario == "replay-failure":
        assert len(out["calls"]) == 2
    else:
        assert len(out["calls"]) == 1

    if caller == "immediate":
        assert out["ok"] is False
        assert out["error"]
        assert out["status"] in (None, 409, 500, 503)
        assert not out["warns"]
    else:
        assert out["ok"] is True
        assert out["warns"]
        assert out["localDraft"] == out["expectedPayload"]


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
