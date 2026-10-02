"""#6657 re-gate: a temporary dropdown option for an ambiguous ``@custom:`` id
must not stamp a guessed provider.

``_ensureModelOptionInDropdown`` creates the pre-hydration temporary option and
``_ensureModelOptionInDropdown``'s dataset fields are read back by
``_clientProviderAuthorityForModel`` as authority for BOTH the persisted state
and the outgoing payload. When the id is ambiguous — ``@custom:gw:8080:free``
means either named provider ``custom:gw`` + model ``8080:free`` or endpoint
``custom:gw:8080`` + model ``free``, and only the catalog can settle it — the
option must keep provider null and the FULL qualified id as the model half so
the backend's config-aware resolver splits it.

Regression walks the real chain the reviewer called out: temporary-option
selection -> ``_modelStateForSelect`` -> ``_writePersistedModelState`` ->
``_readPersistedModelState`` -> ``_modelProviderForSend`` (the chat payload
provider), against the named-``gw`` row of the review's truth table.

Invariants locked here:
  - ambiguous ``@custom:`` id, NO authority  -> {model: full id, provider null}
    through persistence and payload          [THE FIX: was custom:gw:8080/free]
  - explicit preferredProviderId             -> routed halves (unchanged)
  - hydrated catalog authority               -> routed halves (unchanged)
  - configured badge authority               -> routed halves (unchanged)
  - unambiguous ``@custom:`` parse           -> routed halves (unchanged)
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function extract(name){
  const start = src.indexOf('function '+name+'(');
  if(start<0) throw new Error('missing '+name);
  let i = src.indexOf('{', start), depth = 0;
  for(; i<src.length; i++){
    if(src[i]==='{') depth++;
    else if(src[i]==='}' && --depth===0) return src.slice(start, i+1);
  }
  throw new Error('unterminated '+name);
}
function extractConst(name){
  const m = src.match(new RegExp('^const '+name+'=.*$', 'm'));
  if(!m) throw new Error('missing '+name);
  return m[0].replace(/^const /, 'var ');
}
function extractLet(name){
  const m = src.match(new RegExp('^let '+name+'=.*$', 'm'));
  if(!m) throw new Error('missing '+name);
  return m[0].replace(/^let /, 'var ');
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
const window={_configuredModelBadges:{}};
const _dynamicModelLabels={};
const _liveModelFetchPending=new Set();
const $=()=>modelSelect;
const getModelLabel=value=>value;
const syncModelChip=()=>{};
const syncSettingsModelChip=()=>{};
const MODEL_STATE_KEY='hermes-webui-model-state';
const store=new Map();
const localStorage={
  getItem(k){return store.has(k)?store.get(k):null;},
  setItem(k,v){store.set(k,String(v));},
  removeItem(k){store.delete(k);},
};

eval(extractConst('_PY_WS_CLASS'));
eval(extractConst('_CUSTOM_SLUG_TRIM_RE'));
eval(extractConst('_CUSTOM_SLUG_HOST_REJECT_RE'));
eval(extractLet('_dynamicProviderIds'));
for(const name of [
  '_customSlugIsEndpointAuthority',
  '_parseQualifiedCustomId',
  '_optionDeclaredProviderId',
  '_clientProviderAuthorityForModel',
  '_persistedProviderAuthorityForModel',
  '_dynamicProviderAuthorityForQualifiedCustomId',
  '_qualifiedCustomIdNeedsBackendAuthority',
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_modelPickerOptionIdentity',
  '_deduplicateModelPickerOptions',
  '_modelStateForSelect',
  '_storedModelProvider',
  '_readPersistedModelState',
  '_writePersistedModelState',
  '_modelProviderForSend',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_refreshOpenModelDropdown',
  '_ensureModelOptionInDropdown',
]) eval(extract(name));

var modelSelect = null;
var S = {session: null};

// Run the full chain for one ensure call: temporary option -> state ->
// persistence -> fresh read -> chat payload provider.
function drive(id, preferredProviderId, badges){
  window._configuredModelBadges = badges || {};
  const sel = new Node('select');
  sel.id = 'modelSelect';
  // No matching option: _applyModelToDropdown misses, forcing the temporary
  // option path under review.
  modelSelect = sel;
  store.clear();
  const value = _ensureModelOptionInDropdown(id, sel, preferredProviderId || null);
  const state = _modelStateForSelect(sel, value);
  _writePersistedModelState(state.model, state.model_provider);
  const reread = _readPersistedModelState();
  const sendProvider = _modelProviderForSend(state.model);
  return {
    value,
    stampedProvider: sel.options.length ? (sel.options[sel.options.length-1].dataset.provider || null) : null,
    state,
    persisted: reread,
    sendProvider,
  };
}

const AMBIG = '@custom:gw:8080:free';
const out = {};
// THE BUG: named `gw` case, no authority yet (catalog not hydrated, no hint,
// no badge). Must defer: full id as model, null provider everywhere.
out.ambiguousNoAuthority = drive(AMBIG, null, {});
// Explicit caller authority still routes.
out.explicitHint = drive(AMBIG, 'custom:gw', {});
// Configured badge authority still routes.
out.badgeAuthority = drive(AMBIG, null, {[AMBIG]: {provider: 'custom:gw'}});
// Hydrated catalog authority still routes (catalog lists named `custom:gw`).
_dynamicProviderIds['custom:gw'] = true;
out.hydratedAuthority = drive(AMBIG, null, {});
delete _dynamicProviderIds['custom:gw'];
// True endpoint provider, no authority: same defer (backend resolves it to
// the endpoint route) — review's second row, also correct.
out.endpointNoAuthority = drive('@custom:llm:8080:free', null, {});
// Unambiguous named-slug parse (model contains a colon, slug is not an
// endpoint): shape authority alone is definitive and still stamps.
out.unambiguousNamed = drive('@custom:backup:model-a:free', null, {});

process.stdout.write(JSON.stringify(out));
"""


def _run():
    assert NODE is not None
    result = subprocess.run([NODE, "-e", _DRIVER, str(UI_JS)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_ambiguous_id_without_authority_defers_to_backend_everywhere():
    row = _run()["ambiguousNoAuthority"]
    assert row["value"] == "@custom:gw:8080:free"
    assert row["stampedProvider"] is None
    # Full qualified id stays the model half; provider stays null through
    # persistence and the outgoing payload — the backend resolves the split.
    assert row["state"] == {"model": "@custom:gw:8080:free", "model_provider": None}
    assert row["persisted"] == {"model": "@custom:gw:8080:free", "model_provider": None}
    assert row["sendProvider"] is None


def test_explicit_provider_authority_still_routes():
    row = _run()["explicitHint"]
    assert row["state"] == {"model": "8080:free", "model_provider": "custom:gw"}
    assert row["persisted"]["model_provider"] == "custom:gw"
    assert row["sendProvider"] == "custom:gw"


def test_configured_badge_authority_still_routes():
    row = _run()["badgeAuthority"]
    assert row["state"] == {"model": "8080:free", "model_provider": "custom:gw"}
    assert row["sendProvider"] == "custom:gw"


def test_hydrated_catalog_authority_still_routes():
    row = _run()["hydratedAuthority"]
    assert row["state"] == {"model": "8080:free", "model_provider": "custom:gw"}
    assert row["sendProvider"] == "custom:gw"


def test_endpoint_spelling_without_authority_defers_like_the_named_case():
    row = _run()["endpointNoAuthority"]
    assert row["state"] == {"model": "@custom:llm:8080:free", "model_provider": None}
    assert row["sendProvider"] is None


def test_unambiguous_shape_parse_still_stamps():
    row = _run()["unambiguousNamed"]
    assert row["state"] == {"model": "model-a:free", "model_provider": "custom:backup"}
    assert row["sendProvider"] == "custom:backup"
