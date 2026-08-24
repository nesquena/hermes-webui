"""Frontend contracts for Settings → Providers custom model management."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_providers_panel_exposes_custom_model_management_hooks():
    assert 'id="providersAddCustomBtn"' in INDEX
    assert "api('/api/providers?summary=1')" in PANELS
    assert "/api/providers/custom-models" in PANELS
    assert "/api/providers/custom-models/test" in PANELS
    assert "/api/providers/custom-models/discover" in PANELS
    assert "/api/providers/custom-models/activate" in PANELS
    assert "/api/providers/custom-models/delete" in PANELS
    for hook in (
        'data-provider-editor',
        'data-provider-action="test"',
        'data-provider-action="discover-models"',
        'data-provider-action="use-discovered"',
        'data-provider-discovery-select',
        'data-provider-action="save"',
        'data-provider-action="default"',
        'data-provider-action="delete"',
        'data-provider-model-list',
        'data-provider-test-status',
    ):
        assert hook in PANELS


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_custom_model_payload_is_write_only_and_preserves_models(tmp_path):
    source = extract_function(PANELS, "_customModelPayload")
    script = tmp_path / "payload.js"
    script.write_text(
        """
const fn = %s;
const makeInput = (value, checked=false) => ({value, checked});
const editor = {
  uid: 'providers:custom:router',
  profile: 'work',
  nameInput: makeInput('Router'),
  baseUrlInput: makeInput('https://router.example/v1'),
  apiModeSelect: makeInput('chat_completions'),
  apiKeyInput: makeInput(''),
  clearKeyInput: makeInput('', false),
  contextLengthInput: makeInput('32000'),
  makeDefaultInput: makeInput('', true),
  modelRows: [
    {input: makeInput('m1'), defaultInput: makeInput('', false)},
    {input: makeInput('m2'), defaultInput: makeInput('', true)},
  ],
};
process.stdout.write(JSON.stringify(fn(editor)));
""" % source,
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload == {
        "uid": "providers:custom:router",
        "profile": "work",
        "name": "Router",
        "base_url": "https://router.example/v1",
        "api_mode": "chat_completions",
        "models": ["m1", "m2"],
        "default_model": "m2",
        "context_length": 32000,
        "clear_api_key": False,
        "make_default": True,
        "enabled": True,
    }
    assert "api_key" not in payload


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_existing_context_length_is_only_sent_when_changed(tmp_path):
    source = extract_function(PANELS, "_customModelPayload")
    script = tmp_path / "context-payload.js"
    script.write_text(
        """
const fn = %s;
const input = (value, checked=false) => ({value, checked});
const base = {
  uid: 'providers:router', profile: 'work', initialContextLength: '32000',
  nameInput: input('Router'), baseUrlInput: input('https://router.example/v1'),
  apiModeSelect: input('bedrock_converse'), apiKeyInput: input(''),
  clearKeyInput: input('', false), makeDefaultInput: input('', false),
  enabledInput: input('', true),
  modelRows: [{input: input('m1'), defaultInput: input('', true)}],
};
const unchanged = fn({...base, contextLengthInput: input('32000')});
const cleared = fn({...base, contextLengthInput: input('')});
const autoUnchanged = fn({...base, initialApiModeExplicit: false, apiModeSelect: input('auto'), contextLengthInput: input('32000')});
process.stdout.write(JSON.stringify({unchanged, cleared, autoUnchanged}));
""" % source,
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, check=True
    )
    payloads = json.loads(result.stdout)
    assert "context_length" not in payloads["unchanged"]
    assert payloads["unchanged"]["api_mode"] == "bedrock_converse"
    assert payloads["cleared"]["context_length"] is None
    assert "api_mode" not in payloads["autoUnchanged"]


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_add_custom_button_is_wired_before_provider_requests_finish(tmp_path):
    source = extract_function(PANELS, "loadProvidersPanel").replace(
        "function loadProvidersPanel", "async function loadProvidersPanel", 1
    )
    script = tmp_path / "provider-add-immediate.js"
    script.write_text(
        """
const addBtn = {onclick: null};
const list = {innerHTML: '', style: {}, querySelector: () => null, appendChild: () => {}};
const empty = {style: {}};
const $ = id => id === 'providersList' ? list : id === 'providersAddCustomBtn' ? addBtn : id === 'providersEmpty' ? empty : null;
const S = {activeProfile: 'default'};
let _providersLoadGeneration = 0;
let _customModelEditorDirty = false;
let _customModelEditorUid = null;
let _customModelData = {providers: []};
const _providerCardEls = new Map();
const _customModelProfile = () => 'default';
const _customModelProfileMatches = owner => owner === 'default';
const _confirmDiscardCustomModelEditor = () => true;
let opened = null;
const _openCustomModelEditor = uid => { opened = uid; };
const _wireProvidersAddCustomButton = () => { addBtn.onclick = () => _openCustomModelEditor('new'); };
const api = () => new Promise(() => {});
const _fetchProviderQuotaStatus = () => new Promise(() => {});
const _buildCustomModelsSection = () => ({});
const _buildProviderQuotaCard = () => null;
const _buildProviderCard = () => ({});
const renderProviderCostChart = () => {};
const esc = String;
const t = value => value;
const loadProvidersPanel = %s;
loadProvidersPanel();
setImmediate(() => {
  if (typeof addBtn.onclick === 'function') addBtn.onclick();
  process.stdout.write(JSON.stringify({wired: typeof addBtn.onclick === 'function', opened}));
});
""" % source,
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {"wired": True, "opened": "new"}


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_open_custom_editor_renders_locally_without_network(tmp_path):
    source = extract_function(PANELS, "_openCustomModelEditor")
    script = tmp_path / "provider-open-local.js"
    script.write_text(
        """
let _providersLoadGeneration = 4;
let _customModelEditorDirty = true;
let _customModelEditorUid = null;
let _customModelData = {providers: []};
let renders = 0;
const _confirmDiscardCustomModelEditor = () => true;
const _customModelProfile = () => 'work';
const _renderCustomModelsSectionInPlace = () => { renders += 1; };
const fn = %s;
fn('new');
process.stdout.write(JSON.stringify({generation: _providersLoadGeneration, dirty: _customModelEditorDirty, uid: _customModelEditorUid, profile: _customModelData.profile, renders}));
""" % source,
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {
        "generation": 5,
        "dirty": False,
        "uid": "new",
        "profile": "work",
        "renders": 1,
    }


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_out_of_order_profile_load_cannot_paint_stale_custom_cards(tmp_path):
    source = extract_function(PANELS, "loadProvidersPanel").replace(
        "function loadProvidersPanel", "async function loadProvidersPanel", 1
    )
    script = tmp_path / "provider-race.js"
    script.write_text(
        """
let activeProfile = 'profile-a';
const S = {};
Object.defineProperty(S, 'activeProfile', {get: () => activeProfile});
let _providersLoadGeneration = 0;
let _customModelEditorDirty = false;
let _customModelData = {providers: []};
let _customModelEditorUid = null;
const _providerCardEls = new Map();
const appended = [];
const list = {
  innerHTML: '', style: {}, querySelector: () => null,
  appendChild: value => appended.push(value),
};
const $ = id => id === 'providersList' ? list : null;
const _customModelProfile = () => activeProfile || 'default';
const _customModelProfileMatches = owner => owner === _customModelProfile();
const _wireProvidersAddCustomButton = () => {};
const deferred = {};
const makeDeferred = key => {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  deferred[key] = {resolve, reject};
  return promise;
};
const api = path => makeDeferred(activeProfile + ':' + path);
const _fetchProviderQuotaStatus = () => Promise.resolve(null);
const _buildCustomModelsSection = data => ({kind: 'custom', profile: data.profile});
const _buildProviderQuotaCard = () => null;
const _buildProviderCard = provider => ({kind: 'builtin', id: provider.id});
const renderProviderCostChart = () => {};
const esc = value => String(value);
const t = value => value;
const loadProvidersPanel = %s;
(async () => {
  const first = loadProvidersPanel();
  activeProfile = 'profile-b';
  const second = loadProvidersPanel();
  deferred['profile-b:/api/providers?summary=1'].resolve({providers: []});
  deferred['profile-b:/api/providers/custom-models'].resolve({providers: []});
  await second;
  deferred['profile-a:/api/providers?summary=1'].resolve({providers: []});
  deferred['profile-a:/api/providers/custom-models'].resolve({providers: []});
  await first;
  process.stdout.write(JSON.stringify(appended));
})().catch(error => { console.error(error); process.exit(1); });
""" % source,
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == [
        {"kind": "custom", "profile": "profile-b"}
    ]


def test_custom_model_editor_has_mobile_layout_contract():
    assert ".custom-model-editor-grid" in STYLE
    assert ".custom-model-row" in STYLE
    assert "@media(max-width:768px)" in STYLE or "@media (max-width:768px)" in STYLE
    assert ".custom-model-actions" in STYLE
    assert ".custom-model-discovery" in STYLE
    assert ".custom-model-field-actions" in STYLE
