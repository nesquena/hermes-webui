"""Sidebar cancellation must not clean a replacement stream after HTTP await.

Executes the real boot.js function, messages.js close helper and ui.js storage
helpers under Node, plus
the real Handler/GET route/cancel_stream with synthetic local stream owners.
No provider, browser package, external service or extension checkout is needed.
"""
import json
from collections import OrderedDict
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading

import pytest


REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

DRIVER = r"""
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(process.argv[1]);
const boot=fs.readFileSync('static/boot.js','utf8');
const ui=fs.readFileSync('static/ui.js','utf8');
const messages=fs.readFileSync('static/messages.js','utf8');
function source(text,name,async=false){
  const start=text.indexOf((async?'async ':'')+'function '+name+'(');
  if(start<0)throw Error('missing '+name);
  const end=text.indexOf('\n'+(async?'async ':'')+'function ',start+1);
  return text.slice(start,end<0?text.length:end);
}
const sid=input.sid||'session-B',old=input.old||'stream-old',fresh=input.fresh||'stream-new';
const session={session_id:sid,active_stream_id:old};
const S={session,activeStreamId:old,busy:true};
const INFLIGHT={[sid]:{streamId:old,messages:['old']}};
const storage=new Map([
  ['hermes-webui-inflight',JSON.stringify({sid,streamId:old})],
  ['hermes-webui-inflight-state',JSON.stringify({[sid]:{streamId:old},other:{streamId:'other'}})],
]);
const effects=[],http=[];
let argument;
const context={S,INFLIGHT,console:{info(){}},document:{baseURI:input.base||'http://localhost/'},URL,
  INFLIGHT_KEY:'hermes-webui-inflight',INFLIGHT_STATE_KEY:'hermes-webui-inflight-state',
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  window:{},
  LIVE_STREAMS:{[sid]:{streamId:old,source:{readyState:1,close:()=>effects.push(['close',sid,old])}}},
  _resumeSessionStreamAfterLiveChat:()=>effects.push(['resume']),
  setBusy:v=>{S.busy=v;effects.push(['busy',v]);},setComposerStatus:()=>effects.push(['status']),
  renderSessionList:()=>effects.push(['render']),_clearPendingPromptsForSession:()=>effects.push(['prompts']),
  stopApprovalPollingForSession:()=>effects.push(['approval']),stopClarifyPollingForSession:()=>effects.push(['clarify']),
  _approvalSessionId:sid,_clarifySessionId:sid,
  stopApprovalPolling:()=>effects.push(['approval-stop']),hideApprovalCard:()=>effects.push(['approval-hide']),
  stopClarifyPolling:()=>effects.push(['clarify-stop']),hideClarifyCard:()=>effects.push(['clarify-hide']),
};
vm.createContext(context);
// Execute the production compaction/budget/write chain, not a save stub.
vm.runInContext(ui.slice(ui.indexOf('const INFLIGHT_STATE_DEFAULT_LIMITS ='),
  ui.indexOf('function loadInflightState(')),context);
vm.runInContext(source(messages,'closeLiveStream'),context);
for(const name of ['_readInflightStateMap','clearInflightState','clearInflight'])
  vm.runInContext(source(ui,name),context);
vm.runInContext(source(boot,'cancelSessionStream',true),context);
function snapshot(){return JSON.parse(JSON.stringify({sid:S.session.session_id,active:S.activeStreamId,
  sessionActive:S.session.active_stream_id,busy:S.busy,inflight:INFLIGHT[sid]||null,
  saved:JSON.parse(storage.get('hermes-webui-inflight-state')||'{}'),
  activeSaved:JSON.parse(storage.get('hermes-webui-inflight')||'null'),
  live:context.LIVE_STREAMS[sid]?context.LIVE_STREAMS[sid].streamId:null}));}
function rotate(){
  const mode=input.mode;
  if(mode==='switch-session'){
    S.session={session_id:'session-A',active_stream_id:fresh};S.activeStreamId=fresh;
    storage.set('hermes-webui-inflight',JSON.stringify({sid:'session-A',streamId:fresh}));
  }else if(mode==='caller-rebound'){
    argument.session_id='session-A';argument.active_stream_id=fresh;
  }else if(mode==='inflight-only'||mode==='unknown-inflight'){
    INFLIGHT[sid]=mode==='unknown-inflight'?{messages:['unknown']}:{streamId:fresh,messages:['new']};
  }else if(mode==='storage-only'){
    storage.set('hermes-webui-inflight',JSON.stringify({sid,streamId:fresh}));
    storage.set('hermes-webui-inflight-state',JSON.stringify({[sid]:{streamId:fresh},other:{streamId:'other'}}));
  }else if(mode==='session-field-only'){
    S.session.active_stream_id=fresh;
  }else if(mode==='active-field-only'){
    S.activeStreamId=fresh;
  }else if(mode==='unknown-active'){
    S.activeStreamId=null;
  }else{
    S.activeStreamId=fresh;S.session.active_stream_id=fresh;
    INFLIGHT[sid]={streamId:fresh,messages:['new']};
    storage.set('hermes-webui-inflight',JSON.stringify({sid,streamId:fresh}));
    storage.set('hermes-webui-inflight-state',JSON.stringify({[sid]:{streamId:fresh},other:{streamId:'other'}}));
  }
}
(async()=>{
  if(input.helper){
    const before=snapshot();
    if(input.mode==='malformed-storage'){
      storage.set('hermes-webui-inflight','{');storage.set('hermes-webui-inflight-state','{');
    }else if(input.mode==='unknown-storage'){
      storage.set('hermes-webui-inflight',JSON.stringify({sid}));
      storage.set('hermes-webui-inflight-state',JSON.stringify({[sid]:{},other:{streamId:'other'}}));
    }
    const rawBefore=Object.fromEntries(storage);
    context.clearInflightState(sid,input.legacy?undefined:input.expected);
    context.clearInflight(input.legacy?undefined:sid,input.legacy?undefined:input.expected);
    console.log(JSON.stringify({before,rawBefore,rawAfter:Object.fromEntries(storage)}));return;
  }
  const nativeFetch=globalThis.fetch;
  let release;
  context.fetch=input.base?async(...args)=>{
    const r=await nativeFetch(...args);http.push({status:r.status,body:await r.clone().json()});return r;
  }:()=>new Promise(resolve=>{release=()=>resolve({ok:input.ok!==false});});
  argument=input.alias?session:{session_id:sid,active_stream_id:old};
  const pending=context.cancelSessionStream(argument);
  let rotated=null;
  if(input.rotate){
    if(input.base){
      await new Promise(resolve=>process.stdin.once('data',resolve));
    }
    rotate();rotated=snapshot();
    if(input.base)console.log('ROTATED');
  }
  if(release)release();
  const result=await pending;
  console.log(JSON.stringify({result,rotated,after:snapshot(),argument,effects,http}));
})().catch(err=>{console.error(err);process.exitCode=1;});
"""


def _node(input_data):
    assert NODE, "Node is required for production JavaScript regression tests"
    completed = subprocess.run(
        [NODE, "-e", DRIVER, json.dumps(input_data)], cwd=REPO,
        capture_output=True, text=True, timeout=15, check=True,
    )
    return json.loads(completed.stdout.splitlines()[-1])


@pytest.mark.parametrize("mode", [
    "replacement", "inflight-only", "unknown-inflight", "session-field-only",
    "active-field-only", "caller-rebound", "unknown-active",
])
@pytest.mark.parametrize("alias", [False, True])
def test_replacement_or_unknown_owner_survives_sidebar_cancel(mode, alias):
    result = _node({"rotate": True, "mode": mode, "alias": alias})
    assert result["result"] is True
    assert result["after"] == result["rotated"]
    assert not any(effect[0] in {"close", "resume", "busy", "prompts", "approval", "clarify"}
                   for effect in result["effects"])
    assert not any(effect[0].endswith(("-stop", "-hide")) for effect in result["effects"])


@pytest.mark.parametrize("ok", [False, True])
def test_matching_owner_and_http_failure_keep_existing_cancel_semantics(ok):
    result = _node({"ok": ok})
    assert result["result"] is ok
    after = result["after"]
    if ok:
        assert after["active"] is None and after["sessionActive"] is None
        assert after["inflight"] is None and after["busy"] is False
        assert after["saved"] == {"other": {"streamId": "other"}}
        assert after["activeSaved"] is None
        assert after["live"] is None
        assert ["close", "session-B", "stream-old"] in result["effects"]
    else:
        assert after["active"] == "stream-old" and after["busy"] is True
        assert after["inflight"]["streamId"] == "stream-old"
        assert result["effects"] == []


def test_background_old_cancel_preserves_different_active_session():
    result = _node({"rotate": True, "mode": "switch-session"})
    assert result["after"]["sid"] == "session-A"
    assert result["after"]["active"] == "stream-new"
    assert result["after"]["busy"] is True
    assert result["after"]["inflight"] is None
    assert result["after"]["activeSaved"] == {"sid": "session-A", "streamId": "stream-new"}


def test_storage_replacement_is_not_deleted_by_old_cancel():
    result = _node({"rotate": True, "mode": "storage-only"})
    assert result["after"]["saved"] == result["rotated"]["saved"]
    assert result["after"]["activeSaved"] == result["rotated"]["activeSaved"]


@pytest.mark.parametrize("mode,expected,legacy,deleted", [
    ("matched", "stream-old", False, True),
    ("mismatched", "stream-new", False, False),
    ("unknown-storage", "stream-old", False, False),
    ("malformed-storage", "stream-old", False, False),
    ("matched", None, True, True),
])
def test_storage_helpers_compare_identity_and_preserve_legacy_calls(mode, expected, legacy, deleted):
    result = _node({"helper": True, "mode": mode, "expected": expected, "legacy": legacy})
    if not deleted:
        assert result["rawAfter"] == result["rawBefore"]
    else:
        assert "hermes-webui-inflight" not in result["rawAfter"]
        assert json.loads(result["rawAfter"]["hermes-webui-inflight-state"]) == {
            "other": {"streamId": "other"}}


@pytest.fixture
def cancel_runtime(monkeypatch):
    """Actual server dispatch; the repo conftest owns isolated home/state."""
    from api import config, models, streaming
    import server

    isolated = Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"]).resolve()
    assert Path(config.STATE_DIR).resolve().is_relative_to(isolated)
    assert Path(models.SESSION_DIR).resolve().is_relative_to(isolated)
    Path(models.SESSION_DIR).mkdir(parents=True, exist_ok=True)
    for name in ("SESSIONS", "STREAMS", "CANCEL_FLAGS", "AGENT_INSTANCES", "ACTIVE_RUNS",
                 "STREAM_SESSION_OWNERS"):
        monkeypatch.setattr(config, name, OrderedDict() if name == "SESSIONS" else {})
        if hasattr(streaming, name):
            monkeypatch.setattr(streaming, name, getattr(config, name))
    monkeypatch.setattr(models, "SESSIONS", config.SESSIONS)
    from api import routes
    for name in ("SESSIONS", "STREAMS", "CANCEL_FLAGS", "AGENT_INSTANCES", "ACTIVE_RUNS"):
        if hasattr(routes, name):
            monkeypatch.setattr(routes, name, getattr(config, name))
    gate, release = threading.Event(), threading.Event()
    mode = {"value": ""}

    class AuditHandler(server.Handler):
        def end_headers(self):
            if self.path.startswith("/api/chat/cancel?") and mode["value"] == "headers":
                gate.set()
                assert release.wait(12), "HTTP response barrier was not released"
            return super().end_headers()

    httpd = server.QuietHTTPServer(("127.0.0.1", 0), AuditHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield config, models, streaming, mode, gate, release, f"http://127.0.0.1:{httpd.server_port}/"
    release.set()
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


@pytest.mark.parametrize("phase,missing", [("headers", False), ("interrupt", False), ("headers", True)])
def test_real_http_old_cancel_preserves_new_frontend_and_backend_owner(cancel_runtime, phase, missing):
    cfg, models, streaming, mode, gate, release, base = cancel_runtime
    sid = f"synthetic-owner-{phase}-{missing}"
    old, fresh = sid + "-old", sid + "-new"
    mode["value"] = phase

    class Agent:
        def __init__(self):
            self.session_id = sid
            self.interrupts = []

        def interrupt(self, reason):
            self.interrupts.append(reason)
            if phase == "interrupt":
                gate.set()
                assert release.wait(12), "interrupt barrier was not released"

    previous, replacement = Agent(), Agent()
    session = models.Session(session_id=sid, title="Synthetic owner regression", messages=[], active_stream_id=old)
    session.save()
    cfg.SESSIONS[sid] = session
    if not missing:
        cfg.STREAMS[old] = queue.Queue()
        cfg.CANCEL_FLAGS[old] = threading.Event()
        cfg.AGENT_INSTANCES[old] = previous
    cfg.register_stream_owner(old, sid)
    payload = {"base": base, "sid": sid, "old": old, "fresh": fresh, "rotate": True, "mode": "replacement"}
    proc = subprocess.Popen([NODE, "-e", DRIVER, json.dumps(payload)], cwd=REPO,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert gate.wait(12), "actual cancellation did not reach its barrier"
        with cfg._get_session_agent_lock(sid):
            session.active_stream_id = fresh
            session.save()
            cfg.STREAMS[fresh] = queue.Queue()
            cfg.CANCEL_FLAGS[fresh] = threading.Event()
            cfg.AGENT_INSTANCES[fresh] = replacement
            cfg.register_stream_owner(fresh, sid)
        proc.stdin.write("rotate\n")
        proc.stdin.flush()
        assert proc.stdout.readline().strip() == "ROTATED"
        release.set()
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stderr
        result = json.loads(stdout.splitlines()[-1])
        assert result["http"] == [{"status": 200, "body": {"ok": True, "cancelled": not missing, "stream_id": old}}]
        assert session.active_stream_id == fresh
        assert json.loads((Path(models.SESSION_DIR) / (sid + ".json")).read_text())["active_stream_id"] == fresh
        assert fresh in cfg.STREAMS and replacement.interrupts == []
        assert len(previous.interrupts) == (0 if missing else 1)
        assert result["after"] == result["rotated"]
    finally:
        release.set()
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
