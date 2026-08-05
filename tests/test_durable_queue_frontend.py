"""Small Node-backed behavior checks for the durable queue frontend seam."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "static/ui.js").read_text(encoding="utf-8")
COMMANDS = (ROOT / "static/commands.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static/messages.js").read_text(encoding="utf-8")
SESSIONS = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function(source, name):
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name} not found"
    brace = source.find("{", start)
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


def test_legacy_browser_queue_restores_for_review_without_auto_submit():
    restore = _block(
        SESSIONS,
        "// Restore any queued message that survived page refresh or tab restore.",
        "// Reconstruct tool calls from message metadata",
    )
    result = _run(
        f"""
        const S={{messages:[{{role:'assistant',timestamp:1}}]}};
        const input={{value:''}};
        let sends=0;
        let cleared=0;
        let toast='';
        const sid='sid';
        function $(id){{return id==='msg'?input:null;}}
        function queueSessionMessage(){{sends+=1;}}
        function autoResize(){{}}
        function showToast(message){{toast=message;}}
        function _readPersistedSessionQueue(){{return [{{text:'legacy draft',_queued_at:5000}}];}}
        function _clearPersistedSessionQueue(){{cleared+=1;}}
        {restore}
        console.log(JSON.stringify({{text:input.value,sends,cleared,toast}}));
        """
    )
    assert result["text"] == "legacy draft"
    assert result["sends"] == 0
    assert result["cleared"] == 1
    assert "review and send when ready" in result["toast"]
