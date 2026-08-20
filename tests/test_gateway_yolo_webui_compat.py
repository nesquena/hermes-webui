"""WebUI-owned YOLO compatibility tests for gateway Runs API chat.

The current Runs API can answer one approval but cannot toggle session YOLO.
These tests pin WebUI's compatibility behavior without relying on an Agent
branch that accepts extra request fields.
"""

import json
import pathlib
import shutil
import subprocess
import threading
import urllib.parse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
from api.runner_client import RunnerClientError

try:
    from tools.approval import disable_session_yolo, is_session_yolo_enabled

    APPROVAL_AVAILABLE = True
except ImportError:
    APPROVAL_AVAILABLE = False


def _js_block(source, start_marker, end_marker):
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_approval_card_yolo_resumes_current_prompt_with_one_webui_request():
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[];",
        "const S={session:{session_id:'browser-session'}};",
        "let _approvalSessionId='browser-session';",
        "let _approvalCurrentId='approval-1';",
        "let _loadSessionGeneration=1;",
        "let _approvalResponding=null;",
        "let _approvalClearedOwner=null;",
        "let _approvalDisplayedOwner={sid:'browser-session',approvalId:'approval-1',runId:'run-1',mirrorToken:'mirror-1'};",
        "let _yoloEnabled=false;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        "const api=async(path,opts={})=>{calls.push([path,JSON.parse(opts.body||'{}')]);return {ok:true,yolo_enabled:true};};",
        "const $=()=>({disabled:false,classList:{contains:v=>v==='visible',add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{}; const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _applyApprovalYoloProjection(", "\nfunction toggleApprovalCardCollapsed"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        extract("toggleYoloFromApproval", "\n// ── Approval polling"),
        "(async()=>{",
        " const ok=await toggleYoloFromApproval();",
        " if(!ok) throw new Error('action failed');",
        " // #7091: respondApproval first reconciles the transport id against the",
        " // authoritative head (best-effort GET), then issues exactly ONE respond POST.",
        " if(calls.length!==2) throw new Error('expected reconcile GET + one respond '+JSON.stringify(calls));",
        " if(calls[0][0]!=='/api/approval/pending?session_id=browser-session') throw new Error('expected reconcile GET first '+JSON.stringify(calls[0]));",
        " const [path,body]=calls[1];",
        " if(path!=='/api/approval/respond') throw new Error('wrong endpoint '+path);",
        " if(JSON.stringify(body)!==JSON.stringify({session_id:'browser-session',choice:'once',approval_id:'approval-1',run_id:'run-1',mirror_token:'mirror-1',yolo:true})) throw new Error('wrong body '+JSON.stringify(body));",
        " if(!_yoloEnabled) throw new Error('UI state not enabled');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_approval_card_yolo_marks_skip_all_busy_while_request_is_pending():
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    script = "\n".join([
        "const classes=()=>{const values=new Set();return {add:v=>values.add(v),remove:v=>values.delete(v),contains:v=>values.has(v)};};",
        "const buttons=Object.fromEntries(['approvalBtnOnce','approvalBtnSession','approvalBtnAlways','approvalBtnDeny','approvalSkipAll'].map(id=>[id,{id,disabled:false,classList:classes()}]));",
        "const card={classList:{contains:v=>v==='visible'}};",
        "const $=id=>id==='approvalCard'?card:buttons[id];",
        "const S={session:{session_id:'browser-session'}};",
        "let _loadSessionGeneration=1; let _approvalSessionId='browser-session'; let _approvalCurrentId='approval-1';",
        "let _approvalResponding=null; let _approvalClearedOwner=null; let _approvalDisplayedOwner={sid:'browser-session',approvalId:'approval-1',runId:'',mirrorToken:''}; let _yoloEnabled=false; let finishRequest;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1'}}]]);",
        "const api=async()=>await new Promise(resolve=>{finishRequest=resolve;});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{}; const _updateYoloPill=()=>{};",
        "const _unmarkApprovalDismissed=()=>{}; const _clearApprovalPendingForSession=()=>{}; const hideApprovalCard=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction showApprovalForSession"),
        _js_block(messages_js, "function _applyApprovalYoloProjection(", "\nfunction toggleApprovalCardCollapsed"),
        _js_block(messages_js, "async function respondApproval(", "\nfunction startApprovalPolling"),
        _js_block(messages_js, "async function toggleYoloFromApproval(", "\n// ── Approval polling"),
        "(async()=>{",
        " const action=toggleYoloFromApproval();",
        " if(!buttons.approvalSkipAll.disabled) throw new Error('clicked Skip all stayed enabled');",
        " if(!buttons.approvalSkipAll.classList.contains('loading')) throw new Error('clicked Skip all lacks loading state');",
        " if(buttons.approvalBtnOnce.classList.contains('loading')) throw new Error('Allow once incorrectly shows loading state');",
        " for(const button of Object.values(buttons)){if(!button.disabled) throw new Error(button.id+' stayed enabled');}",
        " finishRequest({ok:true,yolo_enabled:true});",
        " if(!(await action)) throw new Error('action failed');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_card_yolo_drains_all_already_parked_local_approvals(monkeypatch):
    from api import routes
    from tools.approval import _ApprovalEntry

    sid = "webui-card-yolo-drain-local"
    approvals = [
        {"approval_id": "approval-local-1", "command": "first"},
        {"approval_id": "approval-local-2", "command": "second"},
    ]
    entries = [_ApprovalEntry(dict(approval)) for approval in approvals]
    response = {}
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=None))
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)

    with routes._lock:
        routes._pending[sid] = [dict(approval) for approval in approvals]
        routes._gateway_queues[sid] = list(entries)

    try:
        routes._handle_approval_respond(
            object(),
            {
                "session_id": sid,
                "choice": "once",
                "approval_id": "approval-local-1",
                "yolo": True,
            },
        )

        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert response["payload"]["yolo_enabled"] is True
        assert [entry.result for entry in entries] == ["once", "once"]
        assert all(entry.event.is_set() for entry in entries)
        with routes._lock:
            assert sid not in routes._pending
            assert sid not in routes._gateway_queues
    finally:
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_card_yolo_drains_all_run_backed_and_local_approvals(monkeypatch):
    from api import route_approvals, routes
    from tools.approval import _ApprovalEntry

    sid = "webui-card-yolo-drain-mixed"
    run_approvals = [
        {
            "approval_id": "approval-run-1",
            "run_id": "run-drain-1",
            "command": "remote first",
            "_gateway_agent_identity_v1": True,
        },
        {
            "approval_id": "approval-run-2",
            "run_id": "run-drain-2",
            "command": "remote second",
            "_gateway_agent_identity_v1": True,
        },
    ]
    local_approval = {"approval_id": "approval-local", "command": "local third"}
    local_entry = _ApprovalEntry(dict(local_approval))
    response = {}
    relays = []
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    def fake_respond(_self, run_id, approval_id, choice):
        relays.append((run_id, approval_id, choice))
        return {"resolved": 1}

    for approval in run_approvals:
        route_approvals.submit_gateway_pending_mirror(sid, dict(approval))
    with routes._lock:
        routes._gateway_queues[sid] = [local_entry]
        routes._pending[sid].append(dict(local_approval))
    first_mirror = route_approvals.gateway_pending_mirror(
        sid,
        approval_id="approval-run-1",
        run_id="run-drain-1",
    )
    assert first_mirror is not None
    assert first_mirror.get("_gateway_mirror_token")

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)

    try:
        routes._handle_approval_respond(
            object(),
            {
                "session_id": sid,
                "choice": "once",
                "approval_id": first_mirror["approval_id"],
                "run_id": first_mirror["run_id"],
                "mirror_token": first_mirror["_gateway_mirror_token"],
                "yolo": True,
            },
        )

        assert response["status"] == 200
        assert response["payload"] == {
            "ok": True,
            "choice": "once",
            "relayed": True,
            "yolo_enabled": True,
        }
        assert relays == [
            ("run-drain-1", "approval-run-1", "once"),
            ("run-drain-2", "approval-run-2", "once"),
        ]
        assert local_entry.result == "once"
        assert local_entry.event.is_set()
        with routes._lock:
            assert sid not in routes._pending
            assert sid not in routes._gateway_queues
    finally:
        disable_session_yolo(sid)
        for approval in run_approvals:
            route_approvals.retire_gateway_pending_mirror(sid, run_id=approval["run_id"])
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
@pytest.mark.parametrize("enable_yolo", [False, True])
def test_stale_card_run_owner_cannot_rebind_to_current_run(monkeypatch, enable_yolo):
    from api import route_approvals, routes
    from tools.approval import _ApprovalEntry

    sid = "webui-card-stale-run-owner"
    stream_id = "stream-current-owner"
    current_run_id = "run-current-owner"
    approval_id = "gateway-reused-approval-id"
    response = {}
    relays = []
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    def fake_respond(_self, run_id, got_approval_id, choice):
        relays.append((run_id, got_approval_id, choice))
        return {"resolved": 1}

    current = {
        "command": "current command",
        "approval_id": approval_id,
        "run_id": current_run_id,
        "_gateway_agent_identity_v1": True,
    }
    current_entry = _ApprovalEntry(current)
    with routes._lock:
        routes._gateway_queues[sid] = [current_entry]
    route_approvals.submit_gateway_pending_mirror(sid, current)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=stream_id))
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    gateway_chat._STREAM_RUN_IDS[stream_id] = current_run_id

    try:
        body: dict[str, object] = {
            "session_id": sid,
            "choice": "once",
            "approval_id": approval_id,
            "run_id": "run-stale-owner",
            "mirror_token": "mirror-stale-owner",
        }
        if enable_yolo:
            body["yolo"] = True
        routes._handle_approval_respond(object(), body)

        assert response["status"] == 409
        assert response["payload"]["ok"] is False
        assert response["payload"]["code"] == "gateway_run_unavailable"
        assert relays == []
        assert not current_entry.event.is_set()
        assert route_approvals.gateway_pending_mirror(
            sid, approval_id=approval_id, run_id=current_run_id
        ) is not None
    finally:
        disable_session_yolo(sid)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=current_run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_approval_card_yolo_uses_authoritative_disabled_response():
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const toasts=[]; let pillUpdates=0;",
        "const S={session:{session_id:'browser-session'}};",
        "let _approvalSessionId='browser-session';",
        "let _approvalCurrentId='approval-1';",
        "let _loadSessionGeneration=1;",
        "let _approvalResponding=null;",
        "let _approvalClearedOwner=null;",
        "let _approvalDisplayedOwner={sid:'browser-session',approvalId:'approval-1',runId:'',mirrorToken:''};",
        "let _yoloEnabled=true;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1'}}]]);",
        "const api=async()=>({ok:true,yolo_enabled:false});",
        "const $=()=>({disabled:false,classList:{contains:v=>v==='visible',add(){},remove(){}}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{}; const _updateYoloPill=()=>{pillUpdates+=1;};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _applyApprovalYoloProjection(", "\nfunction toggleApprovalCardCollapsed"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        extract("toggleYoloFromApproval", "\n// ── Approval polling"),
        "(async()=>{",
        " const ok=await toggleYoloFromApproval();",
        " if(!ok) throw new Error('approval action failed');",
        " if(_yoloEnabled!==false) throw new Error('authoritative disabled state was ignored');",
        " if(pillUpdates!==1) throw new Error('pill was not updated exactly once');",
        " if(JSON.stringify(toasts)!==JSON.stringify(['yolo_disabled'])) throw new Error('wrong toast '+JSON.stringify(toasts));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_failed_approval_relay_applies_authoritative_yolo_and_restores_card():
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(start_marker, end_marker):
        start = messages_js.index(start_marker)
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const toasts=[]; let pillUpdates=0; let cardRenders=0; let statuses=[];",
        "const S={session:{session_id:'browser-session'}};",
        "let _approvalSessionId='browser-session';",
        "let _approvalCurrentId='approval-1';",
        "let _loadSessionGeneration=1;",
        "let _approvalResponding=null;",
        "let _approvalClearedOwner=null;",
        "let _approvalDisplayedOwner={sid:'browser-session',approvalId:'approval-1',runId:'',mirrorToken:''};",
        "let _yoloEnabled=false;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1'}}]]);",
        "const api=async()=>{const e=new Error('relay failed');e.status=502;e.body=JSON.stringify({ok:false,error:'relay failed',yolo_enabled:true});throw e;};",
        "const $=()=>({disabled:false,classList:{contains:v=>v==='visible',add(){},remove(){}}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=msg=>statuses.push(msg);",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{}; const _updateYoloPill=()=>{pillUpdates+=1;};",
        "const _approvalPromptBelongsToActiveSession=()=>true;",
        "const _renderPendingApprovalForActiveSession=()=>{cardRenders+=1;};",
        extract("function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        extract("function _restoreFailedApprovalResponse(", "\nfunction toggleApprovalCardCollapsed"),
        extract("async function respondApproval(", "\nfunction startApprovalPolling"),
        extract("async function toggleYoloFromApproval(", "\n// ── Approval polling"),
        "(async()=>{",
        " const ok=await toggleYoloFromApproval();",
        " if(ok) throw new Error('failed relay reported success');",
        " if(_yoloEnabled!==true) throw new Error('authoritative enabled state was ignored');",
        " if(pillUpdates!==1) throw new Error('pill was not updated exactly once');",
        " if(cardRenders!==1) throw new Error('approval card was not restored');",
        " if(JSON.stringify(toasts)!==JSON.stringify(['relay failed'])) throw new Error('wrong toast '+JSON.stringify(toasts));",
        " if(JSON.stringify(statuses)!==JSON.stringify(['relay failed'])) throw new Error('wrong status '+JSON.stringify(statuses));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_yolo_command_resumes_visible_approval_through_same_atomic_action():
    commands_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "commands.js").read_text()
    start = commands_js.index("async function cmdYolo(")
    body = commands_js[start:commands_js.index("\n// ── Branch / fork command", start)]
    script = "\n".join([
        "const calls=[];",
        "const S={session:{session_id:'browser-session'}};",
        "let _loadSessionGeneration=1;",
        "let _approvalSessionId='browser-session';",
        "let _approvalCurrentId='approval-1';",
        "let _yoloEnabled=false;",
        "let atomicCalls=0;",
        "const api=async(path,opts={})=>{calls.push([path,opts]);return {yolo_enabled:false};};",
        "const toggleYoloFromApproval=async()=>{atomicCalls+=1;return true;};",
        "const $=()=>({classList:{contains:()=>true}});",
        "const _captureApprovalResponseOwner=()=>({sid:'browser-session',generation:1,approvalId:'approval-1'});",
        "const t=k=>k; const showToast=()=>{}; const _updateYoloPill=()=>{}; const hideApprovalCard=()=>{};",
        body,
        "(async()=>{",
        " await cmdYolo();",
        " if(atomicCalls!==1) throw new Error('atomic action not used');",
        " if(calls.length!==1||!calls[0][0].startsWith('/api/session/yolo?')) throw new Error('unexpected requests '+JSON.stringify(calls));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_yolo_command_uses_authoritative_post_response_state():
    commands_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "commands.js").read_text()
    start = commands_js.index("async function cmdYolo(")
    body = commands_js[start:commands_js.index("\n// ── Branch / fork command", start)]
    script = "\n".join([
        "const calls=[]; const toasts=[]; let pillUpdates=0; let cardHides=0;",
        "const S={session:{session_id:'browser-session'}};",
        "let _loadSessionGeneration=1;",
        "let _approvalSessionId=null; let _approvalCurrentId=null; let _yoloEnabled=false;",
        "const api=async(path,opts={})=>{calls.push(path);return path.includes('?')?{yolo_enabled:false}:{ok:true,yolo_enabled:false};};",
        "const toggleYoloFromApproval=async()=>{throw new Error('card path must not run');};",
        "const $=()=>({classList:{contains:()=>false}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg);",
        "const _updateYoloPill=()=>{pillUpdates+=1;}; const hideApprovalCard=()=>{cardHides+=1;};",
        body,
        "(async()=>{",
        " await cmdYolo();",
        " if(_yoloEnabled!==false) throw new Error('authoritative disabled state was ignored');",
        " if(pillUpdates!==1) throw new Error('pill was not updated exactly once');",
        " if(cardHides!==0) throw new Error('card hidden despite settled disabled state');",
        " if(JSON.stringify(toasts)!==JSON.stringify(['yolo_disabled'])) throw new Error('wrong toast '+JSON.stringify(toasts));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_yolo_command_applies_authoritative_state_from_failed_post():
    commands_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "commands.js").read_text()
    start = commands_js.index("async function cmdYolo(")
    body = commands_js[start:commands_js.index("\n// ── Branch / fork command", start)]
    script = "\n".join([
        "const toasts=[]; let pillUpdates=0; let cardHides=0;",
        "const S={session:{session_id:'browser-session'}};",
        "let _loadSessionGeneration=1;",
        "let _approvalSessionId=null; let _approvalCurrentId=null; let _yoloEnabled=false;",
        "const api=async(path)=>{if(path.includes('?'))return {yolo_enabled:false};const e=new Error('relay busy');e.body=JSON.stringify({error:'relay busy',yolo_enabled:true});throw e;};",
        "const toggleYoloFromApproval=async()=>{throw new Error('card path must not run');};",
        "const $=()=>({classList:{contains:()=>false}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg);",
        "const _updateYoloPill=()=>{pillUpdates+=1;}; const hideApprovalCard=()=>{cardHides+=1;};",
        body,
        "(async()=>{",
        " await cmdYolo();",
        " if(_yoloEnabled!==true) throw new Error('authoritative enabled error state was ignored');",
        " if(pillUpdates!==1) throw new Error('pill was not updated exactly once');",
        " if(cardHides!==0) throw new Error('failed request hid the card');",
        " if(JSON.stringify(toasts)!==JSON.stringify(['YOLO: relay busy'])) throw new Error('wrong toast '+JSON.stringify(toasts));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _run_with_approval_events(monkeypatch, *, yolo_enabled, auto_error=None):
    requests = []
    browser_events = []
    approvals = []
    responses = []

    class Response:
        def __init__(self, *, body=b"", lines=()):
            self.body = body
            self.lines = list(lines)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _size=-1):
            return self.body

        def __iter__(self):
            return iter(self.lines)

    event_lines = [
        b'event: approval.request\n',
        b'data: {"event":"approval.request","approval_id":"approval-1","tool":"terminal","command":"one"}\n',
        b'event: approval.request\n',
        b'data: {"event":"approval.request","approval_id":"approval-2","tool":"terminal","command":"two"}\n',
        b'event: run.completed\n',
        b'data: {"event":"run.completed","output":"done"}\n',
    ]
    responses.extend([
        Response(body=b'{"run_id":"run-1"}'),
        Response(lines=event_lines),
    ])

    def fake_urlopen(req, timeout=0):
        requests.append(req)
        return responses.pop(0)

    def fake_auto(base_url, api_key, run_id, approval_id):
        approvals.append((base_url, api_key, run_id, approval_id))
        if auto_error:
            raise auto_error

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_chat, "update_active_run", lambda *a, **k: None)
    monkeypatch.setattr(gateway_chat, "_publish_gateway_run_id", lambda *a, **k: None)
    if yolo_enabled is not None:
        monkeypatch.setattr(gateway_chat, "_gateway_session_yolo_enabled", lambda _sid: yolo_enabled)
    monkeypatch.setattr(gateway_chat, "_auto_approve_gateway_run", fake_auto)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setattr("api.route_approvals.submit_gateway_pending_mirror", lambda _sid, data: (data, 1))
    monkeypatch.setattr("api.route_approvals.retire_gateway_pending_mirror", lambda *a, **k: None)

    final, _usage = gateway_chat._run_gateway_runs_api_streaming(
        "browser-session",
        "hello",
        "test-model",
        "/tmp",
        "stream-1",
        "http://gateway.local",
        "secret",
        [],
        {},
        put_gateway_event=lambda name, data: browser_events.append((name, data)),
        cancel_event=threading.Event(),
    )
    return final, requests, approvals, browser_events


def test_enabled_gateway_yolo_auto_approves_every_later_prompt(monkeypatch):
    final, requests, approvals, browser_events = _run_with_approval_events(
        monkeypatch, yolo_enabled=True
    )

    assert final == "done"
    assert approvals == [
        ("http://gateway.local", "secret", "run-1", "approval-1"),
        ("http://gateway.local", "secret", "run-1", "approval-2"),
    ]
    assert not [event for event in browser_events if event[0] == "approval"]
    run_body = json.loads(requests[0].data)
    assert "yolo" not in run_body


def test_disabled_gateway_yolo_surfaces_prompts_without_auto_approval(monkeypatch):
    _final, _requests, approvals, browser_events = _run_with_approval_events(
        monkeypatch, yolo_enabled=False
    )

    assert approvals == []
    assert [event[1]["approval_id"] for event in browser_events if event[0] == "approval"] == [
        "approval-1",
        "approval-2",
    ]


def test_gateway_yolo_auto_approval_failure_falls_back_to_visible_card(monkeypatch):
    _final, _requests, approvals, browser_events = _run_with_approval_events(
        monkeypatch,
        yolo_enabled=True,
        auto_error=RunnerClientError("pristine API rejected approval"),
    )

    assert len(approvals) == 2
    assert [event[1]["approval_id"] for event in browser_events if event[0] == "approval"] == [
        "approval-1",
        "approval-2",
    ]


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_inflight_yolo_relay_does_not_auto_approve_later_prompt(monkeypatch):
    from api import route_approvals

    sid = "browser-session"
    disable_session_yolo(sid)
    token = route_approvals.begin_session_yolo_transition(sid)
    try:
        _final, _requests, approvals, browser_events = _run_with_approval_events(
            monkeypatch,
            yolo_enabled=None,
        )

        assert approvals == []
        assert [event[1]["approval_id"] for event in browser_events if event[0] == "approval"] == [
            "approval-1",
            "approval-2",
        ]
    finally:
        route_approvals.finish_session_yolo_transition(sid, token, succeeded=False)
        disable_session_yolo(sid)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_overlapping_yolo_relays_publish_only_confirmed_success():
    from api import route_approvals

    sid = "webui-yolo-overlapping-relays"
    disable_session_yolo(sid)
    first = route_approvals.begin_session_yolo_transition(sid)
    second = route_approvals.begin_session_yolo_transition(sid)
    try:
        assert is_session_yolo_enabled(sid) is False
        route_approvals.finish_session_yolo_transition(sid, first, succeeded=True)
        assert is_session_yolo_enabled(sid) is True
        route_approvals.finish_session_yolo_transition(sid, second, succeeded=False)
        assert is_session_yolo_enabled(sid) is True
    finally:
        route_approvals.finish_session_yolo_transition(sid, first, succeeded=False)
        route_approvals.finish_session_yolo_transition(sid, second, succeeded=False)
        disable_session_yolo(sid)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
@pytest.mark.parametrize("relay_fails", [False, True])
def test_card_yolo_uses_plain_runs_approval_and_rolls_back_on_failure(monkeypatch, relay_fails):
    from api import route_approvals as route_approvals
    from api import routes

    sid = "webui-yolo-route-failure" if relay_fails else "webui-yolo-route-success"
    stream_id = "stream-yolo-route"
    run_id = "run-yolo-route"
    disable_session_yolo(sid)
    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    calls = []
    states_during_relay = []

    def fake_respond(_self, got_run_id, approval_id, choice):
        calls.append((got_run_id, approval_id, choice))
        states_during_relay.append(bool(is_session_yolo_enabled(sid)))
        if relay_fails:
            raise RunnerClientError("relay failed")
        return {"resolved": 1}

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=stream_id))
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    gateway_chat._STREAM_RUN_IDS[stream_id] = run_id

    approval = {
        "command": "touch /tmp/webui-yolo-test",
        "description": "test",
        "approval_id": "approval-route",
        "run_id": run_id,
        "_gateway_agent_identity_v1": True,
    }
    route_approvals.submit_gateway_pending_mirror(sid, approval)
    try:
        routes._handle_approval_respond(
            object(),
            {
                "session_id": sid,
                "choice": "once",
                "approval_id": "approval-route",
                "yolo": True,
            },
        )
        assert calls == [(run_id, "approval-route", "once")]
        assert states_during_relay == [False]
        assert captured["status"] == (502 if relay_fails else 200)
        assert captured["payload"]["ok"] is (not relay_fails)
        assert is_session_yolo_enabled(sid) is (not relay_fails)
        if not relay_fails:
            assert captured["payload"]["yolo_enabled"] is True
    finally:
        disable_session_yolo(sid)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_failed_relay_does_not_undo_concurrent_explicit_yolo_enable(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-yolo-route-concurrent-enable"
    stream_id = "stream-yolo-route-concurrent"
    run_id = "run-yolo-route-concurrent"
    relay_started = threading.Event()
    release_relay = threading.Event()
    response = {}
    disable_session_yolo(sid)

    def fake_respond(_self, _run_id, _approval_id, _choice):
        relay_started.set()
        assert release_relay.wait(timeout=5)
        raise RunnerClientError("relay failed")

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=stream_id))
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    gateway_chat._STREAM_RUN_IDS[stream_id] = run_id
    route_approvals.submit_gateway_pending_mirror(sid, {
        "command": "touch /tmp/webui-yolo-test",
        "description": "test",
        "approval_id": "approval-route-concurrent",
        "run_id": run_id,
        "_gateway_agent_identity_v1": True,
    })

    worker = threading.Thread(target=routes._handle_approval_respond, args=(
        object(),
        {
            "session_id": sid,
            "choice": "once",
            "approval_id": "approval-route-concurrent",
            "yolo": True,
        },
    ))
    try:
        worker.start()
        assert relay_started.wait(timeout=5)
        explicit_enable = getattr(
            route_approvals,
            "set_session_yolo_enabled",
            lambda session_key, _enabled: route_approvals.enable_session_yolo(session_key),
        )
        explicit_enable(sid, True)
        release_relay.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert response["status"] == 502
        assert response["payload"]["ok"] is False
        assert response["payload"]["yolo_enabled"] is True
        assert is_session_yolo_enabled(sid) is True
    finally:
        release_relay.set()
        worker.join(timeout=5)
        disable_session_yolo(sid)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_successful_relay_reports_concurrent_explicit_yolo_disable(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-yolo-route-concurrent-disable"
    stream_id = "stream-yolo-route-concurrent-disable"
    run_id = "run-yolo-route-concurrent-disable"
    relay_started = threading.Event()
    release_relay = threading.Event()
    response = {}
    disable_session_yolo(sid)

    def fake_respond(_self, _run_id, _approval_id, _choice):
        relay_started.set()
        assert release_relay.wait(timeout=5)
        return {"resolved": 1}

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=stream_id))
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    gateway_chat._STREAM_RUN_IDS[stream_id] = run_id
    route_approvals.submit_gateway_pending_mirror(sid, {
        "command": "touch /tmp/webui-yolo-test",
        "description": "test",
        "approval_id": "approval-route-concurrent-disable",
        "run_id": run_id,
        "_gateway_agent_identity_v1": True,
    })

    worker = threading.Thread(target=routes._handle_approval_respond, args=(
        object(),
        {
            "session_id": sid,
            "choice": "once",
            "approval_id": "approval-route-concurrent-disable",
            "yolo": True,
        },
    ))
    try:
        worker.start()
        assert relay_started.wait(timeout=5)
        route_approvals.set_session_yolo_enabled(sid, False)
        release_relay.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert response["payload"]["yolo_enabled"] is False
        assert is_session_yolo_enabled(sid) is False
    finally:
        release_relay.set()
        worker.join(timeout=5)
        disable_session_yolo(sid)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_yolo_post_without_local_card_relays_run_backed_approval(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-yolo-post-poll-lag"
    run_id = "run-yolo-post-poll-lag"
    handler = object()
    response = {}
    calls = []
    disable_session_yolo(sid)

    def fake_respond(_self, got_run_id, approval_id, choice):
        calls.append((got_run_id, approval_id, choice))
        return {"resolved": 1}

    def fake_j(got_handler, data, status=200, extra_headers=None):
        assert got_handler is handler
        response.update(payload=data, status=status)
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid, "enabled": True})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    route_approvals.submit_gateway_pending_mirror(sid, {
        "command": "touch /tmp/webui-yolo-test",
        "description": "test",
        "approval_id": "approval-yolo-post",
        "run_id": run_id,
        "_gateway_agent_identity_v1": True,
    })

    try:
        routes.handle_post(handler, urllib.parse.urlparse("/api/session/yolo"))

        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert response["payload"]["yolo_enabled"] is True
        assert calls == [(run_id, "approval-yolo-post", "once")]
        assert is_session_yolo_enabled(sid) is True
        assert route_approvals.gateway_pending_mirror(sid, run_id=run_id) is None
    finally:
        disable_session_yolo(sid)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_yolo_post_preserves_mirror_while_owned_relay_fails(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-yolo-post-owned-failure"
    run_id = "run-yolo-post-owned-failure"
    approval_id = "approval-yolo-post-owned-failure"
    card_handler = object()
    toggle_handler = object()
    relay_started = threading.Event()
    release_relay = threading.Event()
    toggle_entered = threading.Event()
    responses = {}
    relay_attempts = []
    disable_session_yolo(sid)

    def fake_respond(_self, _run_id, _approval_id, _choice):
        relay_attempts.append((_run_id, _approval_id, _choice))
        relay_started.set()
        assert release_relay.wait(timeout=5)
        raise RunnerClientError("relay failed")

    def fake_read_body(_handler):
        toggle_entered.set()
        return {"session_id": sid, "enabled": True}

    def fake_j(handler, data, status=200, extra_headers=None):
        responses[handler] = {"payload": data, "status": status}
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", fake_read_body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, **_kwargs: SimpleNamespace(active_stream_id=None, profile=None),
    )
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    route_approvals.submit_gateway_pending_mirror(sid, {
        "command": "touch /tmp/webui-yolo-test",
        "description": "test",
        "approval_id": approval_id,
        "run_id": run_id,
        "_gateway_agent_identity_v1": True,
    })
    worker = threading.Thread(target=routes._handle_approval_respond, args=(
        card_handler,
        {"session_id": sid, "choice": "once", "approval_id": approval_id, "yolo": True},
    ))
    toggle_worker = threading.Thread(
        target=lambda: routes.handle_post(
            toggle_handler, urllib.parse.urlparse("/api/session/yolo")
        )
    )

    try:
        worker.start()
        assert relay_started.wait(timeout=5)
        toggle_worker.start()
        assert toggle_entered.wait(timeout=5)
        assert toggle_worker.is_alive()
        assert toggle_handler not in responses

        release_relay.set()
        worker.join(timeout=5)
        toggle_worker.join(timeout=5)
        assert not worker.is_alive()
        assert not toggle_worker.is_alive()
        assert relay_attempts == [
            (run_id, approval_id, "once"),
            (run_id, approval_id, "once"),
        ]
        assert responses[card_handler]["status"] == 502
        assert responses[toggle_handler]["status"] == 502
        assert responses[card_handler]["payload"]["yolo_enabled"] is False
        assert responses[toggle_handler]["payload"]["yolo_enabled"] is False
        assert is_session_yolo_enabled(sid) is False
        assert route_approvals.gateway_pending_mirror(
            sid, approval_id=approval_id, run_id=run_id
        ) is not None
    finally:
        release_relay.set()
        worker.join(timeout=5)
        toggle_worker.join(timeout=5)
        disable_session_yolo(sid)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_yolo_post_keeps_transition_unconfirmed_until_second_mirror_check(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-yolo-post-transition-window"
    handler = object()
    observed_transition_states = []
    observed_gateway_states = []
    response = {}
    mirror = {
        "command": "touch /tmp/webui-yolo-test",
        "description": "test",
        "approval_id": "approval-transition-window",
        "run_id": "run-transition-window",
        "_gateway_mirror": True,
        "_gateway_agent_identity_v1": True,
    }
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    def fake_mirrors(_sid):
        with route_approvals._yolo_transition_lock:
            observed_transition_states.append(bool(route_approvals._yolo_transitions.get(sid)))
        return [dict(mirror)]

    def fake_relay(_sid, _mirror, _choice, *, enable_yolo):
        assert enable_yolo is False
        with route_approvals._yolo_transition_lock:
            observed_transition_states.append(bool(route_approvals._yolo_transitions.get(sid)))
        observed_gateway_states.append(gateway_chat._gateway_session_yolo_enabled(sid))
        return {
            "ok": True,
            "choice": "once",
            "relayed": True,
            "yolo_enabled": False,
        }, 200

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid, "enabled": True})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr(routes, "gateway_pending_mirrors", fake_mirrors)
    monkeypatch.setattr(routes, "_relay_gateway_run_approval", fake_relay)

    try:
        routes.handle_post(handler, urllib.parse.urlparse("/api/session/yolo"))
        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert response["payload"]["yolo_enabled"] is True
        assert observed_transition_states == [True, True]
        assert observed_gateway_states == [False]
        assert is_session_yolo_enabled(sid) is True
    finally:
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_no_run_approval_success_reports_authoritative_yolo_state(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-no-run-authoritative-success"
    response = {}
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    original_enable = route_approvals.enable_session_yolo

    def racing_enable(session_key):
        original_enable(session_key)
        disable_session_yolo(session_key)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=None))
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: True)
    monkeypatch.setattr(routes, "resolve_gateway_pending_local_no_run_mirror", lambda *_a: (True, 1, None, 0))
    monkeypatch.setattr(route_approvals, "enable_session_yolo", racing_enable)

    try:
        routes._handle_approval_respond(
            object(),
            {"session_id": sid, "choice": "once", "approval_id": "approval-no-run", "yolo": True},
        )
        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert response["payload"]["yolo_enabled"] is False
    finally:
        disable_session_yolo(sid)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_local_approval_success_reports_authoritative_yolo_state(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-local-authoritative-success"
    response = {}
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    original_enable = route_approvals.enable_session_yolo

    def racing_enable(session_key):
        original_enable(session_key)
        disable_session_yolo(session_key)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=None))
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(routes, "_resolve_approval_legacy", lambda *_a: True)
    monkeypatch.setattr(route_approvals, "enable_session_yolo", racing_enable)

    try:
        routes._handle_approval_respond(
            object(),
            {"session_id": sid, "choice": "once", "approval_id": "approval-local", "yolo": True},
        )
        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert response["payload"]["yolo_enabled"] is False
    finally:
        disable_session_yolo(sid)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_yolo_post_serializes_post_snapshot_gateway_approval(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-yolo-post-snapshot-handoff"
    handler = object()
    response = {}
    worker_results = []
    workers = []
    auto_approved = []
    approval = {
        "command": "touch /tmp/webui-yolo-test",
        "description": "test",
        "approval_id": "approval-post-snapshot",
        "run_id": "run-post-snapshot",
        "_gateway_mirror": True,
        "_gateway_agent_identity_v1": True,
    }

    class NotifyingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.contended = threading.Event()

        def __enter__(self):
            if not self._lock.acquire(blocking=False):
                self.contended.set()
                self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self._lock.release()

    handoff_lock = NotifyingLock()
    original_finish = routes.finish_session_yolo_transition
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    def racing_finish(session_key, token, *, succeeded):
        worker = threading.Thread(
            target=lambda: worker_results.append(
                gateway_chat._settle_gateway_run_approval(sid, dict(approval), "http://gateway", "")
            )
        )
        worker.start()
        workers.append(worker)
        assert handoff_lock.contended.wait(timeout=1)
        return original_finish(session_key, token, succeeded=succeeded)

    monkeypatch.setattr(route_approvals, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(routes, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(gateway_chat, "_auto_approve_gateway_run", lambda *_a: auto_approved.append(True))
    monkeypatch.setattr(routes, "finish_session_yolo_transition", racing_finish)
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid, "enabled": True})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr(routes, "gateway_pending_mirror", lambda _sid: None)
    monkeypatch.setattr(routes, "reconcile_gateway_pending_mirror_locked", lambda _sid: (None, 0, False))
    monkeypatch.setattr(routes, "resolve_gateway_approval", lambda *_a, **_k: 0)

    try:
        routes.handle_post(handler, urllib.parse.urlparse("/api/session/yolo"))
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
        assert response["status"] == 200
        assert response["payload"] == {"ok": True, "yolo_enabled": True}
        assert worker_results == [(True, None, 0)]
        assert auto_approved == [True]
        assert route_approvals.gateway_pending_mirror(sid) is None
    finally:
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_yolo_drain_serializes_post_drain_local_approval(monkeypatch):
    from api import route_approvals, routes
    from tools.approval import _ApprovalEntry

    sid = "webui-yolo-post-drain-local-handoff"
    response = {}
    worker_results = []
    worker_errors = []
    workers = []
    initial = _ApprovalEntry({"approval_id": "approval-initial", "command": "initial"})
    late = _ApprovalEntry({"command": "late"})

    class NotifyingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.contended = threading.Event()

        def __enter__(self):
            if not self._lock.acquire(blocking=False):
                self.contended.set()
                self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self._lock.release()

    handoff_lock = NotifyingLock()
    original_drain = route_approvals.resolve_gateway_pending_local_all
    disable_session_yolo(sid)
    with routes._lock:
        routes._gateway_queues[sid] = [initial]
        routes._pending[sid] = [dict(initial.data)]

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    def notify_late_local_approval():
        try:
            worker_results.append(
                route_approvals.settle_gateway_pending_local_notification(sid, dict(late.data))
            )
        except BaseException as exc:
            worker_errors.append(exc)

    def paused_after_drain(session_key, choice, reason=None):
        result = original_drain(session_key, choice, reason)
        with routes._lock:
            routes._gateway_queues.setdefault(sid, []).append(late)
        worker = threading.Thread(target=notify_late_local_approval)
        worker.start()
        workers.append(worker)
        assert handoff_lock.contended.wait(timeout=1)
        assert not late.event.is_set()
        return result

    monkeypatch.setattr(route_approvals, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(routes, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(routes, "resolve_gateway_pending_local_all", paused_after_drain)
    monkeypatch.setattr(routes, "j", fake_j)

    try:
        routes._handle_approval_respond(
            object(),
            {
                "session_id": sid,
                "choice": "once",
                "approval_id": "approval-initial",
                "yolo": True,
            },
        )
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()

        assert worker_errors == []
        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert response["payload"]["yolo_enabled"] is True
        assert initial.event.is_set()
        assert initial.result == "once"
        assert late.event.is_set()
        assert late.result == "once"
        assert worker_results == [(True, None, 0)]
        with routes._lock:
            assert sid not in routes._gateway_queues
            assert sid not in routes._pending
    finally:
        for worker in workers:
            worker.join(timeout=1)
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_yolo_disable_linearizes_after_selected_gateway_autoapproval(monkeypatch):
    from api import routes

    sid = "webui-yolo-disable-autoapprove-race"
    approval_started = threading.Event()
    release_approval = threading.Event()
    disable_entered = threading.Event()
    disable_handoff_attempted = threading.Event()
    disable_handoff_acquired = threading.Event()
    events = []
    worker_results = []
    response = {}
    approval = {
        "approval_id": "approval-disable-race",
        "run_id": "run-disable-race",
        "_gateway_agent_identity_v1": True,
    }
    routes.set_session_yolo_enabled(sid, True)
    original_handoff = routes.gateway_yolo_handoff

    @contextmanager
    def observed_disable_handoff(session_key):
        disable_handoff_attempted.set()
        with original_handoff(session_key):
            disable_handoff_acquired.set()
            yield

    def fake_auto_approve(*_args):
        events.append("approval-started")
        approval_started.set()
        assert release_approval.wait(timeout=1)
        events.append("approval-finished")

    def fake_read_body(_handler):
        disable_entered.set()
        return {"session_id": sid, "enabled": False}

    def fake_j(_handler, data, status=200, extra_headers=None):
        events.append("disable-response")
        response.update(payload=data, status=status)
        return data

    monkeypatch.setattr(gateway_chat, "_auto_approve_gateway_run", fake_auto_approve)
    monkeypatch.setattr(routes, "read_body", fake_read_body)
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "gateway_yolo_handoff", observed_disable_handoff)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)

    approval_worker = threading.Thread(
        target=lambda: worker_results.append(
            gateway_chat._settle_gateway_run_approval(sid, dict(approval), "http://gateway", "")
        )
    )
    disable_worker = threading.Thread(
        target=lambda: routes.handle_post(object(), urllib.parse.urlparse("/api/session/yolo"))
    )

    try:
        approval_worker.start()
        assert approval_started.wait(timeout=1)
        disable_worker.start()
        assert disable_entered.wait(timeout=1)
        assert disable_handoff_attempted.wait(timeout=1)
        assert not disable_handoff_acquired.is_set()
        assert "disable-response" not in events
        release_approval.set()
        approval_worker.join(timeout=1)
        disable_worker.join(timeout=1)
        assert not approval_worker.is_alive()
        assert not disable_worker.is_alive()
        assert disable_handoff_acquired.is_set()
        assert worker_results == [(True, None, 0)]
        assert events == ["approval-started", "approval-finished", "disable-response"]
        assert response["payload"] == {"ok": True, "yolo_enabled": False}
        assert is_session_yolo_enabled(sid) is False
    finally:
        release_approval.set()
        disable_session_yolo(sid)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_disable_that_wins_handoff_precedes_enable_drain(monkeypatch):
    from api import routes
    from tools.approval import _ApprovalEntry

    sid = "webui-yolo-disable-before-enable-handoff"
    handoff_lock = threading.Lock()
    enable_handoff_attempted = threading.Event()
    allow_enable_handoff = threading.Event()
    responses = {}
    worker_errors = []
    entry = _ApprovalEntry({"approval_id": "approval-after-disable", "command": "late enable"})

    @contextmanager
    def ordered_handoff(_session_key):
        role = threading.current_thread().name
        if role == "yolo-enable":
            enable_handoff_attempted.set()
            assert allow_enable_handoff.wait(timeout=1)
        with handoff_lock:
            yield

    def fake_j(_handler, data, status=200, extra_headers=None):
        responses[threading.current_thread().name] = {"payload": data, "status": status}
        return data

    def run_enable():
        try:
            routes._handle_approval_respond(
                object(),
                {
                    "session_id": sid,
                    "choice": "once",
                    "approval_id": "approval-after-disable",
                    "yolo": True,
                },
            )
        except BaseException as exc:
            worker_errors.append(exc)

    def run_disable():
        try:
            routes.handle_post(object(), urllib.parse.urlparse("/api/session/yolo"))
        except BaseException as exc:
            worker_errors.append(exc)

    disable_session_yolo(sid)
    with routes._lock:
        routes._gateway_queues[sid] = [entry]
        routes._pending[sid] = [dict(entry.data)]

    monkeypatch.setattr(routes, "gateway_yolo_handoff", ordered_handoff)
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid, "enabled": False})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)

    enable_worker = threading.Thread(target=run_enable, name="yolo-enable")
    disable_worker = threading.Thread(target=run_disable, name="yolo-disable")
    try:
        enable_worker.start()
        assert enable_handoff_attempted.wait(timeout=1)
        disable_worker.start()
        disable_worker.join(timeout=1)
        assert not disable_worker.is_alive()
        assert responses["yolo-disable"]["payload"] == {"ok": True, "yolo_enabled": False}
        assert not entry.event.is_set()

        allow_enable_handoff.set()
        enable_worker.join(timeout=1)
        assert not enable_worker.is_alive()
        assert worker_errors == []
        assert responses["yolo-enable"]["status"] == 200
        assert responses["yolo-enable"]["payload"]["ok"] is True
        assert responses["yolo-enable"]["payload"]["yolo_enabled"] is True
        assert entry.event.is_set()
        assert entry.result == "once"
        assert is_session_yolo_enabled(sid) is True
    finally:
        allow_enable_handoff.set()
        enable_worker.join(timeout=1)
        disable_worker.join(timeout=1)
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_yolo_post_first_relay_serializes_next_gateway_approval(monkeypatch):
    from api import route_approvals, routes

    sid = "webui-yolo-first-relay-next-approval"
    run_id = "run-first-relay-next-approval"
    response = {}
    stream_results = []
    stream_workers = []
    auto_approved = []
    approval_a = {
        "command": "touch /tmp/webui-yolo-a",
        "description": "first",
        "approval_id": "approval-first",
        "run_id": run_id,
        "_gateway_mirror": True,
        "_gateway_agent_identity_v1": True,
    }
    approval_b = {
        "command": "touch /tmp/webui-yolo-b",
        "description": "second",
        "approval_id": "approval-second",
        "run_id": run_id,
        "_gateway_mirror": True,
        "_gateway_agent_identity_v1": True,
    }

    class NotifyingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.contended = threading.Event()

        def __enter__(self):
            if not self._lock.acquire(blocking=False):
                self.contended.set()
                self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self._lock.release()

    handoff_lock = NotifyingLock()
    disable_session_yolo(sid)
    route_approvals.submit_gateway_pending_mirror(sid, dict(approval_a))

    def next_stream_approval():
        stream_results.append(
            gateway_chat._settle_gateway_run_approval(sid, dict(approval_b), "http://gateway", "")
        )

    def fake_respond(_self, got_run_id, approval_id, choice):
        assert (got_run_id, approval_id, choice) == (run_id, "approval-first", "once")
        worker = threading.Thread(target=next_stream_approval)
        worker.start()
        stream_workers.append(worker)
        assert handoff_lock.contended.wait(timeout=1)
        return {"resolved": 1}

    def fake_auto_approve(_base_url, _api_key, got_run_id, approval_id):
        auto_approved.append((got_run_id, approval_id))

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    monkeypatch.setattr(route_approvals, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(routes, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(gateway_chat, "_auto_approve_gateway_run", fake_auto_approve)
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid, "enabled": True})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)

    try:
        routes.handle_post(object(), urllib.parse.urlparse("/api/session/yolo"))
        for worker in stream_workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
        assert response["status"] == 200
        assert response["payload"] == {
            "ok": True,
            "choice": "once",
            "relayed": True,
            "yolo_enabled": True,
        }
        assert stream_results == [(True, None, 0)]
        assert auto_approved == [(run_id, "approval-second")]
        assert route_approvals.gateway_pending_mirror(sid) is None
    finally:
        disable_session_yolo(sid)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
@pytest.mark.parametrize("relay_fails", [False, True])
def test_card_yolo_first_relay_serializes_next_gateway_approval(monkeypatch, relay_fails):
    from api import route_approvals, routes

    sid = (
        "webui-card-yolo-first-relay-failure"
        if relay_fails
        else "webui-card-yolo-first-relay-success"
    )
    run_id = "run-card-yolo-first-relay"
    response = {}
    stream_results = []
    stream_workers = []
    auto_approved = []
    approval_a = {
        "command": "touch /tmp/webui-yolo-a",
        "description": "first",
        "approval_id": "approval-card-first",
        "run_id": run_id,
        "_gateway_mirror": True,
        "_gateway_agent_identity_v1": True,
    }
    approval_b = {
        "command": "touch /tmp/webui-yolo-b",
        "description": "second",
        "approval_id": "approval-card-second",
        "run_id": run_id,
        "_gateway_mirror": True,
        "_gateway_agent_identity_v1": True,
    }

    class NotifyingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.contended = threading.Event()

        def __enter__(self):
            if not self._lock.acquire(blocking=False):
                self.contended.set()
                self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self._lock.release()

    handoff_lock = NotifyingLock()
    disable_session_yolo(sid)
    route_approvals.submit_gateway_pending_mirror(sid, dict(approval_a))

    def next_stream_approval():
        stream_results.append(
            gateway_chat._settle_gateway_run_approval(
                sid, dict(approval_b), "http://gateway", ""
            )
        )

    def fake_respond(_self, got_run_id, approval_id, choice):
        assert (got_run_id, approval_id, choice) == (
            run_id,
            "approval-card-first",
            "once",
        )
        worker = threading.Thread(target=next_stream_approval)
        worker.start()
        stream_workers.append(worker)
        handoff_lock.contended.wait(timeout=1)
        if relay_fails:
            raise RunnerClientError("relay failed")
        return {"resolved": 1}

    def fake_auto_approve(_base_url, _api_key, got_run_id, approval_id):
        auto_approved.append((got_run_id, approval_id))

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    monkeypatch.setattr(route_approvals, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(routes, "gateway_yolo_handoff", lambda _sid: handoff_lock)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=None))
    monkeypatch.setattr(gateway_chat, "_auto_approve_gateway_run", fake_auto_approve)
    monkeypatch.setattr("api.runner_client.HttpRunnerClient.respond_approval", fake_respond)
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "j", fake_j)

    try:
        routes._handle_approval_respond(
            object(),
            {
                "session_id": sid,
                "choice": "once",
                "approval_id": "approval-card-first",
                "yolo": True,
            },
        )
        for worker in stream_workers:
            worker.join(timeout=1)
            assert not worker.is_alive()

        assert handoff_lock.contended.is_set()
        assert response["status"] == (502 if relay_fails else 200)
        assert response["payload"]["ok"] is (not relay_fails)
        assert response["payload"]["yolo_enabled"] is (not relay_fails)
        assert stream_results and stream_results[0][0] is (not relay_fails)
        if relay_fails:
            assert auto_approved == []
            assert route_approvals.gateway_pending_mirror(
                sid, approval_id=approval_b["approval_id"], run_id=run_id
            ) is not None
        else:
            assert auto_approved == [(run_id, approval_b["approval_id"])]
            assert route_approvals.gateway_pending_mirror(
                sid, approval_id=approval_b["approval_id"], run_id=run_id
            ) is None
    finally:
        for worker in stream_workers:
            worker.join(timeout=1)
        disable_session_yolo(sid)
        route_approvals.retire_gateway_pending_mirror(sid, run_id=run_id)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_card_yolo_response_does_not_overwrite_switched_session_state():
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    respond_approval = extract("respondApproval", "\nfunction startApprovalPolling")
    toggle_yolo = extract("toggleYoloFromApproval", "\n// ── Approval polling")

    def run(api_source, expected_ok):
        script = "\n".join([
            "const toasts=[]; let pillUpdates=0;",
            "const S={session:{session_id:'old-session'}};",
            "let _loadSessionGeneration=1;",
            "let _approvalSessionId='old-session';",
            "let _approvalCurrentId='approval-1';",
            "let _approvalResponding=null;",
            "let _approvalClearedOwner=null;",
            "let _approvalDisplayedOwner={sid:'old-session',approvalId:'approval-1',runId:'',mirrorToken:''};",
            "let _yoloEnabled=false;",
            "const _approvalPendingBySession=new Map([['old-session',{pending:{approval_id:'approval-1'}}]]);",
            f"const api={api_source};",
            "const $=()=>({disabled:false,classList:{contains:v=>v==='visible',add(){},remove(){}}});",
            "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=()=>{};",
            "const _unmarkApprovalDismissed=()=>{};",
            "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
            "const hideApprovalCard=()=>{}; const _updateYoloPill=()=>{pillUpdates+=1;};",
            "const _restoreFailedApprovalResponse=()=>{};",
            _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
            _js_block(messages_js, "function _applyApprovalYoloProjection(", "\nfunction toggleApprovalCardCollapsed"),
            respond_approval,
            toggle_yolo,
            "(async()=>{",
            " const ok=await toggleYoloFromApproval();",
            f" if(ok!=={str(expected_ok).lower()}) throw new Error('unexpected result '+ok);",
            " if(_yoloEnabled!==false) throw new Error('stale response changed new session state');",
            " if(pillUpdates!==0) throw new Error('stale response updated the new session pill');",
            " if(toasts.length!==0) throw new Error('stale response emitted a toast');",
            "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
        ])
        result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr

    run(
        "async()=>{S.session.session_id='new-session';return {ok:true,yolo_enabled:true};}",
        False,
    )
    run(
        "async()=>{S.session.session_id='new-session';const e=new Error('relay failed');e.body=JSON.stringify({error:'relay failed',yolo_enabled:true});throw e;}",
        False,
    )
    run(
        "async()=>{_loadSessionGeneration=2;return {ok:true,yolo_enabled:true};}",
        False,
    )
    run(
        "async()=>{_loadSessionGeneration=2;const e=new Error('relay failed');e.body=JSON.stringify({error:'relay failed',yolo_enabled:true});throw e;}",
        False,
    )


def _run_card_yolo_owner_scenario(scenario):
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(start_marker, end_marker):
        start = messages_js.index(start_marker)
        return messages_js[start:messages_js.index(end_marker, start)]

    owner_sid = "card-session"
    active_sid = "other-session" if scenario.startswith("inactive-") else owner_sid
    approval_id = "Approval-Case-ID" if scenario.startswith("case-distinct-") else "approval-1"
    script = "\n".join([
        "const calls=[]; const toasts=[]; const statuses=[]; const controlWrites=[];",
        "let pillUpdates=0; let cardHideMutations=0; let pendingClears=0; let cardRenders=0;",
        f"const scenario={json.dumps(scenario)};",
        f"const ownerSid={json.dumps(owner_sid)}; const approvalId={json.dumps(approval_id)};",
        f"const S={{session:{{session_id:{json.dumps(active_sid)}}}}};",
        "let _loadSessionGeneration=1;",
        "let _approvalSessionId=ownerSid; let _approvalCurrentId=approvalId;",
        "let _approvalResponding=null; let _approvalClearedOwner=null; let _yoloEnabled=false;",
        "let _approvalHideTimer=null; let _approvalVisibleSince=0; let _approvalSignature='owner-card';",
        "const initialPending=(scenario==='mirror-distinct-success'||scenario==='poll-cleared-success')?{approval_id:approvalId,run_id:'run-owner-1',_gateway_mirror_token:'mirror-owner-1'}:{approval_id:approvalId};",
        "let _approvalDisplayedOwner={sid:ownerSid,approvalId,runId:initialPending.run_id||'',mirrorToken:initialPending._gateway_mirror_token||''};",
        "const _approvalPendingBySession=new Map([[ownerSid,{pending:initialPending}]]);",
        "const classList={",
        " values:new Set(['visible']),",
        " contains(v){return this.values.has(v);},",
        " add(v){this.values.add(v);},",
        " remove(...values){for(const v of values){if(v==='visible'&&this.values.has(v))cardHideMutations+=1;this.values.delete(v);}},",
        " toggle(v,on){if(on===undefined)on=!this.values.has(v);if(on)this.values.add(v);else this.values.delete(v);return on;},",
        "};",
        "const card={classList,hidden:false,setAttribute(){},removeAttribute(){}};",
        "const textNode={textContent:''};",
        "const makeButton=()=>{let value=false;return{classList:{add(){},remove(){}},get disabled(){return value;},set disabled(next){value=!!next;controlWrites.push(value);}};};",
        "const elements={approvalCard:card,approvalCmd:{...textNode},approvalDesc:{...textNode},approvalCounter:{...textNode,style:{}},approvalBtnOnce:makeButton(),approvalBtnSession:makeButton(),approvalBtnAlways:makeButton(),approvalBtnDeny:makeButton()};",
        "const $=id=>elements[id]||null;",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=msg=>statuses.push(msg);",
        "const _setPromptFlyoutHidden=()=>{};",
        "const _updateYoloPill=()=>{pillUpdates+=1;}; const syncTopbar=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _clearApprovalPendingForSession=sid=>{pendingClears+=1;_approvalPendingBySession.delete(sid);};",
        "const _approvalPromptBelongsToActiveSession=sid=>!!(sid&&S.session&&S.session.session_id===sid);",
        "const _renderPendingApprovalForActiveSession=()=>{cardRenders+=1;};",
        "const api=async(path,opts={})=>{",
        " calls.push({path,body:opts.body?JSON.parse(opts.body):null});",
        " if(path.startsWith('/api/approval/pending'))return {pending:null};",
        " if(scenario.startsWith('same-generation-')){_loadSessionGeneration=2;_approvalSessionId=ownerSid;_approvalCurrentId=approvalId;classList.add('visible');}",
        " if(scenario==='poll-cleared-success'){_approvalPendingBySession.delete(ownerSid);}",
        " if(scenario==='mirror-distinct-success'){_approvalPendingBySession.set(ownerSid,{pending:{approval_id:approvalId,run_id:'run-owner-2',_gateway_mirror_token:'mirror-owner-2'}});_approvalDisplayedOwner={sid:ownerSid,approvalId,runId:'run-owner-2',mirrorToken:'mirror-owner-2'};classList.add('visible');}",
        " if(scenario==='case-distinct-id'){_approvalCurrentId=approvalId.toLowerCase();_approvalDisplayedOwner={sid:ownerSid,approvalId:_approvalCurrentId,runId:'',mirrorToken:''};classList.add('visible');}",
        " if(scenario==='case-distinct-pending'){_approvalPendingBySession.set(ownerSid,{pending:{approval_id:approvalId.toLowerCase()}});}",
        " if(scenario.endsWith('-error')){const e=new Error('relay failed');e.body=JSON.stringify({error:'relay failed',yolo_enabled:true});throw e;}",
        " return {ok:true,yolo_enabled:true,stale_cleared:scenario==='same-generation-stale-cleared'||scenario==='case-distinct-pending'};",
        "};",
        extract("function _clearApprovalHideTimer(", "\n// Track session_id of the active approval"),
        extract("function _approvalMirrorOwnerFor(", "\nfunction startApprovalPolling"),
        extract("async function toggleYoloFromApproval(", "\n// ── Approval polling"),
        "(async()=>{",
        " const ok=await toggleYoloFromApproval();",
        " await new Promise(resolve=>setTimeout(resolve,0));",
        " if(scenario==='poll-cleared-success'){",
        "  if(!ok)throw new Error('retired polling projection discarded valid success');",
        "  if(calls.length!==2)throw new Error('valid response made extra requests '+JSON.stringify(calls));",
        "  if(_approvalPendingBySession.has(ownerSid))throw new Error('polling projection was recreated');",
        "  if(_approvalResponding!==null)throw new Error('response owner was not released');",
        "  if(cardHideMutations!==1||pendingClears!==0||cardRenders!==0)throw new Error('wrong retired-owner cleanup '+JSON.stringify({cardHideMutations,pendingClears,cardRenders}));",
        "  if(_yoloEnabled!==true||pillUpdates!==1)throw new Error('valid success did not project YOLO');",
        "  if(JSON.stringify(toasts)!==JSON.stringify(['yolo_enabled'])||statuses.length)throw new Error('wrong valid-success feedback '+JSON.stringify({toasts,statuses}));",
        "  return;",
        " }",
        " if(scenario==='case-distinct-pending'){",
        "  if(!ok)throw new Error('owned response reported failure');",
        "  if(calls.length!==3)throw new Error('expected reconcile + response plus requery '+JSON.stringify(calls));",
        "  if(calls[1].body.approval_id!==approvalId)throw new Error('approval ID changed '+JSON.stringify(calls[1].body));",
        "  const successor=_approvalPendingBySession.get(ownerSid);",
        "  if(!successor||successor.pending.approval_id!==approvalId.toLowerCase())throw new Error('case-distinct pending successor was cleared');",
        "  if(JSON.stringify(controlWrites)!==JSON.stringify([true,true,true,true]))throw new Error('response changed successor controls '+JSON.stringify(controlWrites));",
        "  if(_approvalResponding!==null)throw new Error('response owner was not released');",
        "  if(cardHideMutations!==1||pendingClears!==0||cardRenders!==0)throw new Error('wrong owned cleanup '+JSON.stringify({cardHideMutations,pendingClears,cardRenders}));",
        "  if(_yoloEnabled!==true||pillUpdates!==1)throw new Error('owned response did not project YOLO');",
        "  if(JSON.stringify(toasts)!==JSON.stringify(['yolo_enabled'])||statuses.length)throw new Error('wrong owned feedback '+JSON.stringify({toasts,statuses}));",
        "  return;",
        " }",
        " if(scenario.startsWith('inactive-')){",
        "  if(ok!==false)throw new Error('inactive card action reported success');",
        "  if(calls.length!==0)throw new Error('inactive card posted '+JSON.stringify(calls));",
        "  if(controlWrites.length!==0)throw new Error('inactive card changed controls '+JSON.stringify(controlWrites));",
        " }else{",
        "  if(calls.length!==2)throw new Error('stale response made extra requests '+JSON.stringify(calls));",
        "  if(calls[1].body.approval_id!==approvalId)throw new Error('approval ID changed '+JSON.stringify(calls[1].body));",
        "  if(JSON.stringify(controlWrites)!==JSON.stringify([true,true,true,true]))throw new Error('stale response changed controls '+JSON.stringify(controlWrites));",
        " }",
        " if(_approvalResponding!==null)throw new Error('response owner was not released');",
        " if(cardHideMutations!==0)throw new Error('stale response hid successor card');",
        " if(pendingClears!==0)throw new Error('stale response cleared successor pending state');",
        " if(cardRenders!==0)throw new Error('stale response rendered a card');",
        " if(_yoloEnabled!==false||pillUpdates!==0)throw new Error('stale response changed YOLO projection');",
        " if(toasts.length||statuses.length)throw new Error('stale response emitted feedback '+JSON.stringify({toasts,statuses}));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run([node, "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("scenario", ["inactive-success", "inactive-error"])
def test_card_yolo_rejects_visible_approval_owned_by_inactive_session(scenario):
    _run_card_yolo_owner_scenario(scenario)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("scenario", [
    "same-generation-success",
    "same-generation-stale-cleared",
    "same-generation-error",
])
def test_card_yolo_ignores_response_from_prior_same_session_generation(scenario):
    _run_card_yolo_owner_scenario(scenario)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_card_yolo_keeps_case_distinct_successor_approval_untouched():
    _run_card_yolo_owner_scenario("case-distinct-id")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_stale_clear_preserves_case_distinct_pending_successor():
    _run_card_yolo_owner_scenario("case-distinct-pending")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_card_yolo_keeps_same_id_different_mirror_successor_untouched():
    _run_card_yolo_owner_scenario("mirror-distinct-success")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_card_yolo_accepts_success_after_polling_retires_projection():
    _run_card_yolo_owner_scenario("poll-cleared-success")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_approval_reconciles_stale_transport_id_for_same_logical_approval():
    """#7091 direction 1: same logical approval, changed transport id.

    respondApproval must adopt the authoritative head's `approval_id` (same
    displayed content + same run/mirror ownership) and complete the original
    click once — the respond POST must carry the head's id, not the stale
    render-time one.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let showApprovalCalls=0; let hideApprovalCalls=0;",
        "const S={session:{session_id:'browser-session'}};",
        "let _approvalSessionId='browser-session';",
        "let _approvalCurrentId='approval-1';",
        "let _loadSessionGeneration=1;",
        "let _approvalResponding=null;",
        "let _approvalClearedOwner=null;",
        "let _approvalDisplayedOwner={sid:'browser-session',approvalId:'approval-1',runId:'run-1',mirrorToken:'mirror-1',displayedKey:'{\"sid\":\"browser-session\",\"desc\":\"\",\"cmd\":\"\",\"run_id\":\"run-1\",\"mirror_token\":\"mirror-1\"}'};",
        "let _yoloEnabled=false;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        "const api=async(path,opts={})=>{",
        " calls.push([path,opts.body?JSON.parse(opts.body):null]);",
        " if(path.startsWith('/api/approval/pending'))return {pending:{approval_id:'approval-1-updated',run_id:'run-1',_gateway_mirror_token:'mirror-1'},pending_count:1};",
        " return {ok:true,yolo_enabled:true};",
        "};",
        "const showApprovalForSession=(sid,pending,count)=>{showApprovalCalls+=1;};",
        "const hideApprovalCard=()=>{hideApprovalCalls+=1;};",
        "const $=()=>({disabled:false,classList:{contains:v=>v==='visible',add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _applyApprovalYoloProjection(", "\nfunction toggleApprovalCardCollapsed"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        extract("toggleYoloFromApproval", "\n// ── Approval polling"),
        "(async()=>{",
        " const ok=await toggleYoloFromApproval();",
        " if(!ok) throw new Error('action failed');",
        " if(calls.length!==2) throw new Error('expected reconcile GET + one respond '+JSON.stringify(calls));",
        " if(!calls[0][0].startsWith('/api/approval/pending')) throw new Error('expected reconcile GET first '+JSON.stringify(calls[0]));",
        " const [path,body]=calls[1];",
        " if(path!=='/api/approval/respond') throw new Error('wrong endpoint '+path);",
        " if(body.approval_id!=='approval-1-updated') throw new Error('stale transport id was not reconciled '+JSON.stringify(body));",
        " if(body.run_id!=='run-1'||body.mirror_token!=='mirror-1') throw new Error('logical owner fields changed '+JSON.stringify(body));",
        " if(!_yoloEnabled) throw new Error('UI state not enabled');",
        " if(showApprovalCalls!==0) throw new Error('same-logical approval must not re-render');",
        " if(hideApprovalCalls!==1) throw new Error('card must hide after success');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_approval_never_auto_transfers_click_to_different_logical_approval():
    """#7091 direction 2: queued A→B while A is still visible.

    The head is a DIFFERENT logical approval (different command): the click
    must NOT be transferred to it. The card re-renders to the head and a fresh
    deliberate click is required — no respond POST is issued for the first
    click.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let showApprovalCalls=[]; let hideApprovalCalls=0;",
        "const S={session:{session_id:'browser-session'}};",
        "let _approvalSessionId='browser-session';",
        "let _approvalCurrentId='approval-1';",
        "let _loadSessionGeneration=1;",
        "let _approvalResponding=null;",
        "let _approvalClearedOwner=null;",
        "let _approvalDisplayedOwner={sid:'browser-session',approvalId:'approval-1',runId:'run-1',mirrorToken:'mirror-1'};",
        "let _yoloEnabled=false;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        "const api=async(path,opts={})=>{",
        " calls.push([path,opts.body?JSON.parse(opts.body):null]);",
        " if(path.startsWith('/api/approval/pending'))return {pending:{approval_id:'approval-2',command:'rm -rf /tmp/other',description:'other command',pattern_key:'other',pattern_keys:['other'],run_id:'run-1',_gateway_mirror_token:'mirror-1'},pending_count:1};",
        " throw new Error('respond must not be called for a different logical approval');",
        "};",
        "const showApprovalForSession=(sid,pending,count)=>{showApprovalCalls.push(pending);_approvalCurrentId=pending.approval_id||null;};",
        "const hideApprovalCard=()=>{hideApprovalCalls+=1;};",
        "const $=()=>({disabled:false,classList:{contains:v=>v==='visible',add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _applyApprovalYoloProjection(", "\nfunction toggleApprovalCardCollapsed"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        extract("toggleYoloFromApproval", "\n// ── Approval polling"),
        "(async()=>{",
        " const ok=await toggleYoloFromApproval();",
        " if(ok) throw new Error('different-logical click must not succeed');",
        " if(calls.length!==1) throw new Error('expected only the reconcile GET '+JSON.stringify(calls));",
        " if(!calls[0][0].startsWith('/api/approval/pending')) throw new Error('expected reconcile GET');",
        " if(showApprovalCalls.length!==1) throw new Error('card was not re-rendered to the head '+JSON.stringify(showApprovalCalls));",
        " if(showApprovalCalls[0].approval_id!=='approval-2') throw new Error('re-render must show the authoritative head');",
        " if(hideApprovalCalls!==0) throw new Error('re-rendered head must stay visible for a fresh click');",
        " if(_approvalResponding!==null) throw new Error('response owner was not released');",
        " if(_yoloEnabled!==false) throw new Error('YOLO must not be projected for a different approval');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_reconcile_await_session_switch_blocks_stale_respond_and_dismiss():
    """#7091 edge case: the reconcile is an await, so a session switch can happen
    while it is in flight. The captured owner must be re-checked after the await:
    neither respondApproval (no POST) nor dismissApprovalCard (no dismiss/hide)
    may act on the session the user navigated away to.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; const dismissed=[]; let hideCount=0; let pendingClears=0; let controlReset=0;",
        "let _approvalSessionId='session-a'; let _approvalCurrentId='approval-a';",
        "let _loadSessionGeneration=1; let _approvalResponding=null; let _approvalClearedOwner=null; let _yoloEnabled=false;",
        "let _approvalHideTimer=null; let _approvalVisibleSince=0; let _approvalSignature='sig-a';",
        "let S={session:{session_id:'session-a'}};",
        "let _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'',mirrorToken:''};",
        "const _approvalPendingBySession=new Map([['session-a',{pending:{approval_id:'approval-a'}}]]);",
        # The reconcile pending GET switches to session-b mid-await.
        "const api=async(path,opts={})=>{",
        " calls.push(path);",
        " if(path.startsWith('/api/approval/pending')){",
        "  S.session={session_id:'session-b'};",
        "  _approvalSessionId='session-b'; _approvalCurrentId='approval-b';",
        "  _approvalDisplayedOwner={sid:'session-b',approvalId:'approval-b',runId:'',mirrorToken:''};",
        "  return {pending:null};",
        " }",
        " throw new Error('respond POST must not fire when the session switched during reconcile');",
        "};",
        "const visible=()=>true;",
        "const $=()=>({disabled:false,classList:{contains:visible,add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _markApprovalDismissed=(sid,id)=>{dismissed.push([sid,id]);};",
        "const _clearApprovalPendingForSession=sid=>{pendingClears+=1;};",
        "const hideApprovalCard=()=>{hideCount+=1;};",
        "const showApprovalForSession=()=>{};",
        "const _setApprovalControlsDisabled=()=>{controlReset+=1;};",
        "const _renderPendingApprovalForActiveSession=()=>{};",
        "const _restoreFailedApprovalResponse=()=>{}; const _applyApprovalYoloProjection=()=>{};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        extract("dismissApprovalCard", "\nfunction _syncApprovalCollapseButton"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        "(async()=>{",
        # Part A: respond must NOT issue a POST (only the reconcile GET happened).
        " const ok=await respondApproval('once');",
        " if(ok) throw new Error('respond should fail when the session switched during reconcile');",
        " if(calls.length!==1) throw new Error('respond issued a POST after session switch '+JSON.stringify(calls));",
        " if(!calls[0].startsWith('/api/approval/pending')) throw new Error('only the reconcile GET should run '+JSON.stringify(calls));",
        " if(_approvalResponding!==null) throw new Error('response owner was not released');",
        " if(controlReset<1) throw new Error('controls were not re-enabled');",
        # Part B: dismiss must not mark/hide/clear the NEW session's card.
        " calls.length=0; dismissed.length=0; hideCount=0; pendingClears=0; controlReset=0;",
        " S.session={session_id:'session-a'}; _approvalSessionId='session-a'; _approvalCurrentId='approval-a';",
        " _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'',mirrorToken:''};",
        " await dismissApprovalCard();",
        " if(dismissed.length!==0) throw new Error('dismiss marked the switched session '+JSON.stringify(dismissed));",
        " if(hideCount!==0) throw new Error('dismiss hid the switched session card');",
        " if(pendingClears!==0) throw new Error('dismiss cleared the switched session pending');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_reconcile_does_not_hide_newer_session_card_on_switch():
    """#7091 P1: when the reconcile's fetch runs while the user switches to a
    different session, the rerender branch must not hide the NEW session's card
    or mutate its approval globals.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let hideCount=0; let rendered=0;",
        "let S={session:{session_id:'session-a'}};",
        "let _loadSessionGeneration=1; let _approvalSessionId='session-a'; let _approvalCurrentId='approval-a';",
        "let _approvalResponding=null; let _approvalClearedOwner=null;",
        "let _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'run-1',mirrorToken:'mirror-1'};",
        "const _approvalPendingBySession=new Map([['session-a',{pending:{approval_id:'approval-a',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        # api returns a DIFFERENT logical head (rerender branch) and switches session mid-fetch.
        "const api=async(path,opts={})=>{",
        " calls.push(path);",
        " if(path.startsWith('/api/approval/pending')){",
        "  S.session={session_id:'session-b'};",
        "  _approvalSessionId='session-b'; _approvalCurrentId='approval-b';",
        "  _approvalDisplayedOwner={sid:'session-b',approvalId:'approval-b',runId:'',mirrorToken:''};",
        "  return {pending:{approval_id:'approval-x',command:'rm -rf /tmp/x',description:'x',pattern_key:'x',pattern_keys:['x'],run_id:'run-1',_gateway_mirror_token:'mirror-1'},pending_count:1};",
        " }",
        " return {ok:true};",
        "};",
        "const showApprovalForSession=(sid,pending,count)=>{ if(S.session&&S.session.session_id===sid){rendered+=1;_approvalCurrentId=pending.approval_id||null;} };",
        "const hideApprovalCard=()=>{hideCount+=1;};",
        "const $=()=>({disabled:false,classList:{contains:v=>v==='visible',add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        "(async()=>{",
        " const r=await _reconcileApprovalToHead({sid:'session-a',approvalId:'approval-a',runId:'run-1',mirrorToken:'mirror-1',generation:1});",
        " if(r!=='rerender') throw new Error('expected rerender '+r);",
        " if(hideCount!==0) throw new Error('reconcile hid the NEW session card '+hideCount);",
        " if(_approvalCurrentId!=='approval-b') throw new Error('reconcile mutated the new session current id '+_approvalCurrentId);",
        " if(_approvalDisplayedOwner.approvalId!=='approval-b') throw new Error('reconcile mutated the new session displayed owner');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_gateway_run_unavailable_clears_orphan_card():
    """#7091 P1: a retired gateway run (gateway_run_unavailable) makes a mirror
    card an unresolvable orphan. The respond failure path must CLEAR it (hide +
    drop pending + re-enable + toast), not restore a card that will keep
    failing.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let hideCount=0; let pendingClears=0; let controlReset=0; let restoreCount=0; const toasts=[];",
        "let S={session:{session_id:'session-a'}};",
        "let _loadSessionGeneration=1; let _approvalSessionId='session-a'; let _approvalCurrentId='approval-a';",
        "let _approvalResponding=null; let _approvalClearedOwner=null; let _yoloEnabled=false;",
        "let _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'run-1',mirrorToken:'mirror-1'};",
        "const _approvalPendingBySession=new Map([['session-a',{pending:{approval_id:'approval-a',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        # api: pending GET -> null (no head); respond -> gateway_run_unavailable.
        "const api=async(path,opts={})=>{",
        " calls.push(path);",
        " if(path.startsWith('/api/approval/pending'))return {pending:null};",
        " return {ok:false,code:'gateway_run_unavailable',error:'Gateway approval could not be relayed because the active run is unavailable'};",
        "};",
        "const visible=()=>true;",
        "const $=()=>({disabled:false,classList:{contains:visible,add(){},remove(){}}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _clearApprovalPendingForSession=sid=>{pendingClears+=1;_approvalPendingBySession.delete(sid);};",
        "const hideApprovalCard=()=>{hideCount+=1;};",
        "const showApprovalForSession=()=>{};",
        "const _setApprovalControlsDisabled=()=>{controlReset+=1;};",
        "const _restoreFailedApprovalResponse=()=>{restoreCount+=1;};",
        "const _renderPendingApprovalForActiveSession=()=>{restoreCount+=1;};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _isGatewayUnavailableOrphan(", "\nfunction _applyApprovalYoloProjection"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        "(async()=>{",
        " const ok=await respondApproval('once');",
        " if(ok) throw new Error('gateway orphan respond should fail');",
        " if(restoreCount!==0) throw new Error('orphan card was restored instead of cleared');",
        " if(hideCount!==1) throw new Error('orphan card was not hidden '+hideCount);",
        " if(pendingClears!==1) throw new Error('orphan pending was not cleared '+pendingClears);",
        " if(controlReset<1) throw new Error('controls were not re-enabled');",
        " if(toasts.length<1) throw new Error('no feedback toast');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_gateway_run_unavailable_keeps_live_successor():
    """#7091 P1: a retired gateway run (gateway_run_unavailable) must not hide a
    live successor that became pending after the reconcile GET. The handler
    re-fetches the head and re-renders the successor instead of clearing/hiding
    the card.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let hideCount=0; let pendingClears=0; let controlReset=0; let restoreCount=0; let rendered=[]; const toasts=[];",
        "let S={session:{session_id:'session-a'}};",
        "let _loadSessionGeneration=1; let _approvalSessionId='session-a'; let _approvalCurrentId='approval-a';",
        "let _approvalResponding=null; let _approvalClearedOwner=null; let _yoloEnabled=false;",
        "let _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'run-1',mirrorToken:'mirror-1'};",
        "const _approvalPendingBySession=new Map([['session-a',{pending:{approval_id:'approval-a',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        # First pending GET (reconcile) -> null; the re-fetch in the handler -> successor B.
        "let pendingCalls=0;",
        "const api=async(path,opts={})=>{",
        " calls.push(path);",
        " if(path.startsWith('/api/approval/pending')){",
        "  pendingCalls+=1;",
        "  if(pendingCalls===1)return {pending:null};",
        "  return {pending:{approval_id:'approval-b',command:'rm -rf /tmp/b',description:'b',pattern_key:'b',pattern_keys:['b'],run_id:'run-2',_gateway_mirror_token:'mirror-2'},pending_count:1};",
        " }",
        " return {ok:false,code:'gateway_run_unavailable',error:'Gateway approval could not be relayed because the active run is unavailable'};",
        "};",
        "const visible=()=>true;",
        "const $=()=>({disabled:false,classList:{contains:visible,add(){},remove(){}}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _clearApprovalPendingForSession=sid=>{pendingClears+=1;};",
        "const hideApprovalCard=()=>{hideCount+=1;};",
        "const showApprovalForSession=(sid,pending,count)=>{rendered.push(pending&&pending.approval_id);_approvalCurrentId=pending&&pending.approval_id;};",
        "const _setApprovalControlsDisabled=()=>{controlReset+=1;};",
        "const _restoreFailedApprovalResponse=()=>{restoreCount+=1;};",
        "const _renderPendingApprovalForActiveSession=()=>{restoreCount+=1;};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _isGatewayUnavailableOrphan(", "\nfunction _applyApprovalYoloProjection"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        "(async()=>{",
        " const ok=await respondApproval('once');",
        " if(ok) throw new Error('gateway orphan respond should fail');",
        " if(restoreCount!==0) throw new Error('orphan path should not restore '+restoreCount);",
        " if(hideCount!==0) throw new Error('live successor card was hidden '+hideCount);",
        " if(pendingClears!==0) throw new Error('live successor pending was cleared '+pendingClears);",
        " if(rendered.length!==1||rendered[0]!=='approval-b') throw new Error('successor B was not re-rendered '+JSON.stringify(rendered));",
        " if(controlReset<1) throw new Error('controls were not re-enabled');",
        " if(toasts.length!==0) throw new Error('a true-orphan toast should not fire when a successor is pending '+JSON.stringify(toasts));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_gateway_run_unavailable_null_head_keeps_successor_rendered_during_fetch():
    """#7091 (Greptile P1): a gateway-unavailable orphan must NOT be cleared when
    a same-session successor B rendered (SSE) while the handler's pending-head
    re-fetch was in flight and that re-fetch returns an empty snapshot. The
    orphan-clear branch must verify the card STILL shows the captured owner —
    not just the session id — or it deletes B's pending projection and hides its
    live card.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let hideCount=0; let pendingClears=0; let controlReset=0; const toasts=[];",
        "let S={session:{session_id:'session-a'}};",
        "let _loadSessionGeneration=1; let _approvalSessionId='session-a'; let _approvalCurrentId='approval-a';",
        "let _approvalResponding=null; let _approvalClearedOwner=null; let _yoloEnabled=false;",
        "let _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'run-1',mirrorToken:'mirror-1'};",
        "const _approvalPendingBySession=new Map([['session-a',{pending:{approval_id:'approval-a',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        # reconcile GET -> null; respond -> gateway_run_unavailable; the handler's
        # re-fetch renders same-session successor B during the await, then returns null.
        "let pendingCalls=0;",
        "const api=async(path,opts={})=>{",
        " calls.push(path);",
        " if(path.startsWith('/api/approval/pending')){",
        "  pendingCalls+=1;",
        "  if(pendingCalls>=2){",
        "   // SSE renders successor B while the handler's re-fetch is in flight.",
        "   _approvalPendingBySession.set('session-a',{pending:{approval_id:'approval-b',command:'rm -rf /tmp/b',description:'b',pattern_key:'b',pattern_keys:['b'],run_id:'run-2',_gateway_mirror_token:'mirror-2'}});",
        "   _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-b',runId:'run-2',mirrorToken:'mirror-2'};",
        "   _approvalCurrentId='approval-b';",
        "  }",
        "  return {pending:null};",
        " }",
        " return {ok:false,code:'gateway_run_unavailable',error:'Gateway approval could not be relayed because the active run is unavailable'};",
        "};",
        "const visible=()=>true;",
        "const $=()=>({disabled:false,classList:{contains:visible,add(){},remove(){}}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _clearApprovalPendingForSession=sid=>{pendingClears+=1;_approvalPendingBySession.delete(sid);};",
        "const hideApprovalCard=()=>{hideCount+=1;};",
        "const showApprovalForSession=()=>{};",
        "const _setApprovalControlsDisabled=()=>{controlReset+=1;};",
        "const _restoreFailedApprovalResponse=()=>{};",
        "const _renderPendingApprovalForActiveSession=()=>{};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _isGatewayUnavailableOrphan(", "\nfunction _applyApprovalYoloProjection"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        "(async()=>{",
        " const ok=await respondApproval('once');",
        " if(ok) throw new Error('gateway orphan respond should fail');",
        " if(hideCount!==0) throw new Error('successor B rendered during fetch was hidden '+hideCount);",
        " if(pendingClears!==0) throw new Error('successor B pending projection was cleared '+pendingClears);",
        " if(toasts.length!==0) throw new Error('orphan toast fired while live successor B is pending '+JSON.stringify(toasts));",
        " if(_approvalDisplayedOwner.approvalId!=='approval-b') throw new Error('successor B no longer owns the card '+JSON.stringify(_approvalDisplayedOwner));",
        " if(!_approvalPendingBySession.has('session-a')) throw new Error('successor B was dropped from the pending map');",
        " if(controlReset<1) throw new Error('controls were not re-enabled');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_click_on_a_never_resolves_same_session_successor_b():
    """#7091 (developer review): a click captured on approval A must never
    resolve a same-session successor B that renders into the pending map while
    the reconcile GET is in flight. The head is compared against the click-time
    snapshot, so B re-renders and requires a fresh click, and A's click sends
    NO response POST.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let rendered=[]; let controlReset=0;",
        "let S={session:{session_id:'session-a'}};",
        "let _loadSessionGeneration=1; let _approvalSessionId='session-a'; let _approvalCurrentId='approval-a';",
        "let _approvalResponding=null; let _approvalClearedOwner=null; let _yoloEnabled=false;",
        "let _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'run-1',mirrorToken:'mirror-1'};",
        "const _approvalPendingBySession=new Map([['session-a',{pending:{approval_id:'approval-a',run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        # The reconcile GET renders a same-session successor B (producer) and returns B.
        "let got=false;",
        "const api=async(path,opts={})=>{",
        " calls.push(path);",
        " if(path.startsWith('/api/approval/pending')){",
        "  if(!got){",
        "   got=true;",
        "   _approvalPendingBySession.set('session-a',{pending:{approval_id:'approval-b',command:'rm -rf /tmp/b',description:'b',pattern_key:'b',pattern_keys:['b'],run_id:'run-2',_gateway_mirror_token:'mirror-2'}});",
        "   _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-b',runId:'run-2',mirrorToken:'mirror-2'};",
        "   _approvalCurrentId='approval-b';",
        "   rendered.push('producer-b');",
        "  }",
        "  return {pending:{approval_id:'approval-b',command:'rm -rf /tmp/b',description:'b',pattern_key:'b',pattern_keys:['b'],run_id:'run-2',_gateway_mirror_token:'mirror-2'},pending_count:1};",
        " }",
        " throw new Error('A click must not POST when a same-session successor rendered mid-GET');",
        "};",
        "const visible=()=>true;",
        "const $=()=>({disabled:false,classList:{contains:visible,add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{};",
        "const showApprovalForSession=(sid,pending,count)=>{rendered.push(pending&&pending.approval_id);};",
        "const _setApprovalControlsDisabled=()=>{controlReset+=1;};",
        "const _renderPendingApprovalForActiveSession=()=>{};",
        "const _restoreFailedApprovalResponse=()=>{}; const _applyApprovalYoloProjection=()=>{};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _isGatewayUnavailableOrphan(", "\nfunction _applyApprovalYoloProjection"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        "(async()=>{",
        " const ok=await respondApproval('once');",
        " if(ok) throw new Error('A click must not succeed when B rendered mid-GET');",
        " if(calls.length!==1) throw new Error('expected only the reconcile GET, no POST '+JSON.stringify(calls));",
        " if(!calls[0].startsWith('/api/approval/pending')) throw new Error('only the reconcile GET should run');",
        " if(_approvalResponding!==null) throw new Error('response owner was not released');",
        " if(controlReset<1) throw new Error('controls were not re-enabled');",
        " if(rendered.length<1||rendered[rendered.length-1]!=='approval-b') throw new Error('B was not rendered for a fresh click '+JSON.stringify(rendered));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_capture_sources_rendered_card_not_map_with_dismissed_successor():
    """#7091 (developer re-gate): the capture must source the snapshot from the
    RENDERED card, not the mutable pending map. A cross-tab-dismissed successor
    B is stored in the map by _rememberApprovalPending and then suppressed from
    rendering, leaving A visibly rendered with B in the map. A's click must not
    resolve B. A and B share run/mirror owners so the owner fields cannot save
    the day; only the rendered-key snapshot can.
    """
    messages_js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()

    def extract(name, end_marker):
        start = messages_js.index(f"async function {name}(")
        return messages_js[start:messages_js.index(end_marker, start)]

    script = "\n".join([
        "const calls=[]; let rendered=[]; let controlReset=0;",
        "let S={session:{session_id:'session-a'}};",
        "let _loadSessionGeneration=1; let _approvalSessionId='session-a'; let _approvalCurrentId='approval-a';",
        "let _approvalResponding=null; let _approvalClearedOwner=null; let _yoloEnabled=false;",
        # A is the visibly rendered card, with its render-time logical key.
        "let _approvalDisplayedOwner={sid:'session-a',approvalId:'approval-a',runId:'run-1',mirrorToken:'mirror-1',displayedKey:'{\"sid\":\"session-a\",\"desc\":\"\",\"cmd\":\"\",\"run_id\":\"run-1\",\"mirror_token\":\"mirror-1\"}'};",
        # The MAP holds a dismissed successor B (same run/mirror, different command) that never rendered.
        "const _approvalPendingBySession=new Map([['session-a',{pending:{approval_id:'approval-b',command:'rm -rf /tmp/b',description:'b',pattern_key:'b',pattern_keys:['b'],run_id:'run-1',_gateway_mirror_token:'mirror-1'}}]]);",
        "const api=async(path,opts={})=>{",
        " calls.push(path);",
        " if(path.startsWith('/api/approval/pending'))return {pending:{approval_id:'approval-b',command:'rm -rf /tmp/b',description:'b',pattern_key:'b',pattern_keys:['b'],run_id:'run-1',_gateway_mirror_token:'mirror-1'},pending_count:1};",
        " throw new Error('A click must not POST approval-b when B never rendered');",
        "};",
        "const visible=()=>true;",
        "const $=()=>({disabled:false,classList:{contains:visible,add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{};",
        "const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{};",
        "const showApprovalForSession=(sid,pending,count)=>{rendered.push(pending&&pending.approval_id);};",
        "const _setApprovalControlsDisabled=()=>{controlReset+=1;};",
        "const _renderPendingApprovalForActiveSession=()=>{};",
        "const _restoreFailedApprovalResponse=()=>{}; const _applyApprovalYoloProjection=()=>{};",
        "const _updateYoloPill=()=>{};",
        _js_block(messages_js, "function _approvalMirrorOwnerFor(", "\nfunction _setApprovalControlsDisabled"),
        _js_block(messages_js, "function _isGatewayUnavailableOrphan(", "\nfunction _applyApprovalYoloProjection"),
        extract("respondApproval", "\nfunction startApprovalPolling"),
        "(async()=>{",
        " const ok=await respondApproval('once');",
        " if(ok) throw new Error('A click must not succeed when the map holds a never-rendered B');",
        " if(calls.length!==1) throw new Error('expected only the reconcile GET, no POST '+JSON.stringify(calls));",
        " if(!calls[0].startsWith('/api/approval/pending')) throw new Error('only the reconcile GET should run');",
        " if(_approvalResponding!==null) throw new Error('response owner was not released');",
        " if(controlReset<1) throw new Error('controls were not re-enabled');",
        " if(rendered.length<1||rendered[rendered.length-1]!=='approval-b') throw new Error('B was not rendered for a fresh click '+JSON.stringify(rendered));",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
