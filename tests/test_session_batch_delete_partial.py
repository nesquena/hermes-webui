"""Behavioural test for the batch delete handler in static/sessions.js.

#8323 review item 1: a refused delete (409, live gateway work) used to reject
the shared `Promise.all`, so the UI skipped cleanup for every deletion that had
already committed — the deleted conversation stayed open, and its next send hit
the 404 path and lost the draft.

This drives the REAL `_deleteSelectedSessionsBatch()` from static/sessions.js
against a mocked browser, mirroring tests/test_goal_command_js_behaviour.py, and
asserts the observable effects.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3] || '';

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error(name + ' not found');
  const start = m.index;
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// ---- mocked UI state: A is the active idle session, B is still running ----
const _selectedSessions = new Set(['A', 'B']);
const S = { session: { session_id: 'A' }, messages: [], entries: [] };
const calls = { drafts: [], handoff: [], toasts: [], exit: 0, loaded: [], renders: 0, bars: 0 };

const t = (k) => k;
const $ = () => null;
const localStorage = { removeItem: () => {} };
function showToast(msg, ms, kind) { calls.toasts.push({ msg, kind: kind || null }); }
function showConfirmDialog() { return Promise.resolve(true); }
function _readComposerModelPick() { return null; }
function _worktreeSessionCount() { return 0; }
function _sessionSnapshotById(sid) { return { session_id: sid }; }
function _forgetComposerDraftLocalFallback(sid) { calls.drafts.push(sid); }
function _worktreeResponseCount() { return 0; }
function _clearHandoffStorageForSession(sid) { calls.handoff.push(sid); }
function _sessionListQueryString() { return ''; }
function _settleEmptyComposerModelAfterFinalSessionDelete() {}
function _hydrateTodosFromSession() {}
function _updateBatchActionBar() { calls.bars++; }
function exitSessionSelectMode() { calls.exit++; _selectedSessions.clear(); }
async function loadSession(sid) { calls.loaded.push(sid); }
async function renderSessionList() { calls.renders++; }

async function api(url, opts) {
  if (url.indexOf('/api/session/delete') === 0) {
    const sid = JSON.parse(opts.body).session_id;
    if (scenario === 'mixed' && sid === 'B') {
      const err = new Error('session_busy');
      err.status = 409;
      throw err;
    }
    return { ok: true };
  }
  return { sessions: [{ session_id: 'C' }] };
}

eval(extractFunc('_sessionDeleteErrorMessage'));
eval(extractFunc('_deleteSelectedSessionsBatch'));

(async () => {
  await _deleteSelectedSessionsBatch();
  process.stdout.write(JSON.stringify({
    calls,
    selected: [..._selectedSessions],
    active: S.session && S.session.session_id,
    busyMessage: _sessionDeleteErrorMessage(new Error('session_busy')),
    ledgerMessage: _sessionDeleteErrorMessage(new Error('runtime_ledger_unavailable')),
  }));
})().catch(e => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
"""


def _run(tmp_path, scenario):
    # `node -e` shifts process.argv, so run the driver as a real file instead.
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(SESSIONS_JS_PATH), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_mixed_batch_delete_finishes_the_committed_ones(tmp_path):
    out = _run(tmp_path, "mixed")

    # The committed session is cleaned up and the active chat is replaced.
    assert out["calls"]["drafts"] == ["A"]
    assert out["calls"]["handoff"] == ["A"]
    assert out["calls"]["loaded"] == ["C"]
    assert out["active"] is None or out["active"] != "A"

    # The refused session stays selected (so the user can retry), with its draft.
    assert out["selected"] == ["B"]
    assert out["calls"]["bars"] >= 1
    assert out["calls"]["exit"] == 0

    # The user is told what happened, not which wire code fired.
    last = out["calls"]["toasts"][-1]
    assert "session_delete_busy" in last["msg"]
    assert "(1/2)" in last["msg"]
    assert last["kind"] == "error"


def test_full_success_batch_delete_clears_selection(tmp_path):
    out = _run(tmp_path, "ok")

    assert out["calls"]["drafts"] == ["A", "B"]
    assert out["calls"]["loaded"] == ["C"]
    assert out["selected"] == []
    assert out["calls"]["exit"] == 1
    assert out["calls"]["toasts"][-1]["msg"] == "session_delete (2)"


def test_refusal_codes_map_to_user_facing_messages(tmp_path):
    out = _run(tmp_path, "ok")

    assert out["busyMessage"] == "session_delete_busy"
    assert out["ledgerMessage"] == "session_delete_ledger_unavailable"
