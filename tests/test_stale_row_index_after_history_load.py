"""Edit / regenerate / fork must use a row's current index after history loads.

Opening the conversation outline loads the full history (``_oldestIdx`` -> 0)
without re-rendering, so the visible tail rows keep ``data-msg-idx`` values that
are relative to the old window. Edit computed ``keep_count = _oldestIdx + msgIdx``
from that stale value and truncated the wrong turn, deleting history (same
pattern as #2184; keep_count 28 instead of 38). The rows' absolute
``data-session-msg-idx`` does not go stale; ``_rowCurrentRawIdx`` derives from it.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _function(src, name):
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert match, f"{name} not found"
    start = match.start()
    i = src.index("{", start) + 1
    depth = 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[start:i]


def _run(script):
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _helpers():
    return "\n".join(_function(UI_JS, n) for n in (
        "_messageSessionIndexBase", "_messageSessionIndexForRawIdx",
        "_messageRawIdxForSessionIndex", "_rowCurrentRawIdx"))


def test_row_index_follows_history_load_without_rerender():
    script = _helpers() + r"""
let _oldestIdx = 10;
// Row rendered while the window started at absolute message 10.
const row = {dataset: {msgIdx: '28', sessionMsgIdx: String(_messageSessionIndexForRawIdx(28))}};
const before = _rowCurrentRawIdx(row, 28);
_oldestIdx = 0;  // full history loaded (outline open), no re-render
const after = _rowCurrentRawIdx(row, 28);
const legacy = {dataset: {msgIdx: '5'}};  // no absolute index: keep the old value
console.log(JSON.stringify({before, after, fallback: _rowCurrentRawIdx(legacy, 5),
  bad: _rowCurrentRawIdx({dataset: {sessionMsgIdx: 'x'}}, 7), none: _rowCurrentRawIdx(null, 3)}));
"""
    out = _run(script)
    assert out["before"] == 28          # unchanged while the window is unchanged
    assert out["after"] == 38           # absolute 38 is raw 38 once _oldestIdx is 0
    assert out["fallback"] == 5 and out["bad"] == 7 and out["none"] == 3


def test_edit_send_uses_current_row_index():
    body = _function(UI_JS, "editMessage")
    send = body[body.index(".msg-edit-send').onclick"):]
    assert "msgIdx = _rowCurrentRawIdx(row, msgIdx);" in send
    assert send.index("_rowCurrentRawIdx(row, msgIdx)") < send.index("await submitEdit(msgIdx, newText)")


def test_regenerate_uses_current_row_index():
    body = _function(UI_JS, "regenerateResponse")
    assert "_rowCurrentRawIdx(row,clickedRowIdx)" in body
    # Must happen before history loads and re-bases the rows (#2184 ordering).
    assert body.index("_rowCurrentRawIdx(row,clickedRowIdx)") < body.index("await _ensureAllMessagesLoaded()")


def test_fork_rereads_index_from_its_row():
    assert 'onclick="forkFromMessage(${rawIdx+1},this)"' in UI_JS
    body = _function(COMMANDS_JS, "forkFromMessage")
    assert body.startswith("async function forkFromMessage(msgIdx, btn)")
    reread = body.index("_rowCurrentRawIdx(forkRow,msgIdx-1)+1")
    # Must happen before the absolute keep_count is captured (#2184 ordering).
    assert reread < body.index("const absoluteKeepCount = _oldestIdx + msgIdx;")


# Run the real edit / regenerate / fork actions on a 40-message session whose
# tail rows (window starting at message 10) were rendered before the full
# history loaded. Network and DOM are stubs; the request bodies are recorded.
ACTIONS = r"""
const assert = require('node:assert/strict');
const calls = [];
let sendBtn = null;
const S = {session: {session_id: 's1', regeneration_revision: 'r1'}, busy: false, messages: []};
// Each action starts from the full 40-message session (an edit truncates it).
const reset = () => { S.messages = Array.from({length: 40}, (_, i) => ({role: i % 2 ? 'assistant' : 'user', content: String(i)})); };
let _oldestIdx = 0, _submitEditInFlight = false;
const window = {};
const document = {createElement: (type) => type === 'textarea'
  ? {value: '', className: '', addEventListener() {}, after() {}, focus() {}, setSelectionRange() {}}
  : {className: '', innerHTML: '', querySelector: (sel) => sel.includes('send') ? (sendBtn = sendBtn || {}) : {}}};
function requestAnimationFrame() {}
function autoResizeTextarea() {}
function _deliberateSessionModelPick() { return null; }
function _reArmRecoveryPick() {}
function renderMessages() {}
function $() { return {value: ''}; }
async function send() { calls.push({send: true}); }
function setStatus(message) { calls.push({status: message}); }
function showToast(message) { calls.push({toast: message}); }
function t(key) { return key; }
async function startRegeneration(sid, revision) { calls.push({regenerate: sid, revision}); }
async function loadSession() {}
async function renderSessionList() {}
async function _ensureAllMessagesLoaded() { _oldestIdx = 0; }
async function api(path, options) { calls.push({path, body: JSON.parse(options.body)}); return {}; }
FUNCTIONS
// A tail row rendered while the window started at absolute message 10.
const staleRow = (rawIdx) => ({
  dataset: {msgIdx: String(rawIdx), sessionMsgIdx: String(rawIdx + 10), rawText: 'old'},
  querySelector: () => ({replaceWith() {}}),
  closest() { return this; },
});
(async () => {
  const out = {};
  // Edit opened before history loads, sent after it loaded (outline opened meanwhile).
  reset(); _oldestIdx = 10; sendBtn = null; calls.length = 0;
  editMessage(staleRow(28)); _oldestIdx = 0;
  await sendBtn.onclick();
  out.editOpenedBefore = calls.find((c) => c.path === '/api/session/truncate').body.keep_count;
  // History already loaded without a re-render, then edit, fork, regenerate.
  reset(); _oldestIdx = 0; sendBtn = null; calls.length = 0;
  editMessage(staleRow(28)); await sendBtn.onclick();
  out.editAfterLoad = calls.find((c) => c.path === '/api/session/truncate').body.keep_count;
  reset(); calls.length = 0;
  const forkRow = staleRow(3);
  await forkFromMessage(4, {closest: () => forkRow});
  out.fork = calls.find((c) => c.path === '/api/session/branch').body.keep_count;
  reset(); calls.length = 0;
  await regenerateResponse(staleRow(29));
  out.regenerate = calls.filter((c) => c.regenerate || c.status);
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exitCode = 1; });
"""


def test_actions_send_the_shown_message_after_history_loads():
    functions = "\n".join([_helpers(), _function(UI_JS, "editMessage"), _function(UI_JS, "submitEdit"),
                           _function(UI_JS, "regenerateResponse"), _function(COMMANDS_JS, "forkFromMessage")])
    out = _run(ACTIONS.replace("FUNCTIONS", functions))
    assert out["editOpenedBefore"] == 38   # last user message is absolute 38
    assert out["editAfterLoad"] == 38
    assert out["fork"] == 14               # fork keeps messages 0..13
    assert out["regenerate"] == [{"regenerate": "s1", "revision": "r1"}]
