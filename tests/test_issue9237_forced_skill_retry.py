"""A forced skill stays with the draft restored after a failed send (#9237).

send() consumes a pending /use directive before POST /api/chat/start and
prepends the directive and the skill content to the message it posts. When
that start fails and send() puts the typed text back as the chat's draft, the
skill must come back with it: the next send of exactly that text in that chat
carries the same forced-skill block once, an edited or abandoned draft drops
it, and a successful retry uses it up.

These tests run the real send(), _restoreComposerDraftAfterFailedSend() and
the restored-draft helpers from static/messages.js in node, switch chats
inside the awaited /api/chat/start stub (the way a click lands during the
request), then return to the chat and send its restored draft.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def _cut(start_marker: str, end_marker: str) -> str:
    start = MESSAGES_JS.index(start_marker)
    end = MESSAGES_JS.index(end_marker, start)
    return MESSAGES_JS[start:end]


SEND_SRC = _cut("async function send(){", "\nasync function startRegeneration(")
CURRENT_PANE_SRC = _cut("function _isSessionCurrentPane(sid) {", "\nfunction _isSessionActivelyViewed(")
# The restored-draft helpers (goal continuation id and forced skill).
DRAFT_HELPERS_SRC = _cut("function _normalizeGoalContinuationId(", "\nfunction _bgTaskCompleteRingBufferAdd(")
RECOVER_AND_RESTORE_SRC = _cut(
    "async function _recoverCompressedSend(", "\nfunction _setComposerAfterCliOnlyExplainer("
)
DEFAULT_TITLE_SRC = _cut(
    "function _sessionTitleLooksDefaultOrProvisional(", "\nfunction _firstUserMessageTitleCandidate("
)

WRITER = {
    "directive": "[USER OVERRIDE] You MUST follow the skill 'writer' content provided below.",
    "name": "writer",
    "content": "Always use the writer guide.",
}
EDITOR = {
    "directive": "[USER OVERRIDE] You MUST follow the skill 'editor' content provided below.",
    "name": "editor",
    "content": "Always use the editor guide.",
}
WRITER_BLOCK = "[FORCED SKILL CONTEXT: writer]"
EDITOR_BLOCK = "[FORCED SKILL CONTEXT: editor]"


HARNESS = r"""
const STEPS = __STEPS__;
const calls = [];
const starts = [];
const plain = value => JSON.parse(JSON.stringify(value, (k, v) => v === undefined ? null : v));
const rec = (name, ...args) => calls.push({name, args: plain(args)});
const input = {value: '', dataset: {}};
const SESSIONS = {
  A: {session_id:'A', workspace:'/a', profile:'pa', model:'model-a', model_provider:'prov-a', title:'Chat A'},
  A2: {session_id:'A2', workspace:'/a', profile:'pa', model:'model-a', model_provider:'prov-a', title:'Chat A'},
  B: {session_id:'B', workspace:'/b', profile:'pb', model:'model-b', model_provider:'prov-b', title:'Chat B'},
};
const S = {
  session: SESSIONS.A, messages: [], pendingFiles: [], toolCalls: [], activeProfile: null, busy: false,
  activeStreamId: null,
};
const DRAFTS = {};
const INFLIGHT = {};
const _pendingSelections = [];
let _loadingSessionId = null;
let _sendInProgress = false;
let _sendInProgressSid = null;
let _queueDrainSid = null;
let _approvalSessionId = null;
let _clarifySessionId = null;
let _projectPickerOpenEpoch = 0;
let _forcedSkillDirectivePending = null;
const COMMANDS = [];
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set();
const localStorage = {setItem(){}, removeItem(){}};
const history = {replaceState(){}};
const window = {_defaultModel:'model-a', _activeProvider:'prov-a', location:{pathname:'/session/A', search:''}};
const document = {querySelector(){ return null; }};
const $ = id => id === 'msg' ? input : null;
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

// A chat switch: the chat that is opened gets its saved draft in the composer.
function openChat(sid){
  S.session = SESSIONS[sid];
  S.messages = [];
  S.busy = false;
  S.activeStreamId = null;
  input.value = DRAFTS[sid] ? DRAFTS[sid].text : '';
}

function _composerTextWithPendingSelections(){ return input.value; }
function _chatPayloadModelState(){ return {model:S.session.model, model_provider:S.session.model_provider}; }
function _flushSelectionBlocksToComposer(){}
function shouldInterceptCompressionRecoveryContinuation(){ return false; }
function _dismissHandoffHint(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(text){ const m = /^\/(\S+)\s*(.*)$/.exec(text); return m ? {name:m[1], args:m[2]} : null; }
async function getAgentCommandMetadata(){ return null; }
async function getBundleCommandMetadata(){ return null; }
function hideCmdDropdown(){}
function renderTray(){}
function autoResize(){}
function _clearComposerDraft(sid){ DRAFTS[sid] = {text:'', files:[], contId:''}; return Promise.resolve(); }
function _saveComposerDraftNow(sid, text, files, contId){
  rec('_saveComposerDraftNow', sid, text);
  DRAFTS[sid] = {text, files, contId: contId || ''};
  return Promise.resolve();
}
function _handleComposerDraftPostFailure(){}
function setComposerStatus(){}
function clearLiveToolCards(){}
function renderMessages(){}
function setBusy(value){ S.busy = value; }
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_label, fn){ if(fn) fn(); }
function _runOptionalPostStartUiStep(_label, fn){ if(fn) fn(); }
function upsertActiveSessionForLocalTurn(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function applySessionTitleUpdate(){}
function _readPendingSessionModel(){ return null; }
function _clearPendingSessionModel(){}
function _writePersistedModelState(){}
function _applyModelToDropdown(){}
function syncTopbar(){}
function syncModelChip(){}
function showLiveRunStatus(){}
function stopApprovalPolling(){}
function stopClarifyPolling(){}
function hideApprovalCard(){}
function hideClarifyCard(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function renderSessionList(){}
function markInflight(){}
function saveInflightState(){}
function clearInflightState(){}
function queueSessionMessage(sid, payload){ rec('queueSessionMessage', sid, payload && payload.text); }
function _clearComposerAfterQueuedSelectionSend(){}
function updateQueueBadge(){}
function showToast(msg){ rec('showToast', String(msg)); }
function t(key, ...args){ return [key, ...args].join(' '); }
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
function attachLiveStream(sid, streamId){ rec('attachLiveStream', sid, streamId); }
async function loadSession(sid){ rec('loadSession', sid); openChat(sid); }
async function newSession(){ throw new Error('unexpected newSession'); }
async function uploadPendingFiles(){ return []; }

let plan = [];
async function api(url, opts){
  if(url !== '/api/chat/start') throw new Error('unexpected api ' + url);
  const body = JSON.parse(opts.body);
  starts.push({session_id: body.session_id, message: body.message});
  const step = plan.shift() || {};
  if(step.switchTo) openChat(step.switchTo);
  const r = step.start || 'ok';
  if(r === 'ok') return {stream_id:'stream-' + starts.length};
  if(r === '409-conflict'){
    const err = new Error('Session already has an active stream');
    err.status = 409;
    throw err;
  }
  if(r === '409-rotated'){
    const err = new Error('session rotated');
    err.status = 409;
    err.body = JSON.stringify({code:'session_rotated', continuation_session_id:'A2'});
    throw err;
  }
  const err = new Error('boom');
  err.status = 500;
  throw err;
}

__DEFAULT_TITLE__
__CURRENT_PANE__
__DRAFT_HELPERS__
__RECOVER_AND_RESTORE__
__SEND__

(async () => {
  for(const step of STEPS){
    if(step.use){
      _forcedSkillDirectivePending = {sessionId: step.useSid || null, promise: Promise.resolve(step.use)};
    } else if(step.open){
      openChat(step.open);
    } else if(step.type !== undefined){
      input.value = step.type;
    } else if(step.send){
      plan = [step];
      await send();
      await tick();
      // The started turn (if any) finishes before the user's next action.
      S.busy = false;
      S.activeStreamId = null;
      for(const sid of Object.keys(INFLIGHT)) delete INFLIGHT[sid];
    }
  }
  console.log(JSON.stringify({calls, starts, drafts: DRAFTS, composer: input.value,
                              session: S.session && S.session.session_id}));
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""


def _run(*steps):
    script = (
        HARNESS.replace("__STEPS__", json.dumps(list(steps)))
        .replace("__DEFAULT_TITLE__", DEFAULT_TITLE_SRC)
        .replace("__CURRENT_PANE__", CURRENT_PANE_SRC)
        .replace("__DRAFT_HELPERS__", DRAFT_HELPERS_SRC)
        .replace("__RECOVER_AND_RESTORE__", RECOVER_AND_RESTORE_SRC)
        .replace("__SEND__", SEND_SRC)
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _use(skill, sid="A"):
    return {"use": skill, "useSid": sid}


def _type(text):
    return {"type": text}


def _send(start="ok", switch_to=None):
    step = {"send": True, "start": start}
    if switch_to:
        step["switchTo"] = switch_to
    return step


def _open(sid):
    return {"open": sid}


def _fail_off_screen(start="409-conflict"):
    """A sends 'hello' with /use writer; the user opens B while start is held; start fails."""
    return [_use(WRITER), _type("hello"), _send(start=start, switch_to="B")]


def _skill_count(message, block=WRITER_BLOCK):
    return message.count(block)


# ── the finding: off-screen start failure, then the user returns and resends ──


@pytest.mark.parametrize("start", ["409-conflict", "500"])
def test_off_screen_failure_keeps_the_skill_for_the_restored_draft(start):
    result = _run(*_fail_off_screen(start), _open("A"), _send())
    first, retry = result["starts"]
    assert _skill_count(first["message"]) == 1
    # The user is back on A with the restored text in the composer.
    assert first["session_id"] == retry["session_id"] == "A"
    assert retry["message"].endswith("hello")
    # The retry is the same forced-skill message a fresh /use send posts.
    assert _skill_count(retry["message"]) == 1
    assert retry["message"] == first["message"]


def test_rotated_off_screen_failure_keeps_the_skill_on_the_continuation_draft():
    result = _run(*_fail_off_screen("409-rotated"), _open("A2"), _send())
    first, retry = result["starts"]
    assert retry["session_id"] == "A2"
    assert _skill_count(retry["message"]) == 1
    assert retry["message"] == first["message"]


# ── on-screen start failure (pre-existing on master) ─────────────────────────


def test_on_screen_start_error_keeps_the_skill_for_the_restored_draft():
    result = _run(_use(WRITER), _type("hello"), _send(start="500"), _send())
    first, retry = result["starts"]
    assert result["session"] == "A"
    assert retry["session_id"] == "A"
    assert _skill_count(retry["message"]) == 1
    assert retry["message"] == first["message"]


# ── controls ────────────────────────────────────────────────────────────────


def test_control_an_edited_draft_drops_the_skill_for_good():
    result = _run(
        *_fail_off_screen(),
        _open("A"),
        _type("hello, reworded"),
        _send(),
        # Typing the old text again later is a new message, not the restored draft.
        _type("hello"),
        _send(),
    )
    _first, edited, later = result["starts"]
    assert edited["message"] == "hello, reworded"
    assert later["message"] == "hello"


def test_control_a_successful_retry_uses_the_skill_up():
    result = _run(*_fail_off_screen(), _open("A"), _send(), _type("hello"), _send())
    _first, retry, again = result["starts"]
    assert _skill_count(retry["message"]) == 1
    assert again["message"] == "hello"


def test_control_the_same_text_in_another_chat_does_not_take_the_skill():
    result = _run(*_fail_off_screen(), _type("hello"), _send(), _open("A"), _send())
    _first, in_b, retry_in_a = result["starts"]
    assert in_b["session_id"] == "B"
    assert in_b["message"] == "hello"
    assert retry_in_a["session_id"] == "A"
    assert _skill_count(retry_in_a["message"]) == 1


def test_control_a_newer_use_for_the_chat_wins_over_the_restored_skill():
    result = _run(*_fail_off_screen(), _open("A"), _use(EDITOR), _send())
    _first, retry = result["starts"]
    assert _skill_count(retry["message"], EDITOR_BLOCK) == 1
    assert _skill_count(retry["message"]) == 0


def test_control_a_failed_send_without_a_skill_restores_plain_text():
    result = _run(_type("hello"), _send(start="409-conflict", switch_to="B"), _open("A"), _send())
    _first, retry = result["starts"]
    assert retry["message"] == "hello"


def test_control_on_screen_conflict_still_queues_the_skill_message():
    result = _run(_use(WRITER), _type("hello"), _send(start="409-conflict"))
    queued = [c["args"] for c in result["calls"] if c["name"] == "queueSessionMessage"]
    assert len(queued) == 1 and queued[0][0] == "A"
    assert _skill_count(queued[0][1]) == 1
