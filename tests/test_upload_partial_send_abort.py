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
    assert "{mergeLiveInput:true}" in branch
    assert "return;" in branch


def test_send_restores_draft_on_total_upload_failure_with_text():
    """The old code only surfaced a total failure when there was no text; with
    text present it fell through and sent a text-only message. Now it restores."""
    catch = _total_upload_catch()
    assert "_restoreComposerDraftAfterFailedSend(_submittedDraftTextForClear, _submittedFiles," in catch
    assert "{mergeLiveInput:true}" in catch
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
_restoreComposerDraftAfterFailedSend(%s, %s, 'sid-1', null, {mergeLiveInput:true});
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
    start = MESSAGES_JS.find("_dropUploadResults(activeSid);")
    assert start != -1, "send() must drop the per-File upload cache when the send proceeds"
    assert start < MESSAGES_JS.find("attachLiveStream(activeSid, streamId, uploadedNames);"), (
        "the cache must be dropped before the stream opens"
    )


def _upload_cache_helpers_source() -> str:
    start = MESSAGES_JS.find("const _uploadedFileResults=new Map();")
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
