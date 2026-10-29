"""Composed recovered-prompt lifecycle at zero and nonzero paging offsets."""

import json
import os
import subprocess
import tempfile

import pytest

from tests.test_inflight_tail_windowed_user_position import (
    NODE,
    SESSIONS_JS_PATH,
    _ensure_loaded_scaffold,
    _fork_caller,
    _fork_harness,
    _function,
    _helper_src,
    _index_helpers,
)


def _run_file(script: str) -> str:
    """Execute via a temp file: the composed script exceeds MAX_ARG_STRLEN."""
    assert NODE, "node is required for this test"
    fd, path = tempfile.mkstemp(suffix=".js", dir=os.environ.get("TMPDIR", "/tmp"))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        result = subprocess.run([NODE, path], capture_output=True, text=True, check=False)
    finally:
        os.unlink(path)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


@pytest.mark.parametrize("oldest_idx", [0, 31])
def test_server_window_load_session_recovery_fork_lifecycle(oldest_idx):
    """Real window helper -> real loadSession -> recovery -> real fork caller.

    Only DOM, transport and unrelated chrome are controlled seams. Recovery
    selection, merge, index maps, load ownership and the fork guard are real.
    Repeated active loads retain one display row; an idle full load replaces it
    with the persisted prompt, which becomes canonically addressable.
    """
    from api.routes import _message_window_for_display

    history = [{"role": "assistant", "content": f"earlier {i}", "_ts": i + 1} for i in range(oldest_idx)]
    saved = [
        {"role": "assistant", "content": "saved activity 0", "_ts": 100},
        {"role": "assistant", "content": "saved activity 1", "_ts": 101},
    ]
    prompt = {"role": "user", "content": "current prompt", "_ts": 100}
    rows, offset = _message_window_for_display(history + saved, msg_limit=2)
    assert offset == oldest_idx
    full_rows, full_offset = _message_window_for_display(history + [prompt] + saved)
    assert full_offset == 0
    src = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    recovery = "\n".join(_function(src, name) for name in (
        "_inflightHasVisibleLiveState", "_serverLiveSnapshotInflight",
        "_selectLiveRecoveryInflight", "_notifyRunJournalSideEffectRecoveryTruncation",
        "_renderRuntimeJournalAnchorActivityScene", "_ensureInflightLiveAssistantMessage",
        "_projectInflightMessagesForActivityBursts",
    ))
    prelude = _fork_harness(_index_helpers(), _helper_src(), _fork_caller(), oldest_idx=0)
    # Replace the unrelated post-fork navigation seam with the actual loader.
    prelude = prelude.replace("async function loadSession() {}", _function(src, "loadSession"))
    script = prelude + _ensure_loaded_scaffold() + recovery + _function(src, "_ensureMessagesLoaded") + f"""
let _loadingOlder = false;
let _yoloEnabled = false;
let settled = false;
let reloadWindowLimit = 2;
const reads = [];
const toasts = [];
function _messageReloadLimitForSession() {{ return reloadWindowLimit; }}
function _captureSameSessionForceReloadHint() {{}}
function _rearmActiveSessionStream() {{}}
function stopApprovalPolling() {{}}
function hideApprovalCard() {{}}
function _updateYoloPill() {{}}
function _resolveSessionModelForDisplaySoon() {{}}
function _acknowledgeSessionVisit() {{}}
function _setActiveSessionUrl() {{}}
function _isMessagingSession() {{ return false; }}
function _hideHandoffHint() {{}}
function _deferWorkspaceRefreshForSession() {{}}
function syncTopbar() {{}}
function renderMessages() {{}}
function ensureLiveWorklogShell() {{}}
function startApprovalPolling() {{}}
function setComposerStatus() {{}}
function setStatus() {{}}
function updateSendBtn() {{}}
function updateQueueBadge() {{}}
function setBusy(value) {{ S.busy = value; }}
function getPendingSessionMessage(session) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {{role:'user', content:text, _ts:(session && session.pending_started_at) || 10, _pending:true}};
}}
var document = {{ getElementById: () => null }};
var localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
function $() {{ return null; }}
function showToast(message) {{ toasts.push(message); }}
function api(url, opts) {{
  if (opts && opts.method === 'POST') {{
    assert.strictEqual(url, '/api/session/branch');
    forkPayloads.push(JSON.parse(opts.body));
    return Promise.resolve({{}}); // Do not persist a real fork in this test.
  }}
  reads.push(url);
  assert(url.startsWith('/api/session?session_id=s1&'));
  const session = {{session_id:'s1', active_stream_id:settled?null:'stream-1',
    pending_started_at:settled?null:100, tool_calls:[], last_usage:{{}}}};
  if (url.includes('messages=1')) {{
    if (settled) {{
      assert(!url.includes('msg_limit='));
      Object.assign(session, {{messages:{json.dumps(full_rows)}, _messages_offset:0, _messages_truncated:false}});
    }} else {{
      assert(url.includes('msg_limit=2'));
      Object.assign(session, {{messages:{json.dumps(rows)}, _messages_offset:{offset}, _messages_truncated:{str(bool(offset)).lower()}}});
    }}
    session.message_count = settled ? {len(full_rows)} : {len(history + saved)};
  }} else assert(url.includes('messages=0'));
  return Promise.resolve({{session}});
}}
INFLIGHT.s1 = {{streamId:'stream-1', messages:[
  {{role:'user', content:'current prompt', _ts:100, _pending:true}},
  {{role:'assistant', content:'working on it', _live:true}},
], toolCalls:[]}};
(async () => {{
  for (let pass=0; pass<2; pass++) {{
    await loadSession('s1', {{force:true}});
    assert.strictEqual(reads.length, (pass+1)*2, 'metadata and message reads both completed');
    assert.strictEqual(_loadingSessionId, null, 'real loader reached its completion boundary');
    assert.strictEqual(_oldestIdx, {oldest_idx});
    assert.deepStrictEqual(S.messages.map(m=>m.content),
      ['current prompt','saved activity 0','saved activity 1','working on it']);
    const savedIdx = S.messages.findIndex(m=>m.content==='saved activity 0');
    assert.strictEqual(_messageSessionIndexForRawIdx(savedIdx), {oldest_idx});
    assert.strictEqual(S.messages.filter(m=>m.role==='user').length, 1);
    for (const busy of [true, false]) {{
      S.busy = busy;
      const before = forkPayloads.length;
      await forkAt(savedIdx);
      assert.strictEqual(forkPayloads.length, before+1);
      assert.strictEqual(forkPayloads.at(-1).keep_count, {oldest_idx + 1});
    }}
    S.busy = true;
    const beforeRefusals = forkPayloads.length;
    await forkFromMessage(1, {{closest:()=>rowFor(0)}});
    await forkFromMessage(S.messages.length, {{closest:()=>rowFor(S.messages.length-1)}});
    assert.strictEqual(forkPayloads.length, beforeRefusals, 'prompt and live rows cannot branch');
    assert.strictEqual(toasts.at(-1), 'Cannot fork a message still being generated.');
  }}
  settled = true;
  delete INFLIGHT.s1;  // The stream ended: a fresh page has no INFLIGHT cache.
  reloadWindowLimit = null;
  await loadSession('s1', {{force:true}});
  assert.strictEqual(reads.length, 6);
  assert.strictEqual(_loadingSessionId, null);
  assert.strictEqual(S.busy, false);
  assert.strictEqual(INFLIGHT.s1, undefined);
  assert.strictEqual(S.messages.filter(m=>m._displayOnlyRow).length, 0);
  assert.strictEqual(S.messages.filter(m=>m.role==='user').length, 1);
  assert.strictEqual(_messageSessionIndexForRawIdx({oldest_idx}), {oldest_idx});
  const before = forkPayloads.length;
  await forkAt({oldest_idx});
  assert.strictEqual(forkPayloads.length, before+1, 'persisted prompt is now branchable');
  assert.strictEqual(forkPayloads.at(-1).keep_count, {oldest_idx + 1});
  console.log(JSON.stringify({{offset:{oldest_idx}, lifecycle:true}}));
}})().catch(e=>{{ console.error(e); process.exit(1); }});
"""
    out = _run_file(script)
    assert '"lifecycle":true' in out, out
