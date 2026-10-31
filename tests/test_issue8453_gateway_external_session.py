"""WebUI mitigation for #8453; these fixtures do not grant Agent authority."""
from collections import OrderedDict
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace
import urllib.error

import pytest

import api.gateway_chat as gateway_chat
import api.helpers as helpers
import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.config import STREAMS


# Exact metadata shape and Gateway response reported in #8453.
DESKTOP = {
    "session_source": "other", "source_tag": "desktop",
    "is_cli_session": False, "read_only": False,
}
REFUSAL = b'{"error":{"message":"permission_denied","type":"invalid_request_error","code":"permission_denied"}}'


@pytest.mark.parametrize("source", [DESKTOP, {
    "session_source": "messaging", "source_tag": "telegram", "read_only": False,
}, {"session_source": "cli", "source_tag": "tui", "is_cli_session": True},
    {"is_cli_session": True}])
@pytest.mark.parametrize("runs_setting", [None, "false", "true"])
def test_gateway_projection_marks_external_chat_unavailable_without_persisting_read_only(monkeypatch, source, runs_setting):
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    if runs_setting is None:
        monkeypatch.delenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", raising=False)
    else:
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", runs_setting)
    projected = helpers.public_session_projection(source)
    assert "another Hermes client" in projected["chat_read_only_reason"]
    assert not source.get("chat_read_only_reason")
    assert projected.get("read_only") == source.get("read_only")


@pytest.mark.parametrize("backend,source", [
    ("legacy", DESKTOP), ("gateway", {}),
    ("gateway", {"session_source": "webui"}),
    ("gateway", {"source_tag": "api_server", "session_source": "api"}),
    ("gateway", {"session_source": "fork"}),
    ("gateway", {"session_source": "fork", "raw_source": "desktop"}),
])
def test_gateway_projection_keeps_local_and_api_chat_available(monkeypatch, backend, source):
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", backend)
    assert not helpers.public_session_projection(source).get("chat_read_only_reason")


@pytest.mark.parametrize("storage", ["sidecar", "agent_metadata", "synth_fallback"])
def test_gateway_chat_start_refuses_desktop_before_import_or_workspace_mutation(monkeypatch, storage):
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args: True)
    def session(sid):
        if storage != "sidecar":
            raise KeyError(sid)
        return SimpleNamespace(**DESKTOP)

    monkeypatch.setattr(routes, "get_session", session)
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda sid: DESKTOP if storage == "agent_metadata" else {})
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda sid: False)
    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", lambda sid: (SimpleNamespace(**DESKTOP), "claimable"))
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda *a: pytest.fail("external session was hydrated for mutation"))
    result = {}
    monkeypatch.setattr(routes, "j", lambda h, payload, **kw: result.update(payload=payload, **kw) or True)
    routes._handle_chat_start(SimpleNamespace(), {"session_id": "desktop-session", "message": "ping"})
    assert result["status"] == 409
    assert result["payload"]["code"] == "gateway_external_session_read_only"


@pytest.mark.parametrize("runs", [False, True])
@pytest.mark.parametrize("status,body,expected_code", [
    (409, REFUSAL, "permission_denied"),
    (400, b'{"error":{"message":"Invalid model","code":"invalid_model"}}', "invalid_model"),
    (502, b"upstream unavailable", None),
])
def test_gateway_http_refusal_survives_event_settlement_and_reload(tmp_path, monkeypatch, runs, status, body, expected_code):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", str(runs).lower())
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a: True)
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {"status": "not_configured", "messages": []})
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda *a: [])

    requests = []

    class RefusingGateway(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.headers.get("Authorization"),
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RefusingGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "fixture-api-key")
    session = models.new_session()
    stream_id = "issue8453-refused"
    session.active_stream_id = stream_id
    session.pending_user_message = "ping"
    session.pending_started_at = 123
    session.save()
    events = []
    STREAMS[stream_id] = SimpleNamespace(put_nowait=events.append)
    try:
        gateway_chat._run_gateway_chat_streaming(session.session_id, "ping", "test", str(tmp_path), stream_id, [])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert len(requests) == 1
    assert requests[0][0] == ("/v1/runs" if runs else "/v1/chat/completions")
    assert requests[0][1] == "Bearer fixture-api-key"
    if runs:
        assert requests[0][2]["session_id"] == session.session_id
    errors = [item[1] for item in events if item[0] == "apperror"]
    assert len(errors) == 1
    error = errors[0]
    assert error["type"] == "gateway_http_error"
    if expected_code:
        assert error["code"] == expected_code
        assert expected_code in error["message"]
    assert error["terminal_session_persisted"] is True
    models.SESSIONS.clear()
    saved = models.get_session(session.session_id)
    assert saved.active_stream_id is None
    assert saved.pending_user_message is None
    assert saved.gateway_run is None
    assert saved.messages[-1]["_error"] is True
    assert error["message"] in saved.messages[-1]["content"]
    assert error["session"]["messages"][-1] == saved.messages[-1]
    assert stream_id not in gateway_chat._STREAM_ENDPOINTS
    assert stream_id not in gateway_chat._STREAM_RUN_LIFECYCLE


def test_gateway_http_error_body_is_bounded_and_always_redacted(monkeypatch):
    monkeypatch.setattr("api.config.load_settings", lambda: {"api_redact_enabled": False})
    body = io.BytesIO(b'{"error":{"message":"Authorization: Bearer secret-fixture-token","code":"permission_denied"}}' + b" " * 5000)
    exc = urllib.error.HTTPError("http://gateway.invalid/v1/runs", 409, "Conflict", Message(), body)
    payload = gateway_chat._gateway_http_error_event(exc, api_key_configured=True)
    assert "secret-fixture-token" not in repr(payload)
    assert body.tell() <= 2048
    assert len(repr(payload)) < 1500


def test_external_composer_disables_send_preserves_draft_and_resets_on_navigation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for frontend behavior test")
    ui = Path("static/ui.js").read_text(encoding="utf-8")
    messages = Path("static/messages.js").read_text(encoding="utf-8")

    def function(source, name):
        start = re.search(rf"^(?:async )?function {name}\(", source, re.M).start()
        end = re.search(r"^(?:async )?function \w+\(", source[start + 1:], re.M)
        return source[start:start + 1 + end.start()] if end else source[start:]

    functions = "\n".join(function(ui, name) for name in (
        "getComposerPrimaryAction", "updateSendBtn",
    )) + "\n" + function(messages, "send")
    script = r"""
const assert=require('node:assert/strict');
const S={session:{chat_read_only_reason:'Continue in the original client.'},busy:false,activeStreamId:null,pendingFiles:[]};
const attrs={'aria-describedby':'composerWorkspaceContext'};
const msg={value:'keep this draft',disabled:false,readOnly:false,getAttribute:k=>attrs[k],setAttribute:(k,v)=>attrs[k]=v,removeAttribute:k=>delete attrs[k]};
const btn={dataset:{},style:{},classList:{toggle(){},remove(){},contains(){return true}},setAttribute(){}};
const notice={hidden:true,textContent:''};
const elements={msg,btnSend:btn,composerChatReadOnlyReason:notice};
const $=id=>elements[id];
const _composerHasContent=()=>!!msg.value;
const _getExplicitBusyCommandAction=()=>null;
const _setComposerPrimaryButtonIcon=()=>{};
const showToast=()=>{};
const window={};
const requestAnimationFrame=fn=>fn();
""" + functions + r"""
(async()=>{
 updateSendBtn();
 assert.equal(msg.readOnly,true);
 assert.equal(btn.disabled,true);
 assert.equal(notice.hidden,false);
 assert.equal(notice.textContent,S.session.chat_read_only_reason);
 assert.match(attrs['aria-describedby'],/composerWorkspaceContext composerChatReadOnlyReason/);
 await send();
 assert.equal(msg.value,'keep this draft');
 S.busy=true;S.activeStreamId='owned-stream';
 assert.equal(getComposerPrimaryAction(),'stop');
 S.busy=false;S.activeStreamId=null;S.session={session_source:'webui'};
 updateSendBtn();
 assert.equal(msg.readOnly,false);
 assert.equal(btn.disabled,false);
 assert.equal(notice.hidden,true);
 assert.equal(attrs['aria-describedby'],'composerWorkspaceContext');
 assert.equal(msg.value,'keep this draft');
 console.log('ok');
})().catch(error=>{console.error(error);process.exit(1)});
"""
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
