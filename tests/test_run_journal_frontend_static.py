import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"could not extract function body for {signature!r}")


def _optional_function_body(src: str, signature: str) -> str:
    """Extract a helper if present, else return "".

    Used for helpers whose ABSENCE is itself the regression under test: the
    probe must still run so the behavioral assertion reports the duplicate,
    rather than dying with a substring-not-found extraction error that says
    nothing about observable behavior.
    """
    try:
        return _function_body(src, signature)
    except (ValueError, AssertionError):
        return ""


def _run_session_identity_probe() -> dict:
    prompt = "same submitted prompt\nwith a second line"
    workspace_prompt = f"[Workspace::v1: /tmp/hermes-webui]\n{prompt}"
    legacy_workspace_prompt = f"[Workspace: /tmp/hermes-webui]\n{prompt}"
    attached_prompt = f"{prompt}\n\n[Attached files: /tmp/a.txt]"
    forced_prompt = (
        "[USER OVERRIDE] You MUST follow the skill 'hermes-webui-coordinator' "
        "content provided below before responding to the next message.\n\n"
        "[FORCED SKILL CONTEXT: hermes-webui-coordinator]\n"
        "skill body that should not make a second user bubble\n"
        "[/FORCED SKILL CONTEXT]\n\n"
        f"{prompt}"
    )
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _stripWorkspaceDisplayPrefix"),
            _function_body(SESSIONS_SRC, "function _messageComparableText"),
            _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
            _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
            _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
            _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
            _function_body(SESSIONS_SRC, "function _opaqueActiveTurnToken"),
            _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
            _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
            _function_body(SESSIONS_SRC, "function _inflightHasVisibleLiveState"),
        ]
    )
    script = f"""
{helpers}
const plain = {{role:'user', content:{json.dumps(prompt)}}};
const workspace = {{role:'user', content:{json.dumps(workspace_prompt)}}};
const legacyWorkspace = {{role:'user', content:{json.dumps(legacy_workspace_prompt)}}};
const attached = {{role:'user', content:{json.dumps(attached_prompt)}}};
const forced = {{role:'user', content:{json.dumps(forced_prompt)}}};
const different = {{role:'user', content:'a different submitted prompt'}};
process.stdout.write(JSON.stringify({{
  workspaceDedupe: _sameTranscriptMessage(plain, workspace),
  legacyWorkspaceDedupe: _sameTranscriptMessage(plain, legacyWorkspace),
  attachedDedupe: _sameTranscriptMessage(plain, attached),
  forcedSkillDedupe: _sameTranscriptMessage(plain, forced),
  differentUserNotDedupe: !_sameTranscriptMessage(plain, different),
  roleMismatchNotDedupe: !_sameTranscriptMessage(plain, {{role:'assistant', content:{json.dumps(prompt)}}}),
  userOnlyInflightVisible: _inflightHasVisibleLiveState({{messages:[plain]}}),
  emptyUserOnlyInflightNotVisible: !_inflightHasVisibleLiveState({{messages:[{{role:'user', content:'   '}}]}}),
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _run_current_turn_scope_probe() -> dict:
    prompt = "repeat me"
    historical_workspace_prompt = f"[Workspace::v1: /tmp/old]\n{prompt}"
    current_workspace_prompt = f"[Workspace::v1: /tmp/current]\n{prompt}"
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _stripWorkspaceDisplayPrefix"),
            _function_body(UI_SRC, "function msgContent"),
            _function_body(UI_SRC, "function _isContextCompactionText"),
            _function_body(UI_SRC, "function _isContextCompactionMessage"),
            _function_body(SESSIONS_SRC, "function _messageComparableText"),
            _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
            _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
            _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
            _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
            _function_body(SESSIONS_SRC, "function _opaqueActiveTurnToken"),
            _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
            _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
            _function_body(SESSIONS_SRC, "function _mergePendingSessionMessage"),
            _function_body(SESSIONS_SRC, "function _mergeInflightTailMessages"),
        ]
    )
    script = f"""
{helpers}
function getPendingSessionMessage(session, messages){{
  const text=String(session&&session.pending_user_message||'').trim();
  if(!text) return null;
  return {{
    role:'user',
    content:text,
    _ts:session.pending_started_at||10,
    _pending:true,
    _active_turn_token:session.active_turn_token,
  }};
}}
const historical = {{role:'user', content:{json.dumps(historical_workspace_prompt)}, _ts:1}};
const historicalAnswer = {{role:'assistant', content:'done', _ts:2}};
const liveAssistant = {{role:'assistant', content:'working', _live:true, _ts:4}};
const activeTurnToken = 'turn-current:3';
const pendingSession = {{pending_user_message:{json.dumps(prompt)}, pending_started_at:3, active_turn_token:activeTurnToken}};

const pendingAfterHistory = [historical, historicalAnswer];
const insertedAfterHistory = _mergePendingSessionMessage(pendingSession, pendingAfterHistory);

const pendingBeforeLive = [historical, historicalAnswer, liveAssistant];
const insertedBeforeLive = _mergePendingSessionMessage(pendingSession, pendingBeforeLive);

const optimisticCurrent = {{role:'user', content:{json.dumps(current_workspace_prompt)}, _ts:3, _active_turn_token:activeTurnToken}};
const pendingWithCurrent = [historical, historicalAnswer, optimisticCurrent, liveAssistant];
const insertedWithCurrent = _mergePendingSessionMessage(pendingSession, pendingWithCurrent);

const inflightAfterHistory = _mergeInflightTailMessages(
  [historical, historicalAnswer],
  [{{role:'user', content:{json.dumps(prompt)}, _ts:3, _active_turn_token:activeTurnToken}}, liveAssistant],
  activeTurnToken
);

const inflightWithCurrent = _mergeInflightTailMessages(
  [historical, historicalAnswer, optimisticCurrent],
  [{{role:'user', content:{json.dumps(prompt)}, _ts:3, _active_turn_token:activeTurnToken}}, liveAssistant],
  activeTurnToken
);

const compaction = {{
  role:'user',
  content:'[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.',
  _ts:3.5,
}};
const compactionBase = [historical, historicalAnswer, optimisticCurrent, compaction];
const compactionCandidate = {{role:'user', content:{json.dumps(prompt)}, _ts:3, _active_turn_token:activeTurnToken}};
const compactionCurrentTail = _currentTailUserMessage(compactionBase);
const compactionTailDuplicate = _hasCurrentTailUserDuplicate(compactionBase, compactionCandidate, activeTurnToken);
const compactionMerged = _mergeInflightTailMessages(
  compactionBase,
  [compactionCandidate, liveAssistant],
  activeTurnToken
);
const insertedAfterCompaction = _mergePendingSessionMessage(pendingSession, compactionMerged);
const compactionPromptCount = compactionMerged.filter(
  m=>m&&m.role==='user'&&m._ts===3&&_normalizeUserTranscriptText(m.content)==={json.dumps(prompt)}
).length;
const compactionMarkerRetained = compactionMerged.some(m=>_isContextCompactionMessage(m));
const compactionLiveAssistantRetained = compactionMerged.some(
  m=>m&&m.role==='assistant'&&m._live&&m.content==='working'
);
const completedBoundaryDedupe = _hasCurrentTailUserDuplicate(
  [historical, historicalAnswer, compaction],
  {{role:'user', content:{json.dumps(prompt)}, _ts:3, _active_turn_token:activeTurnToken}},
  activeTurnToken
);
const distinctCompletedTurnPromptCount = inflightAfterHistory.filter(
  m=>m&&m.role==='user'&&_normalizeUserTranscriptText(m.content)==={json.dumps(prompt)}
).length;

process.stdout.write(JSON.stringify({{
  insertedAfterHistory,
  pendingAfterHistoryRoles: pendingAfterHistory.map(m=>m.role),
  insertedBeforeLive,
  pendingBeforeLiveRoles: pendingBeforeLive.map(m=>m.role),
  insertedWithCurrent,
  pendingWithCurrentRoles: pendingWithCurrent.map(m=>m.role),
  inflightAfterHistoryRoles: inflightAfterHistory.map(m=>m.role),
  inflightWithCurrentRoles: inflightWithCurrent.map(m=>m.role),
  insertedAfterCompaction,
  compactionCurrentTailContent: compactionCurrentTail&&compactionCurrentTail.content,
  compactionTailDuplicate,
  compactionPromptCount,
  compactionMarkerRetained,
  compactionLiveAssistantRetained,
  completedBoundaryDedupe,
  distinctCompletedTurnPromptCount,
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _run_pending_session_message_probe() -> dict:
    prompt = "repeat me"
    historical_workspace_prompt = f"[Workspace::v1: /tmp/old]\n{prompt}"
    current_workspace_prompt = f"[Workspace::v1: /tmp/current]\n{prompt}"
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _stripWorkspaceDisplayPrefix"),
            _function_body(UI_SRC, "function msgContent"),
            _function_body(SESSIONS_SRC, "function _messageComparableText"),
            _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
            _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
            _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
            _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
            _function_body(SESSIONS_SRC, "function _opaqueActiveTurnToken"),
            _function_body(UI_SRC, "function _pendingCurrentTailUserMessage"),
            _optional_function_body(UI_SRC, "function _messageTimestampSeconds"),
            _optional_function_body(UI_SRC, "function _activeTurnTokenMatches"),
            _optional_function_body(UI_SRC, "function _pendingActiveTurnUserMessage"),
            _function_body(UI_SRC, "function _isContextCompactionText"),
            _function_body(UI_SRC, "function _isContextCompactionMessage"),
            _function_body(UI_SRC, "function getPendingSessionMessage"),
        ]
    )
    script = f"""
const _PENDING_ACTIVE_TURN_TS_EPSILON=1e-6;
{helpers}
const prompt = {json.dumps(prompt)};
const historical = {{role:'user', content:prompt, _ts:1}};
const historicalWorkspace = {{role:'user', content:{json.dumps(historical_workspace_prompt)}, _ts:1}};
const historicalAnswer = {{role:'assistant', content:'done', _ts:2}};
const currentTail = {{role:'user', content:prompt, _ts:3}};
const currentWorkspaceTail = {{role:'user', content:{json.dumps(current_workspace_prompt)}, _ts:3}};
const currentTailForCompaction = {{role:'user', content:prompt, _ts:3}};
const liveAssistant = {{role:'assistant', content:'working', _live:true, _ts:4}};
const attachments = [{{name:'note.txt', path:'note.txt', mime:'text/plain'}}];
const compactionMarker = {{
  role:'user',
  content:'[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.',
  _ts:3.5,
}};
const repeatedPromptTurnOne = {{role:'user', content:prompt, _ts:1}};
const repeatedPromptAnswerOne = {{role:'assistant', content:'done', _ts:2}};
const repeatedPromptTurnTwo = {{role:'user', content:prompt, _ts:3}};
const repeatedPromptAnswerTwo = {{role:'assistant', content:'done', _ts:4}};
const repeatedCompletedBase = [
  repeatedPromptTurnOne,
  repeatedPromptAnswerOne,
  repeatedPromptTurnTwo,
  repeatedPromptAnswerTwo,
];

const fromHistoricalSameText = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:3}},
  [historical, historicalAnswer]
);
const fromHistoricalWorkspace = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:3}},
  [historicalWorkspace, historicalAnswer]
);
const exactCurrentMessages = [historical, historicalAnswer, currentTail];
const exactCurrentResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4, pending_attachments:attachments}},
  exactCurrentMessages
);
const workspaceCurrentResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4}},
  [historical, historicalAnswer, currentWorkspaceTail]
);
const liveAfterCurrentResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4}},
  [historical, historicalAnswer, currentWorkspaceTail, liveAssistant]
);
const differentTailResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4}},
  [historical, historicalAnswer, {{role:'user', content:'different prompt', _ts:3}}]
);
const compactionTailResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:4, pending_attachments:attachments}},
  [historical, historicalAnswer, currentTailForCompaction, compactionMarker]
);
const repeatedCompletedResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:5}},
  repeatedCompletedBase
);
const repeatedCompletedMessages = repeatedCompletedResult
  ? [...repeatedCompletedBase, repeatedCompletedResult]
  : repeatedCompletedBase;
const repeatedCompletedPromptCount = repeatedCompletedMessages.filter(
  m=>m&&m.role==='user'&&_normalizeUserTranscriptText(m.content)===prompt
).length;
const compactionCurrentTail = _pendingCurrentTailUserMessage([historical, historicalAnswer, currentTailForCompaction, compactionMarker]);

// Mid-run reload: the server already reconciled the active turn's user row into
// the transcript AND this turn's assistant/tool output follows it, while
// pending_user_message is still set. The strict tail scan cannot see the user
// row (it stops at the completed assistant), so without the pending_started_at
// fallback the prompt is materialized a second time -> duplicate user bubble.
const midRunStartedAt = 500;
const midRunTranscript = [
  {{role:'user', content:'earlier question', timestamp:100}},
  {{role:'assistant', content:'earlier answer', timestamp:200}},
  {{role:'user', content:prompt, timestamp:midRunStartedAt}},
  {{role:'assistant', content:'', timestamp:midRunStartedAt+15}},
  {{role:'tool', content:'{{"ok":true}}', timestamp:midRunStartedAt+16}},
  {{role:'assistant', content:'partial answer', timestamp:midRunStartedAt+60}},
];
const midRunResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:midRunStartedAt}},
  midRunTranscript
);
const midRunWorkspaceTranscript = [
  {{role:'user', content:'earlier question', timestamp:100}},
  {{role:'assistant', content:'earlier answer', timestamp:200}},
  {{role:'user', content:{json.dumps(current_workspace_prompt)}, timestamp:midRunStartedAt}},
  {{role:'assistant', content:'partial answer', timestamp:midRunStartedAt+30}},
];
const midRunWorkspaceResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:midRunStartedAt}},
  midRunWorkspaceTranscript
);
// A same-text row from an OLDER turn must never be adopted: only the row whose
// timestamp matches pending_started_at identifies the active turn.
const staleSameTextTranscript = [
  {{role:'user', content:prompt, timestamp:midRunStartedAt-600}},
  {{role:'assistant', content:'old answer', timestamp:midRunStartedAt-500}},
];
const staleSameTextResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:midRunStartedAt}},
  staleSameTextTranscript
);
// Legacy/absent pending_started_at disables the fallback rather than guessing.
const noStartedAtResult = getPendingSessionMessage(
  {{pending_user_message:prompt}},
  midRunTranscript
);

// Regression (review round 2): two completed identical-text turns whose
// started_at differ by ~1s. The old 1.5s tolerance adopted the EARLIER row as
// the "active turn", hiding the second (still-pending) turn and copying its
// attachments onto the old row. With the precision-only epsilon the second turn
// is materialized and the first row stays untouched.
const rapidRepeatTurnOne = {{role:'user', content:prompt, timestamp:100}};
const rapidRepeatAnswerOne = {{role:'assistant', content:'done', timestamp:101}};
const rapidRepeatSecondAttachments = [{{name:'second.txt', path:'second.txt', mime:'text/plain'}}];
const rapidRepeatResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:101, pending_attachments:rapidRepeatSecondAttachments}},
  [rapidRepeatTurnOne, rapidRepeatAnswerOne]
);
// Exact token identity: the eager-checkpoint row carries _active_turn_token
// built from the same stream_id + started_at as the session — adopted even
// though its timestamp (499.9) is NOT within the epsilon of pending_started_at.
const tokenIdentityTranscript = [
  {{role:'user', content:'earlier question', timestamp:100}},
  {{role:'assistant', content:'earlier answer', timestamp:200}},
  {{role:'user', content:prompt, timestamp:499.9, _active_turn_token:'stream-abc:500'}},
  {{role:'assistant', content:'partial', timestamp:560}},
];
const tokenIdentityResult = getPendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:midRunStartedAt, active_turn_token:'stream-abc:500'}},
  tokenIdentityTranscript
);

process.stdout.write(JSON.stringify({{
  historicalSameTextSurvives: !!fromHistoricalSameText && fromHistoricalSameText.content===prompt && fromHistoricalSameText._pending===true,
  historicalWorkspaceSurvives: !!fromHistoricalWorkspace && fromHistoricalWorkspace.content===prompt && fromHistoricalWorkspace._pending===true,
  exactCurrentTailDedupe: exactCurrentResult===null,
  exactCurrentTailAttachmentsCopied: Array.isArray(currentTail.attachments) && currentTail.attachments[0].name==='note.txt',
  workspaceCurrentTailDedupe: workspaceCurrentResult===null,
  liveAfterCurrentTailDedupe: liveAfterCurrentResult===null,
  differentCurrentTailSurvives: !!differentTailResult && differentTailResult.content===prompt && differentTailResult._pending===true,
  compactionBoundaryDedupe: compactionTailResult===null,
  compactionBoundaryCurrentTail: compactionCurrentTail&&compactionCurrentTail.role==='user'&&compactionCurrentTail.content===prompt,
  compactionCurrentTailAttachmentsCopied: Array.isArray(currentTailForCompaction.attachments) && currentTailForCompaction.attachments[0].name==='note.txt',
  repeatedCompletedPromptsRemainValid: repeatedCompletedPromptCount===3,
  midRunReloadDedupe: midRunResult===null,
  midRunWorkspaceReloadDedupe: midRunWorkspaceResult===null,
  staleSameTextStillMaterializes: !!staleSameTextResult && staleSameTextResult._pending===true,
  legacyNoStartedAtStillMaterializes: !!noStartedAtResult && noStartedAtResult._pending===true,
  rapidRepeatSecondTurnReturned: !!rapidRepeatResult && rapidRepeatResult._pending===true && rapidRepeatResult.content===prompt && Array.isArray(rapidRepeatResult.attachments) && rapidRepeatResult.attachments[0].name==='second.txt',
  rapidRepeatFirstRowKeptClean: !Array.isArray(rapidRepeatTurnOne.attachments),
  tokenIdentityDedupe: tokenIdentityResult===null,
  isContextCompactionText: _isContextCompactionText(compactionMarker.content),
  isContextCompactionMessage: _isContextCompactionMessage(compactionMarker),
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _run_tail_scanner_boundary_probe() -> list[dict]:
    prompt = "scan me"
    canonical_helper = _optional_function_body(
        UI_SRC, "function _isCanonicalAssistantToolCallEnvelope"
    ) or "function _isCanonicalAssistantToolCallEnvelope(){return false;}"
    ownership_helper = _optional_function_body(
        UI_SRC, "function _isTailActivityOwnedByCandidateTurn"
    ) or "function _isTailActivityOwnedByCandidateTurn(){return false;}"
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _timestampSeconds"),
            _function_body(UI_SRC, "function _firstValidTimestampSeconds"),
            ownership_helper,
            canonical_helper,
            _function_body(UI_SRC, "function _pendingCurrentTailUserMessage"),
            _function_body(UI_SRC, "function msgContent"),
            _function_body(UI_SRC, "function _isContextCompactionText"),
            _function_body(UI_SRC, "function _isContextCompactionMessage"),
            _function_body(SESSIONS_SRC, "function _opaqueActiveTurnToken"),
            _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
            _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
            _function_body(SESSIONS_SRC, "function _mergeInflightTailMessages"),
        ]
    )
    script = f"""
{helpers}
function _sameTranscriptMessage(a,b){{
  return !!(a&&b&&a.role===b.role&&String(a.content||'').trim()===String(b.content||'').trim());
}}
const prompt = {json.dumps(prompt)};
const priorUser = {{role:'user', content:'older prompt', _ts:1}};
const currentUser = {{role:'user', content:prompt, _ts:10, _active_turn_token:'turn-1:10'}};
const currentCandidate = {{role:'user', content:prompt, _ts:10, _active_turn_token:'turn-1:10'}};
const canonicalCall = {{id:'call-1', type:'function', function:{{name:'inspect'}}}};
const canonicalTopNameCall = {{call_id:'call-2', name:'inspect'}};
const sameTurnEnvelope = {{role:'assistant', content:'', _ts:10, tool_calls:[canonicalCall]}};
const topNameEnvelope = {{role:'assistant', content:'', _ts:10, tool_calls:[canonicalTopNameCall]}};
const sameTurnTool = {{role:'tool', content:'result', tool_call_id:'call-1', _ts:11}};
const ordinaryAssistant = {{role:'assistant', content:'done', _ts:12}};
const liveAssistant = {{role:'assistant', content:'working', _live:true, _ts:12, _active_turn_token:'turn-1:10'}};
const compaction = {{role:'user', content:'[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.', _ts:10.5}};

const malformed = [
  {{name:'empty array', msg:{{role:'assistant', _ts:10, tool_calls:[]}}}},
  {{name:'non-array', msg:{{role:'assistant', _ts:10, tool_calls:'not-an-array'}}}},
  {{name:'null entry', msg:{{role:'assistant', _ts:10, tool_calls:[null]}}}},
  {{name:'primitive entry', msg:{{role:'assistant', _ts:10, tool_calls:[1]}}}},
  {{name:'missing id', msg:{{role:'assistant', _ts:10, tool_calls:[{{function:{{name:'inspect'}}}}]}}}},
  {{name:'missing name', msg:{{role:'assistant', _ts:10, tool_calls:[{{id:'call-3'}}]}}}},
  {{name:'mixed valid and invalid', msg:{{role:'assistant', _ts:10, tool_calls:[canonicalCall,{{id:'call-4'}}]}}}},
];
const cases = [
  {{name:'canonical envelope', messages:[priorUser,currentUser,sameTurnEnvelope], expect:prompt}},
  {{name:'canonical top-level name', messages:[priorUser,currentUser,topNameEnvelope], expect:prompt}},
  {{name:'canonical envelope plus tool result', messages:[priorUser,currentUser,sameTurnEnvelope,sameTurnTool], expect:prompt}},
  {{name:'tool result only', messages:[priorUser,currentUser,sameTurnTool], expect:prompt}},
  {{name:'ordinary assistant boundary', messages:[priorUser,currentUser,ordinaryAssistant], expect:null}},
  {{name:'older envelope activity', messages:[priorUser,currentUser,{{...sameTurnEnvelope,_ts:9}}], currentExpect:prompt, pendingExpect:null}},
  {{name:'older tool activity', messages:[priorUser,currentUser,sameTurnEnvelope,{{...sameTurnTool,_ts:9}}], currentExpect:prompt, pendingExpect:null}},
  {{name:'missing envelope timestamp', messages:[priorUser,currentUser,{{...sameTurnEnvelope,_ts:undefined}}], currentExpect:prompt, pendingExpect:null}},
  {{name:'missing tool timestamp', messages:[priorUser,currentUser,sameTurnEnvelope,{{...sameTurnTool,_ts:undefined}}], currentExpect:prompt, pendingExpect:null}},
  {{name:'missing candidate timestamp', messages:[priorUser,currentUser,sameTurnEnvelope], candidateStart:null, currentExpect:prompt, pendingExpect:null}},
  {{name:'same-turn activity', messages:[priorUser,currentUser,{{...sameTurnEnvelope,timestamp:10,_ts:undefined}},{{...sameTurnTool,timestamp:10.1,_ts:undefined}}], expect:prompt}},
  {{name:'compaction marker', messages:[priorUser,currentUser,compaction,sameTurnEnvelope], expect:prompt}},
  {{name:'live tail', messages:[priorUser,currentUser,liveAssistant], expect:prompt}},
  {{name:'live tail missing token', messages:[priorUser,currentUser,{{role:'assistant', content:'working', _live:true, _ts:12}}], currentExpect:null, pendingExpect:prompt}},
  {{name:'live tail mismatched token', messages:[priorUser,currentUser,{{role:'assistant', content:'working', _live:true, _ts:12, _active_turn_token:'turn-2:10'}}], currentExpect:null, pendingExpect:prompt}},
  ...malformed.map(tc=>({{name:tc.name, messages:[priorUser,currentUser,tc.msg], expect:null}})),
];
const tailPrompt = (row) => row&&row.role==='user' ? String(row.content||'') : null;
const results = cases.map(tc=>({{
  name:tc.name,
    current:tailPrompt(_currentTailUserMessage(
    tc.messages,
    tc.candidateStart===undefined?10:tc.candidateStart,
    undefined,
    tc.candidate===undefined?currentCandidate:tc.candidate,
    tc.activeTurnToken===undefined?'turn-1:10':tc.activeTurnToken,
  )),
  pending:tailPrompt(_pendingCurrentTailUserMessage(tc.messages,tc.candidateStart===undefined?10:tc.candidateStart)),
  currentExpect:tc.currentExpect===undefined?tc.expect:tc.currentExpect,
  pendingExpect:tc.pendingExpect===undefined?tc.expect:tc.pendingExpect,
}}));

// A newer repeated prompt must survive an older no-final-assistant tool tail.
const olderToolTail = [
  {{role:'user',content:prompt,_ts:1}},
  {{role:'assistant',content:'',_ts:2,tool_calls:[canonicalTopNameCall]}},
  {{role:'tool',content:'old result',_ts:3,tool_call_id:'call-2'}},
];
const newerPrompt = {{role:'user',content:prompt,_ts:10}};
const merged = _mergeInflightTailMessages(
  olderToolTail,
  [newerPrompt,{{role:'assistant',content:'working',_live:true,_ts:11}}],
  'new-turn-token'
);
const timestampOnlyCandidate = {{role:'user',content:prompt,timestamp:10}};
results.push({{
  name:'newer repeated prompt after older tool tail',
  currentDuplicate:_hasCurrentTailUserDuplicate(olderToolTail,newerPrompt),
  mergedUserTimestamps:merged.filter(m=>m&&m.role==='user').map(m=>m._ts),
  expect:false,
  mergedExpect:[1,10],
}});
results.push({{
  name:'candidate timestamp field',
  currentDuplicate:_hasCurrentTailUserDuplicate([currentUser,sameTurnEnvelope],timestampOnlyCandidate,'turn-1:10'),
  expect:false,
}});
results.push({{
  name:'same-token candidate after tool activity',
  currentDuplicate:_hasCurrentTailUserDuplicate([currentUser,sameTurnEnvelope],currentCandidate,'turn-1:10'),
  expect:true,
}});
process.stdout.write(JSON.stringify(results));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _run_cross_clock_inflight_probe() -> dict:
    """Exercise inflight dedupe with server-ahead persisted activity."""
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _timestampSeconds"),
            _function_body(UI_SRC, "function _firstValidTimestampSeconds"),
            _function_body(UI_SRC, "function _isTailActivityOwnedByCandidateTurn"),
            _function_body(UI_SRC, "function _isCanonicalAssistantToolCallEnvelope"),
            _function_body(UI_SRC, "function msgContent"),
            _function_body(UI_SRC, "function _isContextCompactionText"),
            _function_body(UI_SRC, "function _isContextCompactionMessage"),
            _function_body(SESSIONS_SRC, "function _opaqueActiveTurnToken"),
            _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
            _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
            _function_body(SESSIONS_SRC, "function _mergeInflightTailMessages"),
        ]
    )
    script = f"""
{helpers}
function _sameTranscriptMessage(a,b){{
  return !!(a&&b&&a.role===b.role&&String(a.content||'').trim()===String(b.content||'').trim());
}}
const prompt = {json.dumps("repeat me")};
const oldCall = {{id:'old-call', function:{{name:'inspect'}}}};
const streamId = 'new-stream';
const pendingStartedAt = 200;
const newToken = `${{streamId}}:${{pendingStartedAt}}`;
const attachment = {{name:'new.txt', path:'new.txt', mime:'text/plain'}};
const candidate = {{
  role:'user', content:prompt, _ts:50, _pending:true,
  _active_turn_token:newToken, attachments:[attachment],
}};
const priorActivity = [
  {{role:'user', content:prompt, timestamp:100, _active_turn_token:'old-stream:100'}},
  {{role:'assistant', content:'', timestamp:101, tool_calls:[oldCall]}},
  {{role:'tool', content:'old result', timestamp:102, tool_call_id:'old-call'}},
];
const live = {{role:'assistant', content:'working', _live:true, _ts:51}};
const skewMerged = _mergeInflightTailMessages(priorActivity, [candidate, live], newToken);
const missingTokenCandidate = {{...candidate}};
delete missingTokenCandidate._active_turn_token;
const missingTokenMerged = _mergeInflightTailMessages(priorActivity, [missingTokenCandidate, live], newToken);

const sameTurnActivity = [
  {{role:'user', content:prompt, timestamp:100, _active_turn_token:newToken}},
  {{role:'assistant', content:'', timestamp:101, tool_calls:[oldCall]}},
  {{role:'tool', content:'current result', timestamp:102, tool_call_id:'old-call'}},
];
const sameTokenMerged = _mergeInflightTailMessages(sameTurnActivity, [candidate, live], newToken);
process.stdout.write(JSON.stringify({{
  skewUserCount:skewMerged.filter(m=>m&&m.role==='user').length,
  skewAttachments:skewMerged.filter(m=>m&&m.role==='user').map(m=>m.attachments||[]),
  missingTokenUserCount:missingTokenMerged.filter(m=>m&&m.role==='user').length,
  sameTokenUserCount:sameTokenMerged.filter(m=>m&&m.role==='user').length,
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _run_turn_ownership_adversarial_probe() -> dict:
    """Exercise recovery, opaque-token dedupe, and fail-closed ownership."""
    helpers = "\n".join(
        [
            _function_body(UI_SRC, "function _timestampSeconds"),
            _function_body(UI_SRC, "function _firstValidTimestampSeconds"),
            _function_body(UI_SRC, "function _isTailActivityOwnedByCandidateTurn"),
            _function_body(UI_SRC, "function _isCanonicalAssistantToolCallEnvelope"),
            _function_body(UI_SRC, "function _pendingCurrentTailUserMessage"),
            _function_body(UI_SRC, "function _activeTurnTokenMatches"),
            _function_body(UI_SRC, "function msgContent"),
            _function_body(UI_SRC, "function _isContextCompactionText"),
            _function_body(UI_SRC, "function _isContextCompactionMessage"),
            _function_body(UI_SRC, "function getPendingSessionMessage"),
            _function_body(SESSIONS_SRC, "function _messageComparableText"),
            _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
            _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
            _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
            _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
            _function_body(SESSIONS_SRC, "function _opaqueActiveTurnToken"),
            _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
            _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
            _function_body(SESSIONS_SRC, "function _mergePendingSessionMessage"),
            _function_body(SESSIONS_SRC, "function _currentTurnAssistantText"),
            _function_body(SESSIONS_SRC, "function _compactTranscriptText"),
            _function_body(SESSIONS_SRC, "function _dropCurrentTurnAssistantMessages"),
            _function_body(SESSIONS_SRC, "function _prepareRunningLiveTail"),
            _function_body(SESSIONS_SRC, "function _mergeInflightTailMessages"),
        ]
    )
    script = f"""
{helpers}
const prompt = 'same prompt';
const newToken = 'opaque:new-turn';
const oldToken = 'opaque:old-turn';
const attachment = {{name:'new.txt', path:'new.txt'}};
const toolCall = {{id:'call-1', function:{{name:'inspect'}}}};

// A: loadSession-style prepare -> drop -> inflight merge -> pending merge.
const priorUser = {{role:'user', content:prompt, timestamp:100, _active_turn_token:oldToken}};
const priorAssistant = {{role:'assistant', content:'completed', timestamp:101}};
const newCandidate = {{
  role:'user', content:prompt, _ts:50, _pending:true,
  _active_turn_token:newToken, attachments:[attachment],
}};
const newLive = {{role:'assistant', content:'working', _live:true, _active_turn_token:newToken}};
let recovery = [priorUser, priorAssistant];
const prepared = _prepareRunningLiveTail(recovery, [newCandidate, newLive], newToken);
if(prepared) recovery = _dropCurrentTurnAssistantMessages(recovery, newToken);
recovery = _mergeInflightTailMessages(recovery, [newCandidate, newLive], newToken);
const pendingMerge = _mergePendingSessionMessage(
  {{pending_user_message:prompt, pending_started_at:200, active_turn_token:newToken, pending_attachments:[attachment]}},
  recovery,
);
const recoveredUsers = recovery.filter(m=>m&&m.role==='user');

// C: token ownership wins over transformed display/model text and adopts files.
const sameTurnBase = [
  {{role:'user', content:'upload-only', timestamp:100, _active_turn_token:newToken}},
  {{role:'assistant', content:'', timestamp:101, tool_calls:[toolCall]}},
  {{role:'tool', content:'result', timestamp:102, tool_call_id:'call-1'}},
];
const transformedCandidate = {{
  role:'user', content:'/moa same prompt', _ts:50, _active_turn_token:newToken,
  attachments:[attachment],
}};
const sameTokenMerged = _mergeInflightTailMessages(
  sameTurnBase,
  [transformedCandidate, {{role:'assistant', content:'live', _live:true}}],
  newToken,
);
const sameTokenUsers = sameTokenMerged.filter(m=>m&&m.role==='user');
const transformedDisplays = [
  'upload-only',
  '/moa same prompt',
  '/bundle invoke same prompt',
  '[FORCED SKILL CONTEXT: demo]\\ncontext\\n[/FORCED SKILL CONTEXT]\\nsame prompt',
];
const transformedTokenResults = transformedDisplays.map(content=>{{
  const base=[
    {{role:'user', content:'original prompt', timestamp:100, _active_turn_token:newToken}},
    {{role:'assistant', content:'', timestamp:101, tool_calls:[toolCall]}},
    {{role:'tool', content:'result', timestamp:102, tool_call_id:'call-1'}},
  ];
  const candidate={{role:'user', content, _ts:50, _active_turn_token:newToken, attachments:[attachment]}};
  const users=_mergeInflightTailMessages(
    base,
    [candidate, {{role:'assistant', content:'live', _live:true}}],
    newToken,
  ).filter(m=>m&&m.role==='user');
  return {{count:users.length, attachments:users[0]&&users[0].attachments||[]}};
}});

// B/D: old cross-clock activity and all uncertain token forms preserve the row.
const oldToolBase = [
  {{role:'user', content:prompt, timestamp:100, _active_turn_token:oldToken}},
  {{role:'assistant', content:'', timestamp:101, tool_calls:[toolCall]}},
  {{role:'tool', content:'old result', timestamp:102, tool_call_id:'call-1'}},
];
const skewMerged = _mergeInflightTailMessages(
  oldToolBase,
  [{{...newCandidate, attachments:[attachment]}}, {{role:'assistant', content:'live', _live:true}}],
  newToken,
);
const uncertainTokens = [undefined, '', 'opaque:new-turn ', 'opaque:other-turn'];
const uncertainUserCounts = uncertainTokens.map(token=>{{
  const candidate={{...newCandidate}};
  if(token===undefined) delete candidate._active_turn_token;
  else candidate._active_turn_token=token;
  return _mergeInflightTailMessages(
    oldToolBase,
    [candidate, {{role:'assistant', content:'live', _live:true}}],
    newToken,
  ).filter(m=>m&&m.role==='user').length;
}});
const liveBoundaryMerged = _mergeInflightTailMessages(
  [{{role:'user', content:prompt, _active_turn_token:oldToken}}, {{role:'assistant', content:'live', _live:true}}],
  [{{...newCandidate}}, {{role:'assistant', content:'new live', _live:true}}],
  newToken,
);
const sameLiveMerged = _mergeInflightTailMessages(
  [
    {{role:'user', content:'original prompt', _active_turn_token:newToken}},
    {{role:'assistant', content:'live', _live:true, _active_turn_token:newToken}},
  ],
  [{{...transformedCandidate}}, {{role:'assistant', content:'new live', _live:true, _active_turn_token:newToken}}],
  newToken,
);

// No activity is still not permission for tokenless or ambiguous text dedupe.
const noActivityBase = [
  {{role:'user', content:'original prompt', _active_turn_token:newToken}},
];
const noActivityCounts = [undefined, '', '   ', oldToken, newToken].map(token=>{{
  const candidate={{...transformedCandidate}};
  if(token===undefined) delete candidate._active_turn_token;
  else candidate._active_turn_token=token;
  return _mergeInflightTailMessages(
    noActivityBase,
    [candidate, {{role:'assistant', content:'live', _live:true, _active_turn_token:newToken}}],
    newToken,
  ).filter(m=>m&&m.role==='user').length;
}});
const duplicateAdjacent = _mergeInflightTailMessages(
  [
    {{role:'user', content:'first', _active_turn_token:newToken}},
    {{role:'user', content:'second', _active_turn_token:newToken}},
  ],
  [{{...transformedCandidate}}, {{role:'assistant', content:'live', _live:true, _active_turn_token:newToken}}],
  newToken,
).filter(m=>m&&m.role==='user').length;
const duplicateSeparated = _mergeInflightTailMessages(
  [
    {{role:'user', content:'first', _active_turn_token:newToken}},
    {{role:'assistant', content:'', timestamp:101, tool_calls:[toolCall]}},
    {{role:'tool', content:'result', timestamp:102, tool_call_id:'call-1'}},
    {{role:'user', content:'second', _active_turn_token:newToken}},
  ],
  [{{...transformedCandidate}}, {{role:'assistant', content:'live', _live:true, _active_turn_token:newToken}}],
  newToken,
).filter(m=>m&&m.role==='user').length;

// H: duplicate imported rows with the same token are ambiguous and preserved.
const ambiguousBase = [
  {{role:'user', content:'first', _active_turn_token:newToken}},
  {{role:'tool', content:'one', timestamp:101}},
  {{role:'user', content:'imported duplicate', _active_turn_token:newToken}},
  {{role:'tool', content:'two', timestamp:102}},
];
const ambiguousMerged = _mergeInflightTailMessages(
  ambiguousBase,
  [{{...transformedCandidate}}, {{role:'assistant', content:'live', _live:true}}],
  newToken,
);

// I: a unique current-token user row can sit after a live boundary in a
// recovered transcript; reconcile it before synthesizing the pending row.
const pendingSession = {{
  pending_user_message:prompt,
  pending_started_at:200,
  active_turn_token:newToken,
  pending_attachments:[attachment],
}};
const pendingAfterLive = [
  {{role:'user', content:'old prompt', _active_turn_token:oldToken}},
  {{role:'assistant', content:'working', _live:true, _active_turn_token:newToken}},
  {{role:'user', content:'/bundle transformed', _active_turn_token:newToken}},
];
const pendingUniqueInserted = _mergePendingSessionMessage(pendingSession, pendingAfterLive);
const pendingZeroMatch = [
  {{role:'user', content:prompt, _active_turn_token:oldToken}},
  {{role:'assistant', content:'working', _live:true, _active_turn_token:newToken}},
  {{role:'user', content:prompt}},
];
const pendingZeroMatchOriginal = pendingZeroMatch[2];
const pendingZeroMatchInserted = _mergePendingSessionMessage(pendingSession, pendingZeroMatch);
const pendingAmbiguous = [
  {{role:'user', content:'old prompt', _active_turn_token:oldToken}},
  {{role:'assistant', content:'working', _live:true, _active_turn_token:newToken}},
  {{role:'user', content:'first imported', _active_turn_token:newToken}},
  {{role:'user', content:'second imported', _active_turn_token:newToken}},
];
const pendingAmbiguousInserted = _mergePendingSessionMessage(pendingSession, pendingAmbiguous);

process.stdout.write(JSON.stringify({{
  prepared,
  pendingMerge,
  recoveredUserCount:recoveredUsers.length,
  recoveredAttachments:recoveredUsers.map(m=>m.attachments||[]),
  sameTokenUserCount:sameTokenUsers.length,
  sameTokenContent:sameTokenUsers[0]&&sameTokenUsers[0].content,
  sameTokenAttachments:sameTokenUsers[0]&&sameTokenUsers[0].attachments||[],
  transformedTokenResults,
  skewUserCount:skewMerged.filter(m=>m&&m.role==='user').length,
  uncertainUserCounts,
  liveBoundaryUserCount:liveBoundaryMerged.filter(m=>m&&m.role==='user').length,
  ambiguousUserCount:ambiguousMerged.filter(m=>m&&m.role==='user').length,
  sameLiveUserCount:sameLiveMerged.filter(m=>m&&m.role==='user').length,
  sameLiveAttachments:sameLiveMerged.filter(m=>m&&m.role==='user').map(m=>m.attachments||[]),
  opaqueWhitespace:_opaqueActiveTurnToken('   '),
  noActivityCounts,
  duplicateAdjacent,
  duplicateSeparated,
  pendingUniqueInserted,
  pendingUniqueRoles:pendingAfterLive.map(m=>m.role),
  pendingUniqueAttachments:pendingAfterLive.filter(m=>m&&m.role==='user').map(m=>m.attachments||[]),
  pendingZeroMatchInserted,
  pendingZeroMatchRoles:pendingZeroMatch.map(m=>m.role),
  pendingZeroMatchUserCount:pendingZeroMatch.filter(m=>m&&m.role==='user').length,
  pendingZeroMatchMoved:pendingZeroMatch.indexOf(pendingZeroMatchOriginal)
    <pendingZeroMatch.findIndex(m=>m&&m.role==='assistant'&&m._live),
  pendingZeroMatchSyntheticCount:pendingZeroMatch.filter(m=>m&&m.role==='user'&&m._pending).length,
  pendingAmbiguousInserted,
  pendingAmbiguousUserCount:pendingAmbiguous.filter(m=>m&&m.role==='user').length,
}}));
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_reattach_path_uses_replay_when_status_reports_journal():
    reattach_pos = MESSAGES_SRC.index("let replayOnly=false;")
    # Window widened to 2200: the SSE-recovery follow-restore fix (the
    # _wasFollowingAtReconnectDead guard + its sticky-unpin check) inserted lines
    # into the reconnect-dead cleanup block between this anchor and the
    # replay-params assertion below, pushing the target string past the old slice.
    block = MESSAGES_SRC[reattach_pos : reattach_pos + 2200]

    assert "st.replay_available" in block
    assert "replayOnly=true" in block
    assert "(reconnecting||replayOnly)?_runJournalReplayParams():''" in block
    assert "_clearOwnerInflightState()" in block


def test_error_reconnect_path_can_restore_from_journal():
    # Anchor on the reconnect block's stable entry point rather than the exact
    # composer-status string: the first status was changed to a template literal
    # `Reconnecting… (1/${_retryDelays.length})` (staged-probe counter), so the
    # old single-quoted "setComposerStatus('Reconnecting" anchor no longer exists.
    reconnect_pos = MESSAGES_SRC.index("_reconnectAttempted=true;")
    block = MESSAGES_SRC[reconnect_pos : reconnect_pos + 1100]

    assert "st.active" in block
    assert "st.replay_available" in block
    assert "Restoring stream" in block
    assert "_runJournalReplayParams()" in block


def test_frontend_replay_cursor_uses_eventsource_last_event_id():
    cursor_pos = MESSAGES_SRC.index("function _rememberRunJournalCursor")
    block = MESSAGES_SRC[cursor_pos : cursor_pos + 1000]

    assert "e.lastEventId" in block
    assert "lastIndexOf(':')" in block
    assert "_lastRunJournalSeq=seq" in block
    assert "source.addEventListener(_runJournalEventName,_rememberRunJournalCursor)" in MESSAGES_SRC
    assert "after_seq=${encodeURIComponent(String(_runJournalReplayAfterSeq()))}" in MESSAGES_SRC
    assert "after_seq=0" not in MESSAGES_SRC


def test_replayed_long_task_events_enter_the_same_live_timeline_handlers():
    """Run-journal replay must not grow a parallel long-task renderer.

    The run-state consistency contract depends on replayed journal events
    flowing through the same EventSource handlers as live streams.  Otherwise a
    live long task can render as Thinking -> progress text -> tool cards, while
    the same journaled event sequence replays as a flattened or reordered scene.
    """
    wire_pos = MESSAGES_SRC.index("function _wireSSE(source)")
    wire_block = MESSAGES_SRC[wire_pos : MESSAGES_SRC.index("async function _restoreSettledSession", wire_pos)]
    replay_events = [
        "reasoning",
        "interim_assistant",
        "tool",
        "tool_complete",
        "compressing",
        "compressed",
        "metering",
        "done",
        "apperror",
    ]

    for event_name in replay_events:
        assert f"source.addEventListener('{event_name}'" in wire_block, (
            f"{event_name} must be handled by the shared live/replay SSE pipeline"
        )

    thinking_helper = MESSAGES_SRC[
        MESSAGES_SRC.index("function _updateLiveThinkingCard") :
        MESSAGES_SRC.index("// Split a content string", MESSAGES_SRC.index("function _updateLiveThinkingCard"))
    ]
    assert "_updateLiveThinkingCard(" in wire_block, "reasoning replay should use the live Thinking card path"
    assert "updateThinking(text, opts)" in thinking_helper and "appendThinking(text, opts)" in thinking_helper, (
        "the shared Thinking helper should still route replay/live reasoning into the Worklog Thinking card path"
    )
    assert "appendLiveToolCard(tc" in wire_block, "tool replay should use live tool-card rendering"
    # Compression replay must dispatch through setCompressionUi(...). The handler
    # body may build the state object inline (`setCompressionUi({...})`) or hoist
    # it into a `state` variable first (`setCompressionUi(state)`) — both forms
    # use the same compression-card path, so accept either. Pinning the literal
    # `{` after the open-paren was over-specific and broke in v0.51.76 when
    # PR #2347 hoisted the state object to share it with `appendLiveCompressionCard`.
    assert ("setCompressionUi({" in wire_block) or ("setCompressionUi(state)" in wire_block), (
        "compression replay should use the compression card path"
    )
    assert "_runJournalReplayParams()" in MESSAGES_SRC, "replay attachments should enter _wireSSE via EventSource"


def test_run_journal_cursor_tracks_every_long_task_timeline_event():
    """Every user-visible long-task event needs cursor tracking for parity replay."""
    cursor_loop_pos = MESSAGES_SRC.index("for(const _runJournalEventName of [")
    cursor_loop = MESSAGES_SRC[cursor_loop_pos : MESSAGES_SRC.index("]", cursor_loop_pos)]
    timeline_events = [
        "token",
        "interim_assistant",
        "reasoning",
        "tool",
        "tool_complete",
        "compressing",
        "compressed",
        "metering",
        "done",
        "apperror",
        "cancel",
    ]

    for event_name in timeline_events:
        assert f"'{event_name}'" in cursor_loop, (
            f"{event_name} must advance the replay cursor to avoid duplicate timeline replay"
        )


def test_server_runtime_journal_snapshot_restores_structured_inflight_state():
    helper_pos = SESSIONS_SRC.index("function _serverLiveSnapshotToolId")
    helper_block = SESSIONS_SRC[helper_pos : helper_pos + 3600]
    load_pos = SESSIONS_SRC.index("async function loadSession")
    load_end = SESSIONS_SRC.index("// ── Handoff hint logic", load_pos)
    load_block = SESSIONS_SRC[load_pos:load_end]

    assert "runtime_journal_snapshot" in load_block
    assert "_serverLiveSnapshotInflight(S.session.runtime_journal_snapshot" in load_block
    assert "!_inflightHasVisibleLiveState(INFLIGHT[sid])" in load_block
    assert "journalSnapshot:true" in helper_block
    assert "lastRunJournalSeq" in helper_block
    assert "last_assistant_text" in helper_block
    assert "activity_burst_anchors" in helper_block
    for key in ("tid", "id", "tool_call_id", "tool_use_id", "call_id"):
        assert key in helper_block


def test_active_reload_keeps_user_only_inflight_visible_until_pending_dedupe():
    """A just-submitted user row is visible live state before first assistant text.

    On an active first-turn reload, the sidecar can still have messages=[] while
    pending_user_message and the submitted turn journal record the same prompt.
    The browser must not discard the user-only optimistic INFLIGHT entry as a
    cursor-only snapshot before pending/live replay reconciliation runs.
    """
    result = _run_session_identity_probe()

    assert result["userOnlyInflightVisible"] is True
    assert result["emptyUserOnlyInflightNotVisible"] is True


def test_pending_user_merge_dedupes_user_turn_variants_by_behavior():
    """Pending user rows and replayed/checkpointed user rows share one turn.

    Execute the same JavaScript helpers the browser uses so the regression test
    catches regex/order/trim mistakes, not just identifier wiring.
    """
    result = _run_session_identity_probe()

    assert result["workspaceDedupe"] is True
    assert result["legacyWorkspaceDedupe"] is True
    assert result["attachedDedupe"] is True
    assert result["forcedSkillDedupe"] is True
    assert result["differentUserNotDedupe"] is True
    assert result["roleMismatchNotDedupe"] is True


def test_user_turn_dedupe_is_scoped_to_current_turn_by_behavior():
    result = _run_current_turn_scope_probe()

    assert result["insertedAfterHistory"] is True
    assert result["pendingAfterHistoryRoles"] == ["user", "assistant", "user"]
    assert result["insertedBeforeLive"] is True
    assert result["pendingBeforeLiveRoles"] == ["user", "assistant", "user", "assistant"]

    assert result["insertedWithCurrent"] is False
    assert result["pendingWithCurrentRoles"] == ["user", "assistant", "user", "assistant"]

    assert result["inflightAfterHistoryRoles"] == ["user", "assistant", "user", "assistant"]
    assert result["inflightWithCurrentRoles"] == ["user", "assistant", "user", "assistant"]

    assert result["insertedAfterCompaction"] is False
    assert result["compactionCurrentTailContent"] == "[Workspace::v1: /tmp/current]\nrepeat me"
    assert result["compactionTailDuplicate"] is True
    assert result["compactionPromptCount"] == 1
    assert result["compactionMarkerRetained"] is True
    assert result["compactionLiveAssistantRetained"] is True
    assert result["completedBoundaryDedupe"] is False
    assert result["distinctCompletedTurnPromptCount"] == 2


def test_get_pending_session_message_keeps_deferred_repeat_prompt_by_behavior():
    """Deferred active reload must not hide a current repeat prompt.

    In the default deferred save mode, chat start persists only
    pending_user_message before the worker appends the display row.  If an older
    turn has the same visible user text, getPendingSessionMessage still has to
    return the current pending row; downstream merge code then dedupes only
    against the current tail.
    """
    result = _run_pending_session_message_probe()

    assert result["historicalSameTextSurvives"] is True
    assert result["historicalWorkspaceSurvives"] is True
    assert result["exactCurrentTailDedupe"] is True
    assert result["exactCurrentTailAttachmentsCopied"] is True
    assert result["workspaceCurrentTailDedupe"] is True
    assert result["liveAfterCurrentTailDedupe"] is True
    assert result["differentCurrentTailSurvives"] is True
    assert result["compactionBoundaryDedupe"] is True
    assert result["compactionBoundaryCurrentTail"] is True
    assert result["compactionCurrentTailAttachmentsCopied"] is True
    assert result["repeatedCompletedPromptsRemainValid"] is True
    # #6649 follow-up: mid-run reload must not duplicate the active turn's user row
    assert result["midRunReloadDedupe"] is True
    assert result["midRunWorkspaceReloadDedupe"] is True
    assert result["staleSameTextStillMaterializes"] is True
    assert result["legacyNoStartedAtStillMaterializes"] is True
    # #6670 review round 2: ~1s-apart identical turns must not be collapsed by
    # the active-turn fallback (no over-dedup, no attachment mis-attachment).
    assert result["rapidRepeatSecondTurnReturned"] is True
    assert result["rapidRepeatFirstRowKeptClean"] is True
    assert result["tokenIdentityDedupe"] is True
    assert result["isContextCompactionText"] is True
    assert result["isContextCompactionMessage"] is True


def test_tail_scanners_fail_closed_and_share_turn_ownership_rules():
    for row in _run_tail_scanner_boundary_probe():
        if row["name"] == "newer repeated prompt after older tool tail":
            assert row["currentDuplicate"] is row["expect"]
            assert row["mergedUserTimestamps"] == row["mergedExpect"]
            continue
        if row["name"] in {"candidate timestamp field", "same-token candidate after tool activity"}:
            assert row["currentDuplicate"] is row["expect"]
            continue
        assert row["current"] == row["currentExpect"], row["name"]
        assert row["pending"] == row["pendingExpect"], row["name"]


def test_inflight_dedupe_uses_active_turn_token_across_clock_skew():
    result = _run_cross_clock_inflight_probe()

    assert result["skewUserCount"] == 2
    assert result["skewAttachments"] == [[], [{"name": "new.txt", "path": "new.txt", "mime": "text/plain"}]]
    assert result["missingTokenUserCount"] == 2
    assert result["sameTokenUserCount"] == 1


def test_turn_ownership_adversarial_recovery_and_fail_closed_cases():
    result = _run_turn_ownership_adversarial_probe()

    assert result["prepared"] is False
    assert result["pendingMerge"] is False
    assert result["recoveredUserCount"] == 2
    assert result["recoveredAttachments"] == [[], [{"name": "new.txt", "path": "new.txt"}]]
    assert result["sameTokenUserCount"] == 1
    assert result["sameTokenContent"] == "upload-only"
    assert result["sameTokenAttachments"] == [{"name": "new.txt", "path": "new.txt"}]
    assert result["transformedTokenResults"] == [
        {"count": 1, "attachments": [{"name": "new.txt", "path": "new.txt"}]},
        {"count": 1, "attachments": [{"name": "new.txt", "path": "new.txt"}]},
        {"count": 1, "attachments": [{"name": "new.txt", "path": "new.txt"}]},
        {"count": 1, "attachments": [{"name": "new.txt", "path": "new.txt"}]},
    ]
    assert result["skewUserCount"] == 2
    assert result["uncertainUserCounts"] == [2, 2, 2, 2]
    assert result["liveBoundaryUserCount"] == 2
    assert result["ambiguousUserCount"] == 3
    assert result["sameLiveUserCount"] == 1
    assert result["sameLiveAttachments"] == [[{"name": "new.txt", "path": "new.txt"}]]
    assert result["opaqueWhitespace"] is None
    assert result["noActivityCounts"] == [2, 2, 2, 2, 1]
    assert result["duplicateAdjacent"] == 3
    assert result["duplicateSeparated"] == 3
    assert result["pendingUniqueInserted"] is False
    assert result["pendingUniqueRoles"] == ["user", "user", "assistant"]
    assert result["pendingUniqueAttachments"] == [[], [{"name": "new.txt", "path": "new.txt"}]]
    assert result["pendingZeroMatchInserted"] is True
    assert result["pendingZeroMatchRoles"] == ["user", "user", "assistant", "user"]
    assert result["pendingZeroMatchUserCount"] == 3
    assert result["pendingZeroMatchMoved"] is False
    assert result["pendingZeroMatchSyntheticCount"] == 1
    assert result["pendingAmbiguousInserted"] is True
    assert result["pendingAmbiguousUserCount"] == 4


def test_live_tool_matching_uses_the_same_aliases_as_live_card_dedup():
    live_tid_pos = MESSAGES_SRC.index("function _liveToolTid")
    live_tid_block = MESSAGES_SRC[live_tid_pos : live_tid_pos + 450]
    find_pos = MESSAGES_SRC.index("function _findPendingLiveToolCallIndex")
    find_block = MESSAGES_SRC[find_pos : find_pos + 900]
    upsert_pos = MESSAGES_SRC.index("function upsertLiveToolCall")
    upsert_block = MESSAGES_SRC[upsert_pos : upsert_pos + 1000]

    for key in ("tid", "id", "tool_call_id", "tool_use_id", "call_id"):
        assert key in live_tid_block
        assert key in find_block
        assert key in upsert_block
