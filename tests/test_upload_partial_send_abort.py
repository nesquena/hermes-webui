import json
import shutil
import subprocess

import pytest

"""Regression coverage: a partially failed upload must not start the agent.

``uploadPendingFiles()`` (static/ui.js) uploads each staged file and only throws
when *every* file failed (``failures===total``). On a partial failure it returns
the files that DID upload. ``send()`` (static/messages.js) used to continue into
``/api/chat/start`` with that subset, so the message went out with a silent
subset of the attachments the user staged — and the failed files were gone from
the tray.

Fix: after the upload, ``send()`` aborts when ``uploaded.length`` is short of the
submitted snapshot, reports it, and restores the draft + staged files so the user
can resend.
"""
from pathlib import Path


ROOT = Path(__file__).parents[1]
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
UI_JS = ROOT.joinpath("static", "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")


def _send_upload_block() -> str:
    start = MESSAGES_JS.find("uploaded=await uploadPendingFiles(")
    assert start != -1, "send() must await uploadPendingFiles()"
    # From the await to the point the optimistic user turn is pushed.
    end = MESSAGES_JS.find("const uploadedNames=", start)
    assert end != -1, "the upload result must be consumed right after the await"
    return MESSAGES_JS[start:end]


def _partial_upload_branch() -> str:
    block = _send_upload_block()
    marker = "if(_submittedFiles.length && uploaded.length<_submittedFiles.length)"
    start = block.find(marker)
    assert start != -1, "send() must detect a partial upload (uploaded < submitted)"
    return block[start:]


def _total_upload_catch() -> str:
    block = _send_upload_block()
    start = block.find("catch(e){")
    assert start != -1, "the upload await must keep its try/catch"
    end = block.find("if(_submittedFiles.length && uploaded.length<_submittedFiles.length)", start)
    assert end != -1
    return block[start:end]


def test_send_aborts_on_partial_upload():
    branch = _partial_upload_branch()
    assert "_restoreComposerDraftAfterFailedSend(_submittedDraftTextForClear, _submittedFiles," in branch
    assert "mergeLiveInput:true" in branch
    assert "uploadOpId:_uploadOp" in branch
    assert "return;" in branch


def test_send_restores_draft_on_total_upload_failure_with_text():
    """The old code only surfaced a total failure when there was no text; with
    text present it fell through and sent a text-only message. Now it restores."""
    catch = _total_upload_catch()
    assert "_restoreComposerDraftAfterFailedSend(_submittedDraftTextForClear, _submittedFiles," in catch
    assert "mergeLiveInput:true" in catch
    assert "uploadOpId:_uploadOp" in catch
    assert "return;" in catch


def test_upload_pending_files_only_throws_on_total_failure():
    start = UI_JS.find("async function uploadPendingFiles(")
    assert start != -1, "uploadPendingFiles() must exist in static/ui.js"
    end = UI_JS.find("\n  return names;", start)
    body = UI_JS[start:end]
    # Partial failures are caught per-file (`catch(e){failures++;...}`) and the
    # subset is returned; the only guard that escalates is the all-failed case.
    assert "catch(e){failures++;" in body
    assert "if(failures===total&&total>0)throw new Error(" in body


def _restore_helper_source() -> str:
    start = MESSAGES_JS.find("function _draftFileKey(")
    end = MESSAGES_JS.find("\nasync function send(){")
    assert start != -1 and end != -1
    return MESSAGES_JS[start:end]


def _run_upload_restore(live_text, live_files, failed_text, failed_files):
    helper = _restore_helper_source()
    script = """
const state = {input: {value: %s}, pendingFiles: %s, saved: null};
const $ = (id) => (id === 'msg' ? state.input : null);
const S = {pendingFiles: state.pendingFiles, session: {session_id: 'sid-1'}};
function autoResize(){}
function updateSendBtn(){}
function renderTray(){ state.trayRendered = true; }
function _saveComposerDraftNow(sid, text, files){ state.saved = {sid, text, files}; }
%s
_restoreComposerDraftAfterFailedSend(%s, %s, 'sid-1', null, '', {mergeLiveInput:true});
state.pendingFiles = S.pendingFiles;
console.log(JSON.stringify({input: state.input.value, files: state.pendingFiles, saved: state.saved}));
""" % (
        json.dumps(live_text),
        json.dumps(live_files),
        helper,
        json.dumps(failed_text),
        json.dumps(failed_files),
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_send_reuses_uploads_and_only_uploads_the_missing_files():
    """A resend after a partial failure must not upload the good files again.

    Each re-upload created another disk copy (a.txt → a-1.txt → a-2.txt).
    send() now splits the submitted snapshot into cached and fresh uploads and
    merges both back in submitted order before the partial-failure check.
    """
    start = MESSAGES_JS.find("setComposerStatus(_submittedFiles.length?'Uploading…':'');")
    assert start != -1
    end = MESSAGES_JS.find("const uploadedNames=", start)
    assert end != -1
    block = MESSAGES_JS[start:end]
    assert "_pendingUploadsFor(_submittedFiles, activeSid)" in block
    assert "files:_filesToUpload" in block, "only the files without a cached result may be uploaded"
    assert "onFileUploaded:" in block, "successful uploads must be reported for reuse"
    assert "uploaded=_submittedFiles.map((file)=>_uploadResultsByFile.get(file)).filter(Boolean);" in block
    assert (
        block.find("uploaded=_submittedFiles.map((file)=>_uploadResultsByFile.get(file)).filter(Boolean);")
        < block.find("if(_submittedFiles.length && uploaded.length<_submittedFiles.length)")
    ), "reused + fresh results must be merged before the partial-failure check"


def test_send_drops_the_reuse_cache_once_the_agent_has_the_turn():
    explicit = MESSAGES_JS.find("if(entry&&entry.sid===String(activeSid||''))_uploadedFileResults.delete(file);")
    start = MESSAGES_JS.find("_dropUploadResults(activeSid);")
    assert explicit != -1, "a successful send must delete the submitted File keys explicitly"
    assert start != -1, "send() must drop the per-File upload cache when the send proceeds"
    assert explicit < start < MESSAGES_JS.find("attachLiveStream(activeSid, streamId, uploadedNames);"), (
        "submitted keys are deleted, then the session index, before the stream opens"
    )


def test_partial_upload_reports_in_the_composer_without_a_toast():
    branch = _partial_upload_branch()
    assert "setComposerStatus(_partialMsg)" in branch
    assert "showToast" not in branch
    catch = _total_upload_catch()
    assert "Upload error:" in catch
    assert "showToast" not in catch


def _upload_cache_helpers_source() -> str:
    start = MESSAGES_JS.find("const _uploadedFileResults=new WeakMap();")
    end = MESSAGES_JS.find("\nasync function send(){")
    assert start != -1 and end != -1
    return MESSAGES_JS[start:end]


def _upload_pending_files_source() -> str:
    start = UI_JS.find("async function uploadPendingFiles(")
    assert start != -1
    # Skip the parameter list: its default (`options={}`) would fool naive brace
    # matching. The body opens right after the `){` that ends the signature.
    sig_end = UI_JS.index("){", start)
    brace = sig_end + 1
    depth = 0
    for i in range(brace, len(UI_JS)):
        if UI_JS[i] == "{":
            depth += 1
        elif UI_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : i + 1]
    raise AssertionError("uploadPendingFiles() is unterminated")


def _run_node(body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable is required for JavaScript behavior checks")
    proc = subprocess.run([node, "-e", body], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_upload_cache_reuses_expires_and_drops_per_session():
    out = _run_node(
        _upload_cache_helpers_source()
        + r"""
const good = {name: 'good.txt'};
const other = {name: 'other.txt'};
const result = {name: 'good.txt', path: '/up/good.txt'};
_rememberUploadResult(good, 'sid-1', result);
const before = _cachedUploadResult(good, 'sid-1');
const crossSession = _cachedUploadResult(good, 'sid-2');
const split = _pendingUploadsFor([good, other], 'sid-1');
_rememberUploadResult(other, 'sid-1', {name: 'other.txt'});
const splitAfter = _pendingUploadsFor([good, other], 'sid-1');
_dropUploadResults('sid-1');
const afterDrop = _cachedUploadResult(good, 'sid-1');
console.log(JSON.stringify({
  before,
  crossSession,
  splitUploads: split.toUpload.map((f) => f.name),
  splitReused: split.results.size,
  splitAfterUploads: splitAfter.toUpload.map((f) => f.name),
  splitAfterReused: splitAfter.results.size,
  afterDrop,
}));
"""
    )
    assert out["before"] == {"name": "good.txt", "path": "/up/good.txt"}
    assert out["crossSession"] is None, "uploads are scoped to their session"
    assert out["splitUploads"] == ["other.txt"], "the cached file must not upload again"
    assert out["splitReused"] == 1
    assert out["splitAfterUploads"] == [], "both results cached: nothing left to upload"
    assert out["splitAfterReused"] == 2
    assert out["afterDrop"] is None


def test_upload_pending_files_reports_only_the_files_that_uploaded():
    """onFileUploaded fires per success — the source of the reuse cache."""
    out = _run_node(
        r"""
class FakeFormData {
  constructor() { this.entries = []; }
  append(key, value, filename) { this.entries.push([key, value, filename]); }
  file() {
    const entry = this.entries.find((e) => e[0] === 'file');
    return entry ? entry[1] : null;
  }
}
const fetchCalls = [];
globalThis.FormData = FakeFormData;
globalThis.document = { baseURI: 'http://webui/' };
globalThis.location = { href: 'http://webui/' };
globalThis.S = { pendingFiles: [], session: { session_id: 'sid-1' }, currentDir: '.' };
const MAX_UPLOAD_BYTES = 1024 * 1024;
const _ARCHIVE_EXTS = /\.zip$/i;
function _redirectIfUnauth() { return false; }
function setStatus() {}
function showToast() {}
function renderTray() {}
function loadDir() {}
function t(key) { return key; }
function _uploadTooLargeMessage() { return 'too large'; }
function _uploadPendingFilesUpdateProgress() {}
function _uploadPendingFilesCurrentSession() { return true; }
globalThis.fetch = async (url, opts) => {
  const name = opts.body.file().name;
  fetchCalls.push(name);
  // bad.txt fails exactly once — the retry succeeds so the resend can finish.
  if (name === 'bad.txt' && fetchCalls.filter((n) => n === 'bad.txt').length === 1) {
    return { ok: false, text: async () => 'boom' };
  }
  return {
    ok: true,
    json: async () => ({ filename: name, path: '/up/' + name, mime: 'text/plain', size: 5, is_image: false }),
  };
};
"""
        + _upload_pending_files_source()
        + r"""
const files = [{name: 'good.txt', size: 5}, {name: 'bad.txt', size: 5}];
const reported = [];
const helpers = %s;
(async () => {
  const names = await uploadPendingFiles({files, sessionId: 'sid-1', clearPending: false, onFileUploaded: (f, r) => {
    reported.push(r.name);
    helpers._rememberUploadResult(f, 'sid-1', r);
  }});
  // Resend with the same File objects: only the failed one may hit the server.
  const retry = helpers._pendingUploadsFor(files, 'sid-1');
  const retryNames = await uploadPendingFiles({files: retry.toUpload, sessionId: 'sid-1', clearPending: false, onFileUploaded: (f, r) => {
    reported.push(r.name);
    helpers._rememberUploadResult(f, 'sid-1', r);
  }});
  const merged = files.map((f) => helpers._cachedUploadResult(f, 'sid-1')).filter(Boolean);
  console.log(JSON.stringify({names, reported, fetchCalls, retryUploads: retry.toUpload.map((f) => f.name), mergedCount: merged.length}));
})();
"""
        % (
            "(function(){"
            + _upload_cache_helpers_source()
            + "return {_rememberUploadResult,_cachedUploadResult,_pendingUploadsFor};})()"
        )
    )
    assert [n["name"] for n in out["names"]] == ["good.txt"], out
    assert out["reported"] == ["good.txt", "bad.txt"], out
    assert out["fetchCalls"] == ["good.txt", "bad.txt", "bad.txt"], (
        "the retry must upload only the file that failed — no new copy of good.txt"
    )
    assert out["retryUploads"] == ["bad.txt"], out
    assert out["mergedCount"] == 2, "reused + fresh results restore the full attachment list"


def test_upload_restore_keeps_text_typed_during_the_upload():
    out = _run_upload_restore(
        "typed while uploading",
        [{"name": "old.txt", "size": 1, "lastModified": 10}],
        "original message",
        [{"name": "old.txt", "size": 1, "lastModified": 10}],
    )
    assert out["input"] == "original message\n\ntyped while uploading"
    assert out["files"] == [{"name": "old.txt", "size": 1, "lastModified": 10}]
    assert out["saved"]["text"] == out["input"]
    assert out["saved"]["files"] == out["files"]


def test_upload_restore_keeps_a_file_staged_during_the_upload():
    out = _run_upload_restore(
        "",
        [{"name": "new.txt", "size": 2, "lastModified": 20}],
        "original message",
        [{"name": "old.txt", "size": 1, "lastModified": 10}],
    )
    assert out["input"] == "original message"
    assert out["files"] == [
        {"name": "old.txt", "size": 1, "lastModified": 10},
        {"name": "new.txt", "size": 2, "lastModified": 20},
    ]
    assert out["saved"]["files"] == out["files"]


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, signature
    brace = src.find("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{signature} is unterminated")


def test_upload_failure_does_not_clear_successful_results():
    """Clearing the cache on every failure re-uploaded a.txt as a-1.txt."""
    for block in (_partial_upload_branch(), _total_upload_catch()):
        assert "_dropUploadResults" not in block
        assert "_forgetUploadedFile" not in block
        assert "_uploadedFileResults.delete" not in block


def test_partial_failure_retries_the_failed_file_only_once():
    """The successful file stays cached across the failed start and is not uploaded again."""
    out = _run_node(
        r"""
class FakeFormData {
  constructor() { this.entries = []; }
  append(key, value, filename) { this.entries.push([key, value, filename]); }
  file() {
    const entry = this.entries.find((e) => e[0] === 'file');
    return entry ? entry[1] : null;
  }
}
const fetchCalls = [];
globalThis.FormData = FakeFormData;
globalThis.document = { baseURI: 'http://webui/' };
globalThis.location = { href: 'http://webui/' };
globalThis.S = { pendingFiles: [], session: { session_id: 'sid-1' }, currentDir: '.' };
const MAX_UPLOAD_BYTES = 1024 * 1024;
const _ARCHIVE_EXTS = /\.zip$/i;
function _redirectIfUnauth() { return false; }
function setStatus() {}
function showToast() {}
function renderTray() {}
function loadDir() {}
function t(key) { return key; }
function _uploadTooLargeMessage() { return 'too large'; }
function _uploadPendingFilesUpdateProgress() {}
function _uploadPendingFilesCurrentSession() { return true; }
globalThis.fetch = async (url, opts) => {
  const name = opts.body.file().name;
  fetchCalls.push(name);
  if (name === 'bad.txt' && fetchCalls.filter((n) => n === 'bad.txt').length === 1) {
    return { ok: false, text: async () => 'boom' };
  }
  return {
    ok: true,
    json: async () => ({ filename: name, path: '/up/' + name, mime: 'text/plain', size: 5, is_image: false }),
  };
};
"""
        + _upload_pending_files_source()
        + r"""
const files = [{name: 'good.txt', size: 5}, {name: 'bad.txt', size: 5}];
const helpers = %s;
(async () => {
  await uploadPendingFiles({files, sessionId: 'sid-1', clearPending: false, onFileUploaded: (f, r) => {
    helpers._rememberUploadResult(f, 'sid-1', r);
  }});
  const afterFailure = {
    good: helpers._cachedUploadResult(files[0], 'sid-1'),
    bad: helpers._cachedUploadResult(files[1], 'sid-1'),
  };
  const retry = helpers._pendingUploadsFor(files, 'sid-1');
  await uploadPendingFiles({files: retry.toUpload, sessionId: 'sid-1', clearPending: false, onFileUploaded: (f, r) => {
    helpers._rememberUploadResult(f, 'sid-1', r);
  }});
  console.log(JSON.stringify({
    afterFailure,
    fetchCalls,
    retryUploads: retry.toUpload.map((f) => f.name),
    reused: helpers._cachedUploadResult(files[0], 'sid-1') && helpers._cachedUploadResult(files[0], 'sid-1').path,
  }));
})();
"""
        % (
            "(function(){"
            + _upload_cache_helpers_source()
            + "return {_rememberUploadResult,_cachedUploadResult,_pendingUploadsFor};})()"
        )
    )
    assert out["afterFailure"]["good"]["path"] == "/up/good.txt"
    assert out["afterFailure"]["bad"] is None, "the failed file must not be reused as a success"
    assert out["retryUploads"] == ["bad.txt"]
    assert out["fetchCalls"] == ["good.txt", "bad.txt", "bad.txt"]
    assert out["reused"] == "/up/good.txt"


def test_partial_failure_then_tray_removal_drops_that_file():
    """The real chip-remove handler must forget that File and leave the other one cached."""
    out = _run_node(
        _upload_cache_helpers_source()
        + _extract_js_function(UI_JS, "function renderTray(){")
        + r"""
const good = {name: 'good.txt', size: 1, lastModified: 1};
const other = {name: 'other.txt', size: 2, lastModified: 2};
_rememberUploadResult(good, 'sid-1', {name: 'good.txt', path: '/up/good.txt'});
_rememberUploadResult(other, 'sid-1', {name: 'other.txt', path: '/up/other.txt'});
const chips = [];
function makeChip() {
  const button = {};
  const chip = {
    className: '',
    classList: {add(){}, remove(){}, contains(){ return false; }},
    dataset: {},
    innerHTML: '',
    appendChild(){},
    querySelector(sel) { return sel === 'button' ? button : null; },
    _button: button,
  };
  chips.push(chip);
  return chip;
}
const tray = {
  innerHTML: '',
  classList: {add(){}, remove(){}, contains(){ return false; }},
  appendChild(){},
};
globalThis.document = {createElement: () => makeChip()};
globalThis.$ = (id) => (id === 'attachTray' ? tray : null);
globalThis.S = {pendingFiles: [good, other]};
globalThis._IMAGE_EXTS = /^$/;
globalThis._SVG_EXTS = /^$/;
globalThis._mediaKindForName = () => '';
globalThis.esc = (value) => String(value == null ? '' : value);
globalThis.li = () => '';
globalThis.t = (key) => key;
globalThis.updateSendBtn = () => {};
renderTray();
chips[0]._button.onclick();
console.log(JSON.stringify({
  pending: S.pendingFiles.map((f) => f.name),
  good: _cachedUploadResult(good, 'sid-1'),
  other: _cachedUploadResult(other, 'sid-1'),
}));
"""
    )
    assert out["pending"] == ["other.txt"]
    assert out["good"] is None
    assert out["other"]["path"] == "/up/other.txt"


def test_partial_failure_then_delete_session_drops_that_sessions_files():
    """deleteSession() must drop the deleted session's cached Files and no others."""
    out = _run_node(
        _upload_cache_helpers_source()
        + _extract_js_function(SESSIONS_JS, "async function deleteSession(")
        + r"""
const good = {name: 'good.txt'};
const other = {name: 'other.txt'};
const keeper = {name: 'keeper.txt'};
_rememberUploadResult(good, 'sid-1', {name: 'good.txt', path: '/up/good.txt'});
_rememberUploadResult(other, 'sid-1', {name: 'other.txt', path: '/up/other.txt'});
_rememberUploadResult(keeper, 'sid-2', {name: 'keeper.txt', path: '/up/keeper.txt'});
globalThis.S = {session: {session_id: 'sid-other'}, messages: [], entries: []};
globalThis._allSessions = [];
globalThis.t = (key) => key;
globalThis._sessionSnapshotById = () => null;
globalThis.showConfirmDialog = async () => true;
globalThis._captureSessionReflowPositions = () => null;
globalThis._clearHandoffStorageForSession = () => {};
globalThis._forgetComposerDraftLocalFallback = () => {};
globalThis._optimisticallyRemoveSessionFromList = () => {};
globalThis._sessionResponseRetainsWorktree = () => false;
globalThis.showToast = () => {};
globalThis.renderSessionList = async () => {};
globalThis.setStatus = () => {};
globalThis.api = async () => ({});
(async () => {
  const deleted = await deleteSession('sid-1');
  console.log(JSON.stringify({
    deleted,
    good: _cachedUploadResult(good, 'sid-1'),
    other: _cachedUploadResult(other, 'sid-1'),
    keeper: _cachedUploadResult(keeper, 'sid-2'),
  }));
})();
"""
    )
    assert out["deleted"] is True
    assert out["good"] is None
    assert out["other"] is None
    assert out["keeper"]["path"] == "/up/keeper.txt"


def test_session_switch_notes_the_departing_draft_before_saving_it():
    start = SESSIONS_JS.find("const draftText = ($('msg') || {}).value || '';")
    assert start != -1, "the session switch must read the departing composer"
    window = SESSIONS_JS[start:start + 900]
    note = window.find("_noteDepartingUploadDraft(currentSid, draftText, draftFiles)")
    save = window.find("_saveComposerDraftNow(currentSid, draftText, draftFiles, _switchContId)")
    assert note != -1, "leaving a session must keep the departing draft snapshot"
    assert save != -1 and note < save, "the snapshot is taken before the switch save"


def _failed_upload_after_session_switch(path):
    """Send original+[a,b], type a newer draft+[c] while they upload, switch, then fail."""
    helper = _restore_helper_source()
    script = """
const state = {input: {value: 'original'}, pendingFiles: [], saved: null};
const $ = (id) => (id === 'msg' ? state.input : null);
const S = {pendingFiles: [], session: {session_id: 'sid-1'}};
function autoResize(){}
function updateSendBtn(){}
function renderTray(){}
function _saveComposerDraftNow(sid, text, files, contId){
  state.saved = {sid, text, files: files.map((f) => f.name), contId: contId || ''};
}
%s
const a = {name:'a.txt', size:1, lastModified:1};
const b = {name:'b.txt', size:2, lastModified:2};
const c = {name:'c.txt', size:3, lastModified:3};
// 1-2. The send already captured original + a.txt + b.txt and cleared the composer.
//      While the upload runs the user types a newer draft and stages c.txt.
state.input.value = 'newer';
S.pendingFiles = [c];
// 3. Switch to another session while that upload is still in flight.
const op = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', state.input.value, S.pendingFiles.slice());
S.session = {session_id: 'sid-2'};
state.input.value = '';
S.pendingFiles = [];
// 4. b.txt fails. Partial and total failure both restore with mergeLiveInput.
_restoreComposerDraftAfterFailedSend('original', [a, b], 'sid-1', null, 'goal-1', {mergeLiveInput:true, uploadOpId: op});
const held = [];
_departingUploadDraftByOp.forEach((snap) => (snap.files || []).forEach((f) => held.push(f.name)));
console.log(JSON.stringify({
  path: %s,
  saved: state.saved,
  visible: state.input.value,
  pending: S.pendingFiles.map((f) => f.name),
  held,
}));
""" % (helper, json.dumps(path))
    return _run_node(script)


def test_partial_and_total_upload_failure_merge_the_departing_draft():
    """A failed upload must not replace the draft the user saved by switching away."""
    for path in ("partial", "total"):
        out = _failed_upload_after_session_switch(path)
        assert out["saved"]["sid"] == "sid-1"
        assert out["saved"]["text"] == "original\n\nnewer", out
        assert out["saved"]["files"] == ["a.txt", "b.txt", "c.txt"], out
        assert out["saved"]["contId"] == "", "the merged newer text must not inherit the rejected continuation id"
        assert out["visible"] == "", "the other session's composer stays untouched"
        assert out["pending"] == []
        assert out["held"] == [], "settling the failed upload must drop its departing files"


def _held_names_js() -> str:
    return r"""
function heldNames(){
  const names=[];
  _departingUploadDraftByOp.forEach((snap)=>{
    (snap.files||[]).forEach((file)=>{ names.push(file.name); });
  });
  return names;
}
"""


def test_upload_attempt_snapshot_is_claimed_before_the_upload_and_dropped_on_success():
    """Success must forget this attempt's id, and only after the upload has finished."""
    upload = MESSAGES_JS.find("uploaded=await uploadPendingFiles(")
    claim = MESSAGES_JS.rfind("_claimDepartingUploadOp(activeSid)", 0, upload)
    assert claim != -1, "the departing snapshot must be claimed before the upload await"
    partial = MESSAGES_JS.find(
        "if(_submittedFiles.length && uploaded.length<_submittedFiles.length)",
        upload,
    )
    forget = MESSAGES_JS.find("_forgetDepartingUploadDraft(activeSid, _uploadOp)", partial)
    release = MESSAGES_JS.find("_releaseDepartingUploadOp(_uploadOp)", forget)
    assert forget != -1 and release != -1
    assert forget < release, "success drops the snapshot, then releases the live op"


def test_session_leave_without_upload_keeps_no_file():
    """loadSession notes the composer on every switch; without an upload that stores nothing."""
    out = _run_node(
        _restore_helper_source()
        + _held_names_js()
        + r"""
const file = {name:'a.txt', size:1, lastModified:1};
_noteDepartingUploadDraft('sid-1', 'draft', [file]);
const other = _claimDepartingUploadOp('sid-2');
_noteDepartingUploadDraft('sid-1', 'still draft', [file]);
_releaseDepartingUploadOp(other);
console.log(JSON.stringify({held: heldNames()}));
"""
    )
    assert out["held"] == []


def test_tray_removal_without_upload_keeps_no_file():
    out = _run_node(
        _restore_helper_source()
        + _extract_js_function(UI_JS, "function renderTray(){")
        + _held_names_js()
        + r"""
const chips = [];
function makeChip() {
  const button = {};
  const chip = {
    className: '',
    classList: {add(){}, remove(){}, contains(){ return false; }},
    dataset: {},
    innerHTML: '',
    appendChild(){},
    querySelector(sel) { return sel === 'button' ? button : null; },
    _button: button,
  };
  chips.push(chip);
  return chip;
}
const tray = {
  innerHTML: '',
  classList: {add(){}, remove(){}, contains(){ return false; }},
  appendChild(){},
};
globalThis.document = {createElement: () => makeChip()};
globalThis.$ = (id) => (id === 'attachTray' ? tray : null);
globalThis.S = {pendingFiles: [], session: {session_id: 'sid-1'}};
globalThis._IMAGE_EXTS = /^$/;
globalThis._SVG_EXTS = /^$/;
globalThis._mediaKindForName = () => '';
globalThis.esc = (value) => String(value == null ? '' : value);
globalThis.li = () => '';
globalThis.t = (key) => key;
globalThis.updateSendBtn = () => {};
const file = {name:'a.txt', size:1, lastModified:1};
_noteDepartingUploadDraft('sid-1', 'draft', [file]);
S.pendingFiles = [file];
renderTray();
chips[chips.length - 1]._button.onclick();
console.log(JSON.stringify({held: heldNames(), pending: S.pendingFiles.map((f) => f.name)}));
"""
    )
    assert out["pending"] == []
    assert out["held"] == []


def _delete_session_prelude() -> str:
    return (
        _restore_helper_source()
        + _extract_js_function(SESSIONS_JS, "async function deleteSession(")
        + _held_names_js()
        + r"""
function t(key){ return key; }
function _sessionDeleteErrorMessage(){ return 'err'; }
globalThis.S = {session: {session_id: 'sid-other'}, messages: [], entries: []};
globalThis._allSessions = [];
globalThis._sessionSnapshotById = () => null;
globalThis._captureSessionReflowPositions = () => null;
globalThis._clearHandoffStorageForSession = () => {};
globalThis._forgetComposerDraftLocalFallback = () => {};
globalThis._optimisticallyRemoveSessionFromList = () => {};
globalThis._sessionResponseRetainsWorktree = () => false;
globalThis.showToast = () => {};
globalThis.renderSessionList = async () => {};
globalThis.setStatus = () => {};
globalThis._optimisticallyRemovedSessionIds = {add(){}, delete(){}};
"""
    )


def test_confirmed_delete_without_upload_keeps_no_file():
    out = _run_node(
        _delete_session_prelude()
        + r"""
const file = {name:'a.txt', size:1, lastModified:1};
_noteDepartingUploadDraft('sid-1', 'draft', [file]);
globalThis.showConfirmDialog = async () => true;
globalThis.api = async () => ({});
(async () => {
  const deleted = await deleteSession('sid-1');
  console.log(JSON.stringify({deleted, held: heldNames()}));
})();
"""
    )
    assert out["deleted"] is True
    assert out["held"] == []


def test_tray_removal_drops_that_files_snapshot_only():
    """No upload is running. Removing a chip drops the snapshot that holds that File."""
    out = _run_node(
        _restore_helper_source()
        + _extract_js_function(UI_JS, "function renderTray(){")
        + _held_names_js()
        + r"""
const chips = [];
function makeChip() {
  const button = {};
  const chip = {
    className: '',
    classList: {add(){}, remove(){}, contains(){ return false; }},
    dataset: {},
    innerHTML: '',
    appendChild(){},
    querySelector(sel) { return sel === 'button' ? button : null; },
    _button: button,
  };
  chips.push(chip);
  return chip;
}
const tray = {
  innerHTML: '',
  classList: {add(){}, remove(){}, contains(){ return false; }},
  appendChild(){},
};
globalThis.document = {createElement: () => makeChip()};
globalThis.$ = (id) => (id === 'attachTray' ? tray : null);
globalThis.S = {pendingFiles: [], session: {session_id: 'sid-1'}};
globalThis._IMAGE_EXTS = /^$/;
globalThis._SVG_EXTS = /^$/;
globalThis._mediaKindForName = () => '';
globalThis.esc = (value) => String(value == null ? '' : value);
globalThis.li = () => '';
globalThis.t = (key) => key;
globalThis.updateSendBtn = () => {};
const oldFile = {name:'old.txt', size:1, lastModified:1};
const newer = {name:'newer.txt', size:2, lastModified:2};
const keeper = {name:'keeper.txt', size:3, lastModified:3};
const op1 = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', 'old', [oldFile]);
_releaseDepartingUploadOp(op1);
const op2 = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', 'newer', [newer]);
_releaseDepartingUploadOp(op2);
const opK = _claimDepartingUploadOp('sid-2');
_noteDepartingUploadDraft('sid-2', 'keep', [keeper]);
_releaseDepartingUploadOp(opK);
S.pendingFiles = [oldFile];
renderTray();
chips[chips.length - 1]._button.onclick();
console.log(JSON.stringify({held: heldNames()}));
"""
    )
    assert out["held"] == ["newer.txt", "keeper.txt"]


def test_confirmed_delete_drops_only_the_ops_confirmed_at_the_start():
    """A snapshot noted while delete is in flight belongs to a newer attempt."""
    out = _run_node(
        _delete_session_prelude()
        + r"""
const oldFile = {name:'old.txt', size:1, lastModified:1};
const newer = {name:'newer.txt', size:2, lastModified:2};
const keeper = {name:'keeper.txt', size:3, lastModified:3};
const op1 = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', 'old', [oldFile]);
_releaseDepartingUploadOp(op1);
const opK = _claimDepartingUploadOp('sid-2');
_noteDepartingUploadDraft('sid-2', 'keep', [keeper]);
_releaseDepartingUploadOp(opK);
let resolveDelete = null;
globalThis.showConfirmDialog = async () => true;
globalThis.api = () => new Promise((resolve) => { resolveDelete = resolve; });
(async () => {
  const done = deleteSession('sid-1');
  for (let i = 0; i < 10 && typeof resolveDelete !== 'function'; i++) await Promise.resolve();
  if (typeof resolveDelete !== 'function') throw new Error('delete did not reach the request');
  const op2 = _claimDepartingUploadOp('sid-1');
  _noteDepartingUploadDraft('sid-1', 'newer', [newer]);
  _releaseDepartingUploadOp(op2);
  resolveDelete({});
  const deleted = await done;
  console.log(JSON.stringify({deleted, held: heldNames()}));
})();
"""
    )
    assert out["deleted"] is True
    assert out["held"] == ["keeper.txt", "newer.txt"]


def test_cancelled_or_failed_delete_keeps_the_departing_snapshot():
    out = _run_node(
        _delete_session_prelude()
        + r"""
const file = {name:'a.txt', size:1, lastModified:1};
const op = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', 'draft', [file]);
_releaseDepartingUploadOp(op);
globalThis.showConfirmDialog = async () => false;
globalThis.api = async () => { throw new Error('should not be called'); };
(async () => {
  const cancelled = await deleteSession('sid-1');
  globalThis.showConfirmDialog = async () => true;
  globalThis.api = async () => { throw new Error('no'); };
  const failed = await deleteSession('sid-1');
  console.log(JSON.stringify({cancelled, failed, held: heldNames()}));
})();
"""
    )
    assert out["cancelled"] is False
    assert out["failed"] is False
    assert out["held"] == ["a.txt"]


def test_successful_upload_drops_only_its_own_snapshot():
    out = _run_node(
        _restore_helper_source()
        + _held_names_js()
        + r"""
const oldFile = {name:'old.txt', size:1, lastModified:1};
const newer = {name:'newer.txt', size:2, lastModified:2};
const op1 = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', 'old', [oldFile]);
const op2 = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', 'newer', [newer]);
_forgetDepartingUploadDraft('sid-1', op1);
_releaseDepartingUploadOp(op1);
const afterOld = heldNames();
_forgetDepartingUploadDraft('sid-1', op2);
_releaseDepartingUploadOp(op2);
_noteDepartingUploadDraft('sid-1', 'after', [newer]);
console.log(JSON.stringify({afterOld, afterNew: heldNames()}));
"""
    )
    assert out["afterOld"] == ["newer.txt"]
    assert out["afterNew"] == []


def test_old_upload_settlement_keeps_a_newer_snapshot():
    """The deferred persist of an older upload must not drop the next attempt's files."""
    out = _run_node(
        _restore_helper_source()
        + _held_names_js()
        + r"""
const state = {input: {value: ''}, pendingFiles: [], saved: null};
const $ = (id) => (id === 'msg' ? state.input : null);
const S = {pendingFiles: [], session: {session_id: 'sid-2'}};
function autoResize(){}
function updateSendBtn(){}
function renderTray(){}
function _saveComposerDraftNow(sid, text, files, contId){
  state.saved = {sid, text, files: files.map((f) => f.name), contId: contId || ''};
}
const a = {name:'a.txt', size:1, lastModified:1};
const b = {name:'b.txt', size:2, lastModified:2};
const c = {name:'c.txt', size:3, lastModified:3};
const d = {name:'d.txt', size:4, lastModified:4};
const op1 = _claimDepartingUploadOp('sid-1');
let release;
const clearPromise = new Promise((resolve) => { release = resolve; });
_restoreComposerDraftAfterFailedSend('original', [a, b], 'sid-1', clearPromise, 'goal-1', {mergeLiveInput:true, uploadOpId: op1});
_noteDepartingUploadDraft('sid-1', 'newer', [c]);
const op2 = _claimDepartingUploadOp('sid-1');
_noteDepartingUploadDraft('sid-1', 'second', [d]);
release();
(async () => {
  await Promise.resolve();
  _takeDepartingUploadDraft('sid-1', op1);
  _forgetDepartingUploadDraft('sid-1', op1);
  console.log(JSON.stringify({
    saved: state.saved,
    held: heldNames(),
    visible: state.input.value,
  }));
})();
"""
    )
    assert out["saved"]["sid"] == "sid-1"
    assert out["saved"]["text"] == "original\n\nnewer", out
    assert out["saved"]["files"] == ["a.txt", "b.txt", "c.txt"], out
    assert out["saved"]["contId"] == "", "merged text must not keep the rejected continuation id"
    assert out["held"] == ["d.txt"]
    assert out["visible"] == ""
