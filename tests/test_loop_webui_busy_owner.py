"""Busy-chat `/loop` intercept: the originating chat owns the command across the registry wait.

Runs the REAL busy-intercept block of `send()` with the real metadata resolver and the real
command transport in Node; only `api()` is a controllable stub.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _fn(src, name):
    start = src.index(f"function {name}(")
    if src[max(0, start - 6):start] == "async ":
        start -= 6
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
        i += 1


def _run(scenario):
    busy = MESSAGES_JS[MESSAGES_JS.index("if(text.startsWith('/')&&!literalSlash){\n        const _pc="):]
    busy = busy[:busy.index("const defaultMessageMode=")]
    fns = "\n".join(_fn(COMMANDS_JS, n) for n in (
        "parseCommand", "_slashCommandSlugVariants", "loadAgentCommandMetadata", "getAgentCommandMetadata",
        "_canonicalizeSlashCommandToken", "_runAgentCommandTransport", "executeAgentCommand"))
    script = f"""
let _agentCommandCache=null,_agentCommandCachePromise=null,_agentCommandCacheReady=false,_agentCommandCacheAvailable=false,_agentCommandMetadataRetryAt=0;
const _AGENT_COMMAND_METADATA_RETRY_COOLDOWN_MS=5000;
{fns}
const S={{session:{{session_id:'A'}},messages:[],activeProfile:'default',busy:true}};
const composer={{value:''}}; const $=()=>composer; const autoResize=()=>{{}};
const toasts=[]; const showToast=m=>toasts.push(m); const renderMessages=()=>{{}};
const posts=[]; let release; const registry=new Promise(r=>{{release=()=>r({{commands:[{{name:'loop',aliases:['proactive']}}]}});}});
const stops=[]; const COMMANDS=[{{name:'stop',fn:()=>stops.push(S.session.session_id)}}];
const api=async(path,opts)=>{{ if(path==='/api/commands') return registry; posts.push(JSON.parse(opts.body)); return {{output:'ok'}}; }};
const flush=()=>new Promise(r=>setTimeout(r,0));
async function busy(text, literalSlash){{ const r=await (async()=>{{ {busy} return 'fallthrough'; }})(); return r===undefined?'returned':r; }}
(async()=>{{ const out={{}};
{scenario}
console.log(JSON.stringify(out)); }})();
"""
    if not NODE:
        pytest.skip("node is required")
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_chat_switch_during_registry_wait_never_touches_the_new_chat():
    out = _run("""
  const bMsgs=[{role:'user',content:'b'}]; composer.value='/proactive pause';
  const p=busy('/proactive pause'); await flush();
  S.session={session_id:'new-chat'}; S.messages=bMsgs; composer.value='B draft';
  release(); out.result=await p;
  Object.assign(out,{posts,composer:composer.value,bMsgs,toasts});""")
    assert out["posts"] == [] and out["composer"] == "B draft"
    assert out["bMsgs"] == [{"role": "user", "content": "b"}] and out["result"] == "returned"
    assert out["toasts"], "the refused command is reported, not silently dropped"


def test_alias_resolves_to_canonical_loop_for_the_originating_chat():
    out = _run("""
  composer.value='/proactive pause'; const p=busy('/proactive pause'); await flush(); release();
  out.result=await p; Object.assign(out,{posts,msgs:S.messages.map(m=>m.role),composer:composer.value});""")
    assert out["posts"] == [{"command": "/loop pause", "session_id": "A"}]
    assert out["msgs"] == ["user", "assistant"] and out["composer"] == ""


def test_native_loop_and_stop_do_not_wait_for_the_registry():
    out = _run("""
  const r1=await Promise.race([busy('/loop pause'),flush().then(()=>'blocked')]);
  const r2=await Promise.race([busy('/stop'),flush().then(()=>'blocked')]);
  Object.assign(out,{r1,r2,posts,stops});""")
    assert out["r1"] == "returned" and out["r2"] == "returned"
    assert out["posts"] == [{"command": "/loop pause", "session_id": "A"}] and out["stops"] == ["A"]


_SEND_HARNESS = r"""
const vm=require('vm');
let release; const registry=new Promise(r=>{release=()=>r({commands:[{name:'loop',aliases:['proactive']}]});});
const posts=[],queued=[],stops=[],toasts=[];
const el=()=>({value:'',style:{},dataset:{},classList:{add(){},remove(){},toggle(){},contains(){return false;}},
  appendChild(){},addEventListener(){},setAttribute(){},removeAttribute(){},focus(){},querySelector(){return null;},querySelectorAll(){return [];}});
const t={console,setTimeout,clearTimeout,Promise,JSON,Date,Array,String,Object,RegExp,Error,Set,Map,Math,
  document:{querySelector(){return null;},querySelectorAll(){return [];},getElementById(){return null;},createElement:el},
  S:{session:{session_id:'A'},messages:[],activeProfile:'default',busy:__BUSY__,pendingFiles:[],activeStreamId:'st'},
  _sendInProgress:false,_sendInProgressSid:null,_pendingSelections:[],__msg:el(),
  _agentCommandCache:null,_agentCommandCachePromise:null,_agentCommandCacheReady:false,_agentCommandCacheAvailable:false,
  _agentCommandMetadataRetryAt:0,_AGENT_COMMAND_METADATA_RETRY_COOLDOWN_MS:5000,
  api:async(path,opts)=>{if(path==='/api/commands')return registry;posts.push([path,JSON.parse(opts.body)]);return {output:'ok'};},
  queueSessionMessage:(sid,e)=>queued.push([sid,e.text]),showToast:m=>toasts.push(m),
  _composerTextWithPendingSelections:()=>t.__msg.value,_chatPayloadModelState:()=>({}),
  _isWebuiDispatchableAgentCommand:()=>true,
};
t.$=id=>id==='msg'?t.__msg:el(); t.window=t;
Object.assign(t,{posts,queued,stops,toasts});
const STUBBED=[];const ctx=new Proxy(t,{has:(o,k)=>k in o,get:(o,k)=>(k in o?o[k]:(STUBBED.push(k),o[k]=()=>{}))});
vm.createContext(ctx);
vm.runInContext(__SRC__+"\nCOMMANDS=[{name:'stop',fn:()=>stops.push(S.session.session_id)}];var _AGENT_COMMANDS_RUN_ON_WEBUI=new Set(['loop']);",ctx);
const flush=()=>new Promise(r=>setTimeout(r,0));
(async()=>{const out={};
__SCENARIO__
out.posts=posts;out.queued=queued;out.stops=stops;out.toasts=toasts;console.log(JSON.stringify(out));})();
"""


def _run_send(scenario, busy):
    src = "\n".join([_fn(COMMANDS_JS, n) for n in (
        "parseCommand", "_slashCommandSlugVariants", "loadAgentCommandMetadata", "getAgentCommandMetadata",
        "_canonicalizeSlashCommandToken", "_runAgentCommandTransport", "executeAgentCommand")]
        + [MESSAGES_JS[MESSAGES_JS.index("const _NATIVE_BUSY_CONTROLS="):MESSAGES_JS.index("async function send(){")]
           if "const _NATIVE_BUSY_CONTROLS=" in MESSAGES_JS else "", _fn(MESSAGES_JS, "send")])
    script = (_SEND_HARNESS.replace("__SRC__", json.dumps(src)).replace("__BUSY__", busy)
              .replace("__SCENARIO__", scenario))
    if not NODE:
        pytest.skip("node is required")
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_full_send_stop_runs_while_an_alias_metadata_lookup_is_parked():
    """Maintainer probe: busy `/proactive pause` parks send() on the registry; a second `/stop` must stop now."""
    out = _run_send("""
  ctx.__msg.value='/proactive pause'; const first=ctx.send(); await flush();
  ctx.__msg.value='/stop'; await ctx.send();
  out.stopsBeforeRelease=[...stops]; out.queuedBeforeRelease=[...queued];
  release(); await first;""", "true")
    assert out["stopsBeforeRelease"] == ["A"] and out["queuedBeforeRelease"] == []
    assert out["posts"] == [["/api/commands/exec", {"command": "/loop pause", "session_id": "A"}]]


def test_full_send_reentrant_genuine_message_still_queues():
    out = _run_send("""
  ctx.__msg.value='/proactive pause'; const first=ctx.send(); await flush();
  ctx.__msg.value='hello there'; await ctx.send(); release(); await first;""", "true")
    assert out["queued"] == [["A", "hello there"]] and out["stops"] == []


def test_idle_loop_command_never_targets_the_chat_switched_to_during_the_registry_wait():
    """Greptile: the idle path awaits getAgentCommandMetadata() before the transport reads S.session."""
    out = _run_send("""
  ctx.__msg.value='/loop stop'; const p=ctx.send(); await flush();
  ctx.S.session={session_id:'B'}; ctx.S.messages=[]; release(); await p;""", "false")
    assert out["posts"] == [] and out["toasts"]


def test_idle_loop_command_targets_the_originating_chat():
    out = _run_send("""
  ctx.__msg.value='/loop stop'; const p=ctx.send(); await flush(); release(); await p;""", "false")
    assert out["posts"] == [["/api/commands/exec", {"command": "/loop stop", "session_id": "A"}]]


@pytest.mark.parametrize("command", ["/loop\tstop", "/loop\n5m check the build", "/proactive  pause"])
def test_commands_exec_loop_args_split_on_any_whitespace(monkeypatch, command):
    import io
    from urllib.parse import urlparse

    from api import loops, routes
    seen, out = [], {}
    monkeypatch.setattr(routes, "read_body", lambda _h: {"command": command, "session_id": "s1"})
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)
    monkeypatch.setattr(routes, "j", lambda _h, obj, *a, **k: out.update(obj) or True)
    monkeypatch.setattr(loops, "run_loop_command", lambda sid, args, request_profile=None: seen.append((sid, args)) or "ok")
    handler = type("H", (), {"headers": {}, "rfile": io.BytesIO(b""), "command": "POST",
                             "path": "/api/commands/exec", "client_address": ("127.0.0.1", 1)})()
    routes.handle_post(handler, urlparse("/api/commands/exec"))
    assert seen == [("s1", command.split(maxsplit=1)[1])] and out == {"output": "ok"}
