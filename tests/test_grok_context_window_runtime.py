"""Execute the real context metadata consumers, not just source-presence guards."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js required for JS runtime coverage")


def _function(file, start, end):
    source = (ROOT / "static" / file).read_text(encoding="utf-8")
    return source[source.index(start):source.index(end, source.index(start))]


@pytest.mark.parametrize("scenario", ["switch", "no-usage", "deferred", "foreign-session", "slash"])
def test_resolved_window_survives_subsequent_usage_sync(scenario):
    sources = {
        "apply": _function("boot.js", "function _applySessionContextMetadataUpdate", "$('modelSelect').onchange"),
        "deferred": _function("sessions.js", "function _resolveSessionModelForDisplaySoon", "// Tracks whether"),
        "slash": _function("commands.js", "async function cmdModel", "\nfunction "),
    }
    script = r"""
const vm=require('vm');
const assert=require('assert/strict');
const sources=JSON.parse(process.argv[1]);
const scenario=process.argv[2];
const payload={session:{session_id:'current',model:'grok-4.6',model_provider:'xai-oauth',
  context_length:500000,threshold_tokens:0,last_prompt_tokens:0}};
let pending;
const rings=[];
const requests=[];
const ctx={
  S:{session:{session_id:'current',context_length:272000,model_provider:'openai-codex'},
    lastUsage:{context_length:272000,threshold_tokens:190400,last_prompt_tokens:1000,
      input_tokens:1200,output_tokens:80,estimated_cost:0.1}},
  _syncCtxIndicator:u=>rings.push(u),
  syncTopbar:()=>{},
  _deferSessionSideEffect:(sid,fn)=>{pending=fn();},
  api:async()=>payload,
  encodeURIComponent, URL,
  document:{baseURI:'http://example.test/'},
  window:{},
  $:()=>({options:[]}),
  t:key=>key,
  showToast:()=>{},
  _buildModelCandidates:()=>({options:[],providerMap:{}}),
  _bestModelMatch:()=>null,
  _nearestModelSuggestion:()=>'',
  _looksLikeVersionedModel:()=>false,
  fetch:async(url,opts)=>{
    requests.push({url,opts});
    return {ok:true,json:async()=>opts?payload:{groups:[],aliases:{}}};
  },
};
vm.createContext(ctx);
vm.runInContext(sources.apply+'\n'+sources.deferred+'\n'+sources.slash,ctx);
(async()=>{
  if(scenario==='no-usage') ctx.S.lastUsage=null;
  if(scenario==='foreign-session'){
    ctx.api=async()=>{ctx.S.session={session_id:'other',context_length:272000};return payload;};
    ctx._resolveSessionModelForDisplaySoon('current');
    await pending;
    assert.equal(ctx.S.session.context_length,272000);
    assert.equal(ctx.S.lastUsage.context_length,272000);
    assert.equal(rings.length,0);
    return;
  }
  if(scenario==='deferred'){
    ctx._resolveSessionModelForDisplaySoon('current');
    await pending;
  }else if(scenario==='slash'){
    await ctx.cmdModel('xai-oauth/grok-4.6');
    assert.equal(requests.length,2);
    const body=JSON.parse(requests[1].opts.body);
    assert.equal(body.model_provider,'xai-oauth');
    assert.equal(body.session_id,'current');
  }else{
    ctx._applySessionContextMetadataUpdate(payload);
  }
  assert.equal(ctx.S.session.context_length,500000);
  assert.equal(rings.at(-1).context_length,500000);
  // A later ring refresh prefers lastUsage: this was the snap-back boundary.
  const u=ctx.S.lastUsage||{};
  assert.equal(u.context_length||ctx.S.session.context_length,500000);
  if(ctx.S.lastUsage){
    assert.equal(u.threshold_tokens,0);
    assert.equal(u.input_tokens,1200);
    assert.equal(u.output_tokens,80);
    assert.equal(u.estimated_cost,0.1);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(sources), scenario],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
