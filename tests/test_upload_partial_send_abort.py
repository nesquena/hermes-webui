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


def test_send_aborts_on_partial_upload():
    block = _send_upload_block()
    # The guard must compare the uploaded count against the submitted snapshot.
    assert "uploaded.length<_submittedFiles.length" in block, (
        "send() must detect a partial upload (uploaded < submitted)"
    )
    # And it must restore the draft + staged files instead of continuing.
    assert "_restoreComposerDraftAfterFailedSend(_submittedDraftTextForClear, _submittedFiles," in block


def test_send_restores_draft_on_total_upload_failure_with_text():
    """The old code only surfaced a total failure when there was no text; with
    text present it fell through and sent a text-only message. Now it restores."""
    block = _send_upload_block()
    assert "catch(e){" in block, "the upload await must keep its try/catch"
    assert "_restoreComposerDraftAfterFailedSend(_submittedDraftTextForClear, _submittedFiles," in block


def test_upload_pending_files_only_throws_on_total_failure():
    start = UI_JS.find("async function uploadPendingFiles(")
    assert start != -1, "uploadPendingFiles() must exist in static/ui.js"
    end = UI_JS.find("\n  return names;", start)
    body = UI_JS[start:end]
    # Partial failures are caught per-file (`catch(e){failures++;...}`) and the
    # subset is returned; the only guard that escalates is the all-failed case.
    assert "catch(e){failures++;" in body
    assert "if(failures===total&&total>0)throw new Error(" in body
