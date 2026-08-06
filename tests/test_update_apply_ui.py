"""Frontend regression coverage for Update Now apply failures (#1321)."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")


def _ui_js() -> str:
    return UI_JS.read_text(encoding="utf-8")


def _run_apply_updates_harness(update_data, responses):
    if NODE is None:
        pytest.skip("node not available for applyUpdates harness")
    js = r"""
const fs = require('fs');
const updateData = JSON.parse(process.argv[1]);
const responses = JSON.parse(process.argv[2]);
const uiPath = process.argv[3];
const src = fs.readFileSync(uiPath, 'utf8');
const start = src.indexOf('async function applyUpdates()');
const end = src.indexOf('function _showUpdateError', start);
const snippet = src.slice(start, end);

const button = { disabled: false, textContent: 'Update Now' };
const errorEl = { style: { display: 'none' }, textContent: '' };
const forceBtn = { style: { display: 'block' }, dataset: { target: 'stale-target' } };
const dom = {
  btnApplyUpdate: button,
  updateError: errorEl,
  btnForceUpdate: forceBtn,
};
const sessionStorage = {
  removed: [],
  removeItem(key) { this.removed.push(key); },
};
const showToasts = [];
const apiCalls = [];
const waitCalls = [];
const errors = [];
let readHealthCalls = 0;

function $(id) { return dom[id] || null; }
async function api(url, opts) {
  const payload = JSON.parse(opts.body);
  apiCalls.push(payload.target);
  const res = responses[apiCalls.length - 1];
  if (res.throwMessage) throw new Error(res.throwMessage);
  return res;
}
async function _readHealthServerIdentity() {
  readHealthCalls += 1;
  return 'baseline-id';
}
function _waitForServerThenReload(opts) {
  waitCalls.push({
    baselineServerIdentity: opts.baselineServerIdentity,
    apiCallsSnapshot: apiCalls.slice(),
  });
}
function _showUpdateError(target, res) { errors.push({ target, message: res.message || '' }); }
function _formatUpdateApplyExceptionMessage(e) { return 'Update failed: ' + e.message; }
function showToast(message, duration, kind) {
  showToasts.push({ message, duration, kind });
}
function setTimeout(cb, ms) { cb(); return 1; }
function clearTimeout() {}

global.window = { _updateApplyInFlight: false, _updateData: updateData };
global.sessionStorage = sessionStorage;
global.$ = $;
global.api = api;
global._readHealthServerIdentity = _readHealthServerIdentity;
global._waitForServerThenReload = _waitForServerThenReload;
global._showUpdateError = _showUpdateError;
global._formatUpdateApplyExceptionMessage = _formatUpdateApplyExceptionMessage;
global.showToast = showToast;
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;

eval(snippet);

(async () => {
  await applyUpdates();
  console.log(JSON.stringify({
    apiCalls,
    waitCalls,
    errors,
    showToasts,
    errorDisplay: errorEl.style.display,
    errorText: errorEl.textContent,
    removedKeys: sessionStorage.removed,
    inFlight: window._updateApplyInFlight,
    buttonDisabled: button.disabled,
    buttonText: button.textContent,
    forceHidden: forceBtn.style.display,
    forceTarget: forceBtn.dataset.target,
    readHealthCalls,
  }));
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
"""
    result = subprocess.run(
        [NODE, "-e", js, json.dumps(update_data), json.dumps(responses), str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node harness failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_update_apply_network_error_has_recovery_message_not_raw_failed_to_fetch():
    """Network/interrupted update apply failures should not surface raw fetch text alone."""
    src = _ui_js()
    assert "function _formatUpdateApplyExceptionMessage" in src
    assert "could not reach the WebUI server" in src
    assert "restarted or the connection was interrupted" in src
    assert "wait a few seconds, reload the page, then check the server" in src
    assert "Update failed: '+e.message" not in src
    assert 'Update failed: "+e.message' not in src


def test_update_apply_structured_server_errors_still_use_json_message_path():
    """Server-reachable JSON errors must keep the existing targeted message path."""
    src = _ui_js()
    apply_start = src.index("async function applyUpdates()")
    show_error_call = src.index("_showUpdateError(target,res);", apply_start)
    reset_button = src.index("resetApplyButton(0);", show_error_call)
    assert show_error_call < reset_button
    assert "const msg='Update failed ('+target+'): '+(res.message||'unknown error');" in src


def test_update_apply_successful_stash_conflict_displays_recovery_message():
    """ok=True stash conflicts must show the server recovery message before restarting."""
    src = _ui_js()
    apply_start = src.index("async function applyUpdates()")
    next_fn = src.index("function _showUpdateError", apply_start)
    body = src[apply_start:next_fn]

    messages_decl = body.index("const stashConflictMessages=[];")
    stash_branch = body.index("if(res.stash_conflict)")
    message_push = body.index("stashConflictMessages.push('Update applied ('+target+'):", stash_branch)
    persistent_display = body.index("errEl.textContent=stashConflictMessages.join('\\n\\n')", message_push)
    message_join = body.index("const stashConflictMessage=stashConflictMessages.join('\\n\\n');", persistent_display)
    restart_wait = body.index("_waitForServerThenReload", message_join)

    assert messages_decl < stash_branch < message_push < persistent_display < message_join < restart_wait
    assert "showToast(stashConflictMessage||'Update applied" in body
    assert "stashConflictMessages.length?10000" in body


def test_update_apply_multiple_stash_conflicts_are_aggregated_not_overwritten():
    """Multiple ok=True stash conflicts must preserve every target recovery message."""
    src = _ui_js()
    apply_start = src.index("async function applyUpdates()")
    next_fn = src.index("function _showUpdateError", apply_start)
    body = src[apply_start:next_fn]

    assert "let stashConflictMessage='';" not in body
    assert "stashConflictMessage='Update applied ('+target+'):" not in body
    assert "const stashConflictMessages=[];" in body
    assert "stashConflictMessages.push('Update applied ('+target+'): " in body
    assert "errEl.textContent=stashConflictMessages.join('\\n\\n')" in body
    assert "const stashConflictMessage=stashConflictMessages.join('\\n\\n');" in body
    assert "showToast(stashConflictMessage||'Update applied" in body


def test_update_apply_network_error_classifier_ignores_http_status_errors():
    """HTTP response errors should not be classified as interrupted transport failures."""
    src = _ui_js()
    fn_start = src.index("function _isUpdateApplyNetworkError(error)")
    fn_end = src.index("function _formatUpdateApplyExceptionMessage", fn_start)
    body = src[fn_start:fn_end]
    compact = re.sub(r"\s+", "", body)
    assert "if(error&&error.status)returnfalse;" in compact
    assert body.index("error.status") < body.index("/Failed to fetch|NetworkError|Load failed/i")
    assert "Failed to fetch|NetworkError|Load failed" in body


def test_update_apply_prevents_duplicate_apply_requests_while_in_flight():
    """Double-clicks should not send a second update apply request during restart race windows."""
    src = _ui_js()
    apply_start = src.index("async function applyUpdates()")
    next_fn = src.index("function _showUpdateError", apply_start)
    body = src[apply_start:next_fn]
    assert "window._updateApplyInFlight" in body
    assert "if(window._updateApplyInFlight) return;" in body
    assert "window._updateApplyInFlight=true;" in body
    assert "window._updateApplyInFlight=false;" in body


def test_update_apply_rejects_zero_target_success_path():
    """Update Now must return before the toast and reload flow when nothing is behind."""
    result = _run_apply_updates_harness(
        {"agent": {"behind": 0}, "webui": {"behind": 0}},
        [],
    )
    assert result["apiCalls"] == []
    assert result["waitCalls"] == []
    assert result["showToasts"] == []
    assert result["errorDisplay"] == "block"
    assert "No update target selected" in result["errorText"]


def test_apply_updates_queues_agent_before_webui():
    result = _run_apply_updates_harness(
        {"agent": {"behind": 1}, "webui": {"behind": 1}},
        [{"ok": True}, {"ok": True}],
    )
    assert result["apiCalls"] == ["agent", "webui"]
    assert result["waitCalls"] == [
        {
            "baselineServerIdentity": "baseline-id",
            "apiCallsSnapshot": ["agent", "webui"],
        }
    ]


def test_apply_updates_wait_for_all_targets_before_reload():
    result = _run_apply_updates_harness(
        {"agent": {"behind": 1}, "webui": {"behind": 1}},
        [{"ok": True}, {"ok": False, "message": "webui failed"}],
    )
    assert result["apiCalls"] == ["agent", "webui"]
    assert result["waitCalls"] == []
    assert result["errors"] == [{"target": "webui", "message": "webui failed"}]
