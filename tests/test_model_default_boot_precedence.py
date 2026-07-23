"""
Regression coverage for model selector drift on fresh browser boot.

A stale browser-persisted model (localStorage) must not suppress the configured
profile/server default on page load. Restored sessions may still apply their own
session model later through loadSession(). The boot fix must not make browser
model-state writes pointless or let later model-list refreshes reset a live
in-page selection.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from api import config as config_api


REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


_DRIVER_SRC = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const panels = fs.readFileSync(process.argv[3], 'utf8');

function extractFuncFrom(src, name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let openParen = src.indexOf('(', start);
  let i = openParen + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < src.length) {
    if (src[i] === '(') parenDepth++;
    else if (src[i] === ')') parenDepth--;
    i++;
  }
  i = src.indexOf('{', i);
  let depth = 1;
  i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
function extractFunc(name) {
  return extractFuncFrom(ui, name);
}
function tryExtractPanelFunc(name) {
  try { return extractFuncFrom(panels, name); } catch (_e) { return ''; }
}

const calls = {syncModelChip: 0, renderModelDropdown: 0, positionModelDropdown: 0, liveFetches: []};
let modelSelect;
let apiModels;

function makeSelect(options, initialValue) {
  const sel = {id: 'modelSelect', options: [], selectedIndex: -1, selectedOptions: []};
  Object.defineProperty(sel, 'value', {
    get() { return this._value || ''; },
    set(v) {
      const idx = this.options.findIndex(o => o.value === v);
      this.selectedIndex = idx;
      if (idx >= 0) {
        this._value = v;
        this.selectedOptions = [this.options[idx]];
      } else {
        this._value = '';
        this.selectedOptions = [];
      }
    }
  });
  Object.defineProperty(sel, 'innerHTML', {
    get() { return ''; },
    set(_v) {
      this.options = [];
      this.value = '';
    }
  });
  sel.querySelector = function(_selector) { return this.options[0] || null; };
  sel.querySelectorAll = function(selector) {
    if (selector === 'option[data-custom]') {
      return this.options.filter(o => o.dataset && o.dataset.custom);
    }
    return [];
  };
  sel.appendChild = function(node) {
    const incoming = node && node.tagName === 'OPTGROUP' ? node.children : [node];
    for (const opt of incoming || []) {
      opt.parentElement = opt.parentElement || node;
      opt.remove = opt.remove || (() => {
        const optIndex = this.options.indexOf(opt);
        if (optIndex >= 0) this.options.splice(optIndex, 1);
        const children = opt.parentElement && opt.parentElement.children;
        if (Array.isArray(children)) {
          const childIndex = children.indexOf(opt);
          if (childIndex >= 0) children.splice(childIndex, 1);
        }
      });
      this.options.push(opt);
      if (this.selectedIndex < 0) this.value = opt.value;
      else if (this._value === opt.value) this.value = opt.value;
    }
  };
  for (const item of options || []) {
    const group = {tagName: 'OPTGROUP', dataset: {provider: item.provider || ''}, children: []};
    const opt = {tagName: 'OPTION', value: item.value, textContent: item.label || item.value, title: '', dataset: {}, parentElement: group};
    group.children.push(opt);
    sel.appendChild(group);
  }
  if (initialValue !== null && initialValue !== undefined) sel.value = initialValue;
  return sel;
}

function $(id) {
  if (id === 'modelSelect') return modelSelect;
  if (id === 'composerModelDropdown') return {classList: {contains(){ return false; }}};
  return null;
}
function t(key) { return key; }
function getModelLabel(v) { return v; }
function syncModelChip() { calls.syncModelChip++; }
function renderModelDropdown() { calls.renderModelDropdown++; }
function _positionModelDropdown() { calls.positionModelDropdown++; }
function _redirectIfUnauth() { return false; }
function _fetchLiveModels(provider, _sel) { calls.liveFetches.push(provider); }

const document = {
  title: '',
  baseURI: 'http://127.0.0.1/hermes/',
  createElement(tag) {
    const upper = tag.toUpperCase();
    if (upper === 'OPTGROUP') {
      return {
        tagName: 'OPTGROUP',
        label: '',
        dataset: {},
        children: [],
        appendChild(opt) { opt.parentElement = this; this.children.push(opt); },
      };
    }
    return {tagName: upper, value: '', textContent: '', title: '', dataset: {}, parentElement: null};
  },
  createTextNode(text) { return {textContent: text}; },
};
const args = JSON.parse(process.argv[4]);
const localStorageData = Object.assign({}, args.localStorage || {});
const localStorage = {
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(localStorageData, key) ? localStorageData[key] : null;
  },
  setItem(key, value) { localStorageData[key] = String(value); },
  removeItem(key) { delete localStorageData[key]; },
};
const window = {_defaultModel: null, _activeProvider: null, _configuredModelBadges: {}};
let _dynamicModelLabels = {};
let _liveModelFetchPending = new Set();
let _liveModelCache = {};
let _modelCatalogContextEpoch = 0;
var _profileSwitchGeneration = args.profileSwitch && args.profileSwitch.generation ? args.profileSwitch.generation : 0;
const MODEL_STATE_KEY = 'hermes-webui-model-state';
window._provisionalBootModelSelection = null;
window._bootSettingsDefaultModelState = null;

function settingsDefaultModelHasExplicitSourceForTest(s) {
  if (!s || !Object.prototype.hasOwnProperty.call(s, 'default_model_has_explicit_source')) return true;
  return s.default_model_has_explicit_source === true;
}
function hydrateBootDefaultModelFromSettingsForTest(s) {
  if (!s) return;
  if (s.default_model_provider) window._activeProvider = s.default_model_provider;
  const defaultModel = String(s.default_model || '');
  if (!defaultModel) return;
  const hasExplicitSource = settingsDefaultModelHasExplicitSourceForTest(s);
  window._defaultModel = defaultModel;
  window._defaultModelHasExplicitSource = hasExplicitSource;
  window._defaultModelEligibleForFreshBoot = hasExplicitSource;
  window._bootSettingsDefaultModelState = {
    model: defaultModel,
    model_provider: s.default_model_provider || null,
    default_model_has_explicit_source: hasExplicitSource,
  };
  const sel = $('modelSelect');
  if (!hasExplicitSource) {
    let selectedState = null;
    if (sel && sel.value) {
      try {
        selectedState = typeof _modelStateForSelect === 'function'
          ? _modelStateForSelect(sel, sel.value)
          : {model: String(sel.value || ''), model_provider: null};
      } catch (_) {
        selectedState = {model: String(sel.value || ''), model_provider: null};
      }
    }
    const selectedOpt = sel && sel.selectedOptions && sel.selectedOptions[0];
    const selectedHasExplicitUiOwnership = !!(
      selectedOpt && selectedOpt.dataset && selectedOpt.dataset.custom
    );
    const persistedState = typeof _readPersistedModelState === 'function'
      ? _readPersistedModelState()
      : null;
    const persistedOwnsSelection = typeof _modelStateMatches === 'function'
      ? _modelStateMatches(selectedState, persistedState)
      : !!(selectedState && persistedState && String(selectedState.model || '') === String(persistedState.model || '')
        && String(selectedState.model_provider || '') === String(persistedState.model_provider || ''));
    window._provisionalBootModelSelection = (selectedState && !persistedOwnsSelection && !selectedHasExplicitUiOwnership)
      ? selectedState
      : null;
    return;
  }
  window._provisionalBootModelSelection = null;
  if (sel && typeof _applyModelToDropdown === 'function') {
    const existingDefaultOpt = Array.from(sel.options).find(o => o.value === defaultModel);
    if (existingDefaultOpt && window._activeProvider && !existingDefaultOpt.dataset.provider) {
      existingDefaultOpt.dataset.provider = window._activeProvider;
    }
    if (!existingDefaultOpt) {
      const opt = document.createElement('option');
      opt.value = defaultModel;
      opt.textContent = typeof getModelLabel === 'function' ? getModelLabel(defaultModel) : defaultModel;
      opt.dataset.custom = '1';
      opt.dataset.provider = window._activeProvider || '';
      sel.querySelectorAll('option[data-custom]').forEach(o => o.remove());
      sel.appendChild(opt);
    }
    _applyModelToDropdown(defaultModel, sel, window._activeProvider || null);
  }
}

for (const name of [
  '_modelCatalogHasRealProviderModels', '_shouldApplyModelPayloadDefault',
  '_currentBootSettingsDefaultOverride', '_applyBootSettingsDefaultOverrideToModelPayload',
  '_getOptionProviderId', '_providerFromModelValue', '_modelStateForSelect',
  '_captureModelDropdownSelection', '_modelStateMatches',
  '_readPersistedModelState', '_writePersistedModelState', '_clearPersistedModelState',
  '_findModelInDropdown', '_refreshOpenModelDropdown',
  '_applyModelToDropdown', '_ensureModelOptionInDropdown',
  '_reconcileModelDropdownSelection',
  '_modelCatalogContextProfile', '_modelCatalogContextGeneration',
  '_modelCatalogContextSnapshot', '_modelCatalogContextStillCurrent',
  '_modelCatalogRequestStillCurrent', '_invalidateModelCatalogContext',
  '_resetModelCatalogSurfacesForProfileSwitch', 'populateModelDropdown'
]) {
  eval(extractFunc(name));
}

apiModels = args.apiModels;
modelSelect = makeSelect(args.initialOptions, args.initialValue);
var S = {session: args.session || null};
S.activeProfile = args.activeProfile || 'default';

if (args.settings) {
  hydrateBootDefaultModelFromSettingsForTest(args.settings);
}
const advanceProfileSwitchStateSource = tryExtractPanelFunc('_advanceBootSettingsDefaultModelStateForProfileSwitch');
if (advanceProfileSwitchStateSource) eval(advanceProfileSwitchStateSource);
if (args.profileSwitch) {
  S.activeProfile = args.profileSwitch.active || 'default';
  window._defaultModel = args.profileSwitch.default_model || null;
  window._activeProvider = args.profileSwitch.default_model_provider || null;
  if (typeof _advanceBootSettingsDefaultModelStateForProfileSwitch === 'function') {
    _advanceBootSettingsDefaultModelStateForProfileSwitch(args.profileSwitch, _profileSwitchGeneration);
  }
}

fetch = async function(url) {
  const href = String(url);
  if (href.includes('api/models')) {
    return {json: async () => apiModels};
  }
  throw new Error('unexpected fetch ' + href);
};

function applyBootSavedStateForTest() {
  const sessionModelState = S.session && S.session.model
    ? {model: S.session.model, model_provider: S.session.model_provider || null}
    : null;
  const savedState = (typeof _readPersistedModelState === 'function')
    ? _readPersistedModelState()
    : null;
  const defaultWinsFreshBoot = !!window._defaultModel && window._defaultModelEligibleForFreshBoot !== false;
  const stateToApply = sessionModelState || (!defaultWinsFreshBoot ? savedState : null);
  const savedModel = stateToApply && stateToApply.model;
  if (savedModel && $('modelSelect')) {
    let applied = (typeof _applyModelToDropdown === 'function')
      ? (sessionModelState
        ? _applyModelToDropdown(sessionModelState.model, $('modelSelect'), sessionModelState.model_provider || null)
        : _applyModelToDropdown(savedState.model, $('modelSelect'), savedState.model_provider || null))
      : null;
    if (!applied && sessionModelState && typeof _ensureModelOptionInDropdown === 'function') {
      applied = _ensureModelOptionInDropdown(sessionModelState.model, $('modelSelect'), sessionModelState.model_provider || null);
    } else if (!applied && !sessionModelState && savedState && savedState.model_provider && typeof _ensureModelOptionInDropdown === 'function') {
      applied = _ensureModelOptionInDropdown(savedState.model, $('modelSelect'), savedState.model_provider || null);
    }
    if (!applied) $('modelSelect').value = stateToApply.model;
    if (!applied && !sessionModelState && $('modelSelect').value !== stateToApply.model) {
      if (typeof _clearPersistedModelState === 'function') _clearPersistedModelState();
      else {
        localStorage.removeItem('hermes-webui-model');
        localStorage.removeItem('hermes-webui-model-state');
      }
    } else if (typeof syncModelChip === 'function') syncModelChip();
  }
}

populateModelDropdown(args.opts || {}).then(() => {
  if (args.applyBootSavedState) applyBootSavedStateForTest();
  process.stdout.write(JSON.stringify({
    selectValue: modelSelect.value,
    selectedProvider: modelSelect.selectedOptions[0] ? _getOptionProviderId(modelSelect.selectedOptions[0]) : null,
    selectedState: modelSelect.value && typeof _modelStateForSelect === 'function'
      ? _modelStateForSelect(modelSelect, modelSelect.value)
      : null,
    optionValues: modelSelect.options.map(o => o.value),
    defaultModel: window._defaultModel,
    defaultModelHasExplicitSource: window._defaultModelHasExplicitSource,
    defaultModelEligibleForFreshBoot: window._defaultModelEligibleForFreshBoot,
    provisionalBootModelSelection: window._provisionalBootModelSelection,
    badgeKeys: Object.keys(window._configuredModelBadges || {}),
    badgeMap: window._configuredModelBadges || {},
    activeProvider: window._activeProvider,
    bootSettingsDefaultModelState: window._bootSettingsDefaultModelState,
    localStorage: localStorageData,
    calls,
  }));
}).catch(err => {
  console.error(err && err.stack || err);
  process.exit(1);
});
"""


_PROFILE_SWITCH_RACE_DRIVER_SRC = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const panels = fs.readFileSync(process.argv[3], 'utf8');
const sessions = fs.readFileSync(process.argv[4], 'utf8');
const args = JSON.parse(process.argv[5]);
console.debug = () => {};

function extractFuncFrom(src, name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let openParen = src.indexOf('(', start);
  let i = openParen + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < src.length) {
    if (src[i] === '(') parenDepth++;
    else if (src[i] === ')') parenDepth--;
    i++;
  }
  i = src.indexOf('{', i);
  let depth = 1;
  i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return {promise, resolve, reject};
}

async function tick(count = 5) {
  for (let i = 0; i < count; i++) await Promise.resolve();
}

async function waitFor(label, predicate) {
  for (let i = 0; i < 80; i++) {
    if (predicate()) return;
    await tick();
  }
  throw new Error('timed out waiting for ' + label + ' with state ' + JSON.stringify({
    profile: typeof S === 'undefined' ? null : S.activeProfile,
    generation: typeof _profileSwitchGeneration === 'undefined' ? null : _profileSwitchGeneration,
    modelRequests: typeof modelRequests === 'undefined' ? null : modelRequests.length,
    renderWaits: typeof renderWaits === 'undefined' ? null : renderWaits.length,
    calls: typeof calls === 'undefined' ? null : calls,
  }));
}

function makeSelect(options, initialValue) {
  const sel = {id: 'modelSelect', options: [], selectedIndex: -1, selectedOptions: []};
  Object.defineProperty(sel, 'value', {
    get() { return this._value || ''; },
    set(v) {
      const idx = this.options.findIndex(o => o.value === v);
      this.selectedIndex = idx;
      if (idx >= 0) {
        this._value = v;
        this.selectedOptions = [this.options[idx]];
      } else {
        this._value = '';
        this.selectedOptions = [];
      }
    }
  });
  Object.defineProperty(sel, 'innerHTML', {
    get() { return ''; },
    set(_v) {
      this.options = [];
      this._value = '';
      this.selectedIndex = -1;
      this.selectedOptions = [];
    }
  });
  sel.querySelector = function(_selector) { return this.options[0] || null; };
  sel.querySelectorAll = function(selector) {
    if (selector === 'option[data-custom]') {
      return this.options.filter(o => o.dataset && o.dataset.custom);
    }
    if (selector === 'optgroup') {
      const groups = [];
      for (const opt of this.options) {
        const group = opt.parentElement;
        if (group && group.tagName === 'OPTGROUP' && !groups.includes(group)) groups.push(group);
      }
      return groups;
    }
    return [];
  };
  sel.appendChild = function(node) {
    const incoming = node && node.tagName === 'OPTGROUP' ? node.children : [node];
    for (const opt of incoming || []) {
      opt.parentElement = opt.parentElement || node;
      opt.remove = opt.remove || (() => {
        const optIndex = this.options.indexOf(opt);
        if (optIndex >= 0) this.options.splice(optIndex, 1);
        const children = opt.parentElement && opt.parentElement.children;
        if (Array.isArray(children)) {
          const childIndex = children.indexOf(opt);
          if (childIndex >= 0) children.splice(childIndex, 1);
        }
        if (this.selectedOptions[0] === opt) this.value = this.options[0] ? this.options[0].value : '';
      });
      this.options.push(opt);
      if (this.selectedIndex < 0) this.value = opt.value;
      else if (this._value === opt.value) this.value = opt.value;
    }
  };
  for (const item of options || []) {
    const group = {tagName: 'OPTGROUP', label: item.provider || '', dataset: {provider: item.provider || ''}, children: []};
    const opt = {tagName: 'OPTION', value: item.value, textContent: item.label || item.value, title: '', dataset: {}, parentElement: group};
    if (item.custom) opt.dataset.custom = '1';
    if (item.provider) opt.dataset.provider = item.provider;
    group.children.push(opt);
    sel.appendChild(group);
  }
  if (initialValue !== null && initialValue !== undefined) sel.value = initialValue;
  return sel;
}

const calls = {syncModelChip: 0, refreshes: 0, renderSessionList: 0, toasts: []};
let modelSelect = makeSelect(
  args.initialOptions || [{provider: 'alpha-provider', value: 'custom/profile-a-model', label: 'Profile A model'}],
  args.initialValue || 'custom/profile-a-model',
);
const localStorageData = {};
const localStorage = {
  getItem(key) { return Object.prototype.hasOwnProperty.call(localStorageData, key) ? localStorageData[key] : null; },
  setItem(key, value) { localStorageData[key] = String(value); },
  removeItem(key) { delete localStorageData[key]; },
};
const window = {
  _defaultModel: args.initialDefault || 'custom/profile-a-model',
  _activeProvider: args.initialProvider || 'alpha-provider',
  _configuredModelBadges: {},
  _modelEndpointErrors: {},
  _provisionalBootModelSelection: null,
  _bootSettingsDefaultModelState: {
    model: args.initialDefault || 'custom/profile-a-model',
    model_provider: args.initialProvider || 'alpha-provider',
    default_model_has_explicit_source: true,
    profile: 'alpha',
    profile_switch_generation: 0,
  },
};
let _dynamicModelLabels = {};
let _liveModelFetchPending = new Set();
let _liveModelCache = {};
let _liveModelFetchSeq = 0;
let _modelCatalogContextEpoch = 0;
var _profileSwitchGeneration = 0;
var _modelDropdownRequestSeq = 0;
var _modelCatalogFallbackRetried = false;
const MODEL_STATE_KEY = 'hermes-webui-model-state';
const document = {
  baseURI: 'http://127.0.0.1/hermes/',
  createElement(tag) {
    const upper = tag.toUpperCase();
    if (upper === 'OPTGROUP') {
      return {
        tagName: 'OPTGROUP',
        label: '',
        dataset: {},
        children: [],
        appendChild(opt) { opt.parentElement = this; this.children.push(opt); },
      };
    }
    return {tagName: upper, value: '', textContent: '', title: '', dataset: {}, parentElement: null};
  },
};
var S = {activeProfile: 'alpha', activeProfileIsDefault: false, session: null, messages: []};
var _currentPanel = '';
var _workspacePanelMode = 'closed';
var _skillsData = null;
var _workspaceList = null;

function $(id) {
  if (id === 'modelSelect') return modelSelect;
  if (id === 'composerModelDropdown') return {classList: {contains(){ return false; }}};
  if (id === 'profileChip') return {classList: {add(){}, remove(){}}, disabled: false};
  if (id === 'titlebarProfileBtn') return {classList: {add(){}, remove(){}}, disabled: false};
  if (id === 'profileChipLabel' || id === 'titlebarProfileLabel') return {textContent: ''};
  return null;
}
function t(key) { return key; }
function getModelLabel(v) { return v; }
function assistantDisplayName() { return 'Hermes'; }
function syncModelChip() { calls.syncModelChip++; }
function syncAppTitlebar() {}
function syncWorkspaceDisplays() {}
function syncReasoningChip() {}
function syncToolsetsChip() {}
function syncTerminalButton() {}
function _syncHermesPanelSessionActions() {}
function renderModelDropdown() {}
function _positionModelDropdown() {}
function _redirectIfUnauth() { return false; }
function _refreshOpenModelDropdown() {}
function startGatewaySSE() {}
function applyBotName() {}
function animateNextSessionListRefresh() {}
function _openProfileSwitchSessionBrowser() {}
function showToast(msg) { calls.toasts.push(msg); }
function loadWorkspaceList() { return Promise.resolve([]); }
function _clearCronDetail() {}

const renderWaits = [];
let renderCount = 0;
async function renderSessionList() {
  calls.renderSessionList++;
  const plan = (args.renderPlan || [])[renderCount++] || 'resolve';
  if (plan === 'defer') {
    const d = deferred();
    renderWaits.push(d);
    return d.promise;
  }
}

const modelRequests = [];
const liveRequests = [];
fetch = function(url) {
  const href = String(url);
  if (href.includes('api/models/live')) {
    const d = deferred();
    liveRequests.push({href, resolve: d.resolve, reject: d.reject});
    return d.promise;
  }
  if (href.includes('api/models')) {
    const d = deferred();
    modelRequests.push({href, resolve: d.resolve, reject: d.reject});
    return d.promise;
  }
  throw new Error('unexpected fetch ' + href);
};

const profilePayloads = {
  beta: {active: 'beta', is_default: false, default_model: 'custom/profile-b-model', default_model_provider: 'beta-provider'},
  gamma: {active: 'gamma', is_default: false, default_model: 'custom/profile-c-model', default_model_provider: 'gamma-provider'},
};
const profileSwitchRequests = [];
api = async function(url, opts = {}) {
  if (url === '/api/profile/switch') {
    const body = JSON.parse(opts.body || '{}');
    if (args.scenario === 'session-mismatch-rapid-beta-gamma') {
      const d = deferred();
      profileSwitchRequests.push({name: body.name, resolve: d.resolve, reject: d.reject});
      return d.promise;
    }
    return profilePayloads[body.name];
  }
  if (url === '/api/settings') return {};
  if (url === '/api/session/update') return {};
  throw new Error('unexpected api ' + url);
};

for (const name of [
  '_modelCatalogHasRealProviderModels', '_shouldApplyModelPayloadDefault',
  '_currentBootSettingsDefaultOverride', '_applyBootSettingsDefaultOverrideToModelPayload',
  '_getOptionProviderId', '_providerFromModelValue', '_modelStateForSelect',
  '_captureModelDropdownSelection', '_modelStateMatches',
  '_readPersistedModelState', '_writePersistedModelState', '_clearPersistedModelState',
  '_findModelInDropdown', '_applyModelToDropdown', '_ensureModelOptionInDropdown',
  '_reconcileModelDropdownSelection', '_providerDefersMissingModelFallback',
]) {
  eval(extractFuncFrom(ui, name));
}
for (const name of [
  '_modelCatalogContextProfile', '_modelCatalogContextGeneration',
  '_modelCatalogContextSnapshot', '_modelCatalogContextStillCurrent',
  '_modelCatalogRequestStillCurrent', '_invalidateModelCatalogContext',
  '_clearLiveModelCatalogState', '_modelCatalogLiveCacheKey',
  '_liveModelFetchPendingForProvider',
  '_resetModelCatalogSurfacesForProfileSwitch',
]) {
  try { eval(extractFuncFrom(ui, name)); } catch (_e) {}
}
eval(extractFuncFrom(ui, '_addLiveModelsToSelect'));
eval(extractFuncFrom(ui, '_fetchLiveModels'));
eval(extractFuncFrom(ui, 'populateModelDropdown'));
eval(extractFuncFrom(ui, 'syncTopbar'));
eval(extractFuncFrom(panels, '_refreshProfileSwitchBackground'));
eval(extractFuncFrom(panels, '_advanceBootSettingsDefaultModelStateForProfileSwitch'));
eval(extractFuncFrom(panels, '_applyAcceptedProfileSwitchModelCatalog'));
eval(extractFuncFrom(panels, '_profileSwitchPanelLoad'));
eval(extractFuncFrom(panels, 'switchToProfile'));
eval(extractFuncFrom(sessions, '_switchProfileForSessionLoad'));
window._ensureModelDropdownReady = () => {
  calls.refreshes++;
  return populateModelDropdown({preferProfileDefaultOnFreshBoot: true});
};

function modelPayload(model, provider) {
  return {
    active_provider: provider,
    default_model: model,
    default_model_has_explicit_source: true,
    configured_model_badges: {[model]: {role: 'primary', label: 'Primary', provider}},
    groups: [{provider, provider_id: provider, models: [{id: model, label: model}]}],
  };
}

function responseFor(payload) {
  return {json: async () => payload};
}

function snapshot() {
  const selectedOpt = modelSelect.selectedOptions[0] || null;
  const selectedState = modelSelect.value ? _modelStateForSelect(modelSelect, modelSelect.value) : null;
  return {
    profile: S.activeProfile,
    generation: _profileSwitchGeneration,
    defaultModel: window._defaultModel || null,
    activeProvider: window._activeProvider || null,
    selectValue: modelSelect.value || '',
    selectedProvider: selectedOpt ? _getOptionProviderId(selectedOpt) : null,
    selectedState,
    optionValues: modelSelect.options.map(o => o.value),
    badgeKeys: Object.keys(window._configuredModelBadges || {}),
    bootSettingsDefaultModelState: window._bootSettingsDefaultModelState || null,
    calls: Object.assign({}, calls),
    modelRequestCount: modelRequests.length,
    liveRequestCount: liveRequests.length,
    liveCacheModelIds: Object.values(_liveModelCache)
      .flat()
      .map(model => model && model.id)
      .filter(Boolean),
    livePendingKeys: Array.from(_liveModelFetchPending),
    profileSwitchRequestCount: profileSwitchRequests.length,
  };
}

async function staleAlphaThenFailedBeta() {
  const alphaPopulate = populateModelDropdown({preferProfileDefaultOnFreshBoot: true});
  await waitFor('initial alpha model request', () => modelRequests.length === 1);
  const betaSwitch = switchToProfile('beta');
  await waitFor('beta active and paused', () => S.activeProfile === 'beta' && renderWaits.length === 1);
  modelRequests[0].resolve(responseFor(modelPayload('custom/profile-a-model', 'alpha-provider')));
  await tick(12);
  const afterStaleAlpha = snapshot();
  renderWaits[0].resolve();
  await waitFor('beta background model request', () => modelRequests.length === 2);
  modelRequests[1].reject(new Error('beta catalog unavailable'));
  await Promise.allSettled([alphaPopulate, betaSwitch]);
  await tick(12);
  return {afterStaleAlpha, final: snapshot()};
}

async function rapidBetaThenGamma() {
  const betaSwitch = switchToProfile('beta');
  await waitFor('beta background model request', () => modelRequests.length === 1);
  await betaSwitch;
  const gammaSwitch = switchToProfile('gamma');
  await waitFor('gamma active and paused', () => S.activeProfile === 'gamma' && renderWaits.length === 1);
  modelRequests[0].resolve(responseFor(modelPayload('custom/profile-b-model', 'beta-provider')));
  await tick(12);
  const afterStaleBeta = snapshot();
  renderWaits[0].resolve();
  await waitFor('gamma background model request', () => modelRequests.length === 2);
  modelRequests[1].reject(new Error('gamma catalog unavailable'));
  await Promise.allSettled([gammaSwitch]);
  await tick(12);
  return {afterStaleBeta, final: snapshot()};
}

async function ownerlessLiveSameProviderCacheLeak() {
  modelSelect = makeSelect(
    [{provider: 'shared-provider', value: 'custom/profile-a-model', label: 'Profile A model'}],
    'custom/profile-a-model',
  );
  S.activeProfile = 'alpha';
  S.session = null;
  S.messages = [];
  _profileSwitchGeneration = 0;
  _modelDropdownRequestSeq = 0;
  _modelCatalogContextEpoch = 0;
  window._defaultModel = 'custom/profile-a-model';
  window._activeProvider = 'shared-provider';
  window._configuredModelBadges = {
    'custom/profile-a-model': {role: 'primary', label: 'Primary', provider: 'shared-provider'},
  };

  const alphaLive = _fetchLiveModels('shared-provider', modelSelect);
  await waitFor('alpha live request', () => liveRequests.length === 1);
  const betaGen = ++_profileSwitchGeneration;
  S.activeProfile = 'beta';
  _resetModelCatalogSurfacesForProfileSwitch({
    active: 'beta',
    default_model: 'custom/profile-b-model',
    default_model_provider: 'shared-provider',
  }, betaGen);
  _advanceBootSettingsDefaultModelStateForProfileSwitch({
    active: 'beta',
    default_model: 'custom/profile-b-model',
    default_model_provider: 'shared-provider',
    default_model_has_explicit_source: true,
  }, betaGen);
  liveRequests[0].resolve(responseFor({
    models: [{id: 'custom/profile-a-live', label: 'Profile A live model'}],
  }));
  await alphaLive;
  await tick(12);
  return {final: snapshot()};
}

async function sessionMismatchSwitchFailedRefresh() {
  profilePayloads.beta = {
    active: 'beta',
    is_default: false,
    default_model: 'custom/profile-b-model',
    default_model_provider: 'shared-provider',
  };
  modelSelect = makeSelect(
    [{provider: 'alpha-provider', value: 'custom/profile-a-model', label: 'Profile A model'}],
    'custom/profile-a-model',
  );
  S.activeProfile = 'alpha';
  S.session = {
    session_id: 'alpha-session',
    title: 'Alpha session',
    model: 'custom/profile-a-model',
    model_provider: 'alpha-provider',
    message_count: 1,
  };
  S.messages = [{role: 'user'}];
  _profileSwitchGeneration = 0;
  _modelDropdownRequestSeq = 0;
  _modelCatalogContextEpoch = 0;
  window._defaultModel = 'custom/profile-a-model';
  window._activeProvider = 'alpha-provider';
  await _switchProfileForSessionLoad('beta');
  await tick(12);
  if (modelRequests.length) {
    modelRequests[0].reject(new Error('beta catalog unavailable'));
    await tick(12);
  }
  return {final: snapshot()};
}

async function sessionMismatchRapidBetaGamma() {
  profilePayloads.beta = {
    active: 'beta',
    is_default: false,
    default_model: 'custom/profile-b-model',
    default_model_provider: 'shared-provider',
  };
  profilePayloads.gamma = {
    active: 'gamma',
    is_default: false,
    default_model: 'custom/profile-c-model',
    default_model_provider: 'shared-provider',
  };
  modelSelect = makeSelect(
    [{provider: 'alpha-provider', value: 'custom/profile-a-model', label: 'Profile A model'}],
    'custom/profile-a-model',
  );
  S.activeProfile = 'alpha';
  S.session = {
    session_id: 'alpha-session',
    title: 'Alpha session',
    model: 'custom/profile-a-model',
    model_provider: 'alpha-provider',
    message_count: 1,
  };
  S.messages = [{role: 'user'}];
  _profileSwitchGeneration = 0;
  _modelDropdownRequestSeq = 0;
  _modelCatalogContextEpoch = 0;
  window._defaultModel = 'custom/profile-a-model';
  window._activeProvider = 'alpha-provider';

  const betaSwitch = _switchProfileForSessionLoad('beta');
  await waitFor('beta switch POST', () => profileSwitchRequests.length === 1);
  const gammaSwitch = _switchProfileForSessionLoad('gamma');
  await waitFor('gamma switch POST', () => profileSwitchRequests.length === 2);
  profileSwitchRequests[1].resolve(profilePayloads.gamma);
  await tick(12);
  profileSwitchRequests[0].resolve(profilePayloads.beta);
  await Promise.allSettled([betaSwitch, gammaSwitch]);
  await tick(12);
  return {final: snapshot()};
}

(async () => {
  let result;
  if (args.scenario === 'rapid-beta-gamma') result = await rapidBetaThenGamma();
  else if (args.scenario === 'ownerless-live-same-provider') result = await ownerlessLiveSameProviderCacheLeak();
  else if (args.scenario === 'session-mismatch-failed-refresh') result = await sessionMismatchSwitchFailedRefresh();
  else if (args.scenario === 'session-mismatch-rapid-beta-gamma') result = await sessionMismatchRapidBetaGamma();
  else result = await staleAlphaThenFailedBeta();
  process.stdout.write(JSON.stringify(result));
})().catch(err => {
  console.error(err && err.stack || err);
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def populate_driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("model_default_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def profile_switch_race_driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("model_default_profile_switch_driver") / "driver.js"
    p.write_text(_PROFILE_SWITCH_RACE_DRIVER_SRC, encoding="utf-8")
    return str(p)


def test_boot_settings_applies_default_without_deleting_browser_model_state():
    snippet = _boot_default_apply_snippet()
    assert "Fresh page boot must prefer an explicit profile/server default" in snippet
    assert "if(sel&&typeof _applyModelToDropdown==='function')" in snippet
    assert "if(sel&&!savedState&&typeof _applyModelToDropdown==='function')" not in BOOT_JS
    assert "_clearPersistedModelState" not in snippet
    assert "localStorage.removeItem('hermes-webui-model')" not in snippet
    assert "localStorage.removeItem('hermes-webui-model-state')" not in snippet


def test_boot_model_dropdown_explicitly_requests_profile_default_precedence():
    assert "const _hydrateModelDropdown=({redirectIfUnauth=null}={})=>populateModelDropdown({" in BOOT_JS
    assert "_hydrateBootDefaultModelFromSettings(s);" in BOOT_JS
    assert "_settingsDefaultModelHasExplicitSource(s)" in BOOT_JS
    assert "preferProfileDefaultOnFreshBoot:true" in BOOT_JS
    assert "const defaultWinsFreshBoot=!!window._defaultModel&&window._defaultModelEligibleForFreshBoot!==false;" in BOOT_JS
    assert "const stateToApply=sessionModelState||(!defaultWinsFreshBoot?savedState:null);" in BOOT_JS


def test_boot_marks_nonexplicit_static_selection_provisional():
    block = _boot_default_apply_snippet()

    assert "const persistedState=(typeof _readPersistedModelState==='function')" in block
    assert "const persistedOwnsSelection=typeof _modelStateMatches==='function'" in block
    assert "window._provisionalBootModelSelection=(selectedState&&!persistedOwnsSelection&&!selectedHasExplicitUiOwnership)" in block
    assert "window._provisionalBootModelSelection=null;" in block
    assert "if(!hasExplicitSource)" in block
    assert "if(!hasExplicitSource) return;" not in block


@pytest.mark.parametrize(
    (
        "config_data",
        "webui_default_model",
        "env_model",
        "expected_model",
        "expected_provider",
        "expected_explicit",
    ),
    [
        ({"model": {"provider": "safe"}}, "", None, "", "safe", False),
        (
            {"model": {"provider": "safe"}},
            "webui-env-default-model",
            None,
            "webui-env-default-model",
            "safe",
            True,
        ),
        ({"model": "legacy-explicit-model"}, "", None, "legacy-explicit-model", None, True),
        (
            {"model": {"provider": "safe", "default": "dict-explicit-model"}},
            "",
            None,
            "dict-explicit-model",
            "safe",
            True,
        ),
        ({"model": {"provider": "safe"}}, "", "env-explicit-model", "env-explicit-model", "safe", True),
    ],
)
def test_load_settings_exposes_default_model_explicit_source(
    monkeypatch,
    config_data,
    webui_default_model,
    env_model,
    expected_model,
    expected_provider,
    expected_explicit,
):
    monkeypatch.setattr(config_api, "DEFAULT_MODEL", webui_default_model, raising=False)
    monkeypatch.setattr(config_api, "cfg", dict(config_data), raising=False)
    monkeypatch.setattr(config_api, "get_config", lambda: dict(config_data))
    monkeypatch.setattr(config_api, "_read_raw_settings_file", lambda: {})
    for key in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    if env_model:
        monkeypatch.setenv("HERMES_MODEL", env_model)

    settings = config_api.load_settings()

    assert settings["default_model"] == expected_model
    assert settings["default_model_has_explicit_source"] is expected_explicit
    if expected_provider:
        assert settings["default_model_provider"] == expected_provider
    else:
        assert "default_model_provider" not in settings


def test_load_settings_marks_webui_default_model_env_source_explicit(monkeypatch):
    monkeypatch.setattr(config_api, "DEFAULT_MODEL", "custom/env-model", raising=False)
    monkeypatch.setattr(
        config_api,
        "cfg",
        {"model": {"provider": "safe"}, "providers": {}, "fallback_providers": []},
        raising=False,
    )
    monkeypatch.setattr(
        config_api,
        "get_config",
        lambda: {"model": {"provider": "safe"}, "providers": {}, "fallback_providers": []},
    )
    monkeypatch.setattr(config_api, "_read_raw_settings_file", lambda: {})
    for key in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)

    settings = config_api.load_settings()

    assert config_api.get_effective_default_model(config_api.cfg) == "custom/env-model"
    assert settings["default_model"] == "custom/env-model"
    assert settings["default_model_has_explicit_source"] is True
    assert settings["default_model_provider"] == "safe"


def test_webui_default_model_env_survives_fresh_import_catalogs(tmp_path):
    env = os.environ.copy()
    for key in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        env.pop(key, None)
    env["HERMES_WEBUI_DEFAULT_MODEL"] = "custom/env-model"
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["HERMES_BASE_HOME"] = str(tmp_path / "home")
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path / "state")
    env["HERMES_WEBUI_DEFAULT_WORKSPACE"] = str(tmp_path / "workspace")
    env["HERMES_CONFIG_PATH"] = str(tmp_path / "home" / "config.yaml")

    code = r"""
import json
import sys
import types
from api import config

fake_pkg = types.ModuleType("hermes_cli")
fake_pkg.__path__ = []
fake_models = types.ModuleType("hermes_cli.models")
fake_models.list_available_providers = lambda: [
    {"id": "openai-codex", "authenticated": True},
]
fake_auth = types.ModuleType("hermes_cli.auth")
fake_auth.get_auth_status = lambda pid: {"key_source": "env", "logged_in": True}
sys.modules["hermes_cli"] = fake_pkg
sys.modules["hermes_cli.models"] = fake_models
sys.modules["hermes_cli.auth"] = fake_auth

config.cfg = {
    "model": {"provider": "openai-codex"},
    "providers": {},
    "fallback_providers": [],
}
config._read_raw_settings_file = lambda: {}
try:
    config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
except Exception:
    config._cfg_mtime = 0.0
config.invalidate_models_cache()

def catalog_has_model(catalog, model_id):
    return any(
        str(model.get("id") or "") == model_id
        for group in catalog.get("groups", [])
        for bucket in ("models", "extra_models")
        for model in group.get(bucket, [])
    )

def catalog_has_real_provider_models(catalog):
    return any(
        str(group.get("provider_id") or "") not in ("", "default")
        and any(str(model.get("id") or "").strip() for model in group.get("models", []))
        for group in catalog.get("groups", [])
    )

def badge_for(catalog, model_id):
    badges = catalog.get("configured_model_badges") or {}
    direct = badges.get(model_id)
    if direct:
        return direct
    matches = [
        badge
        for key, badge in badges.items()
        if model_id in str(key)
    ]
    return matches[0] if matches else None

settings = config.load_settings()
static_catalog = config._static_models_catalog_without_live_probes()
config.invalidate_models_cache()
live_catalog = config.get_available_models(force_refresh=True)

print(json.dumps({
    "DEFAULT_MODEL": config.DEFAULT_MODEL,
    "effective": config.get_effective_default_model(config.cfg),
    "explicit": config._default_model_has_explicit_source(config.cfg),
    "settings_default": settings.get("default_model"),
    "settings_explicit": settings.get("default_model_has_explicit_source"),
    "settings_provider": settings.get("default_model_provider"),
    "static_default": static_catalog.get("default_model"),
    "static_explicit": static_catalog.get("default_model_has_explicit_source"),
    "static_has_real_provider_models": catalog_has_real_provider_models(static_catalog),
    "static_has_override": catalog_has_model(static_catalog, "custom/env-model"),
    "static_badge": badge_for(static_catalog, "custom/env-model"),
    "live_default": live_catalog.get("default_model"),
    "live_explicit": live_catalog.get("default_model_has_explicit_source"),
    "live_has_real_provider_models": catalog_has_real_provider_models(live_catalog),
    "live_has_override": catalog_has_model(live_catalog, "custom/env-model"),
    "live_badge": badge_for(live_catalog, "custom/env-model"),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["DEFAULT_MODEL"] == "custom/env-model"
    assert payload["effective"] == "custom/env-model"
    assert payload["explicit"] is True
    assert payload["settings_default"] == "custom/env-model"
    assert payload["settings_explicit"] is True
    assert payload["settings_provider"] == "openai-codex"

    for prefix in ("static", "live"):
        assert payload[f"{prefix}_default"] == "custom/env-model"
        assert payload[f"{prefix}_explicit"] is True
        assert payload[f"{prefix}_has_real_provider_models"] is True
        assert payload[f"{prefix}_has_override"] is True
        assert payload[f"{prefix}_badge"] == {
            "role": "primary",
            "label": "Primary",
            "provider": "openai-codex",
        }


def test_save_settings_drops_derived_default_model_metadata(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "font_size": "small",
                "default_model": "stale-fallback",
                "default_model_provider": "stale-provider",
                "default_model_has_explicit_source": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_api, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_api, "cfg", {"model": "legacy-explicit-model"}, raising=False)
    monkeypatch.setattr(config_api, "get_config", lambda: {"model": "legacy-explicit-model"})
    for key in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)

    saved = config_api.save_settings({"font_size": "large"})
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    reloaded = config_api.load_settings()

    assert persisted["font_size"] == "large"
    for key in (
        "default_model",
        "default_model_provider",
        "default_model_has_explicit_source",
    ):
        assert key not in persisted
    assert saved["default_model"] == "legacy-explicit-model"
    assert saved["default_model_has_explicit_source"] is True
    assert "default_model_provider" not in saved
    assert reloaded["default_model"] == "legacy-explicit-model"
    assert reloaded["default_model_has_explicit_source"] is True
    assert "default_model_provider" not in reloaded


def test_populate_model_dropdown_reconciles_selection_after_rebuild():
    assert "let previousSelection=_captureModelDropdownSelection(sel);" in UI_JS
    assert "const rawProvisionalBootSelection=window._provisionalBootModelSelection||null;" in UI_JS
    assert "data=_applyBootSettingsDefaultOverrideToModelPayload(data,opts);" in UI_JS
    assert "if(opts&&opts.preferProfileDefaultOnFreshBoot) window._bootSettingsDefaultModelState=null;" in UI_JS
    assert "const persistedState=(typeof _readPersistedModelState==='function')?_readPersistedModelState():null;" in UI_JS
    assert "window._provisionalBootModelSelection=null;" in UI_JS
    assert "_reconcileModelDropdownSelection(sel,data,previousSelection,opts);" in UI_JS
    snippet = _reconcile_selection_snippet()
    assert "preferProfileDefaultOnFreshBoot" in snippet
    # #4363: each branch now routes through _applyOrEnsure, which delegates to
    # _ensureModelOptionInDropdown so a cross-provider model missing from a
    # partially-rebuilt catalog is injected as a custom option instead of the
    # browser silently snapping the <select> to its first <option>. The
    # branch ORDER + per-branch model/provider arguments are unchanged.
    assert "_shouldApplyModelPayloadDefault(data)" in snippet
    assert "_applyOrEnsure(data.default_model, data.active_provider||null)" in snippet
    assert "_applyOrEnsure(activeSession.model, activeSession.model_provider||null)" in snippet
    assert "_applyOrEnsure(previousState.model, previousState.model_provider||null)" in snippet
    # the helper must fall back to injecting the missing option, not return null
    assert "_ensureModelOptionInDropdown(modelId, sel, providerId)" in snippet
    assert "_readPersistedModelState()" not in snippet
    assert "localStorage.getItem('hermes-webui-model')" not in snippet


def test_profile_switch_advances_boot_settings_default_before_background_refresh():
    switch_start = PANELS_JS.index("async function switchToProfile(name) {")
    switch_body = PANELS_JS[switch_start : PANELS_JS.index("function openProfileCreate", switch_start)]
    accepted_switch_idx = switch_body.index("if (_switchGen !== _profileSwitchGeneration) return false;")
    invalidate_idx = switch_body.index("if (typeof _invalidateModelCatalogContext === 'function') _invalidateModelCatalogContext();")
    active_profile_idx = switch_body.index("S.activeProfile = data.active || name;")
    helper_call_idx = switch_body.index("_applyAcceptedProfileSwitchModelCatalog(data,_switchGen)")
    panel_load_idx = switch_body.index("await _profileSwitchPanelLoad();")
    refresh_idx = switch_body.index("_refreshProfileSwitchBackground(_switchGen);")

    assert accepted_switch_idx < invalidate_idx < active_profile_idx < helper_call_idx < panel_load_idx < refresh_idx
    helper = PANELS_JS[
        PANELS_JS.index("function _advanceBootSettingsDefaultModelStateForProfileSwitch")
        : switch_start
    ]
    assert "window._bootSettingsDefaultModelState=null;" in helper
    assert "profile_switch_generation:gen" in helper
    accepted_helper = PANELS_JS[
        PANELS_JS.index("function _applyAcceptedProfileSwitchModelCatalog")
        : PANELS_JS.index("async function loadProfilesPanel", PANELS_JS.index("function _applyAcceptedProfileSwitchModelCatalog"))
    ]
    assert "_resetModelCatalogSurfacesForProfileSwitch(data,gen)" in accepted_helper
    assert "_advanceBootSettingsDefaultModelStateForProfileSwitch(data,gen);" in accepted_helper


def test_model_select_onchange_retires_provisional_boot_marker():
    start = BOOT_JS.index("$('modelSelect').onchange=async()=>{")
    end = BOOT_JS.index("if(typeof _writePersistedModelState==='function')", start)
    block = BOOT_JS[start:end]

    assert "window._provisionalBootModelSelection=null;" in block


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_non_boot_model_refresh_preserves_current_in_page_selection(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={},
        session=None,
    )

    assert got["selectValue"] == "@expensive:gpt-5.5"
    assert got["selectedProvider"] == "expensive"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_model_refresh_prefers_profile_default_over_stale_selection(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
    )

    assert got["selectValue"] == "@safe:gpt-4o-mini"
    assert got["selectedProvider"] == "safe"
    assert got["defaultModelHasExplicitSource"] is True
    assert got["defaultModelEligibleForFreshBoot"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_model_refresh_does_not_synthesize_nonexplicit_fallback_when_catalog_has_models(
    populate_driver_path,
):
    fallback_model = "fallback/model-that-is-not-in-provider-catalog"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        api_default_model=fallback_model,
        api_default_model_has_explicit_source=False,
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
            {"provider": "Expensive", "provider_id": "expensive", "models": [{"id": "@expensive:gpt-5.5", "label": "GPT-5.5"}]},
        ],
    )

    assert got["selectValue"] == "@expensive:gpt-5.5"
    assert fallback_model not in got["optionValues"]
    assert f"@safe:{fallback_model}" not in got["optionValues"]
    assert got["defaultModelHasExplicitSource"] is False
    assert got["defaultModelEligibleForFreshBoot"] is False


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_model_refresh_applies_explicit_env_default_when_catalog_has_models(
    populate_driver_path,
):
    env_default = "custom/env-model"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        settings={
            "default_model": env_default,
            "default_model_provider": "safe",
            "default_model_has_explicit_source": True,
        },
        api_default_model=env_default,
        api_default_model_has_explicit_source=True,
        api_active_provider="safe",
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
            {"provider": "Expensive", "provider_id": "expensive", "models": [{"id": "@expensive:gpt-5.5", "label": "GPT-5.5"}]},
        ],
        initial_options=[
            {"provider": "expensive", "value": "@expensive:gpt-5.5", "label": "GPT-5.5"},
        ],
        local_storage=_persisted_model_storage("@expensive:gpt-5.5", "expensive"),
        apply_boot_saved_state=True,
    )

    assert got["selectedState"] == {
        "model": env_default,
        "model_provider": "safe",
    }
    assert got["selectedProvider"] == "safe"
    assert env_default in got["selectValue"]
    assert "@expensive:gpt-5.5" in got["localStorage"]["hermes-webui-model"]
    assert got["defaultModelHasExplicitSource"] is True
    assert got["defaultModelEligibleForFreshBoot"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_model_refresh_keeps_current_settings_default_over_stale_cached_models_payload(
    populate_driver_path,
):
    current_settings_default = "custom/env-model-b"
    stale_cached_default = "custom/env-model-a"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        settings={
            "default_model": current_settings_default,
            "default_model_provider": "safe",
            "default_model_has_explicit_source": True,
        },
        api_default_model=stale_cached_default,
        api_default_model_has_explicit_source=True,
        api_active_provider="safe",
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
            {"provider": "Stale", "provider_id": "safe", "models": [{"id": stale_cached_default, "label": stale_cached_default}]},
        ],
        initial_options=[
            {"provider": "expensive", "value": "@expensive:gpt-5.5", "label": "GPT-5.5"},
        ],
        local_storage=_persisted_model_storage("@expensive:gpt-5.5", "expensive"),
        apply_boot_saved_state=True,
    )

    assert got["selectedState"] == {
        "model": current_settings_default,
        "model_provider": "safe",
    }
    assert got["defaultModel"] == current_settings_default
    assert got["selectedProvider"] == "safe"
    assert current_settings_default in got["selectValue"]
    assert stale_cached_default not in got["selectedState"]["model"]
    assert got["defaultModelHasExplicitSource"] is True
    assert got["defaultModelEligibleForFreshBoot"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_switch_model_refresh_keeps_switched_profile_default_over_boot_snapshot(
    populate_driver_path,
):
    boot_model = "custom/profile-a-model"
    switched_model = "custom/profile-b-model"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value=boot_model,
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        active_profile="alpha",
        settings={
            "default_model": boot_model,
            "default_model_provider": "alpha-provider",
            "default_model_has_explicit_source": True,
        },
        profile_switch={
            "active": "beta",
            "default_model": switched_model,
            "default_model_provider": "beta-provider",
            "generation": 1,
        },
        api_default_model=switched_model,
        api_default_model_has_explicit_source=True,
        api_active_provider="beta-provider",
        api_groups=[
            {
                "provider": "Beta",
                "provider_id": "beta-provider",
                "models": [{"id": switched_model, "label": "Profile B model"}],
            }
        ],
        initial_options=[
            {"provider": "alpha-provider", "value": boot_model, "label": "Profile A model"},
        ],
    )

    assert got["selectedState"] == {
        "model": switched_model,
        "model_provider": "beta-provider",
    }
    assert got["defaultModel"] == switched_model
    assert got["activeProvider"] == "beta-provider"
    assert got["selectedProvider"] == "beta-provider"
    assert got["selectValue"] == switched_model
    assert switched_model in got["optionValues"]
    assert boot_model not in got["optionValues"]
    assert boot_model not in got["badgeKeys"]
    assert got["badgeMap"].get(boot_model) is None
    assert got["bootSettingsDefaultModelState"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_switch_rejects_deferred_previous_profile_model_catalog(
    profile_switch_race_driver_path,
):
    got = _run_profile_switch_race_driver(
        profile_switch_race_driver_path,
        scenario="stale-alpha-failed-beta",
        render_plan=["defer"],
    )

    for snapshot_name in ("afterStaleAlpha", "final"):
        snapshot = got[snapshot_name]
        assert snapshot["profile"] == "beta"
        assert snapshot["defaultModel"] == "custom/profile-b-model"
        assert snapshot["activeProvider"] == "beta-provider"
        assert snapshot["selectedState"] == {
            "model": "custom/profile-b-model",
            "model_provider": "beta-provider",
        }
        assert snapshot["selectValue"] == "custom/profile-b-model"
        assert "custom/profile-a-model" not in snapshot["optionValues"]
        assert "custom/profile-a-model" not in snapshot["badgeKeys"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_rapid_profile_switch_rejects_superseded_model_catalog_response(
    profile_switch_race_driver_path,
):
    got = _run_profile_switch_race_driver(
        profile_switch_race_driver_path,
        scenario="rapid-beta-gamma",
        render_plan=["resolve", "defer"],
    )

    for snapshot_name in ("afterStaleBeta", "final"):
        snapshot = got[snapshot_name]
        assert snapshot["profile"] == "gamma"
        assert snapshot["defaultModel"] == "custom/profile-c-model"
        assert snapshot["activeProvider"] == "gamma-provider"
        assert snapshot["selectedState"] == {
            "model": "custom/profile-c-model",
            "model_provider": "gamma-provider",
        }
        assert snapshot["selectValue"] == "custom/profile-c-model"
        assert "custom/profile-b-model" not in snapshot["optionValues"]
        assert "custom/profile-b-model" not in snapshot["badgeKeys"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_ownerless_live_model_fetch_rejects_previous_profile_same_provider_cache(
    profile_switch_race_driver_path,
):
    got = _run_profile_switch_race_driver(
        profile_switch_race_driver_path,
        scenario="ownerless-live-same-provider",
        render_plan=[],
    )

    snapshot = got["final"]
    assert snapshot["profile"] == "beta"
    assert snapshot["generation"] == 1
    assert snapshot["defaultModel"] == "custom/profile-b-model"
    assert snapshot["activeProvider"] == "shared-provider"
    assert snapshot["selectedState"] == {
        "model": "custom/profile-b-model",
        "model_provider": "shared-provider",
    }
    assert snapshot["selectValue"] == "custom/profile-b-model"
    assert "custom/profile-a-model" not in snapshot["optionValues"]
    assert not any("profile-a-live" in value for value in snapshot["optionValues"])
    assert snapshot["liveCacheModelIds"] == []
    assert snapshot["livePendingKeys"] == []


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_profile_mismatch_switch_owns_model_catalog_refresh_when_refresh_fails(
    profile_switch_race_driver_path,
):
    got = _run_profile_switch_race_driver(
        profile_switch_race_driver_path,
        scenario="session-mismatch-failed-refresh",
        render_plan=[],
    )

    snapshot = got["final"]
    assert snapshot["profile"] == "beta"
    assert snapshot["generation"] == 1
    assert snapshot["defaultModel"] == "custom/profile-b-model"
    assert snapshot["activeProvider"] == "shared-provider"
    assert snapshot["selectedState"] == {
        "model": "custom/profile-b-model",
        "model_provider": "shared-provider",
    }
    assert snapshot["selectValue"] == "custom/profile-b-model"
    assert "custom/profile-a-model" not in snapshot["optionValues"]
    assert snapshot["modelRequestCount"] == 1
    assert snapshot["calls"]["refreshes"] == 1
    assert snapshot["bootSettingsDefaultModelState"] == {
        "model": "custom/profile-b-model",
        "model_provider": "shared-provider",
        "default_model_has_explicit_source": True,
        "profile": "beta",
        "profile_switch_generation": 1,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_profile_mismatch_switch_rejects_superseded_post_response(
    profile_switch_race_driver_path,
):
    got = _run_profile_switch_race_driver(
        profile_switch_race_driver_path,
        scenario="session-mismatch-rapid-beta-gamma",
        render_plan=[],
    )

    snapshot = got["final"]
    assert snapshot["profile"] == "gamma"
    assert snapshot["generation"] == 2
    assert snapshot["defaultModel"] == "custom/profile-c-model"
    assert snapshot["activeProvider"] == "shared-provider"
    assert snapshot["selectedState"] == {
        "model": "custom/profile-c-model",
        "model_provider": "shared-provider",
    }
    assert snapshot["selectValue"] == "custom/profile-c-model"
    assert "custom/profile-b-model" not in snapshot["optionValues"]
    assert "custom/profile-a-model" not in snapshot["optionValues"]
    assert snapshot["profileSwitchRequestCount"] == 2
    assert snapshot["bootSettingsDefaultModelState"] == {
        "model": "custom/profile-c-model",
        "model_provider": "shared-provider",
        "default_model_has_explicit_source": True,
        "profile": "gamma",
        "profile_switch_generation": 2,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settings_boot_then_model_refresh_does_not_recreate_nonexplicit_provider_fallback(
    populate_driver_path,
):
    fallback_model = "fallback/model-that-is-not-in-provider-catalog"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value=None,
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        settings={
            "default_model": fallback_model,
            "default_model_provider": "safe",
            "default_model_has_explicit_source": False,
        },
        api_default_model=fallback_model,
        api_default_model_has_explicit_source=False,
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
        ],
        initial_options=[
            {"provider": "openai", "value": "@openai:gpt-4o", "label": "GPT-4o"},
        ],
    )

    assert got["selectValue"] == "@safe:gpt-4o-mini"
    assert got["selectedProvider"] == "safe"
    assert "@openai:gpt-4o" not in got["optionValues"]
    assert fallback_model not in got["optionValues"]
    assert f"@safe:{fallback_model}" not in got["optionValues"]
    assert all(fallback_model not in key for key in got["badgeKeys"])
    assert got["defaultModelHasExplicitSource"] is False
    assert got["defaultModelEligibleForFreshBoot"] is False


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_preserves_provider_owned_persisted_model_matching_static_option(
    populate_driver_path,
):
    persisted_model = "openai/gpt-5.4-mini"
    persisted_provider = "openai"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value=None,
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        settings={
            "default_model": "fallback/model-that-is-not-in-provider-catalog",
            "default_model_provider": "safe",
            "default_model_has_explicit_source": False,
        },
        api_default_model="fallback/model-that-is-not-in-provider-catalog",
        api_default_model_has_explicit_source=False,
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
        ],
        initial_options=[
            {"provider": persisted_provider, "value": persisted_model, "label": persisted_model},
        ],
        local_storage=_persisted_model_storage(persisted_model, persisted_provider),
        apply_boot_saved_state=True,
    )

    assert got["selectedState"] == {
        "model": persisted_model,
        "model_provider": persisted_provider,
    }
    assert got["selectedProvider"] == persisted_provider
    assert got["selectValue"]
    assert got["provisionalBootModelSelection"] is None
    assert got["localStorage"]["hermes-webui-model"] == persisted_model
    assert json.loads(got["localStorage"]["hermes-webui-model-state"]) == {
        "model": persisted_model,
        "model_provider": persisted_provider,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_provisional_marker_does_not_steal_same_model_under_different_provider(
    populate_driver_path,
):
    persisted_model = "gpt-5.4-mini"
    persisted_provider = "custom:work"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value=None,
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        settings={
            "default_model": "fallback/model-that-is-not-in-provider-catalog",
            "default_model_provider": "safe",
            "default_model_has_explicit_source": False,
        },
        api_default_model="fallback/model-that-is-not-in-provider-catalog",
        api_default_model_has_explicit_source=False,
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
        ],
        initial_options=[
            {"provider": "openai", "value": persisted_model, "label": persisted_model},
        ],
        local_storage=_persisted_model_storage(persisted_model, persisted_provider),
        apply_boot_saved_state=True,
    )

    assert got["selectedState"] == {
        "model": persisted_model,
        "model_provider": persisted_provider,
    }
    assert got["selectedProvider"] == persisted_provider
    assert got["selectValue"]
    assert not any(value == persisted_model for value in got["optionValues"])
    assert got["provisionalBootModelSelection"] is None
    assert got["localStorage"]["hermes-webui-model"] == persisted_model
    assert json.loads(got["localStorage"]["hermes-webui-model-state"]) == {
        "model": persisted_model,
        "model_provider": persisted_provider,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    ("settings", "explicit_model"),
    [
        ({"default_model": "legacy-explicit-model", "default_model_has_explicit_source": True}, "legacy-explicit-model"),
        (
            {
                "default_model": "dict-explicit-model",
                "default_model_provider": "safe",
                "default_model_has_explicit_source": True,
            },
            "dict-explicit-model",
        ),
        (
            {
                "default_model": "env-explicit-model",
                "default_model_provider": "safe",
                "default_model_has_explicit_source": True,
            },
            "env-explicit-model",
        ),
        ({"default_model": "absent-provenance-model", "default_model_provider": "safe"}, "absent-provenance-model"),
    ],
)
def test_settings_boot_still_seeds_explicit_and_legacy_absent_defaults(
    populate_driver_path,
    settings,
    explicit_model,
):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        settings=settings,
        api_default_model=explicit_model,
        api_default_model_has_explicit_source=True,
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": explicit_model, "label": explicit_model}]},
        ],
        initial_options=[],
    )

    assert got["selectValue"] == explicit_model
    assert explicit_model in got["optionValues"]
    assert got["defaultModelHasExplicitSource"] is True
    assert got["defaultModelEligibleForFreshBoot"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_model_refresh_keeps_emergency_fallback_when_catalog_has_no_provider_models(
    populate_driver_path,
):
    fallback_model = "fallback/model-that-is-not-in-provider-catalog"
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
        settings={
            "default_model": fallback_model,
            "default_model_provider": "safe",
            "default_model_has_explicit_source": False,
        },
        api_default_model=fallback_model,
        api_default_model_has_explicit_source=False,
        api_active_provider="safe",
        api_groups=[],
        initial_options=[],
    )

    assert got["selectValue"] == fallback_model
    assert fallback_model in got["optionValues"]
    assert got["defaultModelHasExplicitSource"] is False
    assert got["defaultModelEligibleForFreshBoot"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_model_wins_over_boot_default_and_previous_selection(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session={"model": "@work:glm-5.1", "model_provider": "work"},
    )

    assert got["selectValue"] == "@work:glm-5.1"
    assert got["selectedProvider"] == "work"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_non_boot_refresh_does_not_reapply_default_when_previous_model_disappears(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@removed:gpt-old",
        opts={},
        session=None,
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
        ],
        initial_options=[
            {"provider": "removed", "value": "@removed:gpt-old", "label": "Old"},
        ],
    )

    # The old value is gone from the refreshed catalog, but non-boot refreshes
    # preserve the live in-page selection by injecting it instead of snapping
    # to the profile default.
    assert got["selectValue"] == "@removed:gpt-old"
    assert got["selectedProvider"] == "removed"
    assert "@removed:gpt-old" in got["optionValues"]


def _run_populate_driver(
    driver_path: str,
    *,
    initial_value: str | None,
    opts: dict,
    session: dict | None,
    api_default_model: str = "@safe:gpt-4o-mini",
    api_default_model_has_explicit_source: bool = True,
    api_active_provider: str = "safe",
    api_groups: list[dict] | None = None,
    initial_options: list[dict] | None = None,
    settings: dict | None = None,
    active_profile: str = "default",
    profile_switch: dict | None = None,
    local_storage: dict | None = None,
    apply_boot_saved_state: bool = False,
):
    groups = api_groups if api_groups is not None else [
        {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
        {"provider": "Expensive", "provider_id": "expensive", "models": [{"id": "@expensive:gpt-5.5", "label": "GPT-5.5"}]},
        {"provider": "Work", "provider_id": "work", "models": [{"id": "@work:glm-5.1", "label": "GLM-5.1"}]},
    ]
    payload = {
        "initialValue": initial_value,
        "initialOptions": initial_options
        if initial_options is not None
        else [
            {"provider": "expensive", "value": "@expensive:gpt-5.5", "label": "GPT-5.5"},
            {"provider": "safe", "value": "@safe:gpt-4o-mini", "label": "GPT-4o mini"},
            {"provider": "work", "value": "@work:glm-5.1", "label": "GLM-5.1"},
        ],
        "apiModels": {
            "active_provider": api_active_provider,
            "default_model": api_default_model,
            "default_model_has_explicit_source": api_default_model_has_explicit_source,
            "configured_model_badges": {},
            "groups": groups,
        },
        "opts": opts,
        "session": session,
        "settings": settings,
        "activeProfile": active_profile,
        "profileSwitch": profile_switch,
        "localStorage": local_storage or {},
        "applyBootSavedState": apply_boot_saved_state,
    }
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(REPO / "static" / "ui.js"), str(REPO / "static" / "panels.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


def _run_profile_switch_race_driver(
    driver_path: str,
    *,
    scenario: str,
    render_plan: list[str],
):
    assert NODE is not None
    payload = {
        "scenario": scenario,
        "renderPlan": render_plan,
    }
    result = subprocess.run(
        [
            NODE,
            driver_path,
            str(REPO / "static" / "ui.js"),
            str(REPO / "static" / "panels.js"),
            str(REPO / "static" / "sessions.js"),
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"profile switch race driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    return json.loads(result.stdout)


def _persisted_model_storage(model: str, provider: str | None) -> dict:
    return {
        "hermes-webui-model": model,
        "hermes-webui-model-state": json.dumps(
            {"model": model, "model_provider": provider}
        ),
    }


def _boot_default_apply_snippet() -> str:
    marker = "function _hydrateBootDefaultModelFromSettings"
    start = BOOT_JS.index(marker)
    return BOOT_JS[start : BOOT_JS.index("(async()=>", start)]


def _reconcile_selection_snippet() -> str:
    marker = "function _reconcileModelDropdownSelection"
    start = UI_JS.index(marker)
    end = UI_JS.index("function _providerQualifiedModelValueForSelect", start)
    return UI_JS[start:end]
