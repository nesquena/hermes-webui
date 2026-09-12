"""Auxiliary-task pickers must keep provider groups that only carry an endpoint error (#7521).

``GET /api/models`` keeps a named custom provider group with ``models: []`` plus
a ``models_endpoint_error`` payload when its ``/v1/models`` probe fails (that
contract is pinned by ``tests/test_issue2540_models_endpoint_error.py`` and is
rendered by the main picker in ``static/ui.js``). The auxiliary-task pickers in
``static/panels.js`` dropped every zero-model group, so a provider with an
unreachable models endpoint silently disappeared from both the provider and the
model selects — the user could neither see the provider nor the reason it had no
models.

Every test here EXECUTES the production code (extracted from ``static/panels.js``
and driven with stubs) and asserts on the resulting option lists — no
source-string greps.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = ROOT / "static" / "panels.js"
NODE = shutil.which("node")

# Shared node prelude: read panels.js, pull top-level ``function`` bodies out of
# the bundle by brace matching, and evaluate them in isolation (same technique
# as tests/test_auxiliary_models_settings.py), plus a minimal element stub good
# enough to drive the option builders and the auxiliary loader.
NODE_PRELUDE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function extract(name){
  const re = new RegExp('(async\\s+)?function\\s+' + name + '\\s*\\(');
  const match = re.exec(src);
  if(!match) throw new Error(name + ' not found');
  const start = match.index;
  let i = src.indexOf('{', start);
  let depth = 0;
  while(i < src.length){
    const ch = src[i];
    if(ch === '{') depth += 1;
    else if(ch === '}') {
      depth -= 1;
      if(depth === 0){
        break;
      }
    }
    i += 1;
  }
  if(depth !== 0) throw new Error(name + ' parse failed');
  return src.slice(start, i + 1);
}

const _byId = {};
function _stubElement(tag){
  return {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    attrs: {},
    disabled: false,
    selected: false,
    _value: '',
    _text: '',
    _html: '',
    _id: '',
    set id(v){ this._id = v; if(v) _byId[v] = this; },
    get id(){ return this._id; },
    set value(v){ this._value = v; },
    get value(){ return this._value; },
    set textContent(v){ this._text = v; },
    get textContent(){ return this._text; },
    set innerHTML(v){ this._html = v; if(v === '') this.children = []; },
    get innerHTML(){ return this._html; },
    setAttribute(name, value){ this.attrs[name] = value; },
    addEventListener(){},
    appendChild(child){ this.children.push(child); return child; },
    insertBefore(child, ref){
      const idx = this.children.indexOf(ref);
      if(idx < 0){ this.children.push(child); } else { this.children.splice(idx, 0, child); }
      return child;
    },
    get options(){ return this.children; },
  };
}

global.document = { createElement: (tag) => _stubElement(tag) };
global.window = global;
global.t = (key) => key;
global.esc = (value) => String(value);
global.li = () => '';
global.$ = (id) => _byId[id] || null;
global.__byId = _byId;
"""

# Loader prelude: the real helpers _loadAuxiliaryModels() calls, plus the module
# state it owns and no-op stubs for the collaborators a headless run cannot touch.
LOADER_PRELUDE = r"""
eval(extract('_modelBareNameForProvider'));
eval(extract('_auxSelectStyle'));
eval(extract('_auxTaskLabelFromMeta'));
eval(extract('_normalizeAuxiliaryTasks'));
eval(extract('_auxProvidersFromModelGroups'));
eval(extract('_buildAuxProviderOptions'));
eval(extract('_buildAuxModelOptions'));
eval(extract('_onAuxProviderChange'));
eval(extract('_loadAuxiliaryModels'));

let _auxTasks = [];
let _auxProviders = [];
let _auxOriginalConfig = {};
let _mainAdvancedConfig = null;

function _bindMainAdvancedOptionsButton(){}
function _openAuxAdvancedOptions(){}
function _markAuxDirty(){}
function _onAuxModelChange(){}
async function _applyAuxModels(){}
function showToast(){}
async function showConfirmDialog(){ return false; }
"""


def _run_node(script: str) -> dict:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", script, str(PANELS_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node probe failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_endpoint_error_group_survives_the_zero_model_filter():
    """A group with models_endpoint_error must be kept; empty groups without it must not."""
    script = (
        NODE_PRELUDE
        + r"""
eval(extract('_auxProvidersFromModelGroups'));

const groups = [
  {provider:'openai-codex', provider_id:'openai-codex', models:[{id:'gpt-5.5', label:'GPT-5.5'}]},
  {provider:'Broken Proxy', provider_id:'custom:broken-proxy', models:[],
   models_endpoint_error:{kind:'network', code:null, message:'Models endpoint unreachable for broken-proxy; verify base_url.'}},
  {provider:'Empty Extra', provider_id:'custom:empty-extra', models:[], extra_models:[]},
  null,
];

const providers = _auxProvidersFromModelGroups(groups);
const broken = providers.find((p) => p.slug === 'custom:broken-proxy') || {};
const healthy = providers.find((p) => p.slug === 'openai-codex') || {};
console.log(JSON.stringify({
  slugs: providers.map((p) => p.slug),
  names: providers.map((p) => p.name),
  brokenError: broken.modelsEndpointError || null,
  brokenModels: broken.models || null,
  healthyModels: (healthy.models || []).length,
}));
"""
    )
    result = _run_node(script)

    assert result["slugs"] == ["openai-codex", "custom:broken-proxy"], (
        "Endpoint-error group must survive the filter while plain empty groups are dropped"
    )
    assert result["names"] == ["openai-codex", "Broken Proxy"]
    assert result["brokenError"] == {
        "kind": "network",
        "code": None,
        "message": "Models endpoint unreachable for broken-proxy; verify base_url.",
    }, "models_endpoint_error must be carried into the auxiliary provider entry"
    assert result["brokenModels"] == []
    assert result["healthyModels"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_auxiliary_load_keeps_endpoint_error_provider_in_both_selects():
    """Driving the real loader must leave the endpoint-error provider in the rendered selects."""
    script = (
        NODE_PRELUDE
        + LOADER_PRELUDE
        + r"""
const message = 'Models endpoint unreachable for broken-proxy; verify base_url.';

async function api(path){
  if(path === '/api/models'){
    return {groups:[
      {provider:'openai-codex', provider_id:'openai-codex', models:[{id:'gpt-5.5', label:'GPT-5.5'}]},
      {provider:'Broken Proxy', provider_id:'custom:broken-proxy', models:[],
       models_endpoint_error:{kind:'network', code:null, message}},
      {provider:'Empty Extra', provider_id:'custom:empty-extra', models:[], extra_models:[]},
    ]};
  }
  if(path === '/api/model/auxiliary'){
    return {main:{}, tasks:[
      {task:'vision', label:'Vision', description:'image analysis',
       provider:'custom:broken-proxy', model:'broken/manual'},
    ]};
  }
  throw new Error('unexpected api call: ' + path);
}

const container = document.createElement('div');
container.id = 'auxModelsContainer';

(async () => {
  await _loadAuxiliaryModels();
  const provSel = $('aux-prov-vision');
  const modelSel = $('aux-model-vision');
  const firstRow = container.children[0];
  console.log(JSON.stringify({
    rowCount: container.children.length,
    rowChildCount: firstRow ? firstRow.children.length : 0,
    providerValues: provSel ? provSel.children.map((o) => o.value) : null,
    providerSelected: provSel ? provSel.children.filter((o) => o.selected).map((o) => o.value) : null,
    modelValues: modelSel ? modelSel.children.map((o) => o.value) : null,
    modelTexts: modelSel ? modelSel.children.map((o) => o.textContent) : null,
    modelSelected: modelSel ? modelSel.children.filter((o) => o.selected).map((o) => o.value) : null,
    hints: modelSel ? modelSel.children
      .filter((o) => o.dataset && o.dataset.modelsEndpointError === '1')
      .map((o) => ({disabled: !!(o.disabled), text: o.textContent})) : null,
  }));
})();
"""
    )
    result = _run_node(script)

    assert result["rowCount"] == 1, "one auxiliary task row must be rendered"
    assert result["providerValues"] == ["auto", "openai-codex", "custom:broken-proxy"], (
        "the rendered provider select must offer the endpoint-error provider and drop the plain empty group"
    )
    assert "custom:empty-extra" not in result["providerValues"]
    assert result["providerSelected"] == ["custom:broken-proxy"], (
        "the configured provider must stay selected so an Apply cannot silently rewrite it to auto"
    )
    assert result["hints"] == [
        {"disabled": True, "text": "\u26a0 Models endpoint unreachable for broken-proxy; verify base_url."}
    ], "the rendered model select must explain the empty list with the provider endpoint error"
    assert result["modelValues"] == ["", "", "broken/manual", "__custom__"]
    assert result["modelSelected"] == ["broken/manual"], "the configured model must stay selected"
    assert result["modelTexts"][1] == "\u26a0 Models endpoint unreachable for broken-proxy; verify base_url."


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_model_select_surfaces_provider_endpoint_error():
    """The model select must explain the empty list instead of looking model-less."""
    script = (
        NODE_PRELUDE
        + r"""
eval(extract('_modelBareNameForProvider'));
eval(extract('_buildAuxModelOptions'));

const message = 'Models endpoint returned 502 for broken-proxy; see logs.';
const broken = [{slug:'custom:broken-proxy', name:'Broken Proxy', models:[],
                 modelsEndpointError:{kind:'http', code:502, message}}];
const withModels = [{slug:'custom:allowed-proxy', name:'Allowed Proxy',
                     models:[{id:'allowed/one', label:'allowed/one'}],
                     modelsEndpointError:{kind:'network', code:null, message:'Models endpoint unreachable for allowed-proxy; verify base_url.'}}];

const brokenSel = document.createElement('select');
const brokenCanonical = _buildAuxModelOptions(brokenSel, 'custom:broken-proxy', broken, 'broken/manual');

const allowedSel = document.createElement('select');
_buildAuxModelOptions(allowedSel, 'custom:allowed-proxy', withModels, '');

console.log(JSON.stringify({
  canonical: brokenCanonical,
  firstValue: brokenSel.children[0] ? brokenSel.children[0].value : null,
  firstDisabled: brokenSel.children[0] ? !!(brokenSel.children[0].disabled) : null,
  hints: brokenSel.children.filter((o) => o.dataset && o.dataset.modelsEndpointError === '1')
    .map((o) => ({value:o.value, disabled:!!(o.disabled), text:o.textContent})),
  customPresent: brokenSel.children.some((o) => o.value === '__custom__'),
  configuredPreserved: brokenSel.children.some((o) => o.value === 'broken/manual' && o.selected === true),
  allowedHints: allowedSel.children.filter((o) => o.dataset && o.dataset.modelsEndpointError === '1').length,
  allowedModels: allowedSel.children.map((o) => o.value),
}));
"""
    )
    result = _run_node(script)

    assert result["canonical"] == "broken/manual", "configured model must stay canonical for the broken provider"
    assert result["firstValue"] == "", "the auto placeholder must stay the first option"
    assert result["firstDisabled"] is False, "the auto placeholder must remain selectable"
    assert result["hints"] == [
        {"value": "", "disabled": True, "text": "\u26a0 Models endpoint returned 502 for broken-proxy; see logs."}
    ], "the model select must carry a disabled hint with the provider endpoint error message"
    assert result["customPresent"] is True, "the custom-model escape hatch must stay available"
    assert result["configuredPreserved"] is True, "the configured model must stay selected"
    assert result["allowedHints"] == 1, "a provider with models must still surface its endpoint error"
    assert result["allowedModels"] == ["", "", "allowed/one", "__custom__"], (
        "models must still be listed after the auto placeholder and the error hint"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_model_select_without_endpoint_error_keeps_previous_shape():
    """Providers without an endpoint error must not gain a hint option."""
    script = (
        NODE_PRELUDE
        + r"""
eval(extract('_modelBareNameForProvider'));
eval(extract('_buildAuxModelOptions'));

const providers = [{slug:'openai-codex', name:'openai-codex', models:[{id:'gpt-5.5', label:'GPT-5.5'}]}];
const sel = document.createElement('select');
const canonical = _buildAuxModelOptions(sel, 'openai-codex', providers, 'gpt-5.5');

console.log(JSON.stringify({
  canonical,
  values: sel.children.map((o) => o.value),
  hints: sel.children.filter((o) => o.dataset && o.dataset.modelsEndpointError === '1').length,
  selected: sel.children.filter((o) => o.selected === true).map((o) => o.value),
}));
"""
    )
    result = _run_node(script)

    assert result["hints"] == 0
    assert result["values"] == ["", "gpt-5.5", "__custom__"]
    assert result["selected"] == ["gpt-5.5"]
