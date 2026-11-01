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
