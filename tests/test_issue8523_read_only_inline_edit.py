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
