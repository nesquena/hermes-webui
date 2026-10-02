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
function drive(id, preferredProviderId, badges, dynamicIds){
  window._configuredModelBadges = badges || {};
  for(const k of Object.keys(_dynamicProviderIds)) delete _dynamicProviderIds[k];
  Object.assign(_dynamicProviderIds, dynamicIds || {});
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
  const lastOpt = sel.options.length ? sel.options[sel.options.length-1] : null;
  return {
    value,
    stampedProvider: lastOpt ? (lastOpt.dataset.provider || null) : null,
    stampedModel: lastOpt ? (lastOpt.dataset.model || null) : null,
    state,
    persisted: reread,
    sendProvider,
  };
}

const AMBIG = '@custom:gw:8080:free';
const MULTI = '@custom:gw:8080:free:32b';
const out = {};
// THE BUG: named `gw` case, no authority yet (catalog not hydrated, no hint,
// no badge). Must defer: full id as model, null provider everywhere.
out.ambiguousNoAuthority = drive(AMBIG, null, {});
// Explicit caller authority still routes.
out.explicitHint = drive(AMBIG, 'custom:gw', {});
// Configured badge authority still routes.
out.badgeAuthority = drive(AMBIG, null, {[AMBIG]: {provider: 'custom:gw'}});
// Hydrated catalog authority still routes (catalog lists named `custom:gw`).
out.hydratedAuthority = drive(AMBIG, null, {}, {'custom:gw': true});
// True endpoint provider, no authority: same defer (backend resolves it to
// the endpoint route) — review's second row, also correct.
out.endpointNoAuthority = drive('@custom:llm:8080:free', null, {});
// Unambiguous named-slug parse (model contains a colon, slug is not an
// endpoint): shape authority alone is definitive and still stamps.
out.unambiguousNamed = drive('@custom:backup:model-a:free', null, {});

// ── 2026-10-02 re-gate: a SECOND valid colon in the model name. With named
// provider `custom:gw` advertising model `8080:free:32b`, the qualified value
// `@custom:gw:8080:free:32b` is also shape-compatible with endpoint
// `custom:gw:8080` + model `free:32b`. The pre-final-colon hint
// `gw:8080:free` is NOT a host:port, so a gate that only inspects it misses
// the ambiguity and the shape guess `custom:gw:8080` stamps and sends.
out.multiNoAuthority = drive(MULTI, null, {});
// Named authority (badge) must win for BOTH halves; the shape guess must not
// override it.
out.multiNamedBadge = drive(MULTI, null, {[MULTI]: {provider: 'custom:gw'}});
// Named authority (explicit hint) routes both halves.
out.multiNamedHint = drive(MULTI, 'custom:gw', {});
// Named authority (hydrated catalog) routes both halves.
out.multiNamedCatalog = drive(MULTI, null, {}, {'custom:gw': true});
// Endpoint-only catalog authority routes the endpoint reading.
out.multiEndpointCatalog = drive(MULTI, null, {}, {'custom:gw:8080': true});

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


# ── 2026-10-02 re-gate: multi-colon model names (second valid colon) ─────────

def test_multi_colon_model_without_authority_defers_to_backend_everywhere():
    """`@custom:gw:8080:free:32b`: the final-colon hint `gw:8080:free` is not a
    host:port, so the pre-2026-10-02 gate called it unambiguous and the shape
    guess `custom:gw:8080` / `free:32b` stamped, persisted and sent. The id is
    ambiguous because the `gw:8080` prefix IS a viable endpoint authority with
    a nonempty `free:32b` remainder — no authority, nothing stamps."""
    row = _run()["multiNoAuthority"]
    assert row["value"] == "@custom:gw:8080:free:32b"
    assert row["stampedProvider"] is None
    assert row["stampedModel"] is None
    assert row["state"] == {"model": "@custom:gw:8080:free:32b", "model_provider": None}
    assert row["persisted"] == {"model": "@custom:gw:8080:free:32b", "model_provider": None}
    assert row["sendProvider"] is None


def test_multi_colon_named_badge_wins_for_both_halves():
    """A configured badge declaring named `custom:gw` routes BOTH halves and is
    not overridden by the shape guess (`requestedProvider || badge.provider`
    used to swallow it). The dataset.model stamp is produced only for an
    explicit requestedProvider (pre-existing contract); the badge's model half
    routes through the provider stamp via _modelStateForSelect."""
    row = _run()["multiNamedBadge"]
    assert row["state"] == {"model": "8080:free:32b", "model_provider": "custom:gw"}
    assert row["stampedProvider"] == "custom:gw"
    assert row["persisted"] == {"model": "8080:free:32b", "model_provider": "custom:gw"}
    assert row["sendProvider"] == "custom:gw"


def test_multi_colon_named_hint_and_catalog_route_both_halves():
    for key in ("multiNamedHint", "multiNamedCatalog"):
        row = _run()[key]
        assert row["state"] == {"model": "8080:free:32b", "model_provider": "custom:gw"}, key
        assert row["sendProvider"] == "custom:gw", key
    assert _run()["multiNamedHint"]["stampedModel"] == "8080:free:32b"


def test_multi_colon_endpoint_catalog_routes_endpoint_reading():
    """Endpoint-only config: the hydrated catalog hit `custom:gw:8080` is real
    authority and routes provider `custom:gw:8080` + model `free:32b`."""
    row = _run()["multiEndpointCatalog"]
    assert row["state"] == {"model": "free:32b", "model_provider": "custom:gw:8080"}
    assert row["sendProvider"] == "custom:gw:8080"
