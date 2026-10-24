"""#7862 re-gate (2026-10-20): resume-token claim boundary in the REAL send().

The maintainer's production-send recordings reproduced three resume-token
boundary defects after the TDZ fix landed:

1. **unrelated human text claims and posts the resume token** — send()
   claimed the one-shot ``/goal resume`` identity for EVERY human send, and
   the server's token admission then spent the pending continuation on the
   unrelated turn (a durable/restored-record route fixture classified the
   unrelated text as goal-related);
2. **generic rejected admission restores the text with an empty identity** —
   the rejected-start restoration passed only ``_goalContinuationId``, so a
   rejected literal ``continue`` came back without its resume token;
3. **active-stream conflict requeues ``continue`` without the ID** — the 409
   requeue also dropped the resume identity.

This file drives the REAL ``send()`` — sliced whole from static/messages.js —
with the REAL token helpers from static/ui.js, in node with recording stubs
(the same method as the maintainer's sandbox; no browser or server runs).
It pins the claim boundary: the resume identity is claimed ONLY for the
literal ``continue`` control, and the same effective invocation ID travels
through POST, the 409 requeue and the rejected-start restoration.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
UI_JS = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_SCENARIOS = ("unrelated", "continue", "continue-conflict", "unrelated-conflict", "continue-rejected", "continue-with-files")

_DRIVER_SRC = r"""
const fs = require('fs');
const messagesSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSrc = fs.readFileSync(process.argv[3], 'utf8');
const scenario = process.argv[4] || '';
const failures = [];

function check(cond, msg){
  if(!cond) failures.push(msg);
}

// ---- comment/string/template-aware brace scanner (the same extractor that
// validates against node --check: send() contains /\s{2,}/g and comment
// braces, so a naive depth counter is not safe) ----
function scanEnd(src, openBrace){
  let i = openBrace + 1, depth = 1, n = src.length, state = 'code', quote = '';
  while(i < n && depth > 0){
    const c = src[i], nxt = i + 1 < n ? src[i+1] : '';
    if(state === 'code'){
      if(c === '/' && nxt === '/'){ state = 'linecomment'; i += 2; continue; }
      if(c === '/' && nxt === '*'){ state = 'blockcomment'; i += 2; continue; }
      if(c === '"' || c === "'"){ state = 'string'; quote = c; i += 1; continue; }
      if(c === '`'){ state = 'template'; i += 1; continue; }
      if(c === '{') depth += 1;
      else if(c === '}'){ depth -= 1; if(depth === 0) return i; }
      i += 1;
    } else if(state === 'linecomment'){
      if(c === '\n') state = 'code';
      i += 1;
    } else if(state === 'blockcomment'){
      if(c === '*' && nxt === '/'){ state = 'code'; i += 2; continue; }
      i += 1;
    } else if(state === 'string'){
      if(c === '\\'){ i += 2; continue; }
      if(c === quote) state = 'code';
      i += 1;
    } else if(state === 'template'){
      if(c === '\\'){ i += 2; continue; }
      if(c === '`') state = 'code';
      i += 1;
    }
  }
  throw new Error('unbalanced braces in ' + src.slice(openBrace, openBrace + 40));
}
function extractTopLevel(src, name, prefix){
  const marker = prefix + ' ' + name + '(';
  const start = src.indexOf(marker);
  if(start < 0) throw new Error('missing ' + marker + ' in source');
  const brace = src.indexOf('{', start);
  if(brace < 0) throw new Error('missing brace for ' + marker);
  return src.slice(start, scanEnd(src, brace) + 1);
}

// ---- REAL token helpers from static/ui.js ----
// The holder is a plain empty-object literal in ui.js; declared here directly
// because `const` inside eval() is scoped to the eval, not the module.
const _RESUME_GOAL_CONTINUATION_TOKENS = {};
eval(extractTopLevel(uiSrc, '_setResumeGoalContinuationToken', 'function'));
eval(extractTopLevel(uiSrc, '_claimResumeGoalContinuationToken', 'function'));
eval(extractTopLevel(uiSrc, '_peekResumeGoalContinuationToken', 'function'));

// ---- REAL helpers + the WHOLE send() from static/messages.js ----
eval(extractTopLevel(messagesSrc, '_normalizeGoalContinuationId', 'function'));
eval(extractTopLevel(messagesSrc, '_isLiteralResumeContinue', 'function'));
const sendStart = messagesSrc.indexOf('async function send(){');
if(sendStart < 0) throw new Error('async function send(){ not found');
const sendText = messagesSrc.slice(sendStart, scanEnd(messagesSrc, messagesSrc.indexOf('{', sendStart)) + 1);
eval(sendText);

// ---- module-level slots send() assigns/reads ----
let _sendInProgress = false;
let _sendInProgressSid = null;
let _queueDrainSid = null;
let _forcedSkillDirectivePending = null;
let _approvalSessionId = null;
let _clarifySessionId = null;
const _pendingSelections = [];
const INFLIGHT = {};

// ---- recording stubs for the browser environment ----
let _currentMsgEl = null;
function $(id){ if(id === 'msg') return _currentMsgEl; return null; }
function autoResize(){}
function hideCmdDropdown(){}
function setComposerStatus(){}
function setBusy(){}
function renderMessages(){}
function clearLiveToolCards(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function upsertActiveSessionForLocalTurn(){}
function updateSendBtn(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function applySessionTitleUpdate(){}
function saveInflightState(){}
function markInflight(){}
function attachLiveStream(){}
function stopApprovalPolling(){}
function stopClarifyPolling(){}
function hideApprovalCard(){}
function hideClarifyCard(){}
function removeThinking(){}
function clearInflightState(){}
function clearOptimisticSessionStreaming(){}
function renderSessionList(){}
function showToast(){}
function renderTray(){}
function _clearComposerDraft(){ return null; }
function _saveComposerDraftNow(){ return null; }
function _handleComposerDraftPostFailure(){}
function _setRestoredGoalContinuationDraft(){}
function _clearRestoredGoalContinuationDraft(){}
function _dismissHandoffHint(){}
function isCompressionUiRunning(){ return false; }
function _clearStaleBusyStateBeforeSend(){}
function shouldInterceptCompressionRecoveryContinuation(){ return false; }
function showCompressionRecoveryContinuationHint(){}
function parseCommand(){ return null; }
const COMMANDS = [];
function _trySteer(){ return Promise.resolve(); }
function cancelStream(){ return Promise.resolve(true); }
function t(k){ return k; }
function _flushSelectionBlocksToComposer(){}
function _takeRestoredDraftGoalContinuationId(){ return ''; }
function _chatPayloadModelState(){ return {model:'test-model', model_provider:'test-provider'}; }
function _readPendingSessionModel(){ return null; }
function _clearPendingSessionModel(){}
function _clearComposerAfterQueuedSelectionSend(){}
function updateQueueBadge(){}
function _runOptionalPreStartUiStep(){}
function _runOptionalPostStartUiStep(){}
function loadSession(){ return Promise.resolve(); }
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
function _recoverCompressedSend(){ return Promise.resolve(false); }
function _writePersistedModelState(){}
function _applyModelToDropdown(){}
function syncTopbar(){}
function syncModelChip(){}
function showLiveRunStatus(){}
function newSession(){ return Promise.resolve(); }
function _composerTextWithPendingSelections(){ return _currentMsgEl ? (_currentMsgEl.value || '') : ''; }
async function uploadPendingFiles({files}){
  return (files || []).map(f => (typeof f === 'string' ? {name:f, path:'/p/'+f} : f));
}
global.window = {};
global.document = { querySelector: () => null };

const calls = [];
let apiMode = 'resolve';
async function api(path, opts){
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.push({path, body});
  if(apiMode === 'reject-409') throw new Error('session already has an active stream');
  if(apiMode === 'reject') throw new Error('boom');
  return {stream_id:'stream-1', title:'T', pending_started_at: Date.now()/1000};
}
const queued = [];
function queueSessionMessage(sid, entry){ queued.push({sid, entry}); }
const restored = [];
function _restoreComposerDraftAfterFailedSend(draftText, filesSnapshot, sid, clearPromise, goalContinuationId){
  restored.push({draftText, sid, goalContinuationId});
}

function freshS(text, files){
  return {
    session:{session_id:'sid1', workspace:'ws', model:'test-model', model_provider:'test-provider', profile:'default', title:'Untitled', active_stream_id:null, pending_started_at:null, read_only:false},
    messages:[], busy:false, pendingFiles:files || [], activeProfile:'default', activeStreamId:null, toolCalls:[], todos:[], todoStateMeta:null,
  };
}

async function run(){
  // reset per-scenario state
  calls.length = 0; queued.length = 0; restored.length = 0;
  Object.keys(INFLIGHT).forEach(k => delete INFLIGHT[k]);
  _sendInProgress = false; _sendInProgressSid = null; _queueDrainSid = null; _forcedSkillDirectivePending = null;
  apiMode = 'resolve';
  global.S = freshS();
  _currentMsgEl = {value:'', dataset:{}};

  const TOKEN = 'tok1';
  if(scenario === 'unrelated'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'hello';
    await send();
    const posted = calls[0] && calls[0].body;
    check(Boolean(posted), 'unrelated text never reached /api/chat/start');
    check(posted && posted.goal_continuation_id === '', 'unrelated text posted the resume token: got ' + JSON.stringify(posted && posted.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === TOKEN, 'unrelated text claimed the one-shot resume token; the real continue can no longer be admitted');
  } else if(scenario === 'continue'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'continue';
    await send();
    const posted = calls[0] && calls[0].body;
    check(Boolean(posted), 'the literal continue never reached /api/chat/start');
    check(posted && posted.goal_continuation_id === TOKEN, 'the literal continue posted without the resume token: got ' + JSON.stringify(posted && posted.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === '', 'the resume token was not consumed one-shot after the continue send');
  } else if(scenario === 'continue-conflict'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'continue';
    apiMode = 'reject-409';
    await send();
    check(queued.length === 1, 'active-stream conflict did not requeue the continue');
    check(queued[0] && queued[0].entry.goal_continuation_id === TOKEN, 'the 409 requeue dropped the resume token: got ' + JSON.stringify(queued[0] && queued[0].entry.goal_continuation_id));
  } else if(scenario === 'unrelated-conflict'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'hello';
    apiMode = 'reject-409';
    await send();
    check(queued.length === 1, 'active-stream conflict did not requeue the unrelated text');
    check(!(queued[0] && queued[0].entry.goal_continuation_id), 'an unrelated 409-requeued entry inherited the resume token');
    check(_peekResumeGoalContinuationToken('sid1') === TOKEN, 'an unrelated 409-requeued entry stole the one-shot resume token');
  } else if(scenario === 'continue-rejected'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'continue';
    apiMode = 'reject';
    await send();
    check(restored.length === 1, 'the rejected continue was not restored into the composer');
    check(restored[0] && restored[0].goalContinuationId === TOKEN, 'the rejected continue restored its text with an EMPTY identity: got ' + JSON.stringify(restored[0] && restored[0].goalContinuationId));
  } else if(scenario === 'continue-with-files'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'continue';
    global.S.pendingFiles = ['f.txt'];
    await send();
    const posted = calls[0] && calls[0].body;
    check(Boolean(posted), 'the attachment send never reached /api/chat/start');
    check(posted && posted.goal_continuation_id === '', 'an attachment-annotated continue claimed the resume token: got ' + JSON.stringify(posted && posted.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === TOKEN, 'an attachment-annotated continue consumed the one-shot resume token');
  } else {
    throw new Error('unknown scenario ' + scenario);
  }
  const result = {ok: failures.length === 0, failures};
  console.log('RESULT ' + JSON.stringify(result));
}

run().catch(err => {
  console.log('RESULT ' + JSON.stringify({ok:false, failures:['driver threw: ' + (err && err.stack ? err.stack : String(err))]}));
  process.exit(1);
});
"""


def _run_scenario(scenario: str) -> dict:
    import tempfile

    assert NODE, "node not on PATH"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(_DRIVER_SRC)
        driver_path = fh.name
    try:
        env = os.environ.copy()
        # node SIGABRTs in this environment unless the channel fd is stripped.
        env.pop("NODE_CHANNEL_FD", None)
        proc = subprocess.run(
            [NODE, driver_path, str(MESSAGES_JS), str(UI_JS), scenario],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in (proc.stdout or "").splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        raise AssertionError(f"node driver produced no RESULT line\n{out[:2000]}")
    finally:
        try:
            os.unlink(driver_path)
        except OSError:
            pass


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_resume_token_claim_boundary(scenario):
    result = _run_scenario(scenario)
    assert result.get("ok"), (
        f"scenario {scenario!r} failed:\n- " + "\n- ".join(result.get("failures") or [])
    )
