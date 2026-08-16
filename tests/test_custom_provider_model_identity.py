"""Regression coverage for exact custom-provider model identity during catalog refresh."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_loader import node_validated_read_options, node_validated_read_snippet

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

_DRIVER = (
    node_validated_read_snippet()
    + "\nconst src=readValidated(process.argv[2], "
    + node_validated_read_options(UI_JS)
    + ");\n"
    + r"""
function extract(name){
  const start=src.indexOf('function '+name+'(');
  if(start<0) throw new Error('missing '+name);
  let i=src.indexOf('{',start), depth=0;
  for(;i<src.length;i++){
    if(src[i]==='{') depth++;
    else if(src[i]==='}'&&--depth===0) return src.slice(start,i+1);
  }
  throw new Error('unterminated '+name);
}
class Node {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.parentElement=null;this._value='';this.textContent='';this.id='';}
  appendChild(child){child.parentElement=this;this.children.push(child);return child;}
  removeChild(child){this.children=this.children.filter(x=>x!==child);child.parentElement=null;}
  querySelectorAll(selector){
    if(selector==='optgroup') return this.children.filter(x=>x.tagName==='OPTGROUP');
    return [];
  }
  get options(){
    if(this.tagName!=='SELECT') return [];
    return this.children.flatMap(x=>x.tagName==='OPTGROUP'?x.children:[x]).filter(x=>x.tagName==='OPTION');
  }
  get value(){return this._value;}
  set value(value){this._value=String(value||'');}
  get selectedOptions(){const hit=this.options.find(x=>x.value===this._value);return hit?[hit]:[];}
}
const document={createElement:tag=>new Node(tag)};
const window={_configuredModelBadges:{},_activeProvider:'custom:cpa'};
const S={session:null};
const _dynamicModelLabels={};
const _liveModelFetchPending=new Set();
const $=()=>null;
const getModelLabel=value=>value;
const syncModelChip=()=>{};
for(const name of ['_getOptionProviderId','_providerFromModelValue','_modelPickerOptionIdentity','_deduplicateModelPickerOptions','_modelStateForSelect','_findModelInDropdown','_refreshOpenModelDropdown','_applyModelToDropdown','_ensureModelOptionInDropdown','_addLiveModelsToSelect']) eval(extract(name));
function makeSelect(selected){
  const sel=new Node('select');sel.id='modelSelect';
  const group=new Node('optgroup');group.dataset.provider='custom:cpa';sel.appendChild(group);
  for(const id of ['jb/gpt-5.6-sol']){const opt=new Node('option');opt.value=id;opt.dataset.provider='custom:cpa';group.appendChild(opt);}
  sel.value=selected||'jb/gpt-5.6-sol';return sel;
}
const bare=makeSelect();
const appliedBare=_ensureModelOptionInDropdown('gpt-5.6-sol',bare,'custom:cpa');
const beforeLive=_modelStateForSelect(bare,bare.value);
_addLiveModelsToSelect('custom:cpa',[{id:'gpt-5.6-sol'},{id:'jb/gpt-5.6-sol'}],bare);
const afterLive=_modelStateForSelect(bare,bare.value);
const qualified=makeSelect('jb/gpt-5.6-sol');
_addLiveModelsToSelect('custom:cpa',[{id:'gpt-5.6-sol'}],qualified);
console.log(JSON.stringify({
  appliedBare,beforeLive,afterLive,
  bareOptions:bare.options.map(x=>x.value),
  qualifiedAfter:_modelStateForSelect(qualified,qualified.value),
}));
"""
)


@pytest.mark.skipif(NODE is None, reason="node not in PATH")
def test_partial_then_live_catalog_preserves_exact_custom_provider_model(tmp_path):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    expected = {"model": "gpt-5.6-sol", "model_provider": "custom:cpa"}
    assert payload["beforeLive"] == expected
    assert payload["afterLive"] == expected
    assert payload["appliedBare"] == "@custom:cpa:gpt-5.6-sol"
    assert "@custom:cpa:gpt-5.6-sol" in payload["bareOptions"]
    assert "@custom:cpa:jb/gpt-5.6-sol" in payload["bareOptions"]
    assert payload["qualifiedAfter"] == {
        "model": "jb/gpt-5.6-sol",
        "model_provider": "custom:cpa",
    }


# Ambiguity guard for the #6195 bare-id fallback (Codex round-2 SHOULD-FIX):
# when two options under the same custom provider normalize to the same target,
# the hinted-fallback must refuse to guess and return null rather than pick
# an arbitrary one.
_AMBIGUITY_DRIVER = (
    node_validated_read_snippet()
    + "\nconst src=readValidated(process.argv[2], "
    + node_validated_read_options(UI_JS)
    + ");\n"
    + r"""
function extract(name){
  const start=src.indexOf('function '+name+'(');
  if(start<0) throw new Error('missing '+name);
  let i=src.indexOf('{',start), depth=0;
  for(;i<src.length;i++){
    if(src[i]==='{') depth++;
    else if(src[i]==='}'&&--depth===0) return src.slice(start,i+1);
  }
  throw new Error('unterminated '+name);
}
function _getOptionProviderId(o){return o.providerId||'';}
for(const name of ['_findModelInDropdown']) eval(extract(name));
// Single unambiguous suffix match -> resolves to the existing namespaced option.
const single={options:[
  {value:'@custom:tok:z-ai/glm-5.2', providerId:'custom:tok'},
  {value:'openai/gpt-4', providerId:'openai'},
]};
// Two same-provider options normalizing to the same target -> ambiguous.
const ambiguous={options:[
  {value:'@custom:tok:z-ai/glm-5.2', providerId:'custom:tok'},
  {value:'@custom:tok:other/glm-5.2', providerId:'custom:tok'},
]};
console.log(JSON.stringify({
  single:_findModelInDropdown('glm-5.2', single, 'custom:tok'),
  ambiguous:_findModelInDropdown('glm-5.2', ambiguous, 'custom:tok'),
  exact:_findModelInDropdown('z-ai/glm-5.2', single, 'custom:tok'),
}));
"""
)


@pytest.mark.skipif(NODE is None, reason="node not in PATH")
def test_hinted_bare_id_fallback_refuses_ambiguous_match(tmp_path):
    driver = tmp_path / "amb_driver.js"
    driver.write_text(_AMBIGUITY_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    # One unambiguous suffix match: the bare id resolves to the existing
    # namespaced catalog option (#6195 repair preserved).
    assert payload["single"] == "@custom:tok:z-ai/glm-5.2"
    # Two same-normalized options under the same provider: refuse to guess.
    assert payload["ambiguous"] is None
    # Exact routed match is unaffected by the fallback path (#6944 fix intact).
    assert payload["exact"] == "@custom:tok:z-ai/glm-5.2"
