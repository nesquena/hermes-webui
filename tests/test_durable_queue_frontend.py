"""Small Node-backed behavior checks for the durable queue frontend seam."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "static/ui.js").read_text(encoding="utf-8")
COMMANDS = (ROOT / "static/commands.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static/messages.js").read_text(encoding="utf-8")
SESSIONS = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
I18N = (ROOT / "static/i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function(source, name):
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name} not found"
    paren = source.find("(", start)
    paren_depth = 0
    pos = paren
    while pos < len(source):
        if source[pos] == "(":
            paren_depth += 1
        elif source[pos] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                break
        pos += 1
    brace = source.find("{", pos)
    depth = 1
    pos = brace + 1
    while depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def _run(script):
    if NODE is None:
        pytest.skip("node not available")
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _block(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _send_node_case(case):
    send = _function(MESSAGES, "send")
    return _run(
        f"""
        const A={{session_id:'A',model:'A-model',model_provider:'A-provider',workspace:'/A',profile:'default',title:'A'}};
        const B={{session_id:'B',model:'B-model',model_provider:'B-provider',workspace:'/B',profile:'default',title:'B'}};
        const input={{value:'A prompt'}};
        const S={{session:A,busy:false,pendingFiles:[],messages:[],toolCalls:[],activeStreamId:null,activeProfile:'default'}};
        const INFLIGHT={{}};
        const window={{_defaultMessageMode:'queue',_defaultModel:'',_activeProvider:null}};
        const document={{querySelector:()=>null,getElementById:()=>null}};
        const localStorage={{setItem:()=>{{}},removeItem:()=>{{}}}};
        const history={{replaceState:()=>{{}}}};
        const navigator={{onLine:true}};
        let _sendInProgress=false;
        let _sendInProgressSid=null;
        let _pendingSelections=[];
        let _forcedSkillDirectivePending=null;
        let _approvalSessionId=null;
        let _clarifySessionId=null;
        const queueCalls=[];
        const loadCalls=[];
        const startPayloads=[];
        const attached=[];
        const toasts=[];
        const restores=[];
        let uploadCalls=0;
        function $(id){{return id==='msg'?input:null;}}
        function _composerTextWithPendingSelections(){{return input.value;}}
        function _flushSelectionBlocksToComposer(){{}}
        function _clearStaleBusyStateBeforeSend(){{}}
        function isCompressionUiRunning(){{return false;}}
        function _dismissHandoffHint(){{}}
        function setComposerStatus(){{}}
        function autoResize(){{}}
        function renderTray(){{}}
        function setBusy(value){{S.busy=!!value;}}
        function updateSendBtn(){{}}
        function renderMessages(){{}}
        function clearLiveToolCards(){{}}
        function ensureLiveWorklogShell(){{}}
        function appendThinking(){{}}
        function syncTopbar(){{}}
        function syncModelChip(){{}}
        function renderSessionList(){{return Promise.resolve();}}
        function renderSessionListFromCache(){{}}
        function upsertActiveSessionForLocalTurn(){{}}
        function applySessionTitleUpdate(){{}}
        function startApprovalPolling(){{}}
        function startClarifyPolling(){{}}
        function stopApprovalPolling(){{}}
        function stopClarifyPolling(){{}}
        function hideApprovalCard(){{}}
        function hideClarifyCard(){{}}
        function removeThinking(){{}}
        function clearOptimisticSessionStreaming(){{}}
        function _fetchYoloState(){{}}
        function markInflight(){{}}
        function saveInflightState(){{}}
        function attachLiveStream(sid,streamId){{attached.push([sid,streamId]);}}
        function _runOptionalPreStartUiStep(_label,fn){{fn();}}
        function _runOptionalPostStartUiStep(_label,fn){{fn();}}
        function _chatPayloadModelState(){{return {{model:S.session.model,model_provider:S.session.model_provider}};}}
        function _readPendingSessionModel(){{return null;}}
        function _clearPendingSessionModel(){{}}
        function _restoreComposerDraftAfterFailedSend(...args){{restores.push(args);}}
        function _clearComposerDraft(){{return Promise.resolve();}}
        function _clearComposerAfterQueuedSelectionSend(){{}}
        function _queueErrorMessage(error){{return error&&error.message||'queue failed';}}
        const localized={{}};
        function t(key){{return localized[key]||key;}}
        function showToast(message){{toasts.push(String(message));}}
        function _prepareSlashTurn(){{return Promise.resolve({{kind:'prompt',message:input.value}});}}
        function _queuePayloadForSlashTurn(plan,message,files,modelState,workspace){{return {{text:message,files,model:modelState.model,model_provider:modelState.model_provider,workspace}};}}
        function queueSessionMessage(sid,payload){{queueCalls.push({{sid,payload}});return Promise.resolve({{accepted:true}});}}
        async function loadSession(sid){{loadCalls.push(sid);}}
        function _modelProviderForSend(model){{return S.session.model_provider;}}
        function _writePersistedModelState(){{}}
        function _applyModelToDropdown(){{}}
        function api(_url,options){{
          startPayloads.push(JSON.parse(options.body));
          return Promise.resolve({{stream_id:'A-stream'}});
        }}
        {case}
        {send}
        (async()=>{{
          await runCase();
          console.log(JSON.stringify({{queueCalls,loadCalls,startPayloads,attached,toasts,restores,uploadCalls,messages:S.messages,input:S.session===B?input.value:input.value,pendingFiles:S.pendingFiles,visibleSid:S.session.session_id,visibleModel:S.session.model,visibleProvider:S.session.model_provider,activeStreamId:S.activeStreamId}}));
        }})().catch(error=>{{console.error(error.stack||error);process.exit(1);}});
        """
    )


def _session_guard_node_case(function_name, call, setup):
    function = _function(SESSIONS, function_name)
    return _run(
        f"""
        const input={{value:'draft text'}};
        const S={{session:{{session_id:'A',composer_draft:null}},pendingFiles:[]}};
        let _newSessionInFlight=null;
        let _loadingSessionId=null;
        let _loadSessionGeneration=0;
        const localized={{
          draft_save_new_chat_cancelled:'localized-new-chat-draft-failure',
          draft_save_session_switch_cancelled:'localized-session-switch-draft-failure',
        }};
        const toasts=[];
        function t(key){{return localized[key]||key;}}
        function showToast(message){{toasts.push(String(message));}}
        function $(id){{return id==='msg'?input:null;}}
        function _setNewSessionPending(){{}}
        function _saveComposerDraftNow(){{return Promise.reject(new Error('draft save failed'));}}
        function _rearmActiveSessionStream(){{}}
        {setup}
        {function}
        (async()=>{{
          await {call};
          console.log(JSON.stringify({{toasts,loadingSessionId:_loadingSessionId}}));
        }})().catch(error=>{{console.error(error.stack||error);process.exit(1);}});
        """
    )


def test_guard_toast_i18n_keys_cover_all_sites_and_locales():
    keys = {
        "send_in_other_session": (MESSAGES, 4),
        "send_cancelled_session_changed": (MESSAGES, 1),
        "draft_save_new_chat_cancelled": (SESSIONS, 1),
        "draft_save_session_switch_cancelled": (SESSIONS, 1),
    }
    for key, (source, expected_count) in keys.items():
        assert source.count(f"t('{key}')") == expected_count

    for raw_message in (
        "A send is still finishing in another session.",
        "Send cancelled because the active session changed.",
        "Could not save the current draft. New chat cancelled.",
        "Could not save the current draft. Session switch cancelled.",
    ):
        assert raw_message not in MESSAGES + SESSIONS

    locale_blocks = re.findall(
        r"(?ms)^  (?:[A-Za-z0-9_-]+|'zh-Hant'): \{\n(.*?)(?=^  (?:[A-Za-z0-9_-]+|'zh-Hant'): \{|^};)",
        I18N,
    )
    assert len(locale_blocks) == 15
    for block in locale_blocks:
        for key in keys:
            assert re.search(rf"(?m)^\s+{re.escape(key)}:", block)


def test_send_other_session_guard_shows_localized_toast():
    result = _send_node_case(
        """
        localized.send_in_other_session='localized-other-session';
        _sendInProgress=true;
        _sendInProgressSid='A';
        S.session=B;
        input.value='B prompt';
        function runCase(){return send();}
        """
    )

    assert result["toasts"] == ["localized-other-session"]
    assert result["queueCalls"] == []
    assert result["startPayloads"] == []


def test_send_active_session_changed_guard_shows_localized_toast():
    result = _send_node_case(
        """
        localized.send_cancelled_session_changed='localized-active-session-changed';
        function uploadPendingFiles(){
          S.session=B;
          return Promise.resolve([]);
        }
        function runCase(){return send();}
        """
    )

    assert result["toasts"] == ["localized-active-session-changed"]
    assert result["queueCalls"] == []
    assert result["startPayloads"] == []


def test_new_chat_draft_save_guard_shows_localized_toast():
    result = _session_guard_node_case(
        "newSession",
        "newSession()",
        "localized.draft_save_new_chat_cancelled='localized-new-chat-draft-failure';",
    )

    assert result["toasts"] == ["localized-new-chat-draft-failure"]


def test_session_switch_draft_save_guard_shows_localized_toast():
    result = _session_guard_node_case(
        "loadSession",
        "loadSession('B')",
        "localized.draft_save_session_switch_cancelled='localized-session-switch-draft-failure';",
    )

    assert result["toasts"] == ["localized-session-switch-draft-failure"]
    assert result["loadingSessionId"] is None


def test_server_hydration_replaces_runtime_queue():
    hydrate = _function(UI, "hydrateSessionQueue")
    result = _run(
        f"""
        const SESSION_QUEUES={{sid:[{{id:'old'}}]}};
        const _queueRenderKeys={{sid:'old-key'}};
        {hydrate}
        hydrateSessionQueue('sid',[{{id:'server',text:'authoritative'}}]);
        console.log(JSON.stringify({{queue:SESSION_QUEUES.sid,key:_queueRenderKeys.sid||null}}));
        """
    )
    assert result == {"queue": [{"id": "server", "text": "authoritative"}], "key": None}


def test_composer_draft_restore_reuses_live_file_handle_after_session_switch():
    helpers = "\n".join(
        [
            *(
                _function(SESSIONS, name)
                for name in (
                    "_composerDraftFileSignature",
                    "_composerDraftFilesForPersist",
                    "_rememberComposerDraftLiveFiles",
                    "_composerDraftPayloadSignature",
                    "_composerDraftHasPayload",
                    "_saveComposerDraftNow",
                )
            ),
            _block(
                SESSIONS,
                "function _restoreComposerDraft(",
                "\n// Clear the saved draft",
            ),
        ]
    )
    result = _run(
        f"""
        let _draftSaveTimer=null;
        let _loadingSessionId='A';
        const _composerDraftKnownPayloadSessions=new Set();
        const _composerDraftLiveFilesBySid=new Map();
        const input={{value:''}};
        const S={{session:{{session_id:'A',composer_draft:null}},pendingFiles:[]}};
        let persisted=null;
        let failSave=false;
        let nullSave=false;
        const timerClears=[];
        function clearTimeout(value){{timerClears.push(value);}}
        function $(id){{return id==='msg'?input:null;}}
        function api(_url,options){{
          if(failSave) return Promise.reject(new Error('save failed'));
          if(nullSave) return Promise.resolve(undefined);
          persisted=JSON.parse(options.body);
          return Promise.resolve({{ok:true}});
        }}
        function _clearComposerDraftRestoreSuppression(){{}}
        function _sessionComposerDraftHasPayload(){{return false;}}
        function _rememberComposerDraftPayloadState(){{}}
        function _isComposerDraftRestoreSuppressed(){{return false;}}
        function autoResize(){{}}
        function updateSendBtn(){{}}
        function renderTray(){{}}
        {helpers}
        Promise.resolve().then(async()=>{{
          const liveFile={{name:'report.pdf',size:42,type:'application/pdf',lastModified:7,liveHandle:true}};
          await _saveComposerDraftNow('A','draft',[liveFile]);
          _restoreComposerDraft({{text:persisted.text,files:persisted.files}},'A');
          const firstSignature=_composerDraftPayloadSignature('',[liveFile]);
          const changedSignature=_composerDraftPayloadSignature('',[{{...liveFile,lastModified:8}}]);
          S.session={{session_id:'B',composer_draft:null}};
          _draftSaveTimer='B-timer';
          timerClears.length=0;
          await _saveComposerDraftNow('A','draft',[liveFile]);
          failSave=true;
          let strictRejected=false;
          try{{await _saveComposerDraftNow('A','draft',[liveFile],true);}}
          catch(_){{strictRejected=true;}}
          failSave=false;
          nullSave=true;
          let strictNullRejected=false;
          try{{await _saveComposerDraftNow('A','draft',[liveFile],true);}}
          catch(_){{strictNullRejected=true;}}
          nullSave=false;
          await _saveComposerDraftNow('A','draft',[liveFile],false,true);
          const casFlag=persisted.if_empty===true;
          console.log(JSON.stringify({{
            persisted:persisted.files,
            restored:S.pendingFiles.length===1&&S.pendingFiles[0]===liveFile,
            liveHandle:S.pendingFiles[0]&&S.pendingFiles[0].liveHandle,
            distinctLastModified:firstSignature!==changedSignature,
            backgroundTimerPreserved:timerClears.length===0,
            strictRejected,
            strictNullRejected,
            casFlag,
          }}));
        }}).catch(error=>{{console.error(error.stack||error);process.exit(1);}});
        """
    )

    assert result["restored"] is True
    assert result["liveHandle"] is True
    assert result["distinctLastModified"] is True
    assert result["backgroundTimerPreserved"] is True
    assert result["strictRejected"] is True
    assert result["strictNullRejected"] is True
    assert result["casFlag"] is True
    assert result["persisted"] == [
        {
            "name": "report.pdf",
            "path": "",
            "size": 42,
            "type": "application/pdf",
            "lastModified": 7,
        }
    ]

    switch_commit = _block(
        SESSIONS,
        "const continuationSid=",
        "S.session=data.session;",
    )
    assert "if(currentSid!==sid)" in switch_commit
    assert "S.pendingFiles=[]" in switch_commit


def test_send_reentrant_after_session_switch_does_not_queue_visible_draft_to_old_session():
    result = _send_node_case(
        """
        let releaseUpload;
        function uploadPendingFiles(){
          uploadCalls++;
          return new Promise(resolve=>{releaseUpload=()=>resolve([{name:'a.txt',path:'/A/a.txt'}]);});
        }
        function runCase(){
          const first=send();
          return Promise.resolve().then(async()=>{
            S.session=B;
            S.pendingFiles=[{name:'b.txt',native:true}];
            input.value='B draft';
            await send();
            const before={text:input.value,files:[...S.pendingFiles]};
            releaseUpload();
            await first;
            if(before.text!==input.value||JSON.stringify(before.files)!==JSON.stringify(S.pendingFiles)) throw new Error('visible draft changed');
          });
        }
        """
    )

    assert result["queueCalls"] == []
    assert result["startPayloads"] == []
    assert result["attached"] == []
    assert result["messages"] == []
    assert result["uploadCalls"] == 1
    assert result["visibleSid"] == "B"
    assert result["pendingFiles"] == [{"name": "b.txt", "native": True}]
    assert result["input"] == "B draft"


def test_file_only_upload_failure_restores_owner_draft_and_files():
    result = _send_node_case(
        """
        S.pendingFiles=[{name:'only.txt',native:true}];
        input.value='';
        function uploadPendingFiles(){uploadCalls++;return Promise.reject(new Error('upload failed'));}
        function runCase(){return send();}
        """
    )

    assert result["uploadCalls"] == 1
    assert result["startPayloads"] == []
    assert len(result["restores"]) == 1
    assert result["restores"][0][0] == ""
    assert result["restores"][0][1] == [{"name": "only.txt", "native": True}]
    assert result["restores"][0][2] == "A"


def test_failed_send_merges_owner_files_with_new_visible_draft():
    restore = _function(MESSAGES, "_restoreComposerDraftAfterFailedSend")
    result = _run(
        f"""
        const oldFile={{name:'old.txt'}};
        const newFile={{name:'new.txt'}};
        const input={{value:'new draft'}};
        const S={{session:{{session_id:'A'}},pendingFiles:[newFile]}};
        const saves=[];
        function $(id){{return id==='msg'?input:null;}}
        function autoResize(){{}}
        function updateSendBtn(){{}}
        function renderTray(){{}}
        function _saveComposerDraftNow(...args){{saves.push(args);return Promise.resolve({{ok:true}});}}
        {restore}
        _restoreComposerDraftAfterFailedSend('',[oldFile],'A',null);
        console.log(JSON.stringify({{
          text:input.value,
          files:S.pendingFiles.map(file=>file.name),
          savedText:saves[0][1],
          savedFiles:saves[0][2].map(file=>file.name),
        }}));
        """
    )

    assert result == {
        "text": "new draft",
        "files": ["old.txt", "new.txt"],
        "savedText": "new draft",
        "savedFiles": ["old.txt", "new.txt"],
    }


def test_send_conflict_after_session_switch_queues_uploaded_owner_payload_once():
    result = _send_node_case(
        """
        function uploadPendingFiles(){
          uploadCalls++;
          return Promise.resolve([{name:'a.txt',path:'/A/a.txt'}]);
        }
        function api(_url,options){
          startPayloads.push(JSON.parse(options.body));
          S.session=B;
          S.pendingFiles=[{name:'b.txt',native:true}];
          input.value='B draft';
          const error=new Error('session already has an active stream');
          error.status=409;
          return Promise.reject(error);
        }
        function runCase(){return send();}
        """
    )

    assert result["uploadCalls"] == 1
    assert result["loadCalls"] == []
    assert len(result["queueCalls"]) == 1
    queued = result["queueCalls"][0]
    assert queued["sid"] == "A"
    assert queued["payload"]["model"] == "A-model"
    assert queued["payload"]["model_provider"] == "A-provider"
    assert queued["payload"]["files"] == [{"name": "a.txt", "path": "/A/a.txt"}]
    assert result["visibleSid"] == "B"
    assert result["pendingFiles"] == [{"name": "b.txt", "native": True}]
    assert result["input"] == "B draft"


def test_send_success_after_session_switch_does_not_write_old_stream_into_visible_session():
    result = _send_node_case(
        """
        function uploadPendingFiles(){
          uploadCalls++;
          return Promise.resolve([{name:'a.txt',path:'/A/a.txt'}]);
        }
        function api(_url,options){
          startPayloads.push(JSON.parse(options.body));
          S.session=B;
          return Promise.resolve({stream_id:'A-stream',effective_model:'A-effective',effective_model_provider:'A-effective-provider'});
        }
        function runCase(){return send();}
        """
    )

    assert result["uploadCalls"] == 1
    assert result["queueCalls"] == []
    assert result["loadCalls"] == []
    assert result["attached"] == [["A", "A-stream"]]
    assert result["visibleSid"] == "B"
    assert result["visibleModel"] == "B-model"
    assert result["visibleProvider"] == "B-provider"
    assert result["activeStreamId"] is None


def test_send_slash_resolution_switch_does_not_start_or_clear_new_session():
    result = _send_node_case(
        """
        const ownerFile={name:'a.txt',native:true};
        const visibleFile={name:'b.txt',native:true};
        S.pendingFiles=[ownerFile];
        input.value='/moa inspect';
        _prepareSlashTurn=async function(){
          S.session=B;
          S.pendingFiles=[visibleFile];
          input.value='B draft';
          return {kind:'turn',message:'inspect',displayText:'/moa inspect',intent:{version:1,command:{name:'moa',transform:'moa'}}};
        };
        function runCase(){return send();}
        """
    )

    assert result["startPayloads"] == []
    assert result["queueCalls"] == []
    assert result["uploadCalls"] == 0
    assert result["visibleSid"] == "B"
    assert result["input"] == "B draft"
    assert result["pendingFiles"] == [{"name": "b.txt", "native": True}]


def test_busy_slash_resolution_switch_does_not_queue_or_clear_new_session():
    result = _send_node_case(
        """
        const ownerFile={name:'a.txt',native:true};
        const visibleFile={name:'b.txt',native:true};
        S.busy=true;
        S.activeStreamId='A-stream';
        S.pendingFiles=[ownerFile];
        input.value='/moa inspect';
        _prepareSlashTurn=async function(){
          S.session=B;
          S.pendingFiles=[visibleFile];
          input.value='B draft';
          return {kind:'turn',message:'inspect',displayText:'/moa inspect',intent:{version:1,command:{name:'moa',transform:'moa'}}};
        };
        function runCase(){return send();}
        """
    )

    assert result["startPayloads"] == []
    assert result["queueCalls"] == []
    assert result["uploadCalls"] == 0
    assert result["visibleSid"] == "B"
    assert result["input"] == "B draft"
    assert result["pendingFiles"] == [{"name": "b.txt", "native": True}]


def test_unchanged_slash_resolution_still_starts_owner_session():
    result = _send_node_case(
        """
        input.value='/moa inspect';
        _prepareSlashTurn=async function(){
          return {kind:'turn',message:'inspect',displayText:'/moa inspect',intent:{version:1,command:{name:'moa',transform:'moa'}}};
        };
        function runCase(){return send();}
        """
    )

    assert len(result["startPayloads"]) == 1
    assert result["startPayloads"][0]["session_id"] == "A"
    assert result["startPayloads"][0]["message"] == "inspect"
    assert result["visibleSid"] == "A"


def test_busy_slash_resolution_uses_captured_owner_workspace():
    result = _send_node_case(
        """
        S.busy=true;
        S.activeStreamId='A-stream';
        input.value='/moa inspect';
        _prepareSlashTurn=async function(){
          S.session.workspace='/changed-after-capture';
          return {kind:'turn',message:'inspect',displayText:'/moa inspect',intent:{version:1,command:{name:'moa',transform:'moa'}}};
        };
        function runCase(){return send();}
        """
    )

    assert len(result["queueCalls"]) == 1
    assert result["queueCalls"][0]["sid"] == "A"
    assert result["queueCalls"][0]["payload"]["workspace"] == "/A"


def test_direct_optimistic_row_keeps_uploaded_attachment_metadata():
    result = _send_node_case(
        """
        S.pendingFiles=[{name:'report.pdf',native:true}];
        function uploadPendingFiles(){
          uploadCalls++;
          return Promise.resolve([{name:'report.pdf',path:'/A/report.pdf',size:42,mime:'application/pdf'}]);
        }
        function runCase(){return send();}
        """
    )

    assert result["uploadCalls"] == 1
    assert result["messages"][0]["attachments"] == [
        {
            "name": "report.pdf",
            "path": "/A/report.pdf",
            "size": 42,
            "mime": "application/pdf",
        }
    ]


def test_resolved_queue_response_moves_cache_off_archived_session():
    hydrate = _function(UI, "hydrateSessionQueue")
    post = _function(UI, "_postSessionQueue")
    result = _run(
        f"""
        const SESSION_QUEUES={{old:[{{id:'optimistic'}}]}};
        const _queueRenderKeys={{old:'old-key'}};
        const _queueMutationChains={{}};
        const badges=[];
        function updateQueueBadge(sid){{badges.push(sid);}}
        function api(){{return Promise.resolve({{session_id:'new',queue:[{{id:'server'}}]}});}}
        {hydrate}
        {post}
        _postSessionQueue('old',{{action:'enqueue'}}).then(response=>{{
          console.log(JSON.stringify({{response,old:SESSION_QUEUES.old||null,new:SESSION_QUEUES.new||null,key:_queueRenderKeys.old||null,badges}}));
        }}).catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    assert result == {
        "response": {"session_id": "new", "queue": [{"id": "server"}]},
        "old": None,
        "new": [{"id": "server"}],
        "key": None,
        "badges": ["old", "new"],
    }


def test_queue_uploads_before_acceptance_and_preserves_browser_file_on_failure():
    queue = _function(UI, "queueSessionMessage")
    result = _run(
        f"""
        const browserFile={{name:'draft.txt',size:3}};
        const SESSION_QUEUE_CAPABILITIES={{sid:'server'}};
        const uploadCalls=[];
        const queueCalls=[];
        async function uploadPendingFiles(options){{uploadCalls.push(options);return [{{name:'draft.txt',path:'/session/draft.txt'}}];}}
        function _postSessionQueue(sid,body){{queueCalls.push({{sid,body}});return Promise.resolve({{accepted:true,queue:[]}});}}
        {queue}
        (async()=>{{
          await queueSessionMessage('sid',{{text:'draft',files:[browserFile],model:'m',model_provider:'p'}});
          let failed=false;
          uploadPendingFiles=async()=>[];
          try{{await queueSessionMessage('sid',{{text:'keep',files:[browserFile]}});}}catch(_err){{failed=true;}}
          console.log(JSON.stringify({{upload:uploadCalls[0],queue:queueCalls[0],failed,browserFile}}));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    assert result["upload"]["clearPending"] is False
    assert result["upload"]["sessionId"] == "sid"
    assert result["queue"]["body"]["files"] == [{"name": "draft.txt", "path": "/session/draft.txt"}]
    assert result["queue"]["body"]["files"][0] != result["browserFile"]
    assert result["failed"] is True
    assert result["browserFile"] == {"name": "draft.txt", "size": 3}


def test_queue_rejects_without_explicit_server_capability_before_upload_or_post():
    queue = _function(UI, "queueSessionMessage")
    result = _run(
        f"""
        const browserFile={{name:'draft.txt',size:3}};
        const input={{value:'keep this draft'}};
        const S={{session:{{session_id:'sid'}},pendingFiles:[browserFile]}};
        const SESSION_QUEUE_CAPABILITIES={{}};
        let uploads=0;
        let posts=0;
        async function uploadPendingFiles(){{uploads+=1;return [{{name:'draft.txt',path:'/uploaded/draft.txt'}}];}}
        function _postSessionQueue(){{posts+=1;return Promise.resolve({{accepted:true}});}}
        {queue}
        (async()=>{{
          let rejected=false;
          try{{await queueSessionMessage('sid',{{text:input.value,files:[browserFile]}});}}
          catch(error){{rejected=error.status===501;}}
          console.log(JSON.stringify({{rejected,uploads,posts,draft:input.value,files:S.pendingFiles}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    assert result == {
        "rejected": True,
        "uploads": 0,
        "posts": 0,
        "draft": "keep this draft",
        "files": [{"name": "draft.txt", "size": 3}],
    }


def test_runner_local_queue_rejects_before_upload_or_local_storage():
    queue = _function(UI, "queueSessionMessage")
    result = _run(
        f"""
        const browserFile={{name:'draft.txt',size:3}};
        const input={{value:'keep this draft'}};
        const S={{session:{{session_id:'sid',queue_capability:'unsupported'}},pendingFiles:[browserFile]}};
        const SESSION_QUEUE_CAPABILITIES={{sid:'unsupported'}};
        let uploads=0;
        let storageWrites=0;
        let apiCalls=0;
        const sessionStorage={{setItem:()=>{{storageWrites+=1;}},removeItem:()=>{{storageWrites+=1;}},getItem:()=>null}};
        const localStorage={{setItem:()=>{{storageWrites+=1;}},removeItem:()=>{{storageWrites+=1;}},getItem:()=>null}};
        async function uploadPendingFiles(){{uploads+=1;return [{{name:'draft.txt',path:'/uploaded/draft.txt'}}];}}
        function _postSessionQueue(){{
          apiCalls+=1;
          const error=new Error('durable WebUI queue is unsupported in runner-local mode');
          error.status=501;
          return Promise.reject(error);
        }}
        {queue}
        (async()=>{{
          let rejected=false;
          try{{await queueSessionMessage('sid',{{text:input.value,files:[browserFile]}});}}
          catch(error){{rejected=error.status===501;}}
          console.log(JSON.stringify({{rejected,uploads,storageWrites,apiCalls,draft:input.value,files:S.pendingFiles}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    assert result == {
        "rejected": True,
        "uploads": 0,
        "storageWrites": 0,
        "apiCalls": 0,
        "draft": "keep this draft",
        "files": [{"name": "draft.txt", "size": 3}],
    }


def test_runner_local_queue_rejects_from_cached_capability_after_session_switch():
    queue = _function(UI, "queueSessionMessage")
    result = _run(
        f"""
        const browserFile={{name:'draft.txt',size:3}};
        const S={{session:{{session_id:'other',queue_capability:'server'}},pendingFiles:[browserFile]}};
        const SESSION_QUEUE_CAPABILITIES={{runner:'unsupported',other:'server'}};
        let uploads=0;
        let apiCalls=0;
        async function uploadPendingFiles(){{uploads+=1;return [];}}
        function _postSessionQueue(){{apiCalls+=1;return Promise.resolve({{accepted:true}});}}
        {queue}
        (async()=>{{
          let rejected=false;
          try{{await queueSessionMessage('runner',{{text:'keep',files:[browserFile]}});}}
          catch(error){{rejected=error.status===501;}}
          console.log(JSON.stringify({{rejected,uploads,apiCalls}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    assert result == {"rejected": True, "uploads": 0, "apiCalls": 0}


def test_runner_local_hydration_ignores_browser_storage_and_uses_server_queue():
    hydrate = _function(UI, "hydrateSessionQueue")
    result = _run(
        f"""
        const SESSION_QUEUES={{}};
        const SESSION_QUEUE_CAPABILITIES={{}};
        const _queueRenderKeys={{}};
        const S={{session:{{session_id:'sid',queue_capability:'unsupported'}}}};
        let browserReads=0;
        function _serverQueueOwned(){{return false;}}
        function _browserQueue(){{browserReads+=1;return [{{id:'legacy'}}];}}
        {hydrate}
        const queue=hydrateSessionQueue('sid',[]);
        console.log(JSON.stringify({{queue,cache:SESSION_QUEUES.sid||null,browserReads}}));
        """
    )
    assert result == {"queue": [], "cache": None, "browserReads": 0}


def test_set_busy_false_never_drains_runtime_queue():
    set_busy = _function(UI, "setBusy")
    result = _run(
        f"""
        const S={{busy:true,session:{{session_id:'sid'}}}};
        let drained=0;
        function updateSendBtn(){{}}
        function _clearActivityElapsedTimer(){{}}
        function setStatus(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function queueSessionMessage(){{drained+=1;}}
        {set_busy}
        setBusy(false);
        console.log(JSON.stringify({{busy:S.busy,drained}}));
        """
    )
    assert result == {"busy": False, "drained": 0}


def test_server_turn_reconciliation_hydrates_queue_and_dedupes_claimed_user_turn():
    reconcile = _function(MESSAGES, "_reconcileServerTurnStarted")
    result = _run(
        f"""
        const S={{session:{{session_id:'sid',pending_user_message:'model prompt with [Attached files: /x]'}},messages:[{{role:'user',content:'tail text',_pending:true}},{{role:'assistant',_live:true,content:'live'}}]}};
        let hydrated=[];
        function hydrateSessionQueue(sid,queue){{hydrated.push({{sid,queue}});}}
        function _mergePendingSessionMessage(session,messages){{
          const item=session.pending_queue_item;
          const candidate={{role:'user',content:item.display_text,attachments:item.files,_pending:true}};
          const duplicate=messages.findIndex(m=>m&&m.role==='user'&&m.content===candidate.content);
          const live=messages.findIndex(m=>m&&m.role==='assistant'&&m._live);
          if(duplicate>=0){{const [row]=messages.splice(duplicate,1);const nextLive=messages.findIndex(m=>m&&m.role==='assistant'&&m._live);messages.splice(nextLive<0?messages.length:nextLive,0,row);}}
          else messages.splice(live<0?messages.length:live,0,candidate);
        }}
        {reconcile}
        const payload={{stream_id:'stream',queue:[{{id:'tail',text:'tail text',display_text:'tail text'}}],queue_item_id:'claimed',queue_item:{{id:'claimed',text:'raw text',display_text:'raw text',files:[{{name:'a.txt'}}]}}}};
        _reconcileServerTurnStarted('sid',payload);
        _reconcileServerTurnStarted('sid',payload);
        console.log(JSON.stringify({{hydrated,pending:S.session.pending_queue_item,attachments:S.session.pending_attachments,messages:S.messages}}));
        """
    )
    assert result["hydrated"] == [
        {"sid": "sid", "queue": [{"id": "tail", "text": "tail text", "display_text": "tail text"}]},
        {"sid": "sid", "queue": [{"id": "tail", "text": "tail text", "display_text": "tail text"}]},
    ]
    assert result["pending"]["id"] == "claimed"
    assert result["attachments"] == [{"name": "a.txt"}]
    assert [message.get("content") for message in result["messages"]].count("raw text") == 1
    assert result["messages"][0]["content"] == "raw text"
    assert [message.get("content") for message in result["messages"]] == [
        "raw text",
        "live",
        "tail text",
    ]


def test_server_turn_reconciliation_matches_string_and_object_attachment_metadata():
    reconcile = _function(MESSAGES, "_reconcileServerTurnStarted")
    helpers = "\n".join(
        _function(SESSIONS, name)
        for name in (
            "_messageComparableText",
            "_stripAttachedFilesMarker",
            "_stripForcedSkillEnvelope",
            "_normalizeUserTranscriptText",
            "_transcriptAttachmentIdentity",
            "_sameTranscriptMessage",
        )
    )
    result = _run(
        f"""
        function msgContent(message){{return message&&message.content||'';}}
        {helpers}
        function hydrateSessionQueue(){{}}
        function renderMessages(){{}}
        function runCase(serverName){{
          const optimistic={{role:'user',content:'inspect',attachments:['report.pdf'],_pending:true}};
          const S={{session:{{session_id:'sid'}},messages:[optimistic]}};
          function _mergePendingSessionMessage(session,messages){{
            const item=session.pending_queue_item;
            const candidate={{role:'user',content:item.display_text,attachments:item.files,_pending:true,queue_item_id:item.id}};
            if(!messages.some(message=>_sameTranscriptMessage(message,candidate))) messages.push(candidate);
            return true;
          }}
          {reconcile}
          _reconcileServerTurnStarted('sid',{{
            queue_item_id:'claimed-'+serverName,
            queue_item:{{id:'claimed-'+serverName,text:'inspect',display_text:'inspect',files:[{{name:serverName,path:'/safe/'+serverName,size:12}}]}}
          }});
          return {{ids:S.messages.map(message=>message.queue_item_id||null),count:S.messages.length}};
        }}
        console.log(JSON.stringify({{matching:runCase('report.pdf'),different:runCase('other.pdf')}}));
        """
    )
    assert result == {
        "matching": {"ids": ["claimed-report.pdf"], "count": 1},
        "different": {"ids": [None, "claimed-other.pdf"], "count": 2},
    }


def test_server_turn_reconciliation_moves_optimistic_tail_after_claimed_turn():
    reconcile = _function(MESSAGES, "_reconcileServerTurnStarted")
    result = _run(
        f"""
        const S={{session:{{session_id:'sid'}},messages:[{{role:'user',content:'tail text',_pending:true}}]}};
        function hydrateSessionQueue(){{}}
        function _mergePendingSessionMessage(session,messages){{
          const item=session.pending_queue_item;
          const duplicate=messages.findIndex(m=>m&&m.role==='user'&&m.content===item.display_text);
          if(duplicate>=0) return false;
          messages.push({{role:'user',content:item.display_text,attachments:item.files,_pending:true}});
          return true;
        }}
        function renderMessages(){{}}
        {reconcile}
        _reconcileServerTurnStarted('sid',{{
          queue:[{{id:'tail',display_text:'tail text'}}],
          queue_item:{{id:'claimed',display_text:'claimed text',files:[]}}
        }});
        console.log(JSON.stringify(S.messages.map(message=>message.content)));
        """
    )
    assert result == ["claimed text", "tail text"]


def test_interrupt_waits_for_acceptance_and_restores_input_on_failure():
    interrupt = _function(COMMANDS, "cmdInterrupt")
    result = _run(
        f"""
        let input={{value:''}};
        const files=[{{name:'draft.txt'}}];
        let cancelCalls=0;
        let accepted=false;
        let release;
        const queuedPromise=new Promise(resolve=>{{release=()=>{{accepted=true;resolve({{accepted:true}});}};}});
        const S={{busy:true,activeStreamId:'stream',session:{{session_id:'sid',model:'m'}},pendingFiles:files}};
        function $(id){{return id==='msg'?input:null;}}
        function t(key){{return key;}}
        function showToast(){{}}
        function _clearAcceptedQueueCommandDraft(){{}}
        function _restoreQueueCommandDraft(sid,text){{input.value=text;}}
        function queueSessionMessage(){{return queuedPromise;}}
        async function cancelStream(){{cancelCalls+=1; if(!accepted) throw new Error('cancelled too early'); return true;}}
        {interrupt}
        (async()=>{{
          const pending=cmdInterrupt('next');
          await Promise.resolve();
          const before=cancelCalls;
          release();
          await pending;
          const acceptedCancel=cancelCalls;
          input.value='';
          accepted=false;
          queueSessionMessage=async()=>{{throw new Error('queue unavailable');}};
          await cmdInterrupt('restore me');
          console.log(JSON.stringify({{before,acceptedCancel,restored:input.value,files:S.pendingFiles.length,cancelCalls}}));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    assert result == {"before": 0, "acceptedCancel": 1, "restored": "restore me", "files": 1, "cancelCalls": 1}


def test_slash_queue_payload_captures_model_and_provider():
    queue = _function(COMMANDS, "cmdQueue")
    interrupt = _function(COMMANDS, "cmdInterrupt")
    result = _run(
        f"""
        const S={{busy:true,activeStreamId:'stream',session:{{session_id:'sid',model:'session-model',model_provider:'session-provider'}},pendingFiles:[]}};
        const calls=[];
        function $(id){{return id==='modelSelect'?{{value:'dropdown-model'}}:null;}}
        function t(key){{return key;}}
        function showToast(){{}}
        function _chatPayloadModelState(){{return {{model:'resolved-model',model_provider:'resolved-provider'}};}}
        function _clearAcceptedQueueCommandDraft(){{}}
        function _restoreQueueCommandDraft(){{}}
        async function queueSessionMessage(sid,body){{calls.push({{sid,body}});return {{accepted:true}};}}
        async function cancelStream(){{return true;}}
        {queue}
        {interrupt}
        (async()=>{{await cmdQueue('queued');await cmdInterrupt('interrupted');console.log(JSON.stringify(calls));}})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    assert [call["body"]["model"] for call in result] == ["resolved-model", "resolved-model"]
    assert [call["body"]["model_provider"] for call in result] == ["resolved-provider", "resolved-provider"]


def test_accepted_queue_cannot_clear_a_newly_visible_session():
    clear = _function(MESSAGES, "_clearComposerAfterQueuedSelectionSend")
    command_clear = _function(COMMANDS, "_clearAcceptedQueueCommandDraft")
    result = _run(
        f"""
        const input={{value:'new session draft'}};
        const newFile={{name:'new.txt'}};
        const oldFile={{name:'old.txt'}};
        const S={{session:{{session_id:'new'}},pendingFiles:[newFile]}};
        const cleared=[];
        const saved=[];
        function $(id){{return id==='msg'?input:null;}}
        const document={{getElementById:()=>input}};
        function _clearComposerDraft(sid,text,files){{cleared.push({{sid,text,files}});}}
        function _saveComposerDraftNow(sid,text,files){{saved.push({{sid,text,files}});}}
        function _clearPendingSelections(){{throw new Error('must not clear new session selections');}}
        function autoResize(){{}}
        function renderTray(){{}}
        {clear}
        {command_clear}
        _clearComposerAfterQueuedSelectionSend('old','old draft',[oldFile]);
        _clearAcceptedQueueCommandDraft('old','old draft',[oldFile]);
        console.log(JSON.stringify({{input:input.value,files:S.pendingFiles,cleared,saved}}));
        """
    )
    assert result["input"] == "new session draft"
    assert result["files"] == [{"name": "new.txt"}]
    assert [entry["sid"] for entry in result["cleared"]] == ["old", "old"]
    assert result["saved"] == []


def test_queue_chip_uses_display_text_and_edit_payload_never_leaks_internal_text():
    render = _function(UI, "_renderQueueChips")
    result = _run(
        f"""
        function classList(){{
          const values=new Set();
          return {{add:()=>{{}},remove:()=>{{}},contains:value=>values.has(value),toggle:(value,force)=>{{if(force===undefined? !values.has(value):force) values.add(value); else values.delete(value);}}}};
        }}
        class Node{{
          constructor(tag){{this.tagName=tag;this.children=[];this.classList=classList();this.style={{}};this.attributes={{}};this.textContent='';this._innerHTML='';}}
          appendChild(child){{this.children.push(child);child.parentNode=this;return child;}}
          setAttribute(name,value){{this.attributes[name]=String(value);}}
          getAttribute(name){{return this.attributes[name]||null;}}
          contains(node){{return node===this||this.children.some(child=>child.contains(node));}}
          get childNodes(){{return this.children;}}
          getBoundingClientRect(){{return {{height:12}};}}
          set innerHTML(value){{this._innerHTML=String(value);if(!value)this.children=[];}}
          get innerHTML(){{return this._innerHTML;}}
        }}
        const nodes={{queueCard:new Node('div'),queueChips:new Node('div'),messages:new Node('div')}};
        const document={{activeElement:null,createElement:tag=>new Node(tag),getElementById:id=>nodes[id]||null}};
        const SESSION_QUEUES={{sid:[{{id:'item',text:'RAW INTERNAL EXPANSION',display_text:'/moa inspect',files:[{{name:'queued.txt'}}],model:'gpt-4o'}}]}};
        const _queueRenderKeys={{}};const _queueCollapsed={{}};let _queueRenderEpoch=0;
        const edits=[];
        function _getSessionQueue(){{return SESSION_QUEUES.sid;}}
        function mutateSessionQueue(sid,action,extra){{edits.push({{sid,action,extra}});return Promise.resolve();}}
        function updateQueueBadge(){{}}
        function _updateQueuePill(){{}}
        function li(){{return '';}}
        function t(){{return 'queued';}}
        function scrollIfPinned(){{}}
        function setTimeout(fn){{fn();return 1;}}
        {render}
        _renderQueueChips('sid');
        const row=nodes.queueChips.children.find(child=>child.className==='queue-card-row');
        const message=row.children[1];
        const initial=message.textContent;
        message.onfocus();message.textContent='  /moa inspect  ';message.onblur();
        const noOpEdits=edits.length;
        message.textContent='  /moa edited  ';message.onblur();
        console.log(JSON.stringify({{initial,noOpEdits,edits,rawVisible:initial.includes('RAW INTERNAL EXPANSION'),rawSent:JSON.stringify(edits).includes('RAW INTERNAL EXPANSION')}}));
        """
    )
    assert result == {
        "initial": "/moa inspect",
        "noOpEdits": 0,
        "edits": [{"sid": "sid", "action": "edit", "extra": {"item_id": "item", "text": "/moa edited"}}],
        "rawVisible": False,
        "rawSent": False,
    }


@pytest.mark.parametrize("branch", ["reentrant", "interrupt", "queue"])
def test_send_queue_toasts_preview_display_text_for_all_acceptance_branches(branch):
    plan_kind = "turn" if branch == "reentrant" else "prompt"
    setup = """
    input.value='/moa inspect';
    _prepareSlashTurn=async function(){return {kind:'%s',message:'INTERNAL EXPANSION',displayText:'/moa inspect',intent:{version:1,command:{name:'moa',transform:'moa'}}};};
    _queuePayloadForSlashTurn=function(plan,message,files,modelState,workspace){return {text:'INTERNAL EXPANSION',display_text:'/moa inspect',files,model:modelState.model,model_provider:modelState.model_provider,workspace,intent:plan.intent};};
    function runCase(){return send();}
    """ % plan_kind
    if branch == "reentrant":
        setup = "_sendInProgress=true;_sendInProgressSid='A';\n" + setup
    else:
        setup = (
            "S.busy=true;S.activeStreamId=null;"
            f"window._defaultMessageMode='{branch}';\n"
            + setup
        )
    result = _send_node_case(setup)
    assert result["queueCalls"][0]["payload"]["text"] == "INTERNAL EXPANSION"
    assert result["queueCalls"][0]["payload"]["display_text"] == "/moa inspect"
    assert result["toasts"] == ['Queued: "/moa inspect"']
    assert "INTERNAL EXPANSION" not in result["toasts"][0]
