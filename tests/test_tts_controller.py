"""Shared browser TTS controller and binary transport tests."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
TTS_JS = (ROOT / "static" / "tts.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def _node(script: str):
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_tts_loads_before_consumers_without_breaking_ui_workspace_dependency():
    tts = INDEX.index('static/tts.js')
    ui = INDEX.index('static/ui.js')
    workspace = INDEX.index('static/workspace.js')
    assert tts < ui < workspace
    for consumer in ('static/panels.js', 'static/boot.js'):
        assert workspace < INDEX.index(consumer)


def test_binary_api_consumes_array_buffer_and_bridges_upstream_signal():
    start = WORKSPACE_JS.index("async function api(")
    end = WORKSPACE_JS.index("\n\nfunction recordClientSSEError", start)
    api_source = WORKSPACE_JS[start:end]
    script = f"""
const api=(0,eval)("("+{json.dumps(api_source)}+")");
globalThis.document={{baseURI:'https://example.test/hermes/'}};
globalThis.location={{href:'https://example.test/hermes/',pathname:'/hermes/',search:''}};
globalThis.window={{location:globalThis.location}};
let actualSignal=null;
globalThis.fetch=async(url,opts)=>{{
  actualSignal=opts.signal;
  return {{ok:true,status:200,headers:{{get:(name)=>name.toLowerCase()==='content-type'?'audio/wav':name.toLowerCase()==='content-length'?'4':null}},arrayBuffer:async()=>new Uint8Array([1,2,3,4]).buffer}};
}};
const caller=new AbortController();
api('/api/tts',{{method:'POST',body:'{{}}',responseType:'binary',signal:caller.signal,retries:0}}).then(result=>{{
  process.stdout.write(JSON.stringify({{urlLength:result.data.byteLength,contentType:result.contentType,contentLength:result.contentLength,bridged:actualSignal!==caller.signal&&!actualSignal.aborted}}));
}});
"""
    assert _node(script) == {
        "urlLength": 4,
        "contentType": "audio/wav",
        "contentLength": 4,
        "bridged": True,
    }


def _controller_prelude(api_impl: str, *, engine="agent") -> str:
    return f"""
globalThis.window=globalThis;
globalThis.addEventListener=()=>{{}};
const stored=new Map([['hermes-tts-engine',{json.dumps(engine)}]]);
globalThis.localStorage={{getItem:key=>stored.has(key)?stored.get(key):null,setItem:(key,value)=>stored.set(key,String(value))}};
globalThis.S={{profile:'default'}};
globalThis.URL={{createObjectURL:()=> 'blob:fixture',revokeObjectURL:()=>{{}}}};
globalThis.Blob=class Blob{{constructor(parts,opts){{this.parts=parts;this.type=opts.type;}}}};
globalThis.Audio=class Audio{{constructor(url){{this.url=url;globalThis.audioCount=(globalThis.audioCount||0)+1;}}play(){{queueMicrotask(()=>this.onended&&this.onended());return Promise.resolve();}}pause(){{}}removeAttribute(){{}}load(){{}}}};
globalThis.api={api_impl};
{TTS_JS}
"""


def test_agent_controller_chunks_by_code_point_and_plays_sequentially_once_each():
    api_impl = """async(path,opts)=>{
  globalThis.calls=(globalThis.calls||[]);calls.push({path,body:opts.body,retries:opts.retries,signal:opts.signal});
  if(path==='/api/tts/capability')return {synthesis_supported:true,active_provider_available:true,request_max_text_length:3};
  return {data:new Uint8Array([82,73,70,70]).buffer,contentType:'audio/wav',contentLength:4,status:200,headers:{}};
}"""
    script = _controller_prelude(api_impl) + """
HermesTTS.speak('A😀B C😀D',{engine:'agent'}).then(result=>{
  const synth=calls.filter(call=>call.path==='/api/tts');
  process.stdout.write(JSON.stringify({chunks:result.chunks,texts:synth.map(call=>JSON.parse(call.body).text),retries:synth.map(call=>call.retries),audioCount,signals:synth.every(call=>call.signal&&typeof call.signal.aborted==='boolean')}));
});
"""
    assert _node(script) == {
        "chunks": 3,
        "texts": ["A😀B", "C😀", "D"],
        "retries": [0, 0, 0],
        "audioCount": 3,
        "signals": True,
    }


def test_agent_controller_rejects_missing_mime_and_never_plays():
    api_impl = """async(path,opts)=>path==='/api/tts/capability'
 ? {synthesis_supported:true,active_provider_available:true,request_max_text_length:100}
 : {data:new Uint8Array([1,2,3]).buffer,contentType:'',contentLength:3,status:200,headers:{}}"""
    script = _controller_prelude(api_impl) + """
HermesTTS.speak('hello',{engine:'agent'}).then(()=>process.exit(2)).catch(error=>{
  process.stdout.write(JSON.stringify({message:error.message,audioCount:globalThis.audioCount||0}));
});
"""
    result = _node(script)
    assert "unsupported audio type" in result["message"]
    assert result["audioCount"] == 0


def test_external_abort_reaches_actual_synthesis_signal_without_late_audio():
    api_impl = """async(path,opts)=>{
  if(path==='/api/tts/capability')return {synthesis_supported:true,active_provider_available:true,request_max_text_length:100};
  globalThis.actualSignal=opts.signal;
  return await new Promise((resolve,reject)=>{
    opts.signal.addEventListener('abort',()=>{const error=new Error('aborted');error.name='AbortError';reject(error);},{once:true});
  });
}"""
    script = _controller_prelude(api_impl) + """
const caller=new AbortController();
const pending=HermesTTS.speak('hello',{engine:'agent',signal:caller.signal}).catch(error=>error.name);
setTimeout(()=>caller.abort('user'),0);
pending.then(name=>process.stdout.write(JSON.stringify({name,aborted:actualSignal.aborted,audioCount:globalThis.audioCount||0})));
"""
    assert _node(script) == {"name": "AbortError", "aborted": True, "audioCount": 0}


def test_replacement_aborts_first_generation_and_only_second_audio_plays():
    api_impl = """async(path,opts)=>{
  if(path==='/api/tts/capability')return {synthesis_supported:true,active_provider_available:true,request_max_text_length:100};
  globalThis.synthCount=(globalThis.synthCount||0)+1;
  if(synthCount===1)return await new Promise((resolve,reject)=>{
    opts.signal.addEventListener('abort',()=>{const error=new Error('replaced');error.name='AbortError';reject(error);},{once:true});
  });
  return {data:new Uint8Array([82,73,70,70]).buffer,contentType:'audio/wav',contentLength:4,status:200,headers:{}};
}"""
    script = _controller_prelude(api_impl) + """
let revoked=0;URL.revokeObjectURL=()=>{revoked+=1};
let firstStops=0;
const first=HermesTTS.speak('first',{engine:'agent',onStop:reason=>{if(reason==='replaced')firstStops+=1}}).catch(error=>error.name);
setTimeout(()=>{
  const second=HermesTTS.speak('second',{engine:'agent'});
  Promise.all([first,second]).then(([firstResult,secondResult])=>process.stdout.write(JSON.stringify({
    firstResult,secondResult,firstStops,synthCount,audioCount:globalThis.audioCount||0,revoked
  })));
},0);
"""
    assert _node(script) == {
        "firstResult": "AbortError",
        "secondResult": {"engine": "agent", "chunks": 1},
        "firstStops": 1,
        "synthCount": 2,
        "audioCount": 1,
        "revoked": 1,
    }


def test_browser_replacement_stale_callback_cannot_cancel_new_speech():
    api_impl = "async()=>{throw new Error('api should not be called')}"
    script = _controller_prelude(api_impl, engine="browser") + """
globalThis.SpeechSynthesisUtterance=class SpeechSynthesisUtterance{constructor(text){this.text=text;}};
let current=null,cancelCount=0;
const spoken=[];
globalThis.speechSynthesis={
  getVoices:()=>[],speaking:false,pause:()=>{},resume:()=>{},
  cancel:()=>{cancelCount+=1;if(current&&current.onerror){const stale=current.onerror;queueMicrotask(()=>stale({error:'canceled'}));}},
  speak:utter=>{current=utter;spoken.push(utter.text);if(utter.text==='second')setTimeout(()=>utter.onend&&utter.onend(),5);}
};
const first=HermesTTS.speak('first',{engine:'browser'}).catch(error=>error.name);
setTimeout(()=>{
  const second=HermesTTS.speak('second',{engine:'browser'});
  Promise.all([first,second]).then(([firstResult,secondResult])=>process.stdout.write(JSON.stringify({firstResult,secondResult,spoken,cancelCount})));
},0);
"""
    assert _node(script) == {
        "firstResult": "AbortError",
        "secondResult": {"engine": "browser", "chunks": 1},
        "spoken": ["first", "second"],
        "cancelCount": 1,
    }


def test_stop_settles_active_agent_media_without_terminal_audio_event():
    api_impl = """async(path,opts)=>path==='/api/tts/capability'
 ? {synthesis_supported:true,active_provider_available:true,request_max_text_length:100}
 : {data:new Uint8Array([82,73,70,70]).buffer,contentType:'audio/wav',contentLength:4,status:200,headers:{}}"""
    script = _controller_prelude(api_impl, engine="agent") + """
let revoked=0;URL.revokeObjectURL=()=>{revoked+=1};
globalThis.Audio=class Audio{constructor(){globalThis.audioCount=(globalThis.audioCount||0)+1;}play(){return Promise.resolve();}pause(){}removeAttribute(){}load(){}};
const pending=HermesTTS.speak('hello',{engine:'agent'}).catch(error=>error.name);
const timer=setInterval(()=>{if((globalThis.audioCount||0)>0){clearInterval(timer);HermesTTS.stop('user');}},1);
pending.then(result=>process.stdout.write(JSON.stringify({result,audioCount,revoked})));
"""
    assert _node(script) == {"result": "AbortError", "audioCount": 1, "revoked": 1}


def test_split_text_never_splits_astral_code_points():
    api_impl = "async()=>({})"
    script = _controller_prelude(api_impl) + """
process.stdout.write(JSON.stringify(HermesTTS.splitText('😀😀😀',2)));
"""
    assert _node(script) == ["😀😀", "😀"]


def test_call_sites_share_controller_and_have_no_direct_tts_fetch():
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "window.HermesTTS.speak(clean" in ui
    assert "window.HermesTTS.speak(clean" in boot
    assert "api/tts" not in ui
    assert "api/tts" not in boot
    assert "HermesTTS.stop('session-changed')" in sessions
    assert "HermesTTS.invalidateCapability()" in panels


def test_former_extension_id_degrades_to_browser_without_overwriting_storage():
    api_impl = "async()=>{throw new Error('api should not be called')}"
    script = _controller_prelude(api_impl, engine="voicevox_local") + """
globalThis.SpeechSynthesisUtterance=class SpeechSynthesisUtterance{constructor(text){this.text=text;this.rate=1;this.pitch=1;}};
globalThis.speechSynthesis={getVoices:()=>[],cancel:()=>{},pause:()=>{},resume:()=>{},speaking:false,speak:utter=>queueMicrotask(()=>utter.onend())};
let degraded=null;
HermesTTS.speak('hello',{onDegraded:value=>degraded=value}).then(result=>{
  process.stdout.write(JSON.stringify({result,degraded,persisted:stored.get('hermes-tts-engine')}));
});
"""
    result = _node(script)
    assert result["result"] == {"engine": "browser", "chunks": 1}
    assert result["degraded"]["persistedEngine"] == "voicevox_local"
    assert result["persisted"] == "voicevox_local"


def test_unavailable_agent_degrades_to_browser_without_overwriting_storage():
    api_impl = "async path=>({synthesis_supported:false,active_provider_available:false,request_max_text_length:100})"
    script = _controller_prelude(api_impl, engine="agent") + """
globalThis.SpeechSynthesisUtterance=class SpeechSynthesisUtterance{constructor(text){this.text=text;this.rate=1;this.pitch=1;}};
globalThis.speechSynthesis={getVoices:()=>[],cancel:()=>{},pause:()=>{},resume:()=>{},speaking:false,speak:utter=>queueMicrotask(()=>utter.onend())};
let degraded=null;
HermesTTS.speak('hello',{onDegraded:value=>degraded=value}).then(result=>{
  process.stdout.write(JSON.stringify({result,degraded,persisted:stored.get('hermes-tts-engine')}));
});
"""
    result = _node(script)
    assert result["result"] == {"engine": "browser", "chunks": 1}
    assert result["degraded"]["persistedEngine"] == "agent"
    assert result["persisted"] == "agent"


def test_agent_output_does_not_require_browser_speech_synthesis():
    api_impl = """async(path,opts)=>path==='/api/tts/capability'
 ? {synthesis_supported:true,active_provider_available:true,request_max_text_length:100}
 : {data:new Uint8Array([82,73,70,70]).buffer,contentType:'audio/wav',contentLength:4,status:200,headers:{}}"""
    script = _controller_prelude(api_impl, engine="agent") + """
delete globalThis.speechSynthesis;
delete globalThis.SpeechSynthesisUtterance;
HermesTTS.speak('hello').then(result=>process.stdout.write(JSON.stringify(result)));
"""
    assert _node(script) == {"engine": "agent", "chunks": 1}


def test_capability_cache_is_scoped_to_active_profile_not_session_profile():
    api_impl = """async()=>{
  globalThis.calls=(globalThis.calls||0)+1;
  return {profile:globalThis.S.activeProfile,synthesis_supported:true,active_provider_available:true};
}"""
    script = _controller_prelude(api_impl) + """
S={activeProfile:'alpha',session:{profile:'old-session'}};
HermesTTS.getCapability().then(first=>{
  S.activeProfile='beta';
  return HermesTTS.getCapability().then(second=>{
    process.stdout.write(JSON.stringify({calls,first:first.profile,second:second.profile}));
  });
});
"""
    assert _node(script) == {"calls": 2, "first": "alpha", "second": "beta"}
