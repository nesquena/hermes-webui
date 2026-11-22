"""Regression coverage for provider-qualified auxiliary model persistence.

``GET /api/models`` may expose WebUI-only ``@provider:model`` routing IDs, but
auxiliary configuration stores provider and model in separate fields.  The
provider-native model value must be used by both the settings UI and the shared
backend persistence boundary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = REPO / "static" / "panels.js"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_auxiliary_picker_uses_provider_native_model_values():
    """A custom-provider catalog must render and select provider-native values."""
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function extract(name){
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if(start < 0) return '';
  let i = src.indexOf('{', start);
  let depth = 0;
  while(i < src.length){
    const ch = src[i];
    if(ch === '{') depth += 1;
    else if(ch === '}'){
      depth -= 1;
      if(depth === 0) return src.slice(start, i + 1);
    }
    i += 1;
  }
  throw new Error(name + ' parse failed');
}

global.t = () => '';
global.document = {
  createElement: () => ({value:'', textContent:'', selected:false}),
};

function makeSelect(){
  return {
    children: [],
    _value: '',
    set innerHTML(_value){ this.children = []; this._value = ''; },
    get innerHTML(){ return ''; },
    get options(){ return this.children; },
    appendChild(opt){ this.children.push(opt); },
    insertBefore(opt, before){
      const idx = this.children.indexOf(before);
      if(idx < 0) this.children.push(opt);
      else this.children.splice(idx, 0, opt);
    },
    set value(value){
      this._value = value;
      for(const opt of this.children) opt.selected = opt.value === value;
    },
    get value(){
      const selected = this.children.find((opt) => opt.selected);
      return selected ? selected.value : this._value;
    },
  };
}

const sharedHelper = extract('_modelBareNameForProvider');
const buildOptions = extract('_buildAuxModelOptions');
if(!buildOptions) throw new Error('_buildAuxModelOptions not found');
eval(sharedHelper + '\n' + buildOptions);

const select = makeSelect();
const providers = [{
  slug: 'my-local-ai-gateway',
  name: 'My Local AI Gateway',
  models: [
    {
      id: '@my-local-ai-gateway:example-chat-model',
      label: 'Example Chat Model',
    },
    {
      id: '@my-local-ai-gateway:example-side-model',
      label: '@my-local-ai-gateway:example-side-model',
    },
    {id: 'vendor/example-model', label: 'Vendor Example Model'},
    {id: 'example-chat-model', label: 'Duplicate Chat Model'},
  ],
}];

const canonicalCurrent = _buildAuxModelOptions(
  select,
  'my-local-ai-gateway',
  providers,
  '@my-local-ai-gateway:example-side-model',
);

const modelOptions = select.options.filter(
  (opt) => opt.value && opt.value !== '__custom__',
);
const helperCases = sharedHelper ? [
  _modelBareNameForProvider('@custom:router-alias:chat-model', 'custom'),
  _modelBareNameForProvider('@custom:backup:model:free', 'custom:backup'),
  _modelBareNameForProvider('vendor/example-model', 'my-local-ai-gateway'),
  _modelBareNameForProvider('@other-gateway:other-model', 'my-local-ai-gateway'),
] : null;

console.log(JSON.stringify({
  values: modelOptions.map((opt) => opt.value),
  labels: modelOptions.map((opt) => opt.textContent),
  selected: select.value,
  canonicalCurrent,
  helperCases,
}));
"""

    proc = subprocess.run(
        [NODE, "-e", script, str(PANELS_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"node probe failed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result == {
        "values": [
            "example-chat-model",
            "example-side-model",
            "vendor/example-model",
        ],
        "labels": [
            "Example Chat Model",
            "example-side-model",
            "Vendor Example Model",
        ],
        "selected": "example-side-model",
        "canonicalCurrent": "example-side-model",
        "helperCases": [
            "router-alias:chat-model",
            "model:free",
            "vendor/example-model",
            "@other-gateway:other-model",
        ],
    }


def test_matching_legacy_value_exposes_explicit_apply_repair():
    """Loading a safe legacy prefix should offer, but not force, a config write."""
    source = PANELS_JS_PATH.read_text(encoding="utf-8")

    assert "if(canonicalModel!==cfg.model) needsCanonicalSave=true;" in source
    assert "applyBtn.style.display=needsCanonicalSave?'':'none'" in source


@pytest.mark.parametrize(
    ("provider", "requested_model", "persisted_model"),
    [
        (
            "my-local-ai-gateway",
            "@my-local-ai-gateway:example-side-model",
            "example-side-model",
        ),
        (
            "custom",
            "@custom:router-alias:chat-model",
            "router-alias:chat-model",
        ),
        ("custom:backup", "@custom:backup:model:free", "model:free"),
        (
            "my-local-ai-gateway",
            "vendor/example-model",
            "vendor/example-model",
        ),
        (
            "my-local-ai-gateway",
            "example-side-model",
            "example-side-model",
        ),
    ],
)
def test_set_auxiliary_model_persists_provider_native_model(
    monkeypatch,
    tmp_path,
    provider,
    requested_model,
    persisted_model,
):
    from api import config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "auxiliary:\n  vision:\n    provider: auto\n    model: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda model: (model, provider, None),
    )

    result = config.set_auxiliary_model("vision", provider, requested_model)

    saved = config._load_yaml_config_file(config_path)["auxiliary"]["vision"]
    assert saved["provider"] == provider
    assert saved["model"] == persisted_model
    assert result["provider"] == provider
    assert result["model"] == persisted_model


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("my-local-ai-gateway", "@other-gateway:other-model"),
        ("my-local-ai-gateway", "@my-local-ai-gateway:"),
        ("auto", "@my-local-ai-gateway:example-side-model"),
    ],
)
def test_set_auxiliary_model_rejects_invalid_qualified_pair_without_write(
    monkeypatch,
    tmp_path,
    provider,
    model,
):
    from api import config

    config_path = tmp_path / "config.yaml"
    original = (
        "auxiliary:\n"
        "  vision:\n"
        "    provider: openai\n"
        "    model: gpt-5.5\n"
    )
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)

    with pytest.raises(ValueError, match="provider-qualified auxiliary model"):
        config.set_auxiliary_model("vision", provider, model)

    assert config_path.read_text(encoding="utf-8") == original
