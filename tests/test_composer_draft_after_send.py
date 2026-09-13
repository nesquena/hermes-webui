"""Regression coverage for stale composer_draft restoration after send."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
COMMANDS_JS = ROOT.joinpath("static", "commands.js").read_text(encoding="utf-8")
I18N_JS = ROOT.joinpath("static", "i18n.js").read_text(encoding="utf-8")


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


def test_late_old_session_clear_does_not_cancel_visible_debounce():
    """A late A settlement must not cancel B's already-scheduled draft save."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    clear_fn = _block(SESSIONS_JS, "function _clearComposerDraftIfMatches", "const SESSION_VIEWED_COUNTS_KEY")
    save_fn = _block(SESSIONS_JS, "function _saveComposerDraft(sid, text, files)", "function _composerDraftHasPayload")
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        let _draftSaveTimer = null;
        const _DRAFT_SAVE_DELAY_MS = 20;
        const _composerDraftKnownPayloadSessions = new Set();
        const saves = [];
        let resolveA;
        let S = {session:{session_id:'A'}};
        function _composerDraftFilesForPersist(files) { return Array.isArray(files) ? files : []; }
        function _composerDraftHasPayload(text, files) { return !!(text || (files && files.length)); }
        function _clearComposerDraftRestoreSuppression() {}
        function _clearRememberedNewChatDraftSession() {}
        function _suppressComposerDraftRestoreAfterSubmit() {}
        function _rememberComposerDraftPayloadState() {}
        function api(url, options) {
          const payload = JSON.parse(options.body);
          if (payload.session_id === 'A') return new Promise(resolve => { resolveA = resolve; });
          saves.push(payload);
          return Promise.resolve({});
        }
        eval(%s);
        eval(%s);

        S = {session:{session_id:'B'}};
        _saveComposerDraft('B', 'B draft', []);
        const lateClear = _clearComposerDraftIfMatches('A', 'captured', []);
        resolveA({compare_cleared:true});
        lateClear.then(async () => {
          await new Promise(resolve => setTimeout(resolve, 40));
          assert.deepStrictEqual(saves.map(item => item.session_id), ['B']);
          assert.strictEqual(saves[0].text, 'B draft');
        }).catch(error => { console.error(error); process.exit(1); });
        """
    ) % (json.dumps(clear_fn), json.dumps(save_fn))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"cross-session debounce harness failed: {proc.stderr}"


def test_interrupt_compare_clear_suppresses_current_owner_stale_restore():
    """A stale same-owner draft response must not repopulate during settlement."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    file_sig = _block(SESSIONS_JS, "function _composerDraftFileSignature", "function _composerDraftFilesForPersist")
    files_persist = _block(SESSIONS_JS, "function _composerDraftFilesForPersist", "function _composerDraftPayloadSignature")
    payload_sig = _block(SESSIONS_JS, "function _composerDraftPayloadSignature", "function _composerDraftPayloadSignatureForSid")
    payload_for_sid = _block(SESSIONS_JS, "function _composerDraftPayloadForSid", "function _composerDraftPayloadSignatureForSid")
    sid_sig = _block(SESSIONS_JS, "function _composerDraftPayloadSignatureForSid", "function _suppressComposerDraftRestoreAfterSubmit")
    suppression = _block(SESSIONS_JS, "function _suppressComposerDraftRestoreAfterSubmit", "function _profileMatchesActiveProfile")
    clear_fn = _block(SESSIONS_JS, "function _clearComposerDraftIfMatches", "const SESSION_VIEWED_COUNTS_KEY")
    restore_fn = _block(SESSIONS_JS, "function _restoreComposerDraft", "// Clear the saved draft")
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:'local'};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function updateSendBtn() {}
        const localStorage = {getItem(){return null;}, removeItem(){}};
        const _composerDraftRestoreSuppressedUntilBySid = new Map();
        const _COMPOSER_DRAFT_RESTORE_SUPPRESS_MS = 30000;
        let _draftSaveTimer = null;
        let _loadingSessionId = null;
        let S = {session:{session_id:'A', composer_draft:{text:'captured', files:[]}}};
        function _composerDraftHasPayload(text, files) { return !!(text || (files && files.length)); }
        function _clearRememberedNewChatDraftSession() {}
        function _rememberComposerDraftPayloadState(sid, text, files) {
          if (S.session.session_id === sid) S.session.composer_draft = {text, files};
        }
        let resolvePost;
        function api() { return new Promise(resolve => { resolvePost = resolve; }); }
        eval(%s);
        eval(%s);
        eval(%s);
        eval(%s);
        eval(%s);
        eval(%s);
        eval(%s);

        const pending = _clearComposerDraftIfMatches('A', 'captured', []);
        _restoreComposerDraft({text:'captured', files:[]}, 'A');
        assert.strictEqual(input.value, 'local', 'the captured stale draft must stay suppressed while POST is pending');
        resolvePost({compare_cleared:true});
        pending.then(() => {
          input.value = 'local';
          _restoreComposerDraft({text:'newer', files:[]}, 'A');
          assert.strictEqual(input.value, 'newer', 'a newer visible draft must clear stale suppression');
        }).catch(error => { console.error(error); process.exit(1); });
        """
    ) % tuple(json.dumps(part) for part in (
        file_sig, files_persist, payload_sig, payload_for_sid, sid_sig, suppression, clear_fn + restore_fn,
    ))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"restore suppression harness failed: {proc.stderr}"


def test_non_empty_draft_save_clears_submit_restore_suppression():
    save_body = _block(SESSIONS_JS, "function _saveComposerDraft(sid, text, files)", "function _composerDraftHasPayload")
    assert "_clearComposerDraftRestoreSuppression(sid);" in save_body
    now_body = _block(SESSIONS_JS, "function _saveComposerDraftNow(sid, text, files, force=false, clearTimer=true, expectedStreamId, interruptRequest=false)", "// Restore composer draft")
    assert "!force && !_composerDraftHasPayload" in now_body
    assert "_clearComposerDraftRestoreSuppression(sid);" in now_body
    assert "if (clearTimer) clearTimeout(_draftSaveTimer);" in now_body
    assert "expectedStreamId" in now_body
    assert "expected_stream_id" in now_body

    clear_matches_body = _block(SESSIONS_JS, "function _clearComposerDraftIfMatches", "const SESSION_VIEWED_COUNTS_KEY")
    assert "expectedStreamId" in clear_matches_body
    assert "expected_stream_id" in clear_matches_body


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
    assert "arguments.length>=3" in helper_body
    assert "_saveComposerDraftNow(sid,textMatches?'':draftText,remaining,true)" in helper_body
    assert "S.session.session_id!==sid" in helper_body

    in_progress_body = _block(MESSAGES_JS, "if (_sendInProgress) {", "  _sendInProgress = true;")
    assert "_clearComposerAfterQueuedSelectionSend();" in in_progress_body
    assert "_clearComposerDraft(_targetSid,_text,S.pendingFiles?[...S.pendingFiles]:[])" in in_progress_body

    busy_body = _block(MESSAGES_JS, "if(S.busy||compressionRunning){", "  if(S.session&&(S.session.read_only||S.session.is_read_only))")
    assert "_clearComposerAfterQueuedSelectionSend(S.session&&S.session.session_id);" in busy_body
    assert busy_body.count("_clearComposerAfterQueuedSelectionSend(S.session&&S.session.session_id);") >= 1
    assert "await _tryInterrupt(text);" in busy_body
    assert "_clearComposerDraft(S.session.session_id,text" not in busy_body
    try_interrupt_body = _block(COMMANDS_JS, "async function _tryInterrupt(", "\nasync function cmdInterrupt")
    assert "'/api/chat/interrupt'" in try_interrupt_body
    assert "retries:0" in try_interrupt_body
    assert "request_id" not in try_interrupt_body
    assert "queueSessionMessage(ownerSid" in try_interrupt_body
    assert "cancelStream(cancelReason)" in try_interrupt_body
    try_steer_body = _block(COMMANDS_JS, "async function _trySteer(", "\nasync function cmdTitle")
    assert "_clearComposerDraft(ownerSid,_steerRestoreText(originalMsg,explicitSteer),pendingFilesSnapshot)" in try_steer_body, (
        "delivered steer must clear the captured owner draft with the submitted payload signature"
    )


def test_accepted_interrupt_settles_captured_draft_in_the_interrupt_request():
    """Accepted redirect must carry settlement data and never issue a second clear request."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:'captured \\n'};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function showToast() {}
        function t(key) { return key; }
        function _composerDraftFilesForPersist(files) { return Array.isArray(files) ? files : []; }
        function _suppressComposerDraftRestoreAfterSubmit() {}
        function _rememberComposerDraftPayloadState() {}
        let clearCalls = 0;
        function _clearComposerDraftIfMatches() { clearCalls += 1; }
        const bodies = [];
        let S = {
          session:{session_id:'A', active_stream_id:'old'},
          activeStreamId:'old',
          pendingFiles:[],
          _composerRevision:0,
        };
        async function api(url, options) {
          assert.strictEqual(url, '/api/chat/interrupt');
          assert.strictEqual(options.retries, 0);
          bodies.push(JSON.parse(options.body));
          return {
            accepted:true,
            fallback:null,
            stream_id:'old',
            compare_cleared:true,
            draft:{text:'', files:[]},
          };
        }
        eval(%s);
        (async () => {
          assert.strictEqual(await _tryInterrupt('captured'), true);
          assert.deepStrictEqual(bodies, [{
            session_id:'A',
            stream_id:'old',
            text:'captured',
            draft_text:'captured \\n',
            draft_files:[],
          }]);
          assert.strictEqual(clearCalls, 0, 'accepted redirect must not start a second draft cleanup request');
          assert.strictEqual(input.value, '');
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % json.dumps(interrupt_src)
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"single-request interrupt harness failed: {proc.stderr}"


def test_interrupt_redirect_is_single_attempt_and_preserves_owner_draft():
    """Exercise redirect, fallback, transport ambiguity, edits, files, and switches."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    clear_src = _block(
        MESSAGES_JS,
        "function _clearComposerAfterQueuedSelectionSend",
        "function _flushSelectionBlocksToComposer",
    )
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:''};
        const document = {getElementById(id) { return id === 'msg' ? input : null; }};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function renderTray() {}
        function _clearPendingSelections() {}
        function t(key) { return key; }
        function showToast() {}
        function updateQueueBadge() {}
        function _chatPayloadModelState() { return {model:'m', model_provider:'p'}; }
        const saves = [];
        const clears = [];
        async function _saveComposerDraftNow(sid, text, files) { saves.push({sid, text, files}); }
        async function _clearComposerDraftIfMatches(sid, text, files) { clears.push({sid, text, files}); }
        const queued = [];
        function queueSessionMessage(sid, payload) { queued.push({sid, payload}); }
        let cancels = 0;
        async function cancelStream(reason) {
          assert.ok(reason === 'busy-interrupt' || reason === 'reason');
          if (!Object.prototype.hasOwnProperty.call(S.session, 'active_stream_id')) {
            cancels += 1;
            return true;
          }
          assert.strictEqual(S.activeStreamId, 'old');
          assert.strictEqual(S.session.active_stream_id, 'old');
          cancels += 1;
          return true;
        }
        let mode = 'accepted';
        let resolveApi = null;
        let httpCalls = 0;
        let S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        async function api(url, options) {
          assert.strictEqual(url, '/api/chat/interrupt');
          assert.strictEqual(options.retries, 0);
          const body = JSON.parse(options.body);
          assert.ok(!Object.prototype.hasOwnProperty.call(body, 'request_id'));
          httpCalls += 1;
          if (mode === 'wait') return new Promise(resolve => { resolveApi = resolve; });
          if (mode === 'ambiguous') throw new TypeError('response lost');
          if (mode === 'server-error') { const e = new Error('bad gateway'); e.status = 502; throw e; }
          if (mode === 'timeout') { const e = new Error('timed out'); e.timeout = true; throw e; }
          if (mode === 'malformed') return 'not-json';
          if (mode === 'redirect-error') return {accepted:false, fallback:'redirect_error', stream_id:'old'};
          if (mode === 'reject') return {accepted:false, fallback:'redirect_rejected', stream_id:'old'};
          return {accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}};
        }
        eval(%s);
        eval(%s);

        function reset() {
          mode = 'accepted';
          resolveApi = null;
          S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
          input.value = '';
        }

        async function acceptedClearsCapturedOnly() {
          reset();
          input.value = 'captured';
          mode = 'wait';
          const pending = _tryInterrupt('captured');
          assert.ok(resolveApi);
          resolveApi({accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}});
          assert.strictEqual(await pending, true);
          assert.strictEqual(input.value, '');
          assert.strictEqual(clears.length, 0, 'accepted redirect must settle in the interrupt response');
        }

        async function laterEditsAndFilesSurvive() {
          reset();
          input.value = 'captured';
          mode = 'wait';
          const laterFile = {name:'later.pdf'};
          const pending = _tryInterrupt('captured');
          input.value = 'later edit';
          S.pendingFiles = [laterFile];
          S._composerRevision = 1;
          resolveApi({accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}});
          assert.strictEqual(await pending, true);
          assert.strictEqual(input.value, 'later edit');
          assert.deepStrictEqual(S.pendingFiles, [laterFile]);
          assert.deepStrictEqual(saves.at(-1), {sid:'A', text:'later edit', files:[laterFile]});
        }

        async function sessionSwitchDoesNotTouchSuccessor() {
          reset();
          input.value = 'captured';
          mode = 'wait';
          const pending = _tryInterrupt('captured');
          S = {session:{session_id:'B', active_stream_id:'successor'}, activeStreamId:'successor', pendingFiles:[]};
          input.value = 'successor draft';
          const successorFile = {name:'successor.pdf'};
          S.pendingFiles = [successorFile];
          resolveApi({accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}});
          assert.strictEqual(await pending, true);
          assert.strictEqual(input.value, 'successor draft');
          assert.deepStrictEqual(S.pendingFiles, [successorFile]);
          assert.strictEqual(clears.length, 0, 'a switched owner must not trigger captured-payload cleanup');
        }

        async function ambiguousResultsDoNotFallback() {
          for (const nextMode of ['ambiguous','server-error','timeout','malformed','redirect-error']) {
            reset();
            mode = nextMode;
            input.value = 'captured';
            const beforeCalls = httpCalls;
            const result = await _tryInterrupt('captured');
            assert.strictEqual(result, false);
            assert.strictEqual(httpCalls, beforeCalls + 1);
            assert.strictEqual(queued.length, 0);
            assert.strictEqual(cancels, 0);
            assert.strictEqual(input.value, 'captured');
            assert.deepStrictEqual(saves.at(-1), {sid:'A', text:'captured', files:[]});
          }
        }

        async function definitiveRejectionQueuesAndCancels() {
          reset();
          mode = 'reject';
          input.value = 'captured';
          assert.strictEqual(await _tryInterrupt('captured'), false);
          assert.strictEqual(queued.length, 1);
          assert.strictEqual(cancels, 1);
          assert.deepStrictEqual(queued[0].payload.files, []);
          assert.deepStrictEqual(clears.at(-1), {sid:'A', text:'captured', files:[]});
        }

        async function staleOrRotatedStreamDoesNotQueueOrCancel() {
          for (const next of [
            {active:'successor', sessionActive:'successor'},
            {active:'old', sessionActive:'successor'},
          ]) {
            reset();
            mode = 'reject';
            input.value = 'captured';
            const queuedBefore = queued.length;
            const cancelsBefore = cancels;
            const savesBefore = saves.length;
            const pending = _tryInterrupt('captured');
            S.activeStreamId = next.active;
            S.session.active_stream_id = next.sessionActive;
            await pending;
            assert.strictEqual(queued.length, queuedBefore, 'stale redirect fallback must not strand a queue item');
            assert.strictEqual(cancels, cancelsBefore, 'stale redirect fallback must not cancel a successor');
            assert.strictEqual(input.value, 'captured');
            assert.strictEqual(saves.length, savesBefore, 'stale stream metadata must not write a captured draft');
          }
        }

        async function missingServerStreamMetadataFailsClosed() {
          reset();
          mode = 'reject';
          input.value = 'captured';
          delete S.session.active_stream_id;
          const queuedBefore = queued.length;
          const cancelsBefore = cancels;
          const savesBefore = saves.length;
          await _tryInterrupt('captured');
          assert.strictEqual(queued.length, queuedBefore, 'missing server stream metadata must not queue');
          assert.strictEqual(cancels, cancelsBefore, 'missing server stream metadata must not cancel');
          assert.strictEqual(input.value, 'captured');
          assert.strictEqual(saves.length, savesBefore, 'missing stream metadata must not write a captured draft');
        }

        async function attachmentsSkipRedirect() {
          reset();
          const file = {name:'attachment.pdf'};
          input.value = 'with attachment';
          S.pendingFiles = [file];
          const beforeCalls = httpCalls;
          assert.strictEqual(await _tryInterrupt('with attachment'), false);
          assert.strictEqual(httpCalls, beforeCalls);
          assert.strictEqual(queued.length, 2);
          assert.deepStrictEqual(queued[1].payload.files, [file]);
          assert.strictEqual(cancels, 2);
          assert.deepStrictEqual(S.pendingFiles, []);
        }

        async function attachmentsWithoutCancellableStreamStayDraft() {
          reset();
          const file = {name:'attachment-dead.pdf'};
          input.value = 'with attachment';
          S.pendingFiles = [file];
          S.activeStreamId = null;
          S.session.active_stream_id = null;
          const queuedBefore = queued.length;
          const cancelsBefore = cancels;
          const savesBefore = saves.length;
          assert.strictEqual(await _tryInterrupt('with attachment'), false);
          assert.strictEqual(queued.length, queuedBefore, 'dead attachment fallback must not strand a queue item');
          assert.strictEqual(cancels, cancelsBefore, 'dead attachment fallback must not cancel without an owner');
          assert.strictEqual(input.value, 'with attachment');
          assert.deepStrictEqual(S.pendingFiles, [file]);
          assert.strictEqual(saves.length, savesBefore, 'missing stream metadata must not write a captured draft');
        }

        (async () => {
          await acceptedClearsCapturedOnly();
          await laterEditsAndFilesSurvive();
          await sessionSwitchDoesNotTouchSuccessor();
          await ambiguousResultsDoNotFallback();
          await definitiveRejectionQueuesAndCancels();
          await staleOrRotatedStreamDoesNotQueueOrCancel();
          await missingServerStreamMetadataFailsClosed();
          await attachmentsSkipRedirect();
          await attachmentsWithoutCancellableStreamStayDraft();
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % (json.dumps(interrupt_src), json.dumps(clear_src))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"interrupt harness failed: {proc.stderr}"


def test_interrupt_fallback_settles_remembered_prefix_and_files():
    """Queue/cancel fallback must compare the persisted payload, not live input."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:''};
        const oldFile = {name:'old.pdf'};
        const sentFile = {name:'sent.pdf'};
        const newerFile = {name:'newer.pdf'};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function renderTray() {}
        function updateQueueBadge() {}
        function t(key) { return key; }
        function showToast() {}
        function _chatPayloadModelState() { return {}; }
        function _composerDraftFilesForPersist(files) { return Array.isArray(files) ? files : []; }
        function _composerDraftPayloadForSid(sid) {
          return S.session && S.session.session_id === sid ? S.session.composer_draft : null;
        }
        function _suppressComposerDraftRestoreAfterSubmit() {}
        function _rememberComposerDraftPayloadState(sid, text, files) {
          if (S.session && S.session.session_id === sid) S.session.composer_draft = {text, files};
        }
        const clears = [];
        let persisted = null;
        let newerOnClear = false;
        let deferClear = false;
        let releaseClear = null;
        let cancelMode = 'idle';
        function settleClear(sid, text, files, expectedStreamId) {
          const streamMatches = S.activeStreamId === expectedStreamId
            && S.session.active_stream_id === expectedStreamId;
          const sessionIsIdle = !S.activeStreamId && !S.session.active_stream_id;
          if (newerOnClear) {
            persisted = {text:'server newer', files:[newerFile]};
            S.session.composer_draft = persisted;
            return {compare_cleared:false, draft:persisted};
          }
          if (!streamMatches && !sessionIsIdle) {
            return {compare_cleared:false, draft:persisted};
          }
          persisted = {text:'', files:[]};
          S.session.composer_draft = persisted;
          return {compare_cleared:true, draft:persisted};
        }
        function _clearComposerDraftIfMatches(sid, text, files, expectedStreamId, interruptRequest) {
          clears.push({sid, text, files, expectedStreamId, interruptRequest});
          if (!deferClear) return Promise.resolve(settleClear(sid, text, files, expectedStreamId));
          return new Promise(resolve => {
            releaseClear = () => resolve(settleClear(sid, text, files, expectedStreamId));
          });
        }
        const queued = [];
        function queueSessionMessage(sid, payload) { queued.push({sid, payload}); }
        let cancels = 0;
        async function cancelStream() {
          cancels += 1;
          if (cancelMode === 'idle') {
            S.activeStreamId = null;
            S.session.active_stream_id = null;
          } else if (cancelMode === 'successor') {
            S.activeStreamId = 'successor';
            S.session.active_stream_id = 'successor';
            S.session.composer_draft = {text:'captured', files:[oldFile]};
          }
          return true;
        }
        let S = {session:{session_id:'A', active_stream_id:'old', composer_draft:{text:'captured', files:[oldFile]}}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        async function api(url) {
          assert.strictEqual(url, '/api/chat/interrupt');
          return {accepted:false, fallback:'redirect_rejected', stream_id:'old'};
        }
        eval(%s);

        function reset(files=[]) {
          newerOnClear = false;
          deferClear = false;
          releaseClear = null;
          cancelMode = 'idle';
          persisted = {text:'captured', files:[oldFile]};
          S = {session:{session_id:'A', active_stream_id:'old', composer_draft:{text:'captured', files:[oldFile]}}, activeStreamId:'old', pendingFiles:files, _composerRevision:0};
          input.value = 'captured more';
        }

        async function cancelBeforeCompareClear() {
          reset();
          deferClear = true;
          const pending = _tryInterrupt('captured more');
          for (let i = 0; i < 8 && !releaseClear; i += 1) await Promise.resolve();
          assert.ok(releaseClear, 'compare-clear must start before cancellation is awaited');
          assert.strictEqual(S.activeStreamId, null, 'fallback cancellation must not wait for cleanup');
          releaseClear();
          await pending;
          assert.deepStrictEqual(persisted, {text:'', files:[]}, 'idle cancellation still permits stale cleanup');
        }

        async function successorBeforeCompareClear() {
          reset();
          deferClear = true;
          cancelMode = 'successor';
          const pending = _tryInterrupt('captured more');
          for (let i = 0; i < 8 && !releaseClear; i += 1) await Promise.resolve();
          assert.ok(releaseClear, 'successor cleanup must remain pending');
          assert.strictEqual(S.activeStreamId, 'successor');
          input.value = 'successor draft';
          S._composerRevision = 1;
          releaseClear();
          await pending;
          assert.deepStrictEqual(
            persisted,
            {text:'captured', files:[oldFile]},
            'successor ownership must reject compare-clear even for the same payload',
          );
          assert.strictEqual(input.value, 'successor draft', 'a newer client draft must survive cleanup rejection');
        }

        (async () => {
          reset();
          await _tryInterrupt('captured more');
          assert.deepStrictEqual(clears.at(-1), {
            sid:'A', text:'captured', files:[oldFile], expectedStreamId:'old', interruptRequest:true,
          });

          reset([sentFile]);
          await _tryInterrupt('captured more');
          assert.deepStrictEqual(clears.at(-1), {
            sid:'A', text:'captured', files:[oldFile], expectedStreamId:'old', interruptRequest:true,
          }, 'attachment fallback must settle the remembered files too');
          assert.deepStrictEqual(queued.at(-1).payload.files, [sentFile]);

          reset();
          newerOnClear = true;
          await _tryInterrupt('captured more');
          assert.deepStrictEqual(persisted, {text:'server newer', files:[newerFile]}, 'newer server draft must survive compare false');
          assert.deepStrictEqual(S.session.composer_draft, {text:'server newer', files:[newerFile]});
          assert.strictEqual(cancels, 3);
          await cancelBeforeCompareClear();
          await successorBeforeCompareClear();
          assert.strictEqual(cancels, 5);
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % json.dumps(interrupt_src)
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"remembered fallback harness failed: {proc.stderr}"


def test_interrupt_preservation_guard_rejects_stale_deferred_writes():
    """Preservation saves require both stream markers and recheck the guard at write time."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:'captured'};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function renderTray() {}
        function t(key) { return key; }
        const toasts = [];
        function showToast(message, ms, type) { toasts.push({message, ms, type}); }
        const calls = [];
        const persisted = [];
        let releaseSave = null;
        let deferSave = false;
        function _saveComposerDraftNow(sid, text, files, _force, _clearTimer, expectedStreamId, interruptRequest) {
          calls.push({sid, text, files, expectedStreamId, interruptRequest});
          const commit = () => {
            if (S.session && String(S.session.active_stream_id || '') === String(expectedStreamId)) {
              persisted.push({sid, text, files});
            }
          };
          if (!deferSave) { commit(); return Promise.resolve(); }
          return new Promise(resolve => { releaseSave = () => { commit(); resolve(); }; });
        }
        async function api() { throw new TypeError('response lost'); }
        let S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        eval(%s);

        function reset() {
          deferSave = false;
          releaseSave = null;
          calls.length = 0;
          persisted.length = 0;
          toasts.length = 0;
          input.value = 'captured';
          S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        }

        (async () => {
          reset();
          deferSave = true;
          const pending = _tryInterrupt('captured');
          for (let i = 0; i < 8 && !releaseSave; i += 1) await Promise.resolve();
          assert.ok(releaseSave, 'ambiguous preservation must reach the guarded save');
          assert.strictEqual(calls[0].expectedStreamId, 'old');
          assert.strictEqual(calls[0].interruptRequest, true);
          S = {session:{session_id:'A', active_stream_id:'successor'}, activeStreamId:'successor', pendingFiles:[], _composerRevision:0};
          input.value = 'successor draft';
          releaseSave();
          await pending;
          assert.deepStrictEqual(persisted, [], 'old deferred save must be rejected after successor admission');
          assert.strictEqual(toasts.at(-1).message, 'cmd_interrupt_uncertain_preserved');

          reset();
          delete S.session.active_stream_id;
          input.value = 'newer edit';
          S._composerRevision = 1;
          await _tryInterrupt('captured');
          assert.deepStrictEqual(calls, [], 'missing server stream metadata must fail closed');
          assert.strictEqual(input.value, 'newer edit');
          assert.strictEqual(toasts.at(-1).message, 'cmd_interrupt_uncertain_preserved');

          reset();
          await _tryInterrupt('captured');
          assert.deepStrictEqual(calls, [{sid:'A', text:'captured', files:[], expectedStreamId:'old', interruptRequest:true}]);
          assert.deepStrictEqual(persisted, [{sid:'A', text:'captured', files:[]}]);
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % json.dumps(interrupt_src)
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"stale preservation harness failed: {proc.stderr}"


def test_interrupt_preservation_network_failure_is_one_attempt_and_warns():
    """An uncertain interrupt and its guarded draft preservation each make one POST."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    save_src = _block(
        SESSIONS_JS,
        "function _saveComposerDraftNow(sid, text, files, force=false, clearTimer=true, expectedStreamId, interruptRequest=false)",
        "// Restore composer draft",
    )
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:'captured'};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function renderTray() {}
        function t(key) { return key; }
        const toasts = [];
        function showToast(message, ms, type) { toasts.push({message, ms, type}); }
        function _composerDraftFilesForPersist(files) { return Array.isArray(files) ? files : []; }
        function _composerDraftHasPayload(text, files) { return !!(text || (files && files.length)); }
        function _sessionComposerDraftHasPayload() { return false; }
        function _composerDraftPayloadForSid() { return null; }
        function _clearComposerDraftRestoreSuppression() {}
        function _suppressComposerDraftRestoreAfterSubmit() {}
        function _clearRememberedNewChatDraftSession() {}
        function _rememberComposerDraftPayloadState() {}
        const _composerDraftKnownPayloadSessions = new Set();
        let _draftSaveTimer = null;
        let S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        let interruptPosts = 0;
        let draftPosts = 0;
        let queueCalls = 0;
        let cancelCalls = 0;
        function queueSessionMessage() { queueCalls += 1; }
        function updateQueueBadge() {}
        async function cancelStream() { cancelCalls += 1; return true; }
        async function api(url, options) {
          if (url === '/api/chat/interrupt') {
            interruptPosts += 1;
            assert.strictEqual(options.retries, 0);
            throw new TypeError('response lost');
          }
          if (url === '/api/session/draft') {
            draftPosts += 1;
            assert.strictEqual(options.retries, 0);
            throw new TypeError('draft save lost');
          }
          throw new Error('unexpected request');
        }
        eval(%s);
        eval(%s);
        (async () => {
          assert.strictEqual(await _tryInterrupt('captured'), false);
          assert.strictEqual(interruptPosts, 1);
          assert.strictEqual(draftPosts, 1);
          assert.strictEqual(queueCalls, 0);
          assert.strictEqual(cancelCalls, 0);
          assert.strictEqual(input.value, 'captured');
          assert.deepStrictEqual(toasts.at(-1), {
            message:'cmd_interrupt_uncertain_preserved', ms:5000, type:'warning',
          });
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % (json.dumps(save_src), json.dumps(interrupt_src))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"single-attempt preservation harness failed: {proc.stderr}"


def test_late_interrupt_preservation_keeps_successor_debounce_without_stale_write():
    """A late A preservation must not write after B owns the session or cancel B's debounce."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    save_src = _block(SESSIONS_JS, "function _saveComposerDraft(sid, text, files)", "function _composerDraftHasPayload")
    save_now_src = _block(
        SESSIONS_JS,
        "function _saveComposerDraftNow(sid, text, files, force=false, clearTimer=true, expectedStreamId, interruptRequest=false)",
        "// Restore composer draft",
    )
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:'captured'};
        function $(id) { return id === 'msg' ? input : null; }
        let _draftSaveTimer = null;
        const _DRAFT_SAVE_DELAY_MS = 20;
        const _composerDraftKnownPayloadSessions = new Set();
        const saves = [];
        let rejectInterrupt = null;
        let S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        function _composerDraftFilesForPersist(files) { return Array.isArray(files) ? files : []; }
        function _composerDraftHasPayload(text, files) { return !!(text || (files && files.length)); }
        function _clearComposerDraftRestoreSuppression() {}
        function _rememberComposerDraftPayloadState() {}
        function api(url, options) {
          const payload = JSON.parse(options.body);
          if (url === '/api/chat/interrupt') {
            return new Promise((resolve, reject) => { rejectInterrupt = reject; });
          }
          saves.push(payload);
          return Promise.resolve({});
        }
        eval(%s);
        eval(%s);
        eval(%s);

        (async () => {
          const pending = _tryInterrupt('captured');
          assert.ok(rejectInterrupt, 'interrupt request must be pending');
          S = {session:{session_id:'B', active_stream_id:'new'}, activeStreamId:'new', pendingFiles:[], _composerRevision:0};
          input.value = 'B pending';
          _saveComposerDraft('B', 'B pending', []);
          rejectInterrupt(new TypeError('response lost'));
          await pending;
          await new Promise(resolve => setTimeout(resolve, 50));
            assert.deepStrictEqual(
                saves.map(({session_id, text}) => ({session_id, text})),
                [{session_id:'B', text:'B pending'}],
                'late A preservation must not write over B and must leave B debounce alive',
            );
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % (json.dumps(interrupt_src), json.dumps(save_src), json.dumps(save_now_src))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"late interrupt preservation harness failed: {proc.stderr}"


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


def test_interrupt_settlement_is_revision_aware_and_cancels_before_draft_settles():
    """Interrupt settlement must preserve edits and never hold cancellation behind draft I/O."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    clear_src = _block(
        MESSAGES_JS,
        "function _clearComposerAfterQueuedSelectionSend",
        "function _flushSelectionBlocksToComposer",
    )
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:''};
        const document = {getElementById(id) { return id === 'msg' ? input : null; }};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function renderTray() {}
        function _clearPendingSelections() {}
        function t(key) { return key; }
        function showToast() {}
        function updateQueueBadge() {}
        function _chatPayloadModelState() { return {model:'m', model_provider:'p'}; }
        const events = [];
        const saves = [];
        const drafts = {A:{text:'captured \\n', files:[]}, B:{text:'new owner draft', files:[]}};
        let resolveDraft = null;
        let resolveSave = null;
        let blockSave = false;
        function _clearComposerDraftIfMatches(sid, text, files) {
          events.push('draft-start');
          return new Promise(resolve => {
            resolveDraft = () => {
              const current = drafts[sid];
              const matched = current && current.text === text && JSON.stringify(current.files) === JSON.stringify(files);
              if (matched) drafts[sid] = {text:'', files:[]};
              events.push('draft-resolved');
              resolve({compare_cleared: matched});
            };
          });
        }
        async function _saveComposerDraftNow(sid, text, files) {
          events.push('save:' + text);
          saves.push({sid, text, files});
          if (blockSave) return new Promise(resolve => { resolveSave = resolve; });
        }
        const queued = [];
        function queueSessionMessage(sid, payload) { queued.push({sid, payload}); }
        let cancelCount = 0;
        async function cancelStream() { events.push('cancel'); cancelCount += 1; return true; }
        let mode = 'accepted';
        let resolveApi = null;
        let httpCalls = 0;
        let S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        async function api(url, options) {
          assert.strictEqual(url, '/api/chat/interrupt');
          assert.strictEqual(options.retries, 0);
          const body = JSON.parse(options.body);
          assert.ok(!Object.prototype.hasOwnProperty.call(body, 'request_id'));
          httpCalls += 1;
          if (mode === 'wait') return new Promise(resolve => { resolveApi = resolve; });
          if (mode === 'ambiguous') throw new TypeError('response lost');
          if (mode === 'reject') return {accepted:false, fallback:'redirect_rejected', stream_id:'old'};
          return {accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}};
        }
        eval(%s);
        eval(%s);

        function reset() {
          resolveApi = null;
          resolveDraft = null;
          resolveSave = null;
          blockSave = false;
          mode = 'accepted';
          events.length = 0;
          saves.length = 0;
          S = {session:{session_id:'A', active_stream_id:'old'}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
          input.value = '';
          drafts.A = {text:'captured \\n', files:[]};
          drafts.B = {text:'new owner draft', files:[]};
        }

        async function waitForDraftStart() {
          for (let i = 0; i < 8 && !resolveDraft; i += 1) await Promise.resolve();
          assert.ok(resolveDraft, 'draft settlement must start');
        }

        async function fallbackCancelsBeforeDraftSettles() {
          reset();
          mode = 'reject';
          input.value = 'captured \\n';
          const pending = _tryInterrupt('captured', 'confirm', 'reason', input.value, 0);
          await waitForDraftStart();
          assert.ok(events.includes('cancel'), 'fallback must initiate cancellation without awaiting draft I/O');
          assert.ok(events.indexOf('cancel') < events.indexOf('draft-resolved') || !events.includes('draft-resolved'));
          resolveDraft();
          await pending;
        }

        async function trailingWhitespaceUsesRawPayload() {
          reset();
          input.value = 'captured \\n';
          mode = 'wait';
          const pending = _tryInterrupt('captured', 'confirm', 'reason', input.value, 0);
          resolveApi({accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}});
          await pending;
          assert.strictEqual(input.value, '');
          assert.strictEqual(resolveDraft, null, 'accepted settlement must not issue a second draft request');
        }

        async function typeThenClearIsNotRestored() {
          reset();
          input.value = 'captured';
          mode = 'wait';
          const pending = _tryInterrupt('captured', 'confirm', 'reason', input.value, 0);
          input.value = '';
          S._composerRevision = 2;
          resolveApi({accepted:false, fallback:'network_error', stream_id:'old'});
          await pending;
          assert.strictEqual(input.value, '', 'a type-then-clear revision must not restore the old payload');
          assert.strictEqual(saves.at(-1).text, '', 'the edited empty draft must be persisted');
        }

        async function fileOnlyEditDuringCompareClearIsRepersisted() {
          reset();
          const laterFile = {name:'later.pdf'};
          input.value = 'captured';
          mode = 'wait';
          const pending = _tryInterrupt('captured', 'confirm', 'reason', input.value, 0);
          input.value = 'later edit';
          S.pendingFiles = [laterFile];
          resolveApi({accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}});
          await pending;
          assert.deepStrictEqual(saves.at(-1), {sid:'A', text:'later edit', files:[laterFile]}, 'concurrent edits/files must survive interrupt settlement');
          assert.strictEqual(resolveDraft, null, 'newer edits must not trigger captured-payload cleanup');
        }

        async function preservationPostFollowupSavesLatestEdit() {
          reset();
          mode = 'ambiguous';
          blockSave = true;
          input.value = 'captured';
          const pending = _tryInterrupt('captured', 'confirm', 'reason', input.value, 0);
          for (let i = 0; i < 8 && !resolveSave; i += 1) await Promise.resolve();
          assert.ok(resolveSave, 'ambiguous preservation must start its save');
          input.value = 'latest edit';
          S._composerRevision = 1;
          blockSave = false;
          resolveSave();
          await pending;
          assert.deepStrictEqual(saves.at(-1), {sid:'A', text:'latest edit', files:[]}, 'edits during preservation POST need a follow-up save');
          assert.strictEqual(saves.length, 2, 'the follow-up should be exactly one later write');
        }

        async function switchedOwnerCompareClearsOnlyMatchingDraft() {
          reset();
          input.value = 'captured';
          mode = 'wait';
          const pending = _tryInterrupt('captured', 'confirm', 'reason', input.value, 0);
          S = {session:{session_id:'B', active_stream_id:'new'}, activeStreamId:'new', pendingFiles:[], _composerRevision:0};
          input.value = 'new owner draft';
          drafts.A = {text:'captured', files:[]};
          resolveApi({accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}});
          await pending;
          assert.deepStrictEqual(drafts.B, {text:'new owner draft', files:[]}, 'the successor owner draft must survive');
          assert.strictEqual(resolveDraft, null, 'successor switch must not issue captured-payload cleanup');
        }

        (async () => {
          await fallbackCancelsBeforeDraftSettles();
          await trailingWhitespaceUsesRawPayload();
          await typeThenClearIsNotRestored();
          await fileOnlyEditDuringCompareClearIsRepersisted();
          await preservationPostFollowupSavesLatestEdit();
          await switchedOwnerCompareClearsOnlyMatchingDraft();
          assert.strictEqual(httpCalls, 6, 'interrupt must make one HTTP attempt per request');
          assert.strictEqual(cancelCount, 1, 'only the definitive fallback cancels');
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % (json.dumps(interrupt_src), json.dumps(clear_src))
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"interrupt settlement harness failed: {proc.stderr}"


def test_interrupt_settlement_uses_remembered_payload_and_warns_on_uncertainty():
    """The compare key is the remembered server payload, with honest preservation warnings."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    assert "cmd_interrupt_uncertain_preserved:" in I18N_JS
    assert "Delivery is uncertain; your draft was preserved." in I18N_JS
    assert "Check the active run before resending." in I18N_JS

    interrupt_src = _block(COMMANDS_JS, "function _interruptOwnerIsCurrent", "async function cmdInterrupt")
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const input = {value:''};
        const oldFile = {name:'old.pdf'};
        const newerFile = {name:'newer.pdf'};
        function $(id) { return id === 'msg' ? input : null; }
        function autoResize() {}
        function renderTray() {}
        function t(key) { return key; }
        const toasts = [];
        function showToast(message, ms, type) { toasts.push({message, ms, type}); }
        function _composerDraftFilesForPersist(files) { return Array.isArray(files) ? files : []; }
        function _composerDraftPayloadForSid(sid) {
          return S.session && S.session.session_id === sid ? S.session.composer_draft : null;
        }
        function _suppressComposerDraftRestoreAfterSubmit() {}
        function _clearComposerDraftRestoreSuppression() {}
        function _rememberComposerDraftPayloadState(sid, text, files) {
          if (S.session && S.session.session_id === sid) S.session.composer_draft = {text, files};
        }
        const saves = [];
        let releaseSave = null;
        let blockSave = false;
        let persisted = null;
        function _saveComposerDraftNow(sid, text, files) {
          saves.push({sid, text, files});
          if (!blockSave) return Promise.resolve();
          return new Promise(resolve => {
            releaseSave = () => {
              persisted = {text, files};
              resolve();
            };
          });
        }
        const bodies = [];
        let resolveApi = null;
        let mode = 'accepted';
        let S = {session:{session_id:'A', active_stream_id:'old', composer_draft:{text:'captured', files:[oldFile]}}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
        async function api(url, options) {
          assert.strictEqual(url, '/api/chat/interrupt');
          const body = JSON.parse(options.body);
          bodies.push(body);
          if (mode === 'wait') return new Promise(resolve => {
            resolveApi = value => {
              if (value && value.compare_cleared === true) persisted = {text:'', files:[]};
              resolve(value);
            };
          });
          if (mode === 'newer') return {accepted:true, stream_id:'old', compare_cleared:false, draft:{text:'newer server draft', files:[newerFile]}};
          if (mode === 'ambiguous') throw new TypeError('response lost');
          persisted = {text:'', files:[]};
          return {accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}};
        }
        const queued = [];
        function queueSessionMessage(sid, payload) { queued.push({sid, payload}); }
        async function cancelStream() { throw new Error('unexpected cancel'); }
        eval(%s);

        function reset() {
          mode = 'accepted';
          resolveApi = null;
          releaseSave = null;
          blockSave = false;
          persisted = {text:'captured', files:[oldFile]};
          saves.length = 0;
          toasts.length = 0;
          S = {session:{session_id:'A', active_stream_id:'old', composer_draft:{text:'captured', files:[oldFile]}}, activeStreamId:'old', pendingFiles:[], _composerRevision:0};
          input.value = 'captured more';
        }

        async function stalePrefixAndFilesUseRememberedCompareKey() {
          reset();
          assert.strictEqual(await _tryInterrupt('captured more'), true);
          assert.deepStrictEqual(bodies.at(-1), {
            session_id:'A', stream_id:'old', text:'captured more',
            draft_text:'captured', draft_files:[oldFile],
          });
          assert.strictEqual(input.value, '');
          assert.deepStrictEqual(persisted, {text:'', files:[]});
        }

        async function newerServerDraftIsPreservedWithWarning() {
          reset();
          mode = 'newer';
          assert.strictEqual(await _tryInterrupt('captured more'), false);
          assert.strictEqual(input.value, 'captured more');
          assert.strictEqual(toasts.at(-1).message, 'cmd_interrupt_uncertain_preserved');
          assert.strictEqual(toasts.at(-1).type, 'warning');
          assert.deepStrictEqual(queued, []);
        }

        async function newerClientDraftIsNotOverwrittenByServerDivergence() {
          reset();
          mode = 'wait';
          const pending = _tryInterrupt('captured more');
          input.value = 'client newer';
          S._composerRevision = 1;
          S.session.composer_draft = {text:'client newer', files:[]};
          resolveApi({accepted:true, stream_id:'old', compare_cleared:false, draft:{text:'newer server draft', files:[newerFile]}});
          assert.strictEqual(await pending, false);
          assert.strictEqual(input.value, 'client newer');
          assert.deepStrictEqual(S.session.composer_draft, {text:'client newer', files:[]});
          assert.strictEqual(toasts.at(-1).message, 'cmd_interrupt_uncertain_preserved');
          assert.strictEqual(toasts.at(-1).type, 'warning');
        }

        async function ambiguousTransportIsPreservedWithWarning() {
          reset();
          mode = 'ambiguous';
          assert.strictEqual(await _tryInterrupt('captured more'), false);
          assert.strictEqual(input.value, 'captured more');
          assert.strictEqual(toasts.at(-1).message, 'cmd_interrupt_uncertain_preserved');
          assert.strictEqual(toasts.at(-1).type, 'warning');
          assert.deepStrictEqual(queued, []);
        }

        async function settlementThenNewerSaveKeepsItsOutcome() {
          reset();
          mode = 'wait';
          blockSave = true;
          const pending = _tryInterrupt('captured more');
          input.value = 'newer edit';
          S._composerRevision = 1;
          resolveApi({accepted:true, stream_id:'old', compare_cleared:true, draft:{text:'', files:[]}});
          for (let i = 0; i < 8 && !releaseSave; i += 1) await Promise.resolve();
          assert.ok(releaseSave, 'the newer edit must save after settlement');
          assert.deepStrictEqual(persisted, {text:'', files:[]}, 'settlement clears the captured persisted payload first');
          releaseSave();
          await pending;
          assert.deepStrictEqual(persisted, {text:'newer edit', files:[]}, 'a later save outcome must survive settlement');
        }

        (async () => {
          await stalePrefixAndFilesUseRememberedCompareKey();
          await newerServerDraftIsPreservedWithWarning();
          await newerClientDraftIsNotOverwrittenByServerDivergence();
          await ambiguousTransportIsPreservedWithWarning();
          await settlementThenNewerSaveKeepsItsOutcome();
        })().catch(error => { console.error(error); process.exit(1); });
        """
    ) % json.dumps(interrupt_src)
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"remembered draft interrupt harness failed: {proc.stderr}"
