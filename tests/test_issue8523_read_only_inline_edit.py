"""#8523 release gate: an inline edit on a read-only session stops before it truncates.

A session another client owns (Desktop, CLI, Telegram, cron, ...) is read-only in WebUI, and ``send()`` returns at its
first line for it. ``submitEdit`` used to post ``/api/session/truncate`` first and only then call ``send()``, so the edit
cut the transcript and ended with no message at all. It now returns before any request and says why in the status line.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(src: str, name: str) -> str:
    start = src.index(f"async function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def test_read_only_check_comes_before_the_truncate_request():
    body = _function_source(UI_JS, "submitEdit")
    assert "chat_read_only_reason" in body
    assert body.index("chat_read_only_reason") < body.index("/api/session/truncate")


_HARNESS = r"""
const calls = [];
let status = null;
const S = {session: {session_id: 'desktop-1', chat_read_only_reason: REASON}, busy: false, messages: [{role: 'user'}]};
let _submitEditInFlight = false, _oldestIdx = 0;
const api = async (url) => { calls.push(url); return {}; };
const setStatus = (text) => { status = text; };
const send = async () => { calls.push('send'); };
const renderMessages = () => {};
const _deliberateSessionModelPick = () => null;
const _reArmRecoveryPick = () => {};
const _ensureAllMessagesLoaded = async () => {};
const $ = () => ({value: ''});
const t = (k) => k;
SOURCE
submitEdit(0, 'edited text').then(() => {
  process.stdout.write(JSON.stringify({calls, status, inFlight: _submitEditInFlight}));
});
"""


def _run(reason):
    src = _HARNESS.replace("REASON", json.dumps(reason)).replace("SOURCE", _function_source(UI_JS, "submitEdit"))
    out = subprocess.run([NODE or "node", "-e", src], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_read_only_session_edit_makes_no_request_and_explains():
    result = _run("Read-only in WebUI. Continue in Desktop.")
    assert result["calls"] == []
    assert result["status"] == "Read-only in WebUI. Continue in Desktop."
    assert result["inFlight"] is False


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_writable_session_edit_still_truncates_then_sends():
    result = _run("")
    assert result["calls"] == ["/api/session/truncate", "send"]
    assert result["status"] is None


# Fable's fold-6 review: the other two write buttons on the last assistant message, regenerate and undo, refuse a
# read-only session the same way, before any request or transcript change.
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")

_ACTION_HARNESS = r"""
const calls = [];
let toast = null;
const S = {session: {session_id: 'desktop-1', chat_read_only_reason: REASON, regeneration_revision: 3},
           busy: false, messages: [{role: 'user'}, {role: 'assistant'}]};
let _oldestIdx = 0;
const api = async (url) => { calls.push(url); return {removed_count: 2, session: {messages: []}}; };
const setStatus = (text) => { toast = text; };
const showToast = (text) => { if (toast === null) toast = text; };
const startRegeneration = async () => { calls.push('startRegeneration'); };
const _rowCurrentRawIdx = (row, idx) => idx;
const clearLiveToolCards = () => {};
const renderMessages = () => {};
const t = (k) => k;
const row = {dataset: {msgIdx: '1'}};
const btn = {closest: () => row};
SOURCE
CALL.then(() => { process.stdout.write(JSON.stringify({calls, toast})); });
"""


def _run_action(src_file, name, call, reason):
    src = (_ACTION_HARNESS.replace("REASON", json.dumps(reason))
           .replace("SOURCE", _function_source(src_file, name)).replace("CALL", call))
    out = subprocess.run([NODE or "node", "-e", src], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_regenerate_and_undo_check_read_only_before_any_request():
    regen = _function_source(UI_JS, "regenerateResponse")
    assert regen.index("chat_read_only_reason") < regen.index("startRegeneration(")
    undo = _function_source(COMMANDS_JS, "cmdUndo")
    assert undo.index("chat_read_only_reason") < undo.index("/api/session/undo")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_read_only_regenerate_makes_no_request_and_explains():
    result = _run_action(UI_JS, "regenerateResponse", "regenerateResponse(btn)", "Read-only in WebUI. Continue in Desktop.")
    assert result == {"calls": [], "toast": "Read-only in WebUI. Continue in Desktop."}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_read_only_undo_makes_no_request_and_explains():
    result = _run_action(COMMANDS_JS, "cmdUndo", "cmdUndo()", "Read-only in WebUI. Continue in Desktop.")
    assert result == {"calls": [], "toast": "Read-only in WebUI. Continue in Desktop."}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_writable_regenerate_and_undo_still_run():
    assert _run_action(UI_JS, "regenerateResponse", "regenerateResponse(btn)", "")["calls"] == ["startRegeneration"]
    assert _run_action(COMMANDS_JS, "cmdUndo", "cmdUndo()", "")["calls"][0] == "/api/session/undo"
