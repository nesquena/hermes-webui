"""Behavioural tests that drive the ACTUAL cmdGoal() from static/commands.js via node.

The source-inspection regressions in test_goal_command_webui.py assert that
certain expressions/order exist inside cmdGoal's source text. They can stay
green when a refactor preserves those strings but sends the wrong
explicit_model_pick or consumes the pending session-model marker at the wrong
time (#6705, greptile-apps P2). This file closes that gap by spawning node on
the real static/commands.js, extracting cmdGoal, and driving it against a
mocked browser environment (sessionStorage, S, window, api) — asserting the
OBSERVABLE effects: the explicit_model_pick field on the /api/goal payload and
the pending marker's survival/consumption in sessionStorage.

Mirrors the approach of test_renderer_js_behaviour.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS_PATH = REPO_ROOT / "static" / "commands.js"
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const commandsSrc = fs.readFileSync(process.argv[2], 'utf8');
const messagesSrc = fs.readFileSync(process.argv[3], 'utf8');
const sessionsSrc = fs.readFileSync(process.argv[4], 'utf8');
const uiSrc = fs.readFileSync(process.argv[5], 'utf8');
const scenario = process.argv[6] || '';

// ---- mocked browser environment ----
const _store = new Map();
global.sessionStorage = {
  getItem: k => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => { _store.set(k, String(v)); },
  removeItem: k => { _store.delete(k); },
};
global.window = {};
// Match the initial pane-loading state owned by static/sessions.js.
const _loadingSessionId = null;

// Pending session-model marker helpers mirroring static/ui.js
// (PENDING_SESSION_MODEL_PREFIX / _readPendingSessionModel / _clearPendingSessionModel).
const PENDING_PREFIX = 'hermes-webui-pending-session-model:';
const MAX_AGE_MS = 10 * 60 * 1000;
const _key = sid => PENDING_PREFIX + String(sid || '');
function rememberPending(sid, model, provider) {
  const s = String(sid || '').trim();
  const value = String(model || '').trim();
  if (!s || !value) return;
  try {
    sessionStorage.setItem(_key(s), JSON.stringify({
      model: value,
      model_provider: provider ? String(provider).trim() : null,
      saved_at: Date.now(),
    }));
  } catch (_) {}
}
function readPending(sid) {
  const s = String(sid || '').trim();
  if (!s) return null;
  try {
    const raw = sessionStorage.getItem(_key(s));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    const model = String(parsed && parsed.model || '').trim();
    if (!model) { sessionStorage.removeItem(_key(s)); return null; }
    const savedAt = Number(parsed.saved_at || 0);
    if (savedAt && Date.now() - savedAt > MAX_AGE_MS) { sessionStorage.removeItem(_key(s)); return null; }
    return {
      model,
      model_provider: parsed && parsed.model_provider ? String(parsed.model_provider) : null,
    };
  } catch (_) { return null; }
}
function clearPending(sid) {
  const s = String(sid || '').trim();
  if (!s) return;
  try { sessionStorage.removeItem(_key(s)); } catch (_) {}
}
const _readPendingSessionModel = readPending;
const _clearPendingSessionModel = clearPending;

// ---- command helpers the extracted cmdGoal references ----
const t = k => k;
const showToast = () => {};
const renderMessages = () => {};
const clearLiveToolCards = () => {};
const appendThinking = () => {};
const setBusy = () => {};
const setComposerStatus = () => {};
const markInflight = () => {};
const saveInflightState = () => {};
const startApprovalPolling = () => {};
const startClarifyPolling = () => {};
const _fetchYoloState = () => {};
const attachLiveStream = () => {};
const renderSessionList = () => {};
const newSession = async () => {};
const $ = () => null;
const INFLIGHT = {};

// ---- api mock: record every /api/goal payload, respond per scenario ----
const _apiCalls = [];
let _nextResponse = () => ({});
async function api(url, opts) {
  _apiCalls.push({ url, body: JSON.parse(opts.body) });
  return _nextResponse();
}

// ---- extract production helpers and cmdGoal from their real files ----
function extractFunc(source, name) {
  // Preserve a leading `async` keyword — dropping it would make the
  // extracted `await` statements a SyntaxError.
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(source);
  if (!m) throw new Error(name + ' not found');
  const start = m.index;
  let i = source.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < source.length) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') depth--;
    i++;
  }
  return source.slice(start, i);
}
eval(extractFunc(messagesSrc, '_isSessionCurrentPane'));
eval(extractFunc(sessionsSrc, '_opaqueActiveTurnToken'));
eval(extractFunc(uiSrc, '_captureSessionActiveTurnIdentity'));
eval(extractFunc(uiSrc, '_acceptedStartMayUpdateSession'));
eval(extractFunc(commandsSrc, 'cmdGoal'));

// ---- scenario state ----
const SID = 'sid-6705-behaviour';
const S = {
  session: {
    session_id: SID,
    workspace: '/tmp/ws',
    model: 'openai/gpt-5.4',
    model_provider: 'openai',
    profile: 'default',
    active_stream_id: null,
  },
  activeProfile: 'default',
  messages: [],
  toolCalls: [],
  activeStreamId: null,
};

(async () => {
  const out = {};
  if (scenario === 'kickoff_consumes') {
    // Pending pick matches the session model; server returns a real kickoff.
    rememberPending(SID, 'openai/gpt-5.4', 'openai');
    window._defaultModel = 'gpt-4o'; window._activeProvider = 'openai';
    _nextResponse = () => ({ stream_id: 's1', session_id: SID, pending_started_at: 1,
      active_turn_token: 'opaque-token-s1',
      effective_model: 'openai/gpt-5.4', effective_model_provider: 'openai' });
    await cmdGoal('ship it');
    out.payload = _apiCalls[0].body;
    out.markerAfter = readPending(SID);
  } else if (scenario === 'control_then_kickoff') {
    // Control-only /goal status: server responds WITHOUT stream_id.
    rememberPending(SID, 'openai/gpt-5.4', 'openai');
    window._defaultModel = 'gpt-4o'; window._activeProvider = 'openai';
    _nextResponse = () => ({ message: 'no active goal', message_key: 'goal_no_active' });
    await cmdGoal('status');
    out.controlPayload = _apiCalls[0].body;
    out.markerAfterControl = readPending(SID);
    // Next real send must still carry the marker and consume it on kickoff.
    _nextResponse = () => ({ stream_id: 's2', session_id: SID, pending_started_at: 1,
      active_turn_token: 'opaque-token-s2' });
    await cmdGoal('ship it');
    out.kickoffPayload = _apiCalls[1].body;
    out.markerAfterKickoff = readPending(SID);
  } else if (scenario === 'midflight_newer_marker_kept') {
    // A newer dropdown selection is recorded WHILE the request is in flight.
    rememberPending(SID, 'openai/gpt-5.4', 'openai');
    window._defaultModel = 'gpt-4o'; window._activeProvider = 'openai';
    _nextResponse = () => {
      rememberPending(SID, 'openai/gpt-6', 'openai');
      return { stream_id: 's3', session_id: SID, pending_started_at: 1,
        active_turn_token: 'opaque-token-s3' };
    };
    await cmdGoal('ship it');
    out.payload = _apiCalls[0].body;
    out.markerAfter = readPending(SID);
  } else if (scenario === 'no_marker_no_pick') {
    // Untouched default session: no pending marker, no cross-provider pick.
    S.session.model = 'gpt-4o'; S.session.model_provider = 'openai';
    window._defaultModel = 'gpt-4o'; window._activeProvider = 'openai';
    _nextResponse = () => ({ stream_id: 's4', session_id: SID, pending_started_at: 1,
      active_turn_token: 'opaque-token-s4' });
    await cmdGoal('ship it');
    out.payload = _apiCalls[0].body;
    out.markerAfter = readPending(SID);
  } else {
    throw new Error('unknown scenario: ' + scenario);
  }
  process.stdout.write(JSON.stringify(out));
})().catch(e => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    """Write the node driver to a tmp file (works around `node -e` arg quirks)."""
    p = tmp_path_factory.mktemp("goal_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run_scenario(driver_path, scenario):
    """Run cmdGoal against the real commands.js with mocked browser state."""
    result = subprocess.run(
        [
            NODE,
            driver_path,
            str(COMMANDS_JS_PATH),
            str(MESSAGES_JS_PATH),
            str(SESSIONS_JS_PATH),
            str(UI_JS_PATH),
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed for {scenario}: {result.stderr}")
    return json.loads(result.stdout)


def test_goal_kickoff_consumes_pending_marker_after_success(driver_path):
    """#6703/#6705: a real kickoff carries explicit_model_pick and consumes the
    one-shot pending marker AFTER the successful response (r.stream_id)."""
    out = _run_scenario(driver_path, "kickoff_consumes")
    assert out["payload"].get("explicit_model_pick") is True
    assert out["markerAfter"] is None


def test_goal_control_command_keeps_marker_and_next_send_still_picks(driver_path):
    """#6705: a control-only /goal (e.g. `/goal status`, no stream_id) must NOT
    consume the pending explicit-pick marker; the next real send still carries
    explicit_model_pick and only then consumes the marker."""
    out = _run_scenario(driver_path, "control_then_kickoff")
    # Control round-trip: marker read for the payload but left intact.
    assert out["controlPayload"].get("explicit_model_pick") is True
    assert out["markerAfterControl"] is not None
    assert out["markerAfterControl"]["model"] == "openai/gpt-5.4"
    # Next real send: still carries the pick, then consumes the marker.
    assert out["kickoffPayload"].get("explicit_model_pick") is True
    assert out["markerAfterKickoff"] is None


def test_goal_kickoff_keeps_newer_midflight_marker(driver_path):
    """#6705: a marker re-recorded while the request is in flight (newer
    dropdown selection) must not be clobbered by the stale consume-clear."""
    out = _run_scenario(driver_path, "midflight_newer_marker_kept")
    assert out["payload"].get("explicit_model_pick") is True
    assert out["markerAfter"] is not None
    assert out["markerAfter"]["model"] == "openai/gpt-6"


def test_goal_kickoff_without_marker_sends_no_explicit_pick(driver_path):
    """#6703: untouched default sessions (no pending marker, no cross-provider
    pick) must not send the marker at all."""
    out = _run_scenario(driver_path, "no_marker_no_pick")
    assert "explicit_model_pick" not in out["payload"]
    assert out["markerAfter"] is None
