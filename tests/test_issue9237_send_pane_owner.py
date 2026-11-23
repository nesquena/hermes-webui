"""send() keeps a pending turn on the chat that issued it (#9237).

send() captures the issuing session id and then awaits attachment uploads, a
forced-skill directive and POST /api/chat/start. If the user switches to
another chat (or New Chat) during one of those awaits, every later write must
stay with the issuing chat: the chat now on screen must not receive the
optimistic turn, the stream id, the effective model, the live SSE attach or
the start-error handling. One start error keeps master's behaviour on or off
screen: a chat that is still running gets the message queued and is reloaded.

These tests run the real send() from static/messages.js in node with
recording stubs and switch the visible chat inside the awaited stub, the same
way the browser interleaves a click during the await.
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
RECOVER_COMPRESSED_SRC = _cut(
    "async function _recoverCompressedSend(", "\nfunction _restoreComposerDraftAfterFailedSend("
)
DEFAULT_TITLE_SRC = _cut(
    "function _sessionTitleLooksDefaultOrProvisional(", "\nfunction _firstUserMessageTitleCandidate("
)

# The real t() and the real en send_offscreen_* strings, so the toasts below
# are asserted as rendered (the whole en block is too long for `node -e`).
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
_EN_START = I18N_JS.index("  en: {")
_EN_BLOCK = I18N_JS[_EN_START:I18N_JS.index("\n  it: {", _EN_START)]
EN_LOCALE_SRC = (
    "const LOCALES = {en: {\n"
    + "\n".join(line for line in _EN_BLOCK.splitlines() if line.startswith("    send_offscreen_"))
    + "\n}};"
)
_T_START = I18N_JS.index("function t(key, ...args) {")
T_SRC = I18N_JS[_T_START:I18N_JS.index("\n}\n", _T_START)] + "\n}"


HARNESS = r"""
const SC = __SCENARIO__;
const calls = [];
let phase = 'before';
const plain = value => JSON.parse(JSON.stringify(value, (k, v) => v === undefined ? null : v));
const rec = (name, ...args) => calls.push({phase, name, args: plain(args)});
const input = {value: SC.text};
const A_SESSION = {session_id:'A', workspace:'/a', profile:'pa', model:'model-a', model_provider:'prov-a',
                   title:'Chat A'};
if('ownerTitle' in SC){
  if(SC.ownerTitle === null) delete A_SESSION.title;
  else A_SESSION.title = SC.ownerTitle;
}
if(SC.ownerCounts) Object.assign(A_SESSION, SC.ownerCounts);
const B_SESSION = {session_id:'B', workspace:'/b', profile:'pb', model:'model-b', model_provider:'prov-b',
                   title:'Chat B'};
const C_SESSION = {session_id:'C', workspace:'/c', profile:'pc', model:'model-c', model_provider:'prov-c',
                   title:'Chat C'};
const S = {
  session: SC.startWithoutSession ? null : A_SESSION,
  messages: SC.startWithoutSession ? [] : ('ownerMessages' in SC ? SC.ownerMessages : [{role:'user', content:'a-row'}]),
  pendingFiles: SC.files ? [{name:'f.txt'}] : [],
  toolCalls: [], activeProfile: null, busy: false, activeStreamId: null,
};
const INFLIGHT = {};
const LIVE = {};
const _pendingSelections = [];
// A chat switch that was already loading when send() began.
let _loadingSessionId = SC.loadingAtStart || null;
let _sendInProgress = false;
let _sendInProgressSid = null;
let _queueDrainSid = null;
let _approvalSessionId = null;
let _clarifySessionId = null;
let _projectPickerOpenEpoch = 0;
const COMMANDS = [];
// A built-in command whose handler opts out (returns false), like /reasoning <level>.
if(SC.echoCommand) COMMANDS.push({name:SC.echoCommand, fn(args){ rec('command.fn', args); return false; }});
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set();
const localStorage = {
  setItem(k, v){ rec('localStorage.setItem', k, v); },
  removeItem(k){ rec('localStorage.removeItem', k); },
};
const history = {replaceState(...a){ rec('history.replaceState', ...a); }};
const window = {_defaultModel:'model-a', _activeProvider:'prov-a', location:{pathname:'/session/A', search:''}};
const document = {querySelector(){ return null; }};
const $ = id => id === 'msg' ? input : null;

function doSwitch(){
  phase = 'after';
  if(SC.switchKind === 'cleared'){
    // The issuing chat was left for an empty pane with no session yet.
    input.value = '';
    S.session = null;
    S.messages = [];
    return;
  }
  if(SC.switchKind === 'loading'){
    // loadSession(B) has started: S.session still points at A until B's
    // metadata arrives.
    _loadingSessionId = 'B';
    return;
  }
  if(SC.switchKind === 'continuation'){
    // The user opened A's compression continuation A2 and staged a new file
    // there with an empty composer before A's start answered.
    input.value = SC.a2Text || '';
    S.session = {session_id:'A2', workspace:'/a', profile:'pa', model:'model-a', model_provider:'prov-a',
                 title:'Chat A'};
    S.messages = [];
    S.pendingFiles = SC.a2Staged || [{name:'newer.txt'}];
    return;
  }
  if(SC.switchKind === 'loadingBack'){
    // The user clicked B, then A again before B loaded: A is reloading.
    _loadingSessionId = 'A';
    return;
  }
  // The switch saved A's composer as A's draft and loaded B's.
  input.value = 'b-draft';
  S.session = B_SESSION;
  if(SC.bStaged) S.pendingFiles = [{name:'b.txt'}];
  S.messages = [{role:'user', content:'b-row'}];
  S.activeStreamId = null;
  S.busy = false;
  if(SC.switchKind === 'chain'){
    // ...and then the user moved on again, from B to C.
    input.value = 'c-draft';
    S.session = C_SESSION;
    S.messages = [{role:'user', content:'c-row'}];
  }
}
function maybeSwitch(at){ if(SC.switchAt === at) doSwitch(); }

function _normalizeGoalContinuationId(id){ return String(id||'').trim(); }
function _takeRestoredDraftGoalContinuationId(){ return ''; }
function _composerTextWithPendingSelections(){ return input.value; }
function _chatPayloadModelState(){
  return {model:S.session && S.session.model || '', model_provider:S.session && S.session.model_provider || null};
}
function _flushSelectionBlocksToComposer(){}
function shouldInterceptCompressionRecoveryContinuation(){ return false; }
function _dismissHandoffHint(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(text){ const m = /^\/(\S+)\s*(.*)$/.exec(text); return m ? {name:m[1], args:m[2]} : null; }
async function getAgentCommandMetadata(name){ rec('getAgentCommandMetadata', name); maybeSwitch('slash'); return null; }
// SC.bundle = {name, message}: a skill-bundle slash command.
async function getBundleCommandMetadata(name){
  if(!SC.bundle || name !== SC.bundle.name) return null;
  rec('getBundleCommandMetadata', name);
  return {name:SC.bundle.name};
}
// Like commands.js: the resolver opens a chat itself when none is open and
// resolves the command for the chat on screen.
async function resolveBundleCommand(text){
  if(!S.session) await newSession();
  rec('resolveBundleCommand', text, S.session && S.session.session_id);
  return {message:SC.bundle.message};
}
function hideCmdDropdown(){}
function renderTray(){}
function autoResize(){}
function _clearComposerDraft(sid, text){ rec('_clearComposerDraft', sid, text); return Promise.resolve(); }
function setComposerStatus(text){ rec('setComposerStatus', text); }
function clearLiveToolCards(){ rec('clearLiveToolCards'); }
function renderMessages(){ rec('renderMessages'); }
function setBusy(value){ rec('setBusy', value); S.busy = value; }
function ensureLiveWorklogShell(){ rec('ensureLiveWorklogShell'); }
function appendThinking(){ rec('appendThinking'); }
function _runOptionalPreStartUiStep(_label, fn){ if(fn) fn(); }
function _runOptionalPostStartUiStep(_label, fn){ if(fn) fn(); }
function upsertActiveSessionForLocalTurn(opts){ rec('upsertActiveSessionForLocalTurn', opts && opts.title); }
function renderSessionListFromCache(){ rec('renderSessionListFromCache'); }
function startApprovalPolling(sid){ rec('startApprovalPolling', sid); }
function startClarifyPolling(sid){ rec('startClarifyPolling', sid); }
function _fetchYoloState(sid){ rec('_fetchYoloState', sid); }
function updateSendBtn(){ rec('updateSendBtn'); }
function applySessionTitleUpdate(sid, title){ rec('applySessionTitleUpdate', sid, title); }
function _readPendingSessionModel(){ return null; }
function _clearPendingSessionModel(){}
function _writePersistedModelState(model){ rec('_writePersistedModelState', model); }
function _applyModelToDropdown(model){ rec('_applyModelToDropdown', model); }
function syncTopbar(){ rec('syncTopbar'); }
function syncModelChip(){}
function showLiveRunStatus(sid){ rec('showLiveRunStatus', sid); }
function stopApprovalPolling(){ rec('stopApprovalPolling'); }
function stopClarifyPolling(){ rec('stopClarifyPolling'); }
function hideApprovalCard(){ rec('hideApprovalCard'); }
function hideClarifyCard(){ rec('hideClarifyCard'); }
function removeThinking(){ rec('removeThinking'); }
function clearOptimisticSessionStreaming(sid){ rec('clearOptimisticSessionStreaming', sid); }
function renderSessionList(){ rec('renderSessionList'); }
function markInflight(sid, streamId){ rec('markInflight', sid, streamId); }
function saveInflightState(sid, state){
  rec('saveInflightState', sid, state && state.streamId, (state && state.messages || []).map(m => m.content));
}
function clearInflightState(sid){ rec('clearInflightState', sid); }
function queueSessionMessage(sid, payload){ rec('queueSessionMessage', sid, payload && payload.text); }
function _clearComposerAfterQueuedSelectionSend(){}
function updateQueueBadge(sid){ rec('updateQueueBadge', sid); }
function showToast(msg, ms, type){
  rec('showToast', String(msg), ms === undefined ? '<undefined>' : ms, type === undefined ? '<undefined>' : type);
}
function _hydrateTodosFromSession(){ rec('_hydrateTodosFromSession'); }
function _syncProviderQuotaForActiveContext(){}
function _restoreComposerDraftAfterFailedSend(text, files, sid){
  rec('_restoreComposerDraftAfterFailedSend', text, sid);
  rec('restore.files', sid, (files || []).map(f => f && f.name));
}
function attachLiveStream(sid, streamId){ rec('attachLiveStream', sid, streamId); LIVE[sid] = streamId; }
async function loadSession(sid){ rec('loadSession', sid); }
async function newSession(){
  rec('newSession');
  S.session = {session_id:'N', workspace:'/n', profile:'pn', model:'model-n', model_provider:'prov-n',
               title:'Untitled'};
  S.messages = [];
}
async function uploadPendingFiles(opts){
  rec('uploadPendingFiles', opts && opts.sessionId);
  maybeSwitch('upload');
  return (opts && opts.files || []).map(f => ({name:f.name, path:'/ws/' + f.name}));
}
let _forcedSkillDirectivePending = SC.directive ? {
  sessionId:'A',
  promise:{then(resolve){
    maybeSwitch('directive');
    resolve({directive:'/use writer', name:'writer', content:'guide'});
  }},
} : null;
let startBody = null;
async function api(url, opts){
  if(url === '/api/commands/moa/resolve'){ rec('api.moaResolve'); return {}; }
  if(url !== '/api/chat/start') throw new Error('unexpected api ' + url);
  startBody = JSON.parse(opts.body);
  rec('api.start', startBody.session_id);
  maybeSwitch('start');
  const r = SC.start || 'ok';
  if(r === 'ok') return {stream_id:'stream-a', title:'A title', effective_model:'model-eff',
                         effective_model_provider:'prov-eff', pending_started_at:123};
  const message = SC.startMessage
    || {'409-conflict':'Session already has an active stream', '404':'Session not found'}[r] || 'boom';
  const err = new Error(message);
  if(r === '404') err.status = 404;
  if(r === '409-conflict') err.status = 409;
  if(r === '500') err.status = 500;
  if(r === '409-rotated'){
    err.status = 409;
    err.body = JSON.stringify({code:'session_rotated', continuation_session_id:'A2'});
  }
  throw err;
}

__EN_LOCALE__
const _locale = LOCALES.en;
__T__
__DEFAULT_TITLE__
__CURRENT_PANE__
__RECOVER_COMPRESSED__
__SEND__

(async () => {
  await send();
  console.log(JSON.stringify({
    calls, startBody, composer: input.value, inflight: INFLIGHT, live: LIVE,
    session: S.session, messages: S.messages, activeStreamId: S.activeStreamId,
    busy: S.busy, sendInProgress: _sendInProgress,
  }));
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""


def _run(**scenario):
    scenario.setdefault("text", "hello")
    script = (
        HARNESS.replace("__SCENARIO__", json.dumps(scenario))
        .replace("__EN_LOCALE__", EN_LOCALE_SRC)
        .replace("__T__", T_SRC)
        .replace("__DEFAULT_TITLE__", DEFAULT_TITLE_SRC)
        .replace("__CURRENT_PANE__", CURRENT_PANE_SRC)
        .replace("__RECOVER_COMPRESSED__", RECOVER_COMPRESSED_SRC)
        .replace("__SEND__", SEND_SRC)
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _after(result, *names):
    return [c for c in result["calls"] if c["phase"] == "after" and (not names or c["name"] in names)]


def _named(result, name):
    return [c["args"] for c in result["calls"] if c["name"] == name]


PANE_WRITES = (
    "setBusy",
    "renderMessages",
    "clearLiveToolCards",
    "ensureLiveWorklogShell",
    "appendThinking",
    "upsertActiveSessionForLocalTurn",
    "renderSessionListFromCache",
    "startApprovalPolling",
    "startClarifyPolling",
    "_fetchYoloState",
    "showLiveRunStatus",
    "syncTopbar",
    "_applyModelToDropdown",
    "_writePersistedModelState",
    "localStorage.setItem",
    "localStorage.removeItem",
    "history.replaceState",
    "_hydrateTodosFromSession",
    "removeThinking",
    "hideApprovalCard",
    "hideClarifyCard",
    "stopApprovalPolling",
    "stopClarifyPolling",
)


def _assert_b_untouched(result):
    assert result["session"]["session_id"] == "B"
    assert result["session"]["model"] == "model-b"
    assert "active_stream_id" not in result["session"]
    assert "pending_started_at" not in result["session"]
    assert result["messages"] == [{"role": "user", "content": "b-row"}]
    assert result["activeStreamId"] is None
    assert result["busy"] is False
    assert result["composer"] == "b-draft"
    assert _after(result, *PANE_WRITES) == []
    # Clearing A's own "Uploading…" status is fine; writing a status is not.
    assert [c for c in _after(result, "setComposerStatus") if c["args"] != [""]] == []
    assert "B" not in result["inflight"]


# ── the optimistic block after the upload / directive awaits ────────────────


@pytest.mark.parametrize("switch_at", ["upload", "directive"])
def test_switch_before_start_keeps_the_optimistic_turn_off_the_new_pane(switch_at):
    result = _run(files=True, directive=True, switchAt=switch_at)
    _assert_b_untouched(result)
    # The turn still belongs to A, with A's transcript and the new user row.
    a = result["inflight"]["A"]
    assert [m["content"] for m in a["messages"]] == ["a-row", "hello"]
    assert a["messages"][-1]["attachments"] == ["f.txt"]
    saved = _named(result, "saveInflightState")
    assert saved and {args[0] for args in saved} == {"A"}
    # Persisted before /api/chat/start so a reload during the request keeps it.
    assert saved[0] == ["A", None, ["a-row", "hello"]]
    start_index = next(i for i, c in enumerate(result["calls"]) if c["name"] == "api.start")
    assert any(c["name"] == "saveInflightState" for c in result["calls"][:start_index])


@pytest.mark.parametrize("switch_at", ["upload", "directive"])
def test_switch_before_start_posts_the_owner_model_workspace_and_profile(switch_at):
    result = _run(files=True, directive=True, switchAt=switch_at)
    body = result["startBody"]
    assert body["session_id"] == "A"
    assert body["model"] == "model-a"
    assert body["model_provider"] == "prov-a"
    assert body["workspace"] == "/a"
    assert body["profile"] == "pa"
    assert body["attachments"] == [{"name": "f.txt", "path": "/ws/f.txt"}]
    assert body["message"].startswith("/use writer")


# ── /api/chat/start succeeded after the switch ──────────────────────────────


def test_switch_during_start_success_keeps_stream_and_model_off_the_new_pane():
    result = _run(switchAt="start")
    _assert_b_untouched(result)
    # A's bookkeeping is still recorded by id.
    assert _named(result, "markInflight") == [["A", "stream-a"]]
    assert ["A", "A title"] in _named(result, "applySessionTitleUpdate")
    assert [args[:2] for args in _named(result, "saveInflightState")][-1] == ["A", "stream-a"]
    assert _after(result, "renderSessionList")


def test_switch_during_start_success_marks_the_owner_for_reattach_instead_of_attaching():
    result = _run(switchAt="start")
    assert _named(result, "attachLiveStream") == []
    assert result["inflight"]["A"]["streamId"] == "stream-a"
    assert result["inflight"]["A"]["reattach"] is True


def test_switch_still_loading_counts_as_off_screen_for_the_started_turn():
    # loadSession(B) is in flight: S.session is still A, but A is leaving.
    result = _run(switchAt="start", switchKind="loading")
    assert result["session"]["session_id"] == "A"
    assert result["activeStreamId"] is None
    assert result["session"]["model"] == "model-a"
    assert "active_stream_id" not in result["session"]
    assert _named(result, "attachLiveStream") == []
    assert _after(result, "ensureLiveWorklogShell", "upsertActiveSessionForLocalTurn", "showLiveRunStatus") == []
    assert result["inflight"]["A"]["reattach"] is True


# ── /api/chat/start failed after the switch ─────────────────────────────────


def _assert_owner_settled(result):
    assert "A" not in result["inflight"]
    assert ["A"] in _named(result, "clearInflightState")
    assert ["A"] in _named(result, "clearOptimisticSessionStreaming")
    assert _named(result, "queueSessionMessage") == []
    assert _named(result, "loadSession") == []
    assert _named(result, "attachLiveStream") == []
    assert _named(result, "showToast")


def test_start_404_after_switch_does_not_clear_or_navigate_the_new_pane():
    result = _run(switchAt="start", start="404")
    _assert_b_untouched(result)
    _assert_owner_settled(result)
    # A was deleted server-side: there is no chat to keep a draft for.
    assert _named(result, "_restoreComposerDraftAfterFailedSend") == []


MASTER_QUEUED = "Current session is still running. Reconnected and queued your message."


def _assert_master_queue_and_return(result):
    # Master's "already has an active stream" branch, on screen or not: A's
    # optimistic state is dropped, the whole message as posted (attachments
    # and forced-skill block included) is queued for A, A is reloaded and
    # nothing is restored as a draft, so a newer draft saved for A stays.
    assert len(_named(result, "api.start")) == 1
    assert "A" not in result["inflight"]
    assert ["A"] in _named(result, "clearInflightState")
    assert _named(result, "queueSessionMessage") == [["A", result["startBody"]["message"]]]
    assert _named(result, "updateQueueBadge") == [["A"]]
    assert _named(result, "loadSession") == [["A"]]
    assert _named(result, "_restoreComposerDraftAfterFailedSend") == []
    assert _named(result, "showToast") == [[MASTER_QUEUED, 2600, "<undefined>"]]
    assert _named(result, "attachLiveStream") == []
    assert not [m for m in result["messages"] if str(m.get("content", "")).startswith("**Error:**")]
    assert _named(result, "setComposerStatus")[-1] == [""]


@pytest.mark.parametrize(
    "scenario",
    [
        {"switchAt": "start"},
        {"switchAt": "start", "switchKind": "chain"},
        {"switchAt": "start", "switchKind": "loading"},
        {"switchAt": "start", "switchKind": "cleared"},
        {"switchAt": "upload", "files": True},
        {"switchAt": "directive", "directive": True, "files": True},
        {"switchAt": "start", "ownerTitle": "Untitled", "ownerMessages": []},
    ],
    ids=["start", "b-then-c", "still-loading", "empty-pane", "upload", "forced-skill", "never-started"],
)
def test_start_conflict_after_switch_queues_for_the_owner_and_reloads_it_as_master(scenario):
    result = _run(start="409-conflict", **scenario)
    _assert_master_queue_and_return(result)
    if scenario.get("directive"):
        queued = _named(result, "queueSessionMessage")[0][1]
        assert queued.startswith("/use writer\n\n[FORCED SKILL CONTEXT: writer]\nguide\n[/FORCED SKILL CONTEXT]")
        assert queued.endswith("hello\n\n[Attached files: /ws/f.txt]")


def test_start_error_after_switch_does_not_add_an_error_bubble_to_the_new_pane():
    result = _run(switchAt="start", start="500")
    _assert_b_untouched(result)
    _assert_owner_settled(result)
    assert _named(result, "_restoreComposerDraftAfterFailedSend") == [["hello", "A"]]


def test_start_session_rotated_after_switch_keeps_the_draft_on_the_issuing_chat():
    """The continuation A2 is off screen: the text stays A's own draft (as on master); A2's saved draft is not
    replaced."""
    result = _run(switchAt="start", start="409-rotated")
    _assert_b_untouched(result)
    _assert_owner_settled(result)
    assert _named(result, "_restoreComposerDraftAfterFailedSend") == [["hello", "A"]]


# ── controls: no switch keeps today's behaviour ────────────────────────────


def test_control_no_switch_start_success_updates_the_pane_and_attaches():
    result = _run(files=True)
    assert result["session"]["session_id"] == "A"
    assert result["activeStreamId"] == "stream-a"
    assert result["session"]["model"] == "model-eff"
    assert result["session"]["active_stream_id"] == "stream-a"
    assert [m["content"] for m in result["messages"]] == ["a-row", "hello"]
    assert ["A", "stream-a"] in _named(result, "attachLiveStream")
    assert ["A"] in _named(result, "startApprovalPolling")
    assert ["hermes-webui-model", "model-eff"] in _named(result, "localStorage.setItem")
    assert result["startBody"]["model"] == "model-a"
    assert result["startBody"]["workspace"] == "/a"
    assert "reattach" not in result["inflight"]["A"]


def test_control_no_switch_start_404_resets_the_deleted_chat():
    result = _run(start="404")
    assert result["session"] is None
    assert _named(result, "history.replaceState")
    assert ["hermes-webui-session"] in _named(result, "localStorage.removeItem")


def test_control_no_switch_start_conflict_queues_and_reloads_the_owner():
    result = _run(start="409-conflict")
    assert _named(result, "queueSessionMessage") == [["A", "hello"]]
    assert _named(result, "loadSession") == [["A"]]
    _assert_master_queue_and_return(result)


def test_control_no_switch_start_error_adds_the_error_bubble():
    result = _run(start="500")
    assert result["messages"][-1] == {"role": "assistant", "content": "**Error:** boom"}
    assert _named(result, "_restoreComposerDraftAfterFailedSend") == [["hello", "A"]]


def test_control_no_switch_session_rotated_moves_to_the_continuation():
    result = _run(start="409-rotated")
    assert _named(result, "loadSession") == [["A2"]]


def test_control_slash_text_without_a_switch_is_sent_to_the_owner():
    result = _run(text="/custom do it")
    assert result["startBody"]["session_id"] == "A"
    assert result["startBody"]["message"] == "/custom do it"


def test_control_send_without_a_session_creates_one_and_sends_there():
    result = _run(text="/custom do it", startWithoutSession=True)
    assert _named(result, "newSession") == [[]]
    assert result["startBody"]["session_id"] == "N"
    assert result["activeStreamId"] == "stream-a"


# ── the off-screen notices name the chat and stay visible (UX review) ───────
#
# Every case asserts the rendered en string, the toast type and the duration
# argument: an error toast passes null so showToast gives it the 20 s error
# default (hover pause, Dismiss); a warning passes an explicit 8000.

SAVED = "Your text is saved there as a draft."


def _toasts(result):
    return _named(result, "showToast")


def _failed(label, reason):
    return f"Message not sent in {label}: {reason}. {SAVED}"


def _failed_rotated(label, reason):
    return f"Message not sent in {label}: {reason}."


def _deleted(label):
    return f"Message not sent: {label} no longer exists, so your text was not saved."


def test_start_error_after_switch_names_the_chat_and_the_reason():
    result = _run(switchAt="start", start="500")
    assert _toasts(result) == [[_failed("\u201cChat A\u201d", "boom"), None, "error"]]


def test_start_error_reason_loses_its_trailing_full_stops():
    result = _run(switchAt="start", start="500", startMessage="Provider quota exceeded.")
    assert _toasts(result) == [[_failed("\u201cChat A\u201d", "Provider quota exceeded"), None, "error"]]
    result = _run(switchAt="start", start="500", startMessage="Upstream refused...")
    assert _toasts(result) == [[_failed("\u201cChat A\u201d", "Upstream refused"), None, "error"]]


def test_start_session_rotated_after_switch_does_not_claim_where_the_text_was_saved():
    # The server keeps a compressed chat's draft with its continuation, which
    # may already hold a draft of its own: the notice makes no "saved" claim.
    result = _run(switchAt="start", start="409-rotated")
    assert _toasts(result) == [[_failed_rotated("\u201cChat A\u201d", "boom"), None, "error"]]
    assert SAVED not in _toasts(result)[0][0]


def test_start_404_after_switch_says_the_text_was_not_saved():
    result = _run(switchAt="start", start="404")
    assert _toasts(result) == [[_deleted("\u201cChat A\u201d"), None, "error"]]


@pytest.mark.parametrize(
    "start, expected",
    [
        ("500", [_failed("\u201cChat A\u201d", "boom"), None, "error"]),
        ("404", [_deleted("\u201cChat A\u201d"), None, "error"]),
    ],
)
def test_start_failure_after_two_switches_names_the_issuing_chat_not_the_one_on_screen(start, expected):
    result = _run(switchAt="start", switchKind="chain", start=start)
    assert result["session"]["session_id"] == "C"
    assert result["composer"] == "c-draft"
    assert _toasts(result) == [expected]


LONG_TITLE = "Planning the migration of billing exports to the new warehouse"
FORTY = "Forty characters exactly in this title!!"
EMOJI_TITLE = "x" * 39 + "\U0001f642" + " and more after the emoji"
TRIM_TITLE = "Status of the release checklist for the mobile app"


@pytest.mark.parametrize(
    "owner_title, label",
    [
        (LONG_TITLE, "\u201cPlanning the migration of billing export\u2026\u201d"),
        (FORTY, "\u201cForty characters exactly in this title!!\u201d"),
        (EMOJI_TITLE, "\u201c" + "x" * 39 + "\U0001f642\u2026\u201d"),
        # A title made from a first message can carry its line breaks.
        ("  Weekly\nsync   notes ", "\u201cWeekly sync notes\u201d"),
        # A cut that lands on a space does not leave "word \u2026".
        (TRIM_TITLE, "\u201cStatus of the release checklist for the\u2026\u201d"),
        # A has a message, so a default title only swaps in the generic phrase.
        ("", "an untitled chat"),
        (None, "an untitled chat"),
        ("Untitled", "an untitled chat"),
        ("New Chat", "an untitled chat"),
    ],
)
def test_offscreen_notices_quote_cut_or_replace_the_owner_title(owner_title, label):
    assert len(FORTY) == 40
    assert TRIM_TITLE[39] == " "
    result = _run(switchAt="start", start="500", ownerTitle=owner_title)
    assert _toasts(result) == [[_failed(label, "boom"), None, "error"]]
    result = _run(switchAt="start", start="404", ownerTitle=owner_title)
    assert _toasts(result) == [[_deleted(label), None, "error"]]


# ── UX review round 2: "an untitled chat", and a chat that never started ────
#
# A chat with a default title and no messages before this send is not in the
# chat list (the server and the sidebar both hide it), so a notice cannot send
# the user there for a draft. It says so as an error, like the 404 notice.

UNTITLED = "an untitled chat"
NEW_CHAT = (
    "Message not sent: the new chat you left never started, so it is not in your chat list. "
    "Please type it again."
)
DEFAULT_TITLES = ["Untitled", "New Chat", "", None]
SLASH = {"text": "/custom do it"}


@pytest.mark.parametrize(
    "scenario, expected",
    [
        ({"switchAt": "start", "start": "500"}, [_failed(UNTITLED, "boom"), None, "error"]),
        ({"switchAt": "start", "start": "404"}, [_deleted(UNTITLED), None, "error"]),
    ],
)
def test_an_untitled_chat_with_messages_reads_an_untitled_chat_in_every_notice(scenario, expected):
    result = _run(ownerTitle="Untitled", **scenario)
    assert _toasts(result) == [expected]


def test_the_untitled_phrase_reads_as_a_sentence():
    result = _run(ownerTitle="Untitled", switchAt="start", start="404")
    assert _toasts(result)[0][0].startswith("Message not sent: an untitled chat no longer exists,")


@pytest.mark.parametrize("start", ["500", "404", "409-rotated"])
@pytest.mark.parametrize("owner_title", DEFAULT_TITLES)
def test_a_never_started_chat_start_failure_says_it_is_not_in_the_list(owner_title, start):
    # A was on screen when its user row was pushed, then the user left during
    # /api/chat/start: "no messages" means before this send, not at the toast.
    result = _run(switchAt="start", start=start, ownerTitle=owner_title, ownerMessages=[])
    _assert_b_untouched(result)
    _assert_owner_settled(result)
    assert _toasts(result) == [[NEW_CHAT, None, "error"]]
    # What is persisted does not change.
    restored = {"404": []}.get(start, [["hello", "A"]])
    assert _named(result, "_restoreComposerDraftAfterFailedSend") == restored


@pytest.mark.parametrize("counts", [{"message_count": 4}, {"visible_message_count": 4}])
@pytest.mark.parametrize(
    "scenario, expected",
    [
        ({"switchAt": "start", "start": "500"}, [_failed(UNTITLED, "boom"), None, "error"]),
        ({"switchAt": "start", "start": "404"}, [_deleted(UNTITLED), None, "error"]),
    ],
)
def test_an_untitled_chat_whose_rows_are_still_loading_is_not_called_new(scenario, expected, counts):
    # Switching to a listed Untitled chat sets S.session before its rows load,
    # so S.messages can be empty while the session already counts messages.
    result = _run(ownerTitle="Untitled", ownerMessages=[], ownerCounts=counts, **scenario)
    assert _toasts(result) == [expected]


@pytest.mark.parametrize("counts", [{"message_count": 0}, {"visible_message_count": 0}, {}])
def test_control_zero_counts_still_mean_a_new_chat(counts):
    result = _run(switchAt="start", start="500", ownerTitle="Untitled", ownerMessages=[], ownerCounts=counts)
    assert _toasts(result) == [[NEW_CHAT, None, "error"]]


@pytest.mark.parametrize("start", ["500", "404"])
def test_a_never_started_chat_left_before_the_start_gets_the_new_chat_notice(start):
    result = _run(files=True, switchAt="upload", start=start, ownerTitle="Untitled", ownerMessages=[])
    _assert_b_untouched(result)
    assert _toasts(result) == [[NEW_CHAT, None, "error"]]


@pytest.mark.parametrize(
    "scenario, expected",
    [
        ({"switchAt": "start", "start": "500"}, [_failed("\u201cChat A\u201d", "boom"), None, "error"]),
        ({"switchAt": "start", "start": "404"}, [_deleted("\u201cChat A\u201d"), None, "error"]),
    ],
)
def test_control_a_titled_chat_without_messages_keeps_the_titled_notices(scenario, expected):
    result = _run(ownerMessages=[], **scenario)
    assert _toasts(result) == [expected]


@pytest.mark.parametrize(
    "scenario",
    [
        {"switchAt": "start", "start": "500"},
        {"switchAt": "start", "start": "404"},
    ],
)
def test_a_never_started_chat_left_for_b_then_c_gets_the_new_chat_notice(scenario):
    result = _run(switchKind="chain", ownerTitle="Untitled", ownerMessages=[], **scenario)
    assert result["session"]["session_id"] == "C"
    assert result["composer"] == "c-draft"
    assert _toasts(result) == [[NEW_CHAT, None, "error"]]


# ── slash paths without a switch: sent exactly as on master ────────────────
#
# The slash-command intercept and its lookups are master's code. These rows
# pass unchanged against master's static/messages.js.


def test_control_an_empty_pane_send_issued_while_a_chat_was_already_loading_still_sends():
    result = _run(text="/custom do it", startWithoutSession=True, loadingAtStart="B")
    assert _named(result, "newSession") == [[]]
    assert result["startBody"]["session_id"] == "N"
    assert [toast for toast in _toasts(result) if toast[0].startswith("Message not sent")] == []


def test_control_an_opted_out_echo_command_from_the_empty_pane_still_sends_in_its_new_chat():
    # The echo branch creates the chat itself, then the handler opts out.
    result = _run(text="/reasoning high", startWithoutSession=True, echoCommand="reasoning")
    assert _named(result, "command.fn") == [["high"]]
    assert _named(result, "newSession") == [[]]
    assert result["startBody"]["session_id"] == "N"
    assert result["startBody"]["message"] == "/reasoning high"
    assert [toast for toast in _toasts(result) if toast[0].startswith("Message not sent")] == []


def test_control_moa_from_the_empty_pane_still_sends_in_its_new_chat():
    result = _run(text="/moa compare these", startWithoutSession=True)
    assert _named(result, "api.moaResolve") == [[]]
    assert _named(result, "newSession") == [[]]
    assert result["startBody"]["session_id"] == "N"
    assert result["startBody"]["message"] == "compare these"
    assert result["startBody"]["moa_config"] is True
    assert [toast for toast in _toasts(result) if toast[0].startswith("Message not sent")] == []


@pytest.mark.parametrize("text", ["hello", "/custom do it"])
def test_control_a_send_issued_while_a_chat_was_already_loading_keeps_its_path(text):
    # No switch began during the send: it still goes to the chat that issued
    # it (off screen, reattached later), as before this guard existed.
    result = _run(text=text, loadingAtStart="B")
    assert result["startBody"]["session_id"] == "A"
    assert result["inflight"]["A"]["reattach"] is True
    assert [toast for toast in _toasts(result) if toast[0].startswith("Message not sent")] == []


def test_control_returning_to_the_issuing_chat_during_the_lookup_still_sends_there():
    result = _run(switchKind="loadingBack", switchAt="slash", **SLASH)
    assert result["startBody"]["session_id"] == "A"
    assert result["startBody"]["message"] == "/custom do it"
    assert [toast for toast in _toasts(result) if toast[0].startswith("Message not sent")] == []


BUNDLE_MESSAGE = "Follow the mybundle steps for: do it"
BUNDLE = {"text": "/mybundle do it", "bundle": {"name": "mybundle", "message": BUNDLE_MESSAGE}}


@pytest.mark.parametrize(
    "scenario, sid, message, new_sessions",
    [
        ({"text": "/custom do it"}, "A", "/custom do it", []),
        ({"text": "/custom do it", "startWithoutSession": True}, "N", "/custom do it", [[]]),
        ({"text": "/moa compare these"}, "A", "compare these", []),
        ({"text": "/moa compare these", "startWithoutSession": True}, "N", "compare these", [[]]),
        (BUNDLE, "A", BUNDLE_MESSAGE, []),
        (dict(BUNDLE, startWithoutSession=True), "N", BUNDLE_MESSAGE, [[]]),
    ],
)
def test_control_slash_sends_without_a_switch_start_where_master_starts_them(scenario, sid, message, new_sessions):
    result = _run(**scenario)
    assert _named(result, "newSession") == new_sessions
    assert _named(result, "api.start") == [[sid]]
    assert result["startBody"]["message"] == message
    assert _named(result, "attachLiveStream") == [[sid, "stream-a"]]
    assert result["activeStreamId"] == "stream-a"
    assert [toast for toast in _toasts(result) if toast[0].startswith("Message not sent")] == []
    assert result["sendInProgress"] is False
    if "bundle" in scenario:
        assert _named(result, "resolveBundleCommand") == [["/mybundle do it", sid]]



# A rotation restores for the issuing chat exactly as master does, wherever the
# user is (the server keeps a compressed chat's draft with its continuation).
# Files staged on an on-screen continuation stay its own; they are not merged.
@pytest.mark.parametrize("kind", ["plain", "continuation", "chain", "cleared", "loading"])
def test_rotated_restore_is_for_the_issuing_chat_as_on_master(kind):
    scenario = {"switchAt": "start", "start": "409-rotated", "files": True}
    if kind != "plain":
        scenario["switchKind"] = kind
    result = _run(**scenario)
    assert _named(result, "_restoreComposerDraftAfterFailedSend") == [["hello", "A"]], kind
    assert _named(result, "restore.files") == [["A", ["f.txt"]]], kind


def test_rotated_failure_leaves_the_on_screen_continuation_and_its_staged_files_alone():
    result = _run(switchAt="start", switchKind="continuation", start="409-rotated", files=True,
                  a2Text="my own words", a2Staged=[{"name": "f.txt"}])
    assert result["composer"] == "my own words"
    assert _named(result, "restore.files") == [["A", ["f.txt"]]]
    assert _toasts(result) == [[_failed_rotated("\u201cChat A\u201d", "boom"), None, "error"]]


def test_rotated_restore_to_a_continuation_off_screen_passes_only_the_snapshot():
    """On B with b.txt staged, a rotation to A2 (not on screen) keeps A's text and file as A's draft."""
    result = _run(switchAt="start", start="409-rotated", files=True, bStaged=True)
    assert _named(result, "restore.files") == [["A", ["f.txt"]]]


def test_failed_start_restore_into_the_issuing_chat_passes_only_the_snapshot():
    """B's staged files are B's: a failure for A never hands them to A's draft."""
    result = _run(switchAt="start", start="500", files=True, bStaged=True)
    assert _named(result, "restore.files") == [["A", ["f.txt"]]]
