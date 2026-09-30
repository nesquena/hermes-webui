"""Regression coverage for #5989's cross-source model picker duplicate."""

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function(source, name):
    match = re.search(rf"function {name}\([^)]*\)\{{", source)
    assert match, f"{name} not found in ui.js"
    start = match.start()
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated {name}")


def _run_harness():
    provider_helper = _function(UI_JS, "_getOptionProviderId")
    identity = _function(UI_JS, "_modelPickerOptionIdentity")
    helper = _function(UI_JS, "_deduplicateModelPickerOptions")
    live_add = _function(UI_JS, "_addLiveModelsToSelect")
    # Real dependency chain of the extracted resolvers (#6657 review): the
    # production functions are extracted verbatim, so every helper they call
    # must be present — no production test accommodations.
    chain = [
        ("const", "_PY_WS_CLASS"),
        ("const", "_CUSTOM_SLUG_TRIM_RE"),
        ("const", "_CUSTOM_SLUG_HOST_REJECT_RE"),
        ("const", "MODEL_STATE_KEY"),
        ("func", "_customSlugIsEndpointAuthority"),
        ("func", "_parseQualifiedCustomId"),
        ("func", "_optionDeclaredProviderId"),
        ("let", "_dynamicProviderIds"),
        ("func", "_clientProviderAuthorityForModel"),
        ("func", "_storedModelProvider"),
        ("func", "_readPersistedModelState"),
        ("func", "_persistedProviderAuthorityForModel"),
        ("func", "_dynamicProviderAuthorityForQualifiedCustomId"),
        ("func", "_qualifiedCustomIdNeedsBackendAuthority"),
        ("func", "_providerFromModelValue"),
        ("func", "_findModelInDropdown"),
        ("func", "_modelStateForSelect"),
        ("func", "_applyModelToDropdown"),
    ]
    preamble = []
    for kind, name in chain:
        if kind == "func":
            preamble.append(_function(UI_JS, name))
            continue
        decl = "const" if kind == "const" else "let"
        match = re.search(rf"^{decl} {name}=.*$", UI_JS, re.MULTILINE)
        assert match, f"{name} not found in ui.js"
        preamble.append(match.group(0).replace(f"{decl} ", "var ", 1))
    script = f"""
{chr(10).join(preamble)}
{provider_helper}
{identity}
{helper}
{live_add}
class Node {{
  constructor(tag) {{ this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={{}}; this.parentElement=null; }}
  appendChild(child) {{ child.parentElement=this; this.children.push(child); return child; }}
  removeChild(child) {{ this.children=this.children.filter(item=>item!==child); child.parentElement=null; }}
  querySelectorAll(selector) {{
    if(selector==='optgroup') return this.children.filter(child=>child.tagName==='OPTGROUP');
    return [];
  }}
  get options() {{
    return this.tagName==='SELECT'
      ? this.children.flatMap(child=>child.tagName==='OPTGROUP'?child.children:[child])
      : undefined;
  }}
}}
globalThis.window={{_activeProvider:'custom:llm-proxy'}};
globalThis.document={{createElement:tag=>new Node(tag)}};
globalThis._dynamicModelLabels={{}};
globalThis.S={{session:null}};
// Production storage (empty): _readPersistedModelState reads localStorage
// through MODEL_STATE_KEY; a missing dependency must be fatal, not silently
// converted to "no authority" by _persistedProviderAuthorityForModel's catch.
globalThis.localStorage={{getItem:()=>null,setItem:()=>{{}},removeItem:()=>{{}}}};
function addCatalog(select, value) {{
  const group=select.querySelectorAll('optgroup')[0] || (()=>{{
    const item=new Node('optgroup'); item.dataset.provider='custom:llm-proxy'; select.appendChild(item); return item;
  }})();
  const catalog=new Node('option'); catalog.value=value; catalog.textContent=value; group.appendChild(catalog);
}}
function snapshot(select) {{
  if(!select.value && select.options[0]) select.value=select.options[0].value;
  _deduplicateModelPickerOptions(select,select.value);
  return {{
    groups:select.querySelectorAll('optgroup').map(item=>item.children.map(option=>option.value)),
    selected:select.value,
    // Provider-state oracle (#6657 review): the REAL _modelStateForSelect
    // must resolve the surviving row's provider through the extracted
    // authority chain — the old simplified shadow returned model_provider
    // null for every row, so restoring it fails these assertions.
    state:_modelStateForSelect(select,select.value),
  }};
}}
function makeSelect() {{
  const select=new Node('select');
  const group=new Node('optgroup'); group.dataset.provider='custom:llm-proxy'; select.appendChild(group);
  return select;
}}
const liveFirst=makeSelect();
_addLiveModelsToSelect('custom:llm-proxy',[{{id:'x-ai/grok-4.5',label:'Grok 4.5'}}],liveFirst);
addCatalog(liveFirst,'x-ai/grok-4.5');
liveFirst.value=liveFirst.options[0].value;
_addLiveModelsToSelect('custom:llm-proxy',[{{id:'x-ai/grok-4.5',label:'Grok 4.5'}}],liveFirst);
const otherGroup=new Node('optgroup'); otherGroup.dataset.provider='custom:other-proxy';
const other=new Node('option'); other.value='x-ai/grok-4.5'; otherGroup.appendChild(other); liveFirst.appendChild(otherGroup);
const catalogFirst=makeSelect();
addCatalog(catalogFirst,'x-ai/grok-4.5');
_addLiveModelsToSelect('custom:llm-proxy',[{{id:'x-ai/grok-4.5',label:'Grok 4.5'}}],catalogFirst);
const selectedBare=makeSelect();
addCatalog(selectedBare,'x-ai/grok-4.5');
selectedBare.value='x-ai/grok-4.5';
_addLiveModelsToSelect('custom:llm-proxy',[{{id:'x-ai/grok-4.5',label:'Grok 4.5'}}],selectedBare);
const sameSuffix=makeSelect();
addCatalog(sameSuffix,'vendor-a/deepseek-v4-pro');
addCatalog(sameSuffix,'vendor-b/catalog/deepseek-v4-pro');
sameSuffix.value='vendor-a/deepseek-v4-pro';
const sameGroupColonSuffix=makeSelect();
_addLiveModelsToSelect('custom:llm-proxy',[{{id:'@custom:llm-proxy:deepseek/deepseek-r1:free',label:'DeepSeek R1 Free'}}],sameGroupColonSuffix);
_addLiveModelsToSelect('custom:llm-proxy',[{{id:'@custom:llm-proxy:meta-llama/llama-3.3:free',label:'Llama 3.3 Free'}}],sameGroupColonSuffix);
const crossProviderLive=new Node('select');
_addLiveModelsToSelect('custom:a',[{{id:'@custom:a:gpt-4o',label:'GPT-4o A'}}],crossProviderLive);
_addLiveModelsToSelect('custom:b',[{{id:'@custom:b:gpt-4o',label:'GPT-4o B'}}],crossProviderLive);
const unnamespaced=makeSelect();
addCatalog(unnamespaced,'gpt-4o');
_addLiveModelsToSelect('custom:llm-proxy',[{{id:'@custom:llm-proxy:gpt-4o',label:'GPT-4o'}}],unnamespaced);
console.log(JSON.stringify({{
  liveFirst:snapshot(liveFirst),
  catalogFirst:snapshot(catalogFirst),
  selectedBare:snapshot(selectedBare),
  sameSuffix:snapshot(sameSuffix),
  sameGroupColonSuffix:snapshot(sameGroupColonSuffix),
  crossProviderLive:snapshot(crossProviderLive),
  unnamespaced:snapshot(unnamespaced),
}}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cross_source_proxy_and_catalog_twin_has_one_routable_row():
    result = _run_harness()
    assert result["liveFirst"] == {
        "groups": [["@custom:llm-proxy:x-ai/grok-4.5"], ["x-ai/grok-4.5"]],
        "selected": "@custom:llm-proxy:x-ai/grok-4.5",
        "state": {"model": "x-ai/grok-4.5", "model_provider": "custom:llm-proxy"},
    }


def test_catalog_first_keeps_the_routable_proxy_row():
    assert _run_harness()["catalogFirst"] == {
        "groups": [["@custom:llm-proxy:x-ai/grok-4.5"]],
        "selected": "@custom:llm-proxy:x-ai/grok-4.5",
        "state": {"model": "x-ai/grok-4.5", "model_provider": "custom:llm-proxy"},
    }


def test_selected_bare_occurrence_survives_same_group_dedup():
    assert _run_harness()["selectedBare"] == {
        "groups": [["x-ai/grok-4.5"]],
        "selected": "x-ai/grok-4.5",
        "state": {"model": "x-ai/grok-4.5", "model_provider": "custom:llm-proxy"},
    }


def test_same_suffix_models_in_one_group_do_not_collapse():
    assert _run_harness()["sameSuffix"] == {
        "groups": [["vendor-a/deepseek-v4-pro", "vendor-b/catalog/deepseek-v4-pro"]],
        "selected": "vendor-a/deepseek-v4-pro",
        "state": {"model": "vendor-a/deepseek-v4-pro", "model_provider": "custom:llm-proxy"},
    }


def test_same_group_colon_suffixed_proxy_models_remain_distinct():
    assert _run_harness()["sameGroupColonSuffix"] == {
        "groups": [[
            "@custom:llm-proxy:deepseek/deepseek-r1:free",
            "@custom:llm-proxy:meta-llama/llama-3.3:free",
        ]],
        "selected": "@custom:llm-proxy:deepseek/deepseek-r1:free",
        "state": {"model": "deepseek/deepseek-r1:free", "model_provider": "custom:llm-proxy"},
    }


def test_live_models_with_same_identity_survive_in_different_provider_groups():
    assert _run_harness()["crossProviderLive"] == {
        "groups": [["@custom:a:gpt-4o"], ["@custom:b:gpt-4o"]],
        "selected": "@custom:a:gpt-4o",
        "state": {"model": "gpt-4o", "model_provider": "custom:a"},
    }


def test_unnamespaced_custom_proxy_value_deduplicates_with_catalog():
    assert _run_harness()["unnamespaced"] == {
        "groups": [["@custom:llm-proxy:gpt-4o"]],
        "selected": "@custom:llm-proxy:gpt-4o",
        "state": {"model": "gpt-4o", "model_provider": "custom:llm-proxy"},
    }
