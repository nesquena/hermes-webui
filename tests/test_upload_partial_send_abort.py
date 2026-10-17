import json
import subprocess

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
