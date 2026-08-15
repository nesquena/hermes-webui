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
        "let _approvalResponding=null;",
        "let _yoloEnabled=false;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1'}}]]);",
        "const api=async(path,opts={})=>{calls.push([path,JSON.parse(opts.body||'{}')]);return {ok:true,yolo_enabled:true};};",
        "const $=()=>({disabled:false,classList:{add(){},remove(){}}});",
        "const t=k=>k; const showToast=()=>{}; const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{}; const _approvalResponseMatches=()=>false;",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{}; const _updateYoloPill=()=>{};",
        extract("respondApproval", "\nfunction startApprovalPolling"),
        extract("toggleYoloFromApproval", "\n// ── Approval polling"),
        "(async()=>{",
        " const ok=await toggleYoloFromApproval();",
        " if(!ok) throw new Error('action failed');",
        " if(calls.length!==1) throw new Error('expected one request '+JSON.stringify(calls));",
        " const [path,body]=calls[0];",
        " if(path!=='/api/approval/respond') throw new Error('wrong endpoint '+path);",
        " if(JSON.stringify(body)!==JSON.stringify({session_id:'browser-session',choice:'once',approval_id:'approval-1',yolo:true})) throw new Error('wrong body '+JSON.stringify(body));",
        " if(!_yoloEnabled) throw new Error('UI state not enabled');",
        "})().catch(e=>{console.error(e.stack||e);process.exit(1)});",
    ])
    result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


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
        "let _approvalResponding=null;",
        "let _yoloEnabled=true;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1'}}]]);",
        "const api=async()=>({ok:true,yolo_enabled:false});",
        "const $=()=>({disabled:false,classList:{add(){},remove(){}}});",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=()=>{};",
        "const _unmarkApprovalDismissed=()=>{}; const _approvalResponseMatches=()=>false;",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{}; const _updateYoloPill=()=>{pillUpdates+=1;};",
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
        "let _approvalResponding=null;",
        "let _yoloEnabled=false;",
        "const _approvalPendingBySession=new Map([['browser-session',{pending:{approval_id:'approval-1'}}]]);",
        "const api=async()=>{const e=new Error('relay failed');e.status=502;e.body=JSON.stringify({ok:false,error:'relay failed',yolo_enabled:true});throw e;};",
        "const t=k=>k; const showToast=msg=>toasts.push(msg); const setStatus=msg=>statuses.push(msg);",
        "const _unmarkApprovalDismissed=()=>{}; const _approvalResponseMatches=()=>false;",
        "const _setApprovalControlsDisabled=()=>{}; const _clearApprovalPendingForSession=()=>{};",
        "const hideApprovalCard=()=>{}; const _updateYoloPill=()=>{pillUpdates+=1;};",
        "const _approvalPromptBelongsToActiveSession=()=>true;",
        "const _renderPendingApprovalForActiveSession=()=>{cardRenders+=1;};",
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
        "let _approvalSessionId='browser-session';",
        "let _approvalCurrentId='approval-1';",
        "let _yoloEnabled=false;",
        "let atomicCalls=0;",
        "const api=async(path,opts={})=>{calls.push([path,opts]);return {yolo_enabled:false};};",
        "const toggleYoloFromApproval=async()=>{atomicCalls+=1;return true;};",
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
        "let _approvalSessionId=null; let _approvalCurrentId=null; let _yoloEnabled=false;",
        "const api=async(path,opts={})=>{calls.push(path);return path.includes('?')?{yolo_enabled:false}:{ok:true,yolo_enabled:false};};",
        "const toggleYoloFromApproval=async()=>{throw new Error('card path must not run');};",
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
        "let _approvalSessionId=null; let _approvalCurrentId=null; let _yoloEnabled=false;",
        "const api=async(path)=>{if(path.includes('?'))return {yolo_enabled:false};const e=new Error('relay busy');e.body=JSON.stringify({error:'relay busy',yolo_enabled:true});throw e;};",
        "const toggleYoloFromApproval=async()=>{throw new Error('card path must not run');};",
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
    responses = {}
    disable_session_yolo(sid)

    def fake_respond(_self, _run_id, _approval_id, _choice):
        relay_started.set()
        assert release_relay.wait(timeout=5)
        raise RunnerClientError("relay failed")

    def fake_j(handler, data, status=200, extra_headers=None):
        responses[handler] = {"payload": data, "status": status}
        return data

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid, "enabled": True})
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

    try:
        worker.start()
        assert relay_started.wait(timeout=5)
        routes.handle_post(toggle_handler, urllib.parse.urlparse("/api/session/yolo"))

        assert responses[toggle_handler]["status"] == 409
        assert responses[toggle_handler]["payload"]["code"] == "gateway_approval_in_progress"
        assert responses[toggle_handler]["payload"]["yolo_enabled"] is False
        assert is_session_yolo_enabled(sid) is False
        assert route_approvals.gateway_pending_mirror(
            sid, approval_id=approval_id, run_id=run_id
        ) is not None

        release_relay.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert responses[card_handler]["status"] == 502
        assert responses[card_handler]["payload"]["yolo_enabled"] is False
        assert is_session_yolo_enabled(sid) is False
        assert route_approvals.gateway_pending_mirror(
            sid, approval_id=approval_id, run_id=run_id
        ) is not None
    finally:
        release_relay.set()
        worker.join(timeout=5)
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

    def fake_reconcile(_sid):
        with route_approvals._yolo_transition_lock:
            observed_transition_states.append(bool(route_approvals._yolo_transitions.get(sid)))
        routes._pending[sid] = [dict(mirror)]
        return mirror, 1, True

    def fake_relay(_sid, _mirror, _choice, *, enable_yolo):
        assert enable_yolo is True
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
    monkeypatch.setattr(routes, "gateway_pending_mirror", lambda _sid: None)
    monkeypatch.setattr(routes, "reconcile_gateway_pending_mirror_locked", fake_reconcile)
    monkeypatch.setattr(routes, "_relay_gateway_run_approval", fake_relay)

    try:
        routes.handle_post(handler, urllib.parse.urlparse("/api/session/yolo"))
        assert response["status"] == 200
        assert response["payload"]["ok"] is True
        assert observed_transition_states == [True, True]
        assert observed_gateway_states == [False]
        assert is_session_yolo_enabled(sid) is False
    finally:
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


@pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")
def test_no_run_approval_success_reports_authoritative_yolo_state(monkeypatch):
    from api import routes

    sid = "webui-no-run-authoritative-success"
    response = {}
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    original_set = routes.set_session_yolo_enabled

    def racing_enable(session_key, enabled):
        original_set(session_key, enabled)
        disable_session_yolo(session_key)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=None))
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: True)
    monkeypatch.setattr(routes, "resolve_gateway_pending_local_no_run_mirror", lambda *_a: (True, 1, None, 0))
    monkeypatch.setattr(routes, "set_session_yolo_enabled", racing_enable)

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
    from api import routes

    sid = "webui-local-authoritative-success"
    response = {}
    disable_session_yolo(sid)

    def fake_j(_handler, data, status=200, extra_headers=None):
        response.update(payload=data, status=status)
        return data

    original_set = routes.set_session_yolo_enabled

    def racing_enable(session_key, enabled):
        original_set(session_key, enabled)
        disable_session_yolo(session_key)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", lambda _sid: SimpleNamespace(active_stream_id=None))
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(routes, "_resolve_approval_legacy", lambda *_a: True)
    monkeypatch.setattr(routes, "set_session_yolo_enabled", racing_enable)

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
                gateway_chat._prepare_gateway_run_approval(sid, dict(approval))
            )
        )
        worker.start()
        workers.append(worker)
        assert handoff_lock.contended.wait(timeout=1)
        return original_finish(session_key, token, succeeded=succeeded)

    monkeypatch.setattr(route_approvals, "_gateway_yolo_handoff_lock", handoff_lock)
    monkeypatch.setattr(routes, "_gateway_yolo_handoff_lock", handoff_lock)
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
        assert route_approvals.gateway_pending_mirror(sid) is None
    finally:
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)
