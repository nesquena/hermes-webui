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
