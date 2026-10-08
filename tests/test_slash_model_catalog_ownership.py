"""Slash intent owns the session before catalog response and JSON awaits."""
import json
import shutil
import subprocess

import pytest

from tests.test_grok_context_window_runtime import _function


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js required for JS runtime coverage")


@pytest.mark.parametrize("route", ["direct", "dropdown", "no-match"])
@pytest.mark.parametrize("boundary", ["response", "json", "fetch-error", "json-error"])
@pytest.mark.parametrize("transition", ["switch", "closed", "same", "empty", "opened"])
def test_slash_catalog_preserves_command_owner(route, boundary, transition):
    sources = {
        "slash": _function("commands.js", "async function cmdModel", "\nfunction "),
        "picker": _function("boot.js", "$('modelSelect').onchange", "$('msg').addEventListener"),
        "apply": _function("boot.js", "function _applySessionContextMetadataUpdate", "$('modelSelect').onchange"),
    }
    script = r"""
const vm=require('vm'), assert=require('assert/strict');
const sources=JSON.parse(process.argv[1]), route=process.argv[2],
  boundary=process.argv[3], transition=process.argv[4];
function deferred(){let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});return {promise,resolve,reject};}
const response=deferred(), decoded=deferred(), requested=deferred(), decoding=deferred();
const calls=[], posts=[];
const select={value:'old',options:[]};
const payload={session:{session_id:'A',context_length:500000,threshold_tokens:0,last_prompt_tokens:0}};
const ctx={S:{session:['empty','opened'].includes(transition)?null:{session_id:'A',model:'old',model_provider:'openai-codex',context_length:272000},
  lastUsage:{context_length:272000,threshold_tokens:190400,input_tokens:1200,output_tokens:80,estimated_cost:0.1}},
  $:()=>select,window:{},document:{baseURI:'http://example.test/'},URL,t:k=>k,
  localStorage:{setItem:()=>calls.push('persist')},
  syncTopbar:()=>calls.push('topbar'),showToast:()=>calls.push('toast'),
  _syncCtxIndicator:()=>calls.push('ring'),
  _modelStateForSelect:()=>({model:'xai-oauth/grok-4.6',model_provider:'xai-oauth'}),
  _buildModelCandidates:()=>({options:[],providerMap:{'xai-oauth/grok-4.6':'xai-oauth'}}),
  _bestModelMatch:()=>route==='dropdown'?'xai-oauth/grok-4.6':null,
  _ensureModelOptionInDropdown:(value,sel)=>{calls.push('option');sel.options.push({value});sel.value=value;},
  _nearestModelSuggestion:()=>'',_looksLikeVersionedModel:()=>false,
  api:async(url,opts)=>{posts.push(JSON.parse(opts.body));return payload;},
  fetch:async(url,opts)=>{
    if(!opts){requested.resolve();return response.promise;}
    posts.push(JSON.parse(opts.body));return {ok:true,json:async()=>payload};
  },
};
vm.createContext(ctx);vm.runInContext(sources.apply+'\n'+sources.picker+'\n'+sources.slash,ctx);
(async()=>{
  const task=ctx.cmdModel(route==='no-match'?'missing':'xai-oauth/grok-4.6');
  await requested.promise;
  if(boundary.startsWith('json')){
    response.resolve({ok:true,json:()=>{decoding.resolve();return decoded.promise;}});
    await decoding.promise;
  }
  if(['switch','opened'].includes(transition)) ctx.S.session={session_id:'B',model:'B-model',model_provider:'B-provider',context_length:64000};
  if(transition==='closed') ctx.S.session=null;
  const before=JSON.stringify(ctx.S), beforeSelect=JSON.stringify(select), beforeCalls=calls.length;
  if(boundary==='fetch-error') response.reject(new Error('catalog unavailable'));
  else if(boundary==='json-error') decoded.reject(new Error('catalog decode failed'));
  else if(boundary==='json') decoded.resolve({groups:[],aliases:{}});
  else response.resolve({ok:true,json:async()=>({groups:[],aliases:{}})});
  await task;
  if(['switch','closed','opened'].includes(transition)){
    assert.equal(posts.length,0,'retired catalog command sent a session update');
    assert.equal(JSON.stringify(ctx.S),before,'retired catalog command mutated active session/usage');
    assert.equal(JSON.stringify(select),beforeSelect,'retired catalog command mutated dropdown');
    assert.equal(calls.length,beforeCalls,'retired catalog command triggered UI/persistence effects');
  }else if(route==='no-match'||(transition==='empty'&&route==='direct')){
    assert.equal(posts.length,0);
    assert.equal(JSON.stringify(ctx.S),before);
    assert.equal(JSON.stringify(select),beforeSelect);
    assert.equal(calls.length,1,'existing no-match feedback remains available');
  }else if(transition==='empty'){
    assert.equal(posts.length,0);
    assert.equal(ctx.S.session,null);
    assert.equal(select.value,'xai-oauth/grok-4.6');
    assert.ok(calls.includes('persist'),'empty composer selection still persists');
  }else{
    assert.equal(posts.length,1);
    assert.equal(posts[0].session_id,'A');
    assert.equal(posts[0].model,'xai-oauth/grok-4.6');
    assert.equal(posts[0].model_provider,'xai-oauth');
    assert.equal(ctx.S.session.model,'xai-oauth/grok-4.6');
    assert.equal(ctx.S.session.model_provider,'xai-oauth');
    assert.equal(ctx.S.session.context_length,500000);
    assert.equal(ctx.S.lastUsage.context_length,500000);
    assert.equal(ctx.S.lastUsage.input_tokens,1200);
    assert.equal(ctx.S.lastUsage.output_tokens,80);
    assert.equal(ctx.S.lastUsage.estimated_cost,0.1);
    if(route==='dropdown') assert.equal(select.value,'xai-oauth/grok-4.6');
  }
})().then(()=>console.log('catalog-ownership-complete')).catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(sources), route, boundary, transition],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "catalog-ownership-complete" in result.stdout, "Node exited before assertions completed"


@pytest.mark.parametrize("transition", ["switch", "closed", "same"])
def test_slash_dropdown_completion_keeps_feedback_with_owner(transition):
    source = _function("commands.js", "async function cmdModel", "\nfunction ")
    script = r"""
const vm=require('vm'),assert=require('assert/strict');
const transition=process.argv[2];
let resolve,startedResolve;
const response=new Promise(r=>resolve=r),started=new Promise(r=>startedResolve=r);
const calls=[],select={value:'old',options:[{value:'grok-4.6'}],
  onchange:()=>{startedResolve();return response;}};
const ctx={S:{session:{session_id:'A'}},$:()=>select,window:{},
  document:{baseURI:'http://example.test/'},URL,t:k=>k,
  fetch:async()=>({ok:true,json:async()=>({groups:[],aliases:{}})}),
  _buildModelCandidates:()=>({options:[],providerMap:{}}),
  _bestModelMatch:()=> 'grok-4.6',showToast:()=>calls.push('toast')};
vm.createContext(ctx);vm.runInContext(process.argv[1],ctx);
(async()=>{
  const task=ctx.cmdModel('grok-4.6');await started;
  if(transition==='switch')ctx.S.session={session_id:'B'};
  if(transition==='closed')ctx.S.session=null;
  const before=JSON.stringify(ctx.S),beforeSelect=JSON.stringify(select);
  resolve();await task;
  assert.equal(JSON.stringify(ctx.S),before);
  assert.equal(JSON.stringify(select),beforeSelect);
  assert.equal(calls.length,transition==='same'?1:0,'completion feedback belongs to command owner');
})().then(()=>console.log('dropdown-completion-complete')).catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        [NODE, "-e", script, source, transition],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dropdown-completion-complete" in result.stdout
