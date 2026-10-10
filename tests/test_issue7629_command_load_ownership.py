"""End-to-end frontend ownership regressions for PR #7629 review feedback.

The Node harness executes the real /reload-skills caller, command transport,
command-result reconciliation, loadSession(), and _ensureMessagesLoaded(). Only
browser/API adapters are deterministic fixtures. Delayed metadata/message
responses make the ownership handoff observable at every awaited seam.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = (ROOT / "static/commands.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static/messages.js").read_text(encoding="utf-8")
SESSIONS = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# This deliberately extracts production functions rather than mirroring their
# control flow in Python. It is the same brace-aware strategy used by the
# neighboring cross-session loader tests.
def _extract_function(source: str, name: str) -> str:
    for marker in (f"async function {name}(", f"function {name}("):
        start = source.find(marker)
        if start >= 0:
            break
    else:
        raise AssertionError(f"{name} not found")
    brace_start = source.find("{", start)
    depth = 0
    string = None
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(brace_start, len(source)):
        ch = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
            continue
        if string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == string:
                string = None
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            continue
        if ch in ("'", '"', "`"):
            string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"could not extract {name}")


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


LOAD_SESSION = _extract_function(SESSIONS, "loadSession")
ENSURE_MESSAGES = _extract_function(SESSIONS, "_ensureMessagesLoaded")
PROFILE_MATCHER = _extract_function(SESSIONS, "_profileMatchesActiveProfile")
COMMAND_RUNTIME = _block(
    COMMANDS,
    "async function executeAgentCommand(text,_meta){",
    "\nasync function resolveBundleCommand",
)
GENERIC_CALLER = _block(
    MESSAGES,
    "if(_parsedCmd.name==='sessions' || _parsedCmd.name==='resume'){",
    "if(_agentCmd&&_agentCmd.category==='Plugin'){",
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def _run_node(scenario: str, branch: str) -> dict:
    script = textwrap.dedent(
        """
        const S={
          session:{session_id:'sid-old',message_count:1},
          activeProfile:'default',activeProfileIsDefault:true,
          messages:[{role:'assistant',content:'old-before-load'}],
          toolCalls:[],pendingFiles:[],busy:false,activeStreamId:null,
        };
        const INFLIGHT={};
        let _loadingSessionId=null;
        let _loadSessionGeneration=0;
        let _pendingCarryForwardSnapshot=null;
        let _loadingOlder=false;
        let _messagesTruncated=false;
        let _oldestIdx=0;
        let _msgLimitMax=500;
        let _messageRenderWindowSize=0;
        const _MSG_LIMIT_MAX=500;
        let _metadataResolve=null, _continuationMetadataResolve=null;
        let _messagesResolve=null, _messagesReject=null;
        let metadataRequested=false, continuationMetadataRequested=false, messagesRequested=false;
        let metadataRequestCount=0, messagesRequestCount=0;
        let continuationDuringLoad=null;
        const apiCalls=[];
        let renderCalls=0, clearLiveCalls=0, rearmCalls=0;
        const composer={value:'/reload-skills'};
        const msgInner={innerHTML:''};
        const $=(id)=>id==='msg'?composer:(id==='msgInner'?msgInner:null);
        const showToast=()=>{};
        const renderMessages=()=>{
          renderCalls++;
          if(Array.isArray(S.messages)&&S.messages.length) msgInner.innerHTML='';
        };
        const renderSessionList=async()=>{};
        const autoResize=()=>{};
        const hideCmdDropdown=()=>{};
        const t=(value)=>value;
        const cliOnlyCommandResponse=()=>'';
        const _rawComposerText='/reload-skills';
        const _approvalCommandMutationGeneration=()=>0;
        const _clearComposerDraft=()=>Promise.resolve(true);
        const _saveComposerDraftNow=()=>Promise.resolve(true);
        const _clearApprovalCommandRetry=()=>{};
        const _rememberApprovalCommandRetry=()=>{};
        const _cronProfileNameIsRootAlias=()=>false;
        const _resolveSessionIdFromSidebarLineage=(sid)=>sid;
        const _messageReloadLimitForSession=()=>2;
        const _currentMessageRenderWindowSize=()=>1;
        const _messageRenderableMessageCount=()=>1;
        const _isSessionActivelyViewedForList=()=>true;
        const _acknowledgeSessionVisit=()=>{};
        const _sessionVisitHasUnreadState=()=>false;
        const _sessionProfileMismatchFromError=()=>null;
        const _switchProfileForSessionLoad=async()=>{};
        const _setSessionViewedCount=()=>{};
        const _clearSameSessionForceReloadHint=()=>{};
        const _captureSameSessionForceReloadHint=()=>{};
        const _adoptRegenerationRevision=()=>{};
        const _clearEmptyComposerModelOverride=()=>{};
        const _applyPendingSessionModelForSession=()=>{};
        const _resolveSessionModelForDisplaySoon=()=>{};
        const _deferWorkspaceRefreshForSession=()=>{};
        const _hydrateTodosFromSession=()=>{};
        const _deferSessionSideEffect=(_sid,fn)=>Promise.resolve(fn());
        const populateModelDropdown=()=>{};
        const _syncCtxIndicator=()=>{};
        const _renderPendingPromptsForActiveSession=()=>{};
        const _restoreComposerDraft=()=>{};
        const _restoreApprovalTransportFailureForSession=()=>{};
        const projectSessionArtifactsForOwner=()=>{};
        const _isMessagingSession=()=>false;
        const _checkAndShowHandoffHint=()=>{};
        const _hideHandoffHint=()=>{};
        const _setActiveSessionUrl=()=>{};
        const startSessionStream=()=>{};
        const stopSessionStream=()=>{};
        const stopApprovalPolling=()=>{};
        const startApprovalPolling=()=>{};
        const stopClarifyPolling=()=>{};
        const startClarifyPolling=()=>{};
        const _fetchYoloState=()=>{};
        const hideApprovalCard=()=>{};
        const hideClarifyCard=()=>{};
        const clearCompressionUi=()=>{};
        const _updateYoloPill=()=>{};
        const _clearDeferredActiveSessionExternalRefresh=()=>{};
        const _resetScrollDirectionTracker=()=>{};
        const _setPendingSessionToolsets=()=>{};
        const _rearmActiveSessionStream=()=>{rearmCalls++;};
        const clearInflightState=()=>{};
        const _serverLiveSnapshotInflight=()=>null;
        const _normalizeInflightReplayCursorForReattach=()=>{};
        const syncTopbar=()=>{};
        const _uploadPendingFilesSyncProgressForSession=()=>{};
        const _clearPendingSelections=()=>{};
        const _clearQueueCardDisplay=()=>{};
        const closeOtherLiveStreams=()=>{};
        const loadInflightState=()=>null;
        const _inflightHasVisibleLiveState=()=>true;
        const _selectLiveRecoveryInflight=(value)=>value;
        const _ensureInflightLiveAssistantMessage=()=>{};
        const _projectInflightMessagesForActivityBursts=(value)=>value.messages||[];
        const _prepareRunningLiveTail=()=>false;
        const _dropCurrentTurnAssistantMessages=(value)=>value;
        const _mergeInflightTailMessages=(value)=>value;
        const _mergePendingSessionMessage=()=>false;
        const _syncToolCallsForLoadedMessages=()=>{};
        const attachLiveStream=()=>{};
        const clearLiveToolCards=()=>{clearLiveCalls++;};
        const _renderRuntimeJournalAnchorActivityScene=()=>false;
        const ensureRunActivityForCurrentTurn=()=>{};
        const ensureLiveWorklogShell=()=>{};
        const placeLiveToolCardsHost=()=>{};
        const setBusy=()=>{};
        const setComposerStatus=()=>{};
        const setStatus=()=>{};
        const updateSendBtn=()=>{};
        const updateQueueBadge=()=>{};
        const resumeManualCompressionForSession=()=>{};
        const localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
        const history={replaceState:()=>{}};
        const window={_carryForwardEphemeralTurnFields:(_old,next)=>next};
        const document={getElementById:()=>null};
        const _appRootPath=()=>'/';
        const api=async(url)=>{
          const target=String(url);
          apiCalls.push(target);
          if(target==='/api/commands/exec')
            return {command_id:'command-old',output:'command complete'};
          if(target.includes('messages=0')){
            metadataRequestCount++;
            metadataRequested=true;
            return new Promise((resolve)=>{
              if(metadataRequestCount===1) _metadataResolve=resolve;
              else{
                continuationMetadataRequested=true;
                _continuationMetadataResolve=resolve;
              }
            });
          }
          if(target.includes('messages=1')){
            messagesRequestCount++;
            messagesRequested=true;
            return new Promise((resolve,reject)=>{_messagesResolve=resolve;_messagesReject=reject;});
          }
          throw new Error('unexpected API call '+target);
        };

        %(profile_matcher)s
        %(command_runtime)s
        %(load_session)s
        %(ensure_messages)s
        const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set(['reload-skills']);
        const getAgentCommandMetadata=async()=>({name:'reload-skills'});

        async function runCaller(){
          const _parsedCmd={name:'reload-skills'};
          const text='/reload-skills';
          %(generic_caller)s
          return 'fell-through';
        }

        (async()=>{
          %(inflight_setup)s
          const callerDone=runCaller();
          for(let i=0;i<10000&&!metadataRequested;i++) await Promise.resolve();
          if(!metadataRequested) throw new Error('metadata request was not reached');
          if(%(scenario_json)s==='continuation'){
            _metadataResolve({session:{
              session_id:'sid-old',message_count:4,active_stream_id:null,
              continuation_session_id:'sid-child'
            }});
            for(let i=0;i<10000&&!continuationMetadataRequested;i++) await Promise.resolve();
            if(!continuationMetadataRequested) throw new Error('continuation metadata request was not reached');
            _continuationMetadataResolve({session:{session_id:'sid-child',message_count:5,active_stream_id:null}});
          }else{
            _metadataResolve({session:{session_id:'sid-old',message_count:4,active_stream_id:%(stream)s}});
          }
          for(let i=0;i<10000&&!messagesRequested;i++) await Promise.resolve();
          if(!messagesRequested) throw new Error('message request was not reached');
          continuationDuringLoad={loading:_loadingSessionId,msgInner:msgInner.innerHTML};
          // This is the review reproduction: the actual command's reconciliation
          // is waiting for the old message response while the user opens New Chat
          // or switches profile.
          if(%(scenario_json)s==='new-chat'||%(scenario_json)s==='new-chat-reject'){
            S.session={session_id:'sid-new',message_count:0};
            S.messages=[{role:'assistant',content:'new-chat-transcript'}];
          }else if(%(scenario_json)s==='profile-switch'||%(scenario_json)s==='profile-switch-reject'){
            S.activeProfile='profile-new';
            S.activeProfileIsDefault=false;
            S.messages=[{role:'assistant',content:'profile-new-transcript'}];
          }
          if(%(scenario_json)s==='new-chat-reject'||%(scenario_json)s==='profile-switch-reject'){
            _messagesReject(new Error('old message response rejected after ownership change'));
          }else if(%(scenario_json)s==='continuation'){
            _messagesResolve({session:{
              session_id:'sid-child',_messages_truncated:false,_messages_offset:0,
              messages:[{role:'assistant',content:'child-command-transcript',_webui_command_id:'command-old'}],
              message_count:5,tool_calls:[]
            }});
          }else{
            _messagesResolve({session:{
              session_id:'sid-old',_messages_truncated:false,_messages_offset:0,
              messages:[{role:'assistant',content:'old-late-transcript',_webui_command_id:'command-old'}],
              message_count:4,tool_calls:[]
            }});
          }
          await callerDone;
          await Promise.resolve();
          console.log(JSON.stringify({
            scenario:%(scenario_json)s,branch:%(branch_json)s,
            sid:S.session&&S.session.session_id,profile:S.activeProfile,
            messages:S.messages,metadataRequested,messagesRequested,
            renderCalls,clearLiveCalls,rearmCalls,
            metadataRequestCount,messagesRequestCount,apiCalls,
            loadingSessionId:_loadingSessionId,msgInner:msgInner.innerHTML,
            continuationDuringLoad,
          }));
        })().catch((error)=>{console.error(error&&error.stack||error);process.exit(1);});
        """
    ) % {
        "profile_matcher": PROFILE_MATCHER,
        "command_runtime": COMMAND_RUNTIME,
        "load_session": LOAD_SESSION,
        "ensure_messages": ENSURE_MESSAGES,
        "generic_caller": GENERIC_CALLER,
        "inflight_setup": (
            "INFLIGHT['sid-old']={messages:[{role:'assistant',content:'old-live-tail'}],toolCalls:[],uploaded:[]};"
            if branch == "inflight" else ""
        ),
        "stream": "'stream-old'" if branch == "inflight" else "null",
        "scenario_json": json.dumps(scenario),
        "branch_json": json.dumps(branch),
    }
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip())


def test_command_reconciliation_passes_explicit_owner_session_and_drops_dead_option():
    """The reconciliation handoff must carry both parts of the owner identity."""
    reconcile = _extract_function(COMMANDS, "_reconcileAgentCommandTranscript")
    assert "ownerProfile:ownerProfile" in reconcile
    assert "ownerSessionId:ownerSid" in reconcile
    assert "commandReconcileId" not in reconcile


@pytest.mark.parametrize("scenario", ["new-chat", "profile-switch", "new-chat-reject", "profile-switch-reject"])
@pytest.mark.parametrize("branch", ["idle", "inflight"])
def test_late_command_load_cannot_mutate_new_foreground(scenario: str, branch: str):
    """Both real message-loader branches fail closed after ownership changes."""
    out = _run_node(scenario, branch)
    profile_switch = scenario.startswith("profile-switch")
    expected_profile = "profile-new" if profile_switch else "default"
    expected_sid = "sid-old" if profile_switch else "sid-new"
    expected_message = "profile-new-transcript" if profile_switch else "new-chat-transcript"
    assert out["sid"] == expected_sid
    assert out["profile"] == expected_profile
    assert [m["content"] for m in out["messages"]] == [expected_message]
    assert out["metadataRequested"] is True
    assert out["messagesRequested"] is True


def test_owner_scoped_command_load_still_reloads_on_normal_completion():
    """A valid owner must retain ordinary reconciliation behavior."""
    out = _run_node("normal", "idle")

    assert out["sid"] == "sid-old"
    assert [m["content"] for m in out["messages"]] == ["old-late-transcript"]
    assert out["metadataRequested"] is True
    assert out["messagesRequested"] is True


def test_owner_scoped_command_load_adopts_canonical_continuation():
    """A verified parent-to-child handoff must load and render the child transcript."""
    out = _run_node("continuation", "idle")

    assert out["apiCalls"] == [
        "/api/commands/exec",
        "/api/session?session_id=sid-old&messages=0&resolve_model=0",
        "/api/session?session_id=sid-child&messages=0&resolve_model=0",
        "/api/session?session_id=sid-child&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1",
    ]
    assert out["metadataRequestCount"] == 2
    assert out["messagesRequestCount"] == 1
    assert out["continuationDuringLoad"]["loading"] == "sid-child"
    assert "Loading conversation" in out["continuationDuringLoad"]["msgInner"]
    assert out["sid"] == "sid-child"
    assert [m["content"] for m in out["messages"]] == ["child-command-transcript"]
    assert out["loadingSessionId"] is None
    assert out["msgInner"] == ""


def test_loader_threads_owner_identity_to_both_calls_and_checks_each_await():
    """Keep the two phase-2 call sites and loader pre/post ownership checks visible."""
    body = _extract_function(SESSIONS, "loadSession")
    assert body.count("ownerSessionId:_expectedLoadSessionId") == 2
    assert body.count("ownerProfile:_expectedLoadProfile") == 2
    ensure = ENSURE_MESSAGES
    assert "ownerSessionId" in ensure
    assert ensure.count("if (!_ownsLoad()) return;") >= 2
    assert "_ownerSessionIsCurrent" in ensure
