"""#6946 re-gate: a real option from provider A must never retire a synthetic,
routable selection owned by provider B (cross-provider orphan dedup).

Two production shapes (per maintainer review #4):
(a) Provider A has a real catalog `shared-model`; a restored provider B
    selection is represented by the synthetic `@provider-b:shared-model` row
    because provider B's catalog entry is not hydrated. Dedup must preserve B.
(b) A provider B real twin is added to the catalog. Only the B
    synthetic/real pair collapses, the B real row becomes the reverse-lookup
    target, and the provider-A row remains.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function(src: str, name: str) -> str:
    import re

    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    assert match, f"{name} not found"
    start = match.start()
    i = src.index("{", match.end())
    depth = 1
    i += 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _run_harness(tmp_path, case: str, with_twin_b: bool) -> dict:
    src = UI_JS_PATH.read_text(encoding="utf-8")
    parts = [
        _function(src, "_getOptionProviderId"),
        _function(src, "_modelPickerOptionIdentity"),
        _function(src, "_modelPickerCanonicalIdentity"),
        _function(src, "_providerQualifiedPresetRest"),
        _function(src, "_deduplicateModelPickerOptions"),
        _function(src, "_findModelInDropdown"),
    ]
    driver = tmp_path / f"driver_{case}.js"
    driver.write_text(
        "\n".join(parts)
        + r"""
class Node {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.parentElement=null; this.value=''; this.textContent=''; }
  appendChild(child) { child.parentElement=this; this.children.push(child); return child; }
  removeChild(child) { this.children=this.children.filter(item=>item!==child); child.parentElement=null; }
  querySelectorAll(selector) {
    if(selector==='optgroup') return this.children.filter(child=>child.tagName==='OPTGROUP');
    return [];
  }
  get options() {
    return this.tagName==='SELECT'
      ? this.children.flatMap(child=>child.tagName==='OPTGROUP'?child.children:[child])
      : undefined;
  }
}
globalThis.window={_activeProvider:null};
globalThis.document={createElement:tag=>new Node(tag)};
globalThis._dynamicModelLabels={};
globalThis._modelStateForSelect=()=>({model:'',model_provider:null});
globalThis._applyModelToDropdown=()=>null;
globalThis.S={session:null};
function makeSelect(withTwinB) {
  const select=new Node('select');
  const groupA=new Node('optgroup'); groupA.dataset.provider='provider-a'; select.appendChild(groupA);
  const realA=new Node('option'); realA.value='shared-model'; realA.textContent='shared-model'; groupA.appendChild(realA);
  if(withTwinB){
    const groupB=new Node('optgroup'); groupB.dataset.provider='provider-b'; select.appendChild(groupB);
    const realB=new Node('option'); realB.value='shared-model'; realB.textContent='shared-model'; groupB.appendChild(realB);
  }
  // Synthetic orphan shaped exactly like _ensureModelOptionInDropdown creates
  // it: dataset.custom='1' AND dataset.provider set (ui.js:3464-3471).
  const orphan=new Node('option'); orphan.value='@provider-b:shared-model'; orphan.textContent='@provider-b:shared-model';
  orphan.dataset.custom='1'; orphan.dataset.provider='provider-b';
  select.appendChild(orphan);
  return select;
}
const WITH_TWIN_B = __WITH_TWIN_B__;
const select=makeSelect(WITH_TWIN_B);
const removed=_deduplicateModelPickerOptions(select,select.value);
const groups=select.querySelectorAll('optgroup').map(item=>item.children.map(option=>option.value));
const orphans=select.children.filter(child=>child.tagName==='OPTION').map(option=>option.value);
const found=_findModelInDropdown('@provider-b:shared-model',select,'provider-b');
// The reverse-lookup result must resolve to a row owned by provider B (the
// provider-aware canonical match filters the provider-A row out).
const foundHasProviderBRow=select.options.some(o=>o.value===found&&(_getOptionProviderId(o)||'')==='provider-b');
process.stdout.write(JSON.stringify({removed,groups,orphans,found,foundHasProviderBRow}));
""".replace(
            "__WITH_TWIN_B__", "true" if with_twin_b else "false"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cross_provider_orphan_not_retired_by_other_provider_twin(tmp_path):
    """Provider A's real `shared-model` option must NOT retire provider B's
    synthetic `@provider-b:shared-model` orphan while provider B has no
    catalog twin (#6946 re-gate)."""
    out = _run_harness(tmp_path, "cross", with_twin_b=False)
    assert out["removed"] == 0, str(out)
    assert out["orphans"] == ["@provider-b:shared-model"], (
        "orphan owned by provider B must survive dedup (provider mismatch): "
        + str(out)
    )
    assert out["groups"] == [["shared-model"]], str(out)


def test_cross_provider_orphan_collapses_onto_real_twin(tmp_path):
    """Once a provider B real twin exists, only the B synthetic/real pair
    collapses; the B real row becomes the reverse-lookup target and the
    provider-A row remains (#6946 re-gate)."""
    out = _run_harness(tmp_path, "twin", with_twin_b=True)
    assert out["removed"] == 1, (
        "only the provider-B orphan must collapse onto its real twin: " + str(out)
    )
    assert out["orphans"] == [], (
        "provider-B synthetic row must be removed once the real twin exists: "
        + str(out)
    )
    assert out["groups"] == [["shared-model"], ["shared-model"]], (
        "provider-A row and provider-B real row must both remain: " + str(out)
    )
    assert out["found"] == "shared-model", str(out)
    assert out["foundHasProviderBRow"] is True, (
        "reverse lookup of @provider-b:shared-model must resolve to the "
        "provider-B real row (provider equality): " + str(out)
    )


def test_apply_model_to_dropdown_selects_provider_b_option_object(tmp_path):
    """#6946 re-gate item 5: when duplicate scalar model values exist across providers,
    _applyModelToDropdown() with preferredProviderId must set selected=true on the
    specific provider-B option object, not merely set the scalar value."""
    src = UI_JS_PATH.read_text(encoding="utf-8")
    parts = [
        _function(src, "_getOptionProviderId"),
        _function(src, "_providerFromModelValue"),
        _function(src, "_modelPickerOptionIdentity"),
        _function(src, "_modelPickerCanonicalIdentity"),
        _function(src, "_providerQualifiedPresetRest"),
        _function(src, "_findModelInDropdown"),
        _function(src, "_modelStateForSelect"),
        _function(src, "_applyModelToDropdown"),
    ]
    driver = tmp_path / "driver_select_obj.js"
    driver.write_text(
        "\n".join(parts)
        + r"""
globalThis.window = {_activeProvider: null};
globalThis.syncModelChip = () => {};
globalThis.syncSettingsModelChip = () => {};
globalThis._refreshOpenModelDropdown = () => {};

const optA = {
  tagName: 'OPTION',
  value: 'shared-model',
  textContent: 'shared-model',
  dataset: {},
  selected: false,
  parentElement: {tagName: 'OPTGROUP', dataset: {provider: 'provider-a'}},
};
const optB = {
  tagName: 'OPTION',
  value: 'shared-model',
  textContent: 'shared-model',
  dataset: {},
  selected: false,
  parentElement: {tagName: 'OPTGROUP', dataset: {provider: 'provider-b'}},
};
const select = {
  id: 'modelSelect',
  options: [optA, optB],
  _val: '',
  get value() { return this._val; },
  set value(v) {
    this._val = v;
    const firstMatch = this.options.find(o => o.value === v);
    this.options.forEach(o => o.selected = (o === firstMatch));
  },
  get selectedOptions() {
    return this.options.filter(o => o.selected);
  }
};

const applied = _applyModelToDropdown('shared-model', select, 'provider-b');
process.stdout.write(JSON.stringify({
  applied,
  optA_selected: optA.selected,
  optB_selected: optB.selected,
  selectedOptionIsB: select.options.find(o => o.selected && o.parentElement.dataset.provider === 'provider-b') === optB,
}));
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(driver)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["applied"] == "shared-model"
    assert out["optB_selected"] is True, "Option B must have selected=true"
    assert out["selectedOptionIsB"] is True, "Provider-B option object must be selected"


def test_production_function_advanced_save_main_and_auxiliary(tmp_path):
    """#6946 re-gate item 3: test production _openAuxAdvancedOptions() for both main
    OpenRouter openrouter/@preset/blue and auxiliary mode. Assert no ReferenceError
    and the exact /api/model/set payload."""
    ui_src = UI_JS_PATH.read_text(encoding="utf-8")
    panels_src = PANELS_JS_PATH.read_text(encoding="utf-8")

    ui_parts = [
        _function(ui_src, "_getOptionProviderId"),
        _function(ui_src, "_providerFromModelValue"),
        _function(ui_src, "_modelPickerOptionIdentity"),
        _function(ui_src, "_modelPickerCanonicalIdentity"),
        _function(ui_src, "_providerQualifiedPresetRest"),
        _function(ui_src, "_modelStateForSelect"),
        _function(ui_src, "_captureModelDropdownSelection"),
    ]
    panels_parts = [
        _function(panels_src, "_auxAdvancedValue"),
        _function(panels_src, "_auxAdvancedInputHtml"),
        _function(panels_src, "_openAuxAdvancedOptions"),
    ]

    driver = tmp_path / "driver_advanced_save.js"
    driver.write_text(
        "\n".join(ui_parts + panels_parts)
        + r"""
let apiCalls = [];
globalThis.api = async (url, opts) => {
  apiCalls.push({url, opts, body: JSON.parse(opts.body)});
  return {ok: true};
};
globalThis.t = k => k;
globalThis.esc = s => s;
globalThis.showToast = () => {};
globalThis._loadAuxiliaryModels = () => {};
globalThis.window = {_activeProvider: null};

const elements = {};
globalThis.$ = id => {
  if (!elements[id]) elements[id] = {value: '', style: {}, focus: () => {}};
  if (!elements[id].focus) elements[id].focus = () => {};
  return elements[id];
};

globalThis._ensureAuxAdvancedModal = () => elements['auxAdvancedOverlay'];
globalThis._mainModelSupportsServiceTier = () => true;
globalThis._auxTaskLabelFromMeta = (k) => ({task: k, label: k});
globalThis._auxTimingInputHtml = () => '';

// Case 1: Main OpenRouter preset
const optPreset = {
  tagName: 'OPTION',
  value: 'openrouter/@preset/blue',
  textContent: '@preset/blue',
  dataset: {},
  parentElement: {tagName: 'OPTGROUP', dataset: {provider: 'openrouter'}},
};
const settingsModel = {
  id: 'settingsModel',
  value: 'openrouter/@preset/blue',
  options: [optPreset],
  selectedOptions: [optPreset],
};
elements['settingsModel'] = settingsModel;
elements['auxAdvancedOverlay'] = {style: {}, dataset: {}};
elements['auxAdvancedTitle'] = {textContent: ''};
elements['auxAdvancedBody'] = {innerHTML: ''};
elements['auxAdvancedSave'] = {onclick: null};
elements['auxAdvancedBaseUrl'] = {value: 'https://openrouter.ai/api/v1', focus: () => {}};
elements['auxAdvancedExtraBody'] = {value: '{"transforms":[]}'};
elements['auxAdvancedApiKey'] = {value: 'sk-or-v1-test'};
elements['auxAdvancedApiKeyClear'] = {checked: false};
elements['auxAdvancedServiceTier'] = {value: 'auto'};

(async () => {
  _openAuxAdvancedOptions('__main__', {provider: 'openrouter', model: 'openrouter/@preset/blue'});
  await elements['auxAdvancedSave'].onclick();

  // Case 2: Auxiliary mode
  elements['aux-prov-title'] = {value: 'anthropic'};
  elements['aux-model-title'] = {value: 'claude-3-5-haiku-20241022'};
  elements['auxAdvancedBaseUrl'] = {value: '', focus: () => {}};
  elements['auxAdvancedExtraBody'] = {value: ''};
  elements['auxAdvancedApiKey'] = {value: ''};
  elements['auxAdvancedApiKeyClear'] = {checked: false};
  elements['auxAdvancedTimeout'] = {value: '30'};
  elements['auxAdvancedDownloadTimeout'] = {value: '60'};
  elements['auxAdvancedMaxConcurrency'] = {value: '2'};

  _openAuxAdvancedOptions('title', {provider: 'anthropic', model: 'claude-3-5-haiku-20241022'});
  await elements['auxAdvancedSave'].onclick();

  process.stdout.write(JSON.stringify(apiCalls));
})().catch(err => {
  console.error(err);
  process.exit(1);
});
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(driver)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)
    assert len(calls) == 2, f"Expected 2 API calls, got {len(calls)}"

    # Check main OpenRouter call
    main_call = calls[0]
    assert main_call["url"] == "/api/model/set"
    assert main_call["body"]["scope"] == "main"
    assert main_call["body"]["task"] == ""
    assert main_call["body"]["provider"] == "openrouter"
    assert main_call["body"]["model"] == "@preset/blue"
    assert main_call["body"]["advanced"]["base_url"] == "https://openrouter.ai/api/v1"
    assert main_call["body"]["advanced"]["extra_body"] == {"transforms": []}
    assert main_call["body"]["advanced"]["api_key"] == "sk-or-v1-test"
    assert main_call["body"]["advanced"]["service_tier"] == "auto"

    # Check auxiliary call
    aux_call = calls[1]
    assert aux_call["url"] == "/api/model/set"
    assert aux_call["body"]["scope"] == "auxiliary"
    assert aux_call["body"]["task"] == "title"
    assert aux_call["body"]["provider"] == "anthropic"
    assert aux_call["body"]["model"] == "claude-3-5-haiku-20241022"
    assert aux_call["body"]["advanced"]["timeout"] == "30"
    assert aux_call["body"]["advanced"]["download_timeout"] == "60"
    assert aux_call["body"]["advanced"]["max_concurrency"] == "2"


def test_behavioral_save_settings_payload_branches(tmp_path):
    """#6946 re-gate item 4: behavioral ordinary saveSettings() payload test for password
    and no-password branches, including the unchanged-baseline no-op."""
    ui_src = UI_JS_PATH.read_text(encoding="utf-8")
    panels_src = PANELS_JS_PATH.read_text(encoding="utf-8")

    ui_parts = [
        _function(ui_src, "_getOptionProviderId"),
        _function(ui_src, "_providerFromModelValue"),
        _function(ui_src, "_modelPickerOptionIdentity"),
        _function(ui_src, "_modelPickerCanonicalIdentity"),
        _function(ui_src, "_providerQualifiedPresetRest"),
        _function(ui_src, "_modelStateForSelect"),
        _function(ui_src, "_captureModelDropdownSelection"),
    ]

    start = panels_src.index("async function saveSettings(andClose){")
    brace = panels_src.index("{", start)
    depth = 0
    end = -1
    for i in range(brace, len(panels_src)):
        if panels_src[i] == "{":
            depth += 1
        elif panels_src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    save_settings_src = panels_src[start:end]

    driver = tmp_path / "driver_save_settings.js"
    driver.write_text(
        "\n".join(ui_parts)
        + "\n"
        + save_settings_src
        + r"""
let postedSettings = [];
let defaultModelCalls = [];
let recordedToasts = [];
let appliedSavedSettingsUiCalls = [];
let updateCurrentPasswordVisibilityCalls = 0;
let renderSettingsAuthStatusCalls = [];
let updateAuthWarningBadgeCalls = [];
let resetSettingsPanelStateCalls = 0;

globalThis._enqueueSettingsPost = async (opts) => {
  postedSettings.push(JSON.parse(opts.body));
  return {ok: true, password_auth_enabled: !!globalThis._settingsPasswordAuthEnabled};
};
globalThis.api = async (url, opts) => {
  if (url === '/api/default-model') {
    defaultModelCalls.push(JSON.parse(opts.body));
  } else if (url === '/api/auth/status') {
    return {authenticated: true, password_auth_enabled: !!globalThis._settingsPasswordAuthEnabled};
  }
  return {ok: true};
};
globalThis.window = globalThis;
globalThis.t = k => k;
globalThis.showToast = (msg, duration, type) => {
  recordedToasts.push({msg: String(msg), duration, type});
};
globalThis.localStorage = {getItem: () => null, setItem: () => {}};
globalThis._speechPreferencesPayloadFromUi = () => ({});
globalThis._structuredCodeViewFromUi = () => ({});
globalThis._composerControlVisibilityPayload = () => ({});
globalThis._getComposerControlOrder = () => [];
globalThis._applySavedSettingsUi = (saved, body, opts) => {
  appliedSavedSettingsUiCalls.push({saved, body, opts});
};
globalThis._updateCurrentPasswordVisibility = () => {
  updateCurrentPasswordVisibilityCalls++;
};
globalThis._renderSettingsAuthStatus = (status) => {
  renderSettingsAuthStatusCalls.push(status);
};
globalThis._updateAuthWarningBadge = (status) => {
  updateAuthWarningBadgeCalls.push(status);
};
globalThis._updateAuthDisabledWarning = () => {};
globalThis._resetSettingsPanelState = () => {
  resetSettingsPanelStateCalls++;
};
globalThis._hideSettingsPanel = () => {};

const elements = {};
globalThis.$ = id => {
  if (!elements[id]) elements[id] = {value: '', style: {}, dataset: {}, focus: () => {}};
  if (!elements[id].dataset) elements[id].dataset = {};
  return elements[id];
};

async function runTests() {
  const optPreset = {
    tagName: 'OPTION',
    value: 'openrouter/@preset/blue',
    textContent: '@preset/blue',
    dataset: {},
    parentElement: {tagName: 'OPTGROUP', dataset: {provider: 'openrouter'}},
  };
  elements['settingsModel'] = {
    id: 'settingsModel',
    value: 'openrouter/@preset/blue',
    options: [optPreset],
    selectedOptions: [optPreset],
  };

  // --- Branch 1: No password, model changed ---
  globalThis._settingsHermesDefaultModelOnOpen = 'claude-3-sonnet';
  globalThis._settingsHermesDefaultModelProviderOnOpen = 'anthropic';
  elements['settingsPassword'] = {value: ''};
  elements['settingsCurrentPassword'] = {value: ''};
  globalThis._settingsPasswordAuthEnabled = false;
  postedSettings = [];
  defaultModelCalls = [];
  recordedToasts = [];
  appliedSavedSettingsUiCalls = [];
  resetSettingsPanelStateCalls = 0;
  await saveSettings(false);
  const branch1Settings = postedSettings[0];
  const branch1DefaultModel = defaultModelCalls[0];
  const branch1Toasts = recordedToasts.slice();
  const branch1AppliedUi = appliedSavedSettingsUiCalls.length > 0;
  const branch1ResetPanel = resetSettingsPanelStateCalls > 0;

  // --- Branch 2: Password set, model changed ---
  elements['settingsPassword'] = {value: 'my-super-secret-pw'};
  elements['settingsCurrentPassword'] = {value: ''};
  globalThis._settingsPasswordAuthEnabled = false;
  postedSettings = [];
  defaultModelCalls = [];
  recordedToasts = [];
  appliedSavedSettingsUiCalls = [];
  updateCurrentPasswordVisibilityCalls = 0;
  renderSettingsAuthStatusCalls = [];
  resetSettingsPanelStateCalls = 0;
  await saveSettings(false);
  const branch2Settings = postedSettings[0];
  const branch2DefaultModel = defaultModelCalls[0];
  const branch2Toasts = recordedToasts.slice();
  const branch2AppliedUi = appliedSavedSettingsUiCalls.length > 0;
  const branch2UpdatePwVis = updateCurrentPasswordVisibilityCalls > 0;
  const branch2RenderAuth = renderSettingsAuthStatusCalls.length > 0;
  const branch2ResetPanel = resetSettingsPanelStateCalls > 0;

  // --- Branch 3: Unchanged baseline no-op ---
  globalThis._settingsHermesDefaultModelOnOpen = '@preset/blue';
  globalThis._settingsHermesDefaultModelProviderOnOpen = 'openrouter';
  elements['settingsPassword'] = {value: ''};
  elements['settingsCurrentPassword'] = {value: ''};
  postedSettings = [];
  defaultModelCalls = [];
  recordedToasts = [];
  appliedSavedSettingsUiCalls = [];
  resetSettingsPanelStateCalls = 0;
  await saveSettings(false);
  const branch3DefaultModelCallsCount = defaultModelCalls.length;
  const branch3Toasts = recordedToasts.slice();
  const branch3AppliedUi = appliedSavedSettingsUiCalls.length > 0;
  const branch3ResetPanel = resetSettingsPanelStateCalls > 0;

  process.stdout.write(JSON.stringify({
    branch1: {
      has_set_password: Object.prototype.hasOwnProperty.call(branch1Settings, '_set_password'),
      default_model_call: branch1DefaultModel,
      toasts: branch1Toasts,
      applied_ui: branch1AppliedUi,
      reset_panel: branch1ResetPanel,
    },
    branch2: {
      set_password: branch2Settings._set_password,
      default_model_call: branch2DefaultModel,
      toasts: branch2Toasts,
      applied_ui: branch2AppliedUi,
      update_pw_vis: branch2UpdatePwVis,
      render_auth: branch2RenderAuth,
      reset_panel: branch2ResetPanel,
    },
    branch3: {
      default_model_calls_count: branch3DefaultModelCallsCount,
      toasts: branch3Toasts,
      applied_ui: branch3AppliedUi,
      reset_panel: branch3ResetPanel,
    }
  }));
}

runTests();
""",
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(driver)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    # Branch 1: no password, default-model called with canonical captured pair, successful completion
    assert out["branch1"]["has_set_password"] is False
    assert out["branch1"]["default_model_call"] == {
        "model": "@preset/blue",
        "provider": "openrouter",
    }
    assert not any("settings_save_failed" in t["msg"] for t in out["branch1"]["toasts"])
    assert any("settings_saved" in t["msg"] for t in out["branch1"]["toasts"])
    assert out["branch1"]["applied_ui"] is True
    assert out["branch1"]["reset_panel"] is True

    # Branch 2: password set, default-model called, auth & visibility updated, successful completion
    assert out["branch2"]["set_password"] == "my-super-secret-pw"
    assert out["branch2"]["default_model_call"] == {
        "model": "@preset/blue",
        "provider": "openrouter",
    }
    assert not any("settings_save_failed" in t["msg"] for t in out["branch2"]["toasts"])
    assert any("settings_saved_pw" in t["msg"] for t in out["branch2"]["toasts"])
    assert out["branch2"]["applied_ui"] is True
    assert out["branch2"]["update_pw_vis"] is True
    assert out["branch2"]["render_auth"] is True
    assert out["branch2"]["reset_panel"] is True

    # Branch 3: unchanged baseline, /api/default-model NOT called, no errors
    assert out["branch3"]["default_model_calls_count"] == 0
    assert not any("settings_save_failed" in t["msg"] for t in out["branch3"]["toasts"])
    assert out["branch3"]["applied_ui"] is True
    assert out["branch3"]["reset_panel"] is True
