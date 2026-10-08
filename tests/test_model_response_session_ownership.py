"""Deterministic late-response checks executing the actual model consumers."""
import json
import shutil
import subprocess

import pytest

from tests.test_grok_context_window_runtime import _function


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js required for JS runtime coverage")


@pytest.mark.parametrize("consumer", ["picker", "slash", "helper"])
@pytest.mark.parametrize("boundary", ["response", "json", "foreign", "same", "same-no-id", "late-no-id", "closed"])
def test_model_response_only_updates_owning_session(consumer, boundary):
    if consumer != "slash" and boundary == "json":
        pytest.skip("Only fetch-based slash consumer has a separate JSON await")
    sources = {
        "apply": _function("boot.js", "function _applySessionContextMetadataUpdate", "$('modelSelect').onchange"),
        "picker": _function("boot.js", "$('modelSelect').onchange", "$('msg').addEventListener"),
        "slash": _function("commands.js", "async function cmdModel", "\nfunction "),
    }
    script = r"""
const vm=require('vm'), assert=require('assert/strict');
const sources=JSON.parse(process.argv[1]), consumer=process.argv[2], boundary=process.argv[3];
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
const response=deferred(), json=deferred(), requested=deferred(), decoding=deferred();
const calls=[];
const payload={session:{session_id:boundary==='foreign'?'foreign':'A',
  context_length:500000,threshold_tokens:0,last_prompt_tokens:0}};
if(boundary.endsWith('no-id')) delete payload.session.session_id;
const select={value:'xai-oauth/grok-4.6',options:[]};
const ctx={S:{session:{session_id:'A',model:'old',model_provider:'openai-codex',context_length:272000},
  lastUsage:{context_length:272000,threshold_tokens:190400,last_prompt_tokens:1000,
    input_tokens:1200,output_tokens:80,estimated_cost:0.1}},
  $:()=>select, window:{}, document:{baseURI:'http://example.test/'}, URL,
  localStorage:{setItem:()=>{}}, t:k=>k,
  syncTopbar:()=>calls.push('topbar'), showToast:()=>calls.push('toast'),
  _syncCtxIndicator:u=>calls.push(['ring',u]),
  _checkProviderMismatch:()=>{calls.push('mismatch');return 'warning';},
  _buildModelCandidates:()=>({options:[],providerMap:{}}),
  _bestModelMatch:()=>null, _nearestModelSuggestion:()=>'', _looksLikeVersionedModel:()=>false,
  api:(url,opts)=>{requested.resolve(JSON.parse(opts.body));return response.promise;},
  fetch:async(url,opts)=>{
    if(!opts) return {ok:true,json:async()=>({groups:[],aliases:{}})};
    requested.resolve(JSON.parse(opts.body));return response.promise;
  },
};
vm.createContext(ctx);
vm.runInContext(sources.apply+'\n'+sources.picker+'\n'+sources.slash,ctx);
(async()=>{
  let task;
  if(consumer==='helper') task=null;
  else {
    task=consumer==='picker'?select.onchange():ctx.cmdModel('xai-oauth/grok-4.6');
    assert.equal((await requested.promise).session_id,'A');
  }
  if(boundary==='json'){
    response.resolve({ok:true,json:()=>{decoding.resolve();return json.promise;}});
    await decoding.promise;
  }
  const late=['response','json','late-no-id','closed'].includes(boundary);
  if(late) ctx.S.session=boundary==='closed'?null:{session_id:'B',model:'B-model',
    model_provider:'B-provider',context_length:64000,threshold_tokens:32000,last_prompt_tokens:900,
    input_tokens:222,output_tokens:33,estimated_cost:0.4};
  const before=JSON.stringify(ctx.S), beforeCalls=calls.length;
  if(consumer==='helper') ctx._applySessionContextMetadataUpdate(payload);
  else {
    if(boundary==='json') json.resolve(payload);
    else response.resolve(consumer==='picker'?payload:{ok:true,json:async()=>payload});
    await task;
  }
  // A helper without request identity retains legacy no-ID compatibility; callers own that guard.
  const reject=boundary==='foreign'||(late&&!(consumer==='helper'&&boundary==='late-no-id'));
  if(reject){
    assert.equal(JSON.stringify(ctx.S),before,'foreign/retired response mutated active state');
    assert.equal(calls.length,beforeCalls,'foreign/retired response triggered UI effects');
  }else{
    assert.equal(ctx.S.session.context_length,500000);
    assert.equal(ctx.S.lastUsage.context_length,500000);
    assert.equal(ctx.S.lastUsage.threshold_tokens,0);
    assert.equal(ctx.S.lastUsage.input_tokens,1200);
    assert.equal(ctx.S.lastUsage.output_tokens,80);
    assert.equal(ctx.S.lastUsage.estimated_cost,0.1);
    if(consumer==='slash'){
      assert.equal(ctx.S.session.model,'xai-oauth/grok-4.6');
      assert.equal(ctx.S.session.model_provider,'xai-oauth');
    }
  }
})().then(()=>console.log('ownership-check-complete')).catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(sources), consumer, boundary],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ownership-check-complete" in result.stdout, "Node exited before assertions completed"
