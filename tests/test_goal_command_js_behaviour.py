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

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3] || '';

// ---- mocked browser environment ----
const _store = new Map();
global.sessionStorage = {
  getItem: k => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => { _store.set(k, String(v)); },
  removeItem: k => { _store.delete(k); },
};
global.window = {};

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
const _toasts = [];
const showToast = (...a) => { _toasts.push(a); };
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
  _apiCalls.push({ url, body: JSON.parse(opts.body),
    timeoutMs: opts.timeoutMs === undefined ? null : opts.timeoutMs,
    retries: opts.retries === undefined ? null : opts.retries });
  return _nextResponse();
}

// ---- extract cmdGoal from the real file and evaluate it ----
function extractFunc(name) {
  // Preserve a leading `async` keyword — dropping it would make the
  // extracted `await` statements a SyntaxError.
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
eval(extractFunc('cmdGoal'));

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
    _nextResponse = () => ({ stream_id: 's1', pending_started_at: 1,
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
    _nextResponse = () => ({ stream_id: 's2', pending_started_at: 1 });
    await cmdGoal('ship it');
    out.kickoffPayload = _apiCalls[1].body;
    out.markerAfterKickoff = readPending(SID);
  } else if (scenario === 'midflight_newer_marker_kept') {
    // A newer dropdown selection is recorded WHILE the request is in flight.
    rememberPending(SID, 'openai/gpt-5.4', 'openai');
    window._defaultModel = 'gpt-4o'; window._activeProvider = 'openai';
    _nextResponse = () => {
      rememberPending(SID, 'openai/gpt-6', 'openai');
      return { stream_id: 's3', pending_started_at: 1 };
    };
    await cmdGoal('ship it');
    out.payload = _apiCalls[0].body;
    out.markerAfter = readPending(SID);
  } else if (scenario === 'no_marker_no_pick') {
    // Untouched default session: no pending marker, no cross-provider pick.
    S.session.model = 'gpt-4o'; S.session.model_provider = 'openai';
    window._defaultModel = 'gpt-4o'; window._activeProvider = 'openai';
    _nextResponse = () => ({ stream_id: 's4', pending_started_at: 1 });
    await cmdGoal('ship it');
    out.payload = _apiCalls[0].body;
    out.markerAfter = readPending(SID);
  } else if (scenario === 'draft_fallback') {
    // Server set a free-form goal because no contract could be drafted.
    _nextResponse = () => ({ ok: true, action: 'draft', stream_id: 's5', pending_started_at: 1,
      message: '⊙ Goal set (20-turn budget): ship it\nCouldn\'t draft a contract (aux model unavailable) — running as a free-form goal.',
      toast_type: 'warning', toast_message: 'Couldn\'t draft a contract (aux model unavailable) — running as a free-form goal.' });
    await cmdGoal('draft ship it');
    out.calls = _apiCalls;
    out.toasts = _toasts;
    out.messages = S.messages.map(m => m.content);
    out.activeStreamId = S.activeStreamId;
  } else if (scenario === 'draft_usage_error') {
    // /api/goal 400: api() throws with the server's message.
    _nextResponse = () => { throw new Error('Usage: /goal draft <objective in plain language>'); };
    await cmdGoal('draft');
    out.calls = _apiCalls;
    out.toasts = _toasts;
    out.messages = S.messages.map(m => m.content);
  } else if (scenario === 'show_detail') {
    _nextResponse = () => ({ ok: true, action: 'show',
      message: '⊙ Goal (active, 0/20 turns): ship it', message_key: 'goal_status_active',
      message_args: [0, 20, 'ship it'], detail: '- Verification: pytest -q passes' });
    await cmdGoal('show');
    out.calls = _apiCalls;
    out.toasts = _toasts;
    out.messages = S.messages.map(m => m.content);
  } else if (scenario === 'plain_set_options') {
    _nextResponse = () => ({ stream_id: 's6', pending_started_at: 1, message: '⊙ Goal set (20-turn budget): ship it' });
    await cmdGoal('ship it');
    out.calls = _apiCalls;
    out.toasts = _toasts;
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
        [NODE, driver_path, str(COMMANDS_JS_PATH), scenario],
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


def test_goal_draft_is_sent_once_with_long_budget_and_warns_on_fallback(driver_path):
    """`/goal draft` waits on the contract drafter and starts a run: one request,
    90s budget, no retry. A free-form fallback toasts the server's warning text."""
    out = _run_scenario(driver_path, "draft_fallback")
    assert len(out["calls"]) == 1
    assert out["calls"][0]["body"]["args"] == "draft ship it"
    assert out["calls"][0]["timeoutMs"] == 90000
    assert out["calls"][0]["retries"] == 0
    assert out["toasts"] == [[
        "Couldn't draft a contract (aux model unavailable) — running as a free-form goal.", None, "warning",
    ]]
    assert out["messages"][0].startswith("⊙ Goal set (20-turn budget): ship it\nCouldn't draft a contract")
    assert out["activeStreamId"] == "s5"


def test_goal_draft_usage_error_toasts_as_error(driver_path):
    out = _run_scenario(driver_path, "draft_usage_error")
    assert len(out["calls"]) == 1
    assert out["toasts"] == [["Usage: /goal draft <objective in plain language>", 3000, "error"]]
    assert out["messages"] == ["**Goal command failed:** Usage: /goal draft <objective in plain language>"]


def test_goal_show_appends_contract_detail_to_status_line(driver_path):
    out = _run_scenario(driver_path, "show_detail")
    assert out["calls"][0]["timeoutMs"] is None and out["calls"][0]["retries"] is None
    assert out["messages"] == ["⊙ Goal (active, 0/20 turns): ship it\n- Verification: pytest -q passes"]
    assert out["toasts"] == [["⊙ Goal (active, 0/20 turns): ship it", 2600]]


def test_plain_goal_request_options_and_toast_are_unchanged(driver_path):
    out = _run_scenario(driver_path, "plain_set_options")
    assert out["calls"][0]["timeoutMs"] is None and out["calls"][0]["retries"] is None
    assert out["toasts"] == [["⊙ Goal set (20-turn budget): ship it", 2600]]
