"""Sidebar stop must not settle the UI when /api/chat/cancel fails."""
from __future__ import annotations

import json
import pathlib
import re
import subprocess


REPO = pathlib.Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    m = re.search(rf"async function {name}\s*\(", src)
    assert m, f"{name} not found in static/boot.js"
    brace_pos = src.index("{", m.end())
    depth = 1
    pos = brace_pos + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    return src[m.start():pos]


CANCEL_SESSION_STREAM_SRC = _extract_function(BOOT_JS, "cancelSessionStream")


def test_source_gates_sidebar_settle_on_http_success():
    assert "return false" in CANCEL_SESSION_STREAM_SRC
    assert "return true" in CANCEL_SESSION_STREAM_SRC
    assert "r.ok" in CANCEL_SESSION_STREAM_SRC, (
        "cancelSessionStream() must check the /api/chat/cancel HTTP status before "
        "closing local UI state"
    )
    assert "if(!respOk)returnfalse;" in "".join(CANCEL_SESSION_STREAM_SRC.split()), (
        "cancelSessionStream() must bail out on failed stop responses"
    )


def test_stop_callers_gate_success_toasts_on_cancel_result():
    compact_commands = "".join(COMMANDS_JS.split())
    compact_messages = "".join(MESSAGES_JS.split())
    compact_sessions = "".join(SESSIONS_JS.split())
    assert "if(awaitcancelStream('slash-stop'))showToast(t('stream_stopped'))" in compact_commands
    assert "elseshowToast(t('cancel_failed'))" in compact_commands
    assert "if(awaitcancelStream('slash-interrupt'))showToast(t('cmd_interrupt_confirm'),2000)" in compact_commands
    assert "if(awaitcancelStream('busy-interrupt'))showToast(t('busy_interrupt_confirm'),2000)" in compact_messages
    assert "if(awaitcancelSessionStream(session))showToast(t('stream_stopped'))" in compact_sessions


_NODE_SCRIPT = r'''
const M = {
  closeCalls: [],
  busyCalls: [],
  composerCalls: [],
  statusCalls: [],
  renderCalls: 0,
  clearCalls: [],
  approvalStops: 0,
  approvalHides: 0,
  clarifyStops: 0,
  clarifyHides: 0,
  fetchCalls: [],
};

globalThis.INFLIGHT = { 'sid-1': { streamId: 'stream-1' } };
globalThis.S = { activeStreamId: 'stream-1', session: { session_id: 'sid-1', active_stream_id: 'stream-1' } };
globalThis.closeLiveStream = (...a) => M.closeCalls.push(a);
globalThis.clearInflightState = (sid) => M.clearCalls.push(['clearInflightState', sid]);
globalThis.clearInflight = () => M.clearCalls.push(['clearInflight']);
globalThis.setBusy = (v) => M.busyCalls.push(v);
globalThis.setComposerStatus = (v) => M.composerCalls.push(v);
globalThis.setStatus = (v) => M.statusCalls.push(v);
globalThis.stopApprovalPolling = () => M.approvalStops += 1;
globalThis.hideApprovalCard = () => M.approvalHides += 1;
globalThis.stopClarifyPolling = () => M.clarifyStops += 1;
globalThis.hideClarifyCard = () => M.clarifyHides += 1;
globalThis.renderSessionList = () => M.renderCalls += 1;
globalThis._approvalSessionId = 'sid-1';
globalThis._clarifySessionId = 'sid-1';
globalThis.document = { baseURI: 'http://localhost:8787/' };
globalThis.location = { href: 'http://localhost:8787/' };
globalThis.fetch = (url, opts) => {
  M.fetchCalls.push({ url: String(url), opts });
  return Promise.resolve({
    ok: false,
    json: () => Promise.resolve({ ok: false, cancelled: false, stream_id: 'stream-1' }),
  });
};

__CANCEL_SESSION_STREAM_SRC__

const session = { session_id: 'sid-1', active_stream_id: 'stream-1' };
await cancelSessionStream(session);
console.log(JSON.stringify({
  sessionActiveStreamId: session.active_stream_id,
  activeStreamId: globalThis.S.activeStreamId,
  closeCalls: M.closeCalls,
  busyCalls: M.busyCalls,
  composerCalls: M.composerCalls,
  renderCalls: M.renderCalls,
  clearCalls: M.clearCalls,
  approvalStops: M.approvalStops,
  approvalHides: M.approvalHides,
  clarifyStops: M.clarifyStops,
  clarifyHides: M.clarifyHides,
  fetchCalls: M.fetchCalls.length,
}));
'''


def test_failed_sidebar_stop_keeps_local_state():
    script = _NODE_SCRIPT.replace("__CANCEL_SESSION_STREAM_SRC__", CANCEL_SESSION_STREAM_SRC)
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=str(REPO),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, (
        f"node subprocess failed:\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["fetchCalls"] == 1
    assert result["sessionActiveStreamId"] == "stream-1"
    assert result["activeStreamId"] == "stream-1"
    assert result["closeCalls"] == []
    assert result["busyCalls"] == []
    assert result["composerCalls"] == []
    assert result["renderCalls"] == 0
    assert result["clearCalls"] == []
    assert result["approvalStops"] == 0
    assert result["approvalHides"] == 0
    assert result["clarifyStops"] == 0
    assert result["clarifyHides"] == 0
