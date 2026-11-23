"""A pending /use skill on a failed send reaches the retry as on master (#9237).

send() consumes a pending /use directive before POST /api/chat/start and
prepends the directive and the skill content to the message it posts. What
master did with that skill when the start failed, and what this branch does:

| start failure                          | master                         | here                           |
|----------------------------------------|--------------------------------|--------------------------------|
| off screen, 409 "active stream"        | queued copy carries the skill  | unchanged (master's branch)    |
|                                        | (and pulled the user back)     |                                |
| off screen, 500                        | draft, skill dropped           | draft, skill dropped           |
| off screen, 404                        | no draft, skill dropped        | no draft, skill dropped        |
| off screen, 409 session_rotated        | skill dropped                  | skill dropped                  |
| on screen, 409 "active stream"         | queued copy carries the skill  | unchanged                      |
| on screen, 500 / rotated               | draft, skill dropped           | unchanged                      |

These tests run the real send(), _restoreComposerDraftAfterFailedSend() and
the restored-draft helpers from static/messages.js in node, switch chats
inside the awaited /api/chat/start stub (the way a click lands during the
request), then return to the chat and send its restored draft or drain its
queue.
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
# The restored-draft helpers (goal continuation id).
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
const EDITOR_SKILL = __EDITOR__;
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
const QUEUE = {};
const SNAPS = {};
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
  _loadingSessionId = null;
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
function queueSessionMessage(sid, payload){
  rec('queueSessionMessage', sid, payload && payload.text);
  (QUEUE[sid] = QUEUE[sid] || []).push(payload);
}
async function _trySteer(text){ rec('_trySteer', text); }
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
  starts.push({session_id: body.session_id, message: body.message, goal: body.goal_continuation_id});
  const step = plan.shift() || {};
  if(step.typeDuring !== undefined){
    input.value = step.typeDuring;
    if(step.switchTo) DRAFTS[S.session.session_id] = {text: step.typeDuring, files: [], contId: ''};
  }
  if(step.switchTo) openChat(step.switchTo);
  // The user runs /use in the chat they opened while this start is held.
  if(step.useDuring) _forcedSkillDirectivePending = {sessionId: step.useDuring, promise: Promise.resolve(EDITOR_SKILL)};
  // loadSession() of another chat has started; S.session is still this one.
  if(step.loadingTo) _loadingSessionId = step.loadingTo;
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
  if(r === '404'){
    const err = new Error('Session not found');
    err.status = 404;
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
    } else if(step.stage){
      S.pendingFiles = step.stage;
    } else if(step.busy){
      // The chat's earlier run is still streaming.
      S.busy = true;
      S.activeStreamId = step.busy;
    } else if(step.snap){
      SNAPS[step.snap] = plain({composer: input.value, pending: S.pendingFiles, dataset: input.dataset,
                                session: S.session && S.session.session_id, loading: _loadingSessionId});
    } else if(step.drain){
      // setBusy(false) drains the chat's next queued message into send().
      S.busy = false;
      S.activeStreamId = null;
      const next = (QUEUE[S.session.session_id] || []).shift();
      if(next){
        input.value = next.text || '';
        await send({goalContinuationId: next.goal_continuation_id || ''});
        await tick();
      }
    } else if(step.send){
      plan = [step];
      await send(step.cont ? {goalContinuationId: step.cont} : undefined);
      await tick();
      // The started turn (if any) finishes before the user's next action.
      S.busy = false;
      S.activeStreamId = null;
      for(const sid of Object.keys(INFLIGHT)) delete INFLIGHT[sid];
    }
  }
  console.log(JSON.stringify({calls, starts, drafts: DRAFTS, composer: input.value, snaps: SNAPS,
                              session: S.session && S.session.session_id}));
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""


def _run(*steps):
    script = (
        HARNESS.replace("__STEPS__", json.dumps(list(steps)))
        .replace("__EDITOR__", json.dumps(EDITOR))
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


def _send(start="ok", switch_to=None, loading_to=None, cont=None, use_during=None, type_during=None):
    step = {"send": True, "start": start}
    if type_during is not None:
        step["typeDuring"] = type_during
    if use_during:
        step["useDuring"] = use_during
    if switch_to:
        step["switchTo"] = switch_to
    if loading_to:
        step["loadingTo"] = loading_to
    if cont:
        step["cont"] = cont
    return step


def _open(sid):
    return {"open": sid}


def _fail_off_screen(start="409-conflict"):
    """A sends 'hello' with /use writer; the user opens B while start is held; start fails."""
    return [_use(WRITER), _type("hello"), _send(start=start, switch_to="B")]


def _skill_count(message, block=WRITER_BLOCK):
    return message.count(block)


def _calls(result, name):
    return [c["args"] for c in result["calls"] if c["name"] == name]


# ── off-screen 409 "active stream": master's queue-and-reload, skill included ─


def test_off_screen_conflict_queues_the_whole_message_with_its_skill_as_master():
    result = _run(*_fail_off_screen())
    (first,) = result["starts"]
    assert first["session_id"] == "A"
    assert _skill_count(first["message"]) == 1
    assert first["message"].endswith("hello")
    # One queued copy for A: exactly the message that was posted, skill and text.
    assert _calls(result, "queueSessionMessage") == [["A", first["message"]]]
    # A is reloaded (the user is taken back to it) and no draft is restored.
    assert _calls(result, "loadSession") == [["A"]]
    assert result["session"] == "A"
    assert _calls(result, "_saveComposerDraftNow") == []
    assert result["composer"] == ""


def test_off_screen_conflict_queued_copy_drains_with_the_skill_once():
    result = _run(*_fail_off_screen(), {"drain": True})
    first, drained = result["starts"]
    assert drained["session_id"] == "A"
    assert drained["message"] == first["message"]
    assert _skill_count(drained["message"]) == 1


@pytest.mark.parametrize("skill", [None, WRITER], ids=["plain", "with-skill"])
def test_off_screen_conflict_keeps_a_newer_draft_saved_in_the_issuing_chat(skill):
    # Send "first" in A, type "follow-up" while the start is held and open B
    # (the switch saves "follow-up" as A's draft). A is still running, so the
    # start is refused: "first" is queued for A and A's draft stays.
    steps = [_use(skill)] if skill else []
    steps += [_type("first"), _send(start="409-conflict", switch_to="B", type_during="follow-up")]
    result = _run(*steps)
    (first,) = result["starts"]
    assert result["drafts"]["A"]["text"] == "follow-up"
    assert _calls(result, "queueSessionMessage") == [["A", first["message"]]]
    if skill:
        assert _skill_count(first["message"]) == 1 and first["message"].endswith("first")
    else:
        assert first["message"] == "first"
    assert _calls(result, "loadSession") == [["A"]]
    assert result["composer"] == "follow-up"


# ── the other start failures drop the skill, as on master ───────────────────


@pytest.mark.parametrize("start", ["500", "409-rotated"])
def test_off_screen_failure_restores_the_text_without_the_skill_as_master(start):
    # A rotation whose continuation is off screen keeps the text as A's own
    # draft (as on master), so the retry is from A in both cases.
    target = "A"
    result = _run(*_fail_off_screen(start), _open(target), _send())
    _first, retry = result["starts"]
    assert retry["session_id"] == target
    assert retry["message"] == "hello"


def test_off_screen_404_keeps_no_draft_and_no_skill_as_master():
    result = _run(*_fail_off_screen("404"), _open("A"), _type("next"), _send())
    _first, later = result["starts"]
    assert _calls(result, "_saveComposerDraftNow") == []
    assert later["message"] == "next"


def test_on_screen_start_error_restores_the_text_without_the_skill_as_master():
    result = _run(_use(WRITER), _type("hello"), _send(start="500"), _send())
    _first, retry = result["starts"]
    assert result["session"] == "A"
    assert retry["message"] == "hello"


def test_control_on_screen_conflict_still_queues_the_skill_message():
    result = _run(_use(WRITER), _type("hello"), _send(start="409-conflict"))
    queued = _calls(result, "queueSessionMessage")
    assert len(queued) == 1 and queued[0][0] == "A"
    assert _skill_count(queued[0][1]) == 1


# ── the on-screen restore is unchanged ──────────────────────────────────────


def test_control_on_screen_failure_still_restores_the_composer_and_token():
    result = _run(_type("hello"), _send(start="500", cont="goal-1"), {"snap": "after"})
    after = result["snaps"]["after"]
    assert after["composer"] == "hello"
    assert after["dataset"] == {"goalContinuationId": "goal-1", "goalContinuationText": "hello"}
    assert result["drafts"]["A"]["contId"] == "goal-1"


# ── steer mode keeps master's routing for everything else ───────────────────


def test_control_steer_mode_still_steers_with_an_ordinary_pending_use():
    # A /use typed while the chat runs waits for the next started turn (master).
    result = _run(_use(WRITER), _type("hello"), {"busy": "stream-a"}, _send())
    assert _calls(result, "_trySteer") == [["hello"]]
    assert _calls(result, "queueSessionMessage") == []
