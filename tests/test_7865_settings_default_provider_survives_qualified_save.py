"""#7865 re-gate: saving a qualified model as the Settings default must keep the
active provider, and a subsequent preferences autosave must not mark the model
dirty.

After #7860 broadened the ``@<provider>:`` strip in ``_modelStateForSelect`` to
every routed provider, ``_captureModelDropdownSelection($('settingsModel'))``
returns the bare model (``grok-4.3``) while the Settings save still reads the
raw select value separately (``@xai-oauth:grok-4.3``). The two save branches in
``saveSettings`` compared them::

    body.default_model_provider=(modelState.model===model)?...:null;

``modelState.model===model`` is false for every qualified option, so
``default_model_provider`` was written as ``null`` and
``_applySavedSettingsUi()`` cleared ``window._activeProvider``. On master the
same save kept ``xai-oauth`` because the helper returned the qualified value
unchanged.

The same representation mismatch made the autosave dirty check
(``_autosavePreferencesSettings``) compare the stripped ``modelState.model``
against the raw qualified ``_settingsHermesDefaultModelOnOpen``, so the picker
read as dirty on every later preferences autosave and the unsaved-changes bar
never cleared.

This drives the REAL functions from static/panels.js under node (same lexical
extraction pattern as tests/test_issue7860_model_picker_split_qualified_id.py):
``saveSettings``, ``_applySavedSettingsUi`` and
``_autosavePreferencesSettings`` are extracted verbatim, their module globals
($, S, window, document, localStorage, api, t, showToast,
_enqueueSettingsPost, ...) are supplied as mocks, and the full Settings-save
path runs against a catalog that renders the model under an ``xai-oauth``
optgroup exactly the way populateModelDropdown builds server groups.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = ROOT / "static" / "panels.js"
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

NODE_BIN = str(NODE)


_EXTRACTOR = r"""
function isIdentifierChar(ch) {
  return /[A-Za-z0-9_$]/.test(ch || '');
}
function previousSignificantToken(source, index) {
  let i = index - 1;
  while (i >= 0 && /\s/.test(source[i])) i -= 1;
  if (i < 0) return '';
  if (!isIdentifierChar(source[i])) return source[i];
  const end = i + 1;
  while (i >= 0 && isIdentifierChar(source[i])) i -= 1;
  return source.slice(i + 1, end);
}
function canStartRegexLiteral(source, index) {
  const token = previousSignificantToken(source, index);
  if (!token) return true;
  if ('({[=,:;!&|?+-*~^<>'.includes(token)) return true;
  return ['return','throw','case','delete','typeof','void','new','in','of','yield','await'].includes(token);
}
function skipQuotedLiteral(source, index, quote) {
  for (let i = index + 1; i < source.length; i += 1) {
    if (source[i] === '\\') { i += 1; continue; }
    if (source[i] === quote) return i;
  }
  throw new Error('unterminated string literal');
}
function skipTemplateLiteral(source, index) {
  for (let i = index + 1; i < source.length; i += 1) {
    if (source[i] === '\\') { i += 1; continue; }
    if (source[i] === '`') return i;
    if (source[i] === '$' && source[i + 1] === '{') {
      let depth = 1;
      i += 1;
      while (i < source.length && depth > 0) {
        i += 1;
        if (source[i] === '{') depth += 1;
        else if (source[i] === '}') depth -= 1;
      }
    }
  }
  throw new Error('unterminated template literal');
}
function skipLineComment(source, index) {
  const nl = source.indexOf('\n', index);
  return nl < 0 ? source.length - 1 : nl;
}
function skipBlockComment(source, index) {
  const end = source.indexOf('*/', index + 2);
  return end < 0 ? source.length - 1 : end + 1;
}
function skipRegexLiteral(source, index) {
  let inClass = false;
  for (let i = index + 1; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === '\\') { i += 1; continue; }
    if (ch === '[') inClass = true;
    if (ch === ']') inClass = false;
    if (ch === '/' && !inClass) {
      while (/[A-Za-z]/.test(source[i + 1] || '')) i += 1;
      return i;
    }
  }
  throw new Error('unterminated regex literal');
}
function _balancedEnd(source, start) {
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === '"' || ch === "'") i = skipQuotedLiteral(source, i, ch);
    else if (ch === '`') i = skipTemplateLiteral(source, i);
    else if (ch === '/' && next === '/') i = skipLineComment(source, i);
    else if (ch === '/' && next === '*') i = skipBlockComment(source, i);
    else if (ch === '/' && canStartRegexLiteral(source, i)) i = skipRegexLiteral(source, i);
    else if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error('unterminated');
}
function extractFunction(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('not found: ' + name);
  return _balancedEnd(source, start);
}
function extractAsyncFunction(source, name) {
  const start = source.indexOf('async function ' + name + '(');
  if (start < 0) throw new Error('not found: ' + name);
  return _balancedEnd(source, start);
}
"""


# The driver reproduces the Settings panel state around the model picker: the
# same catalog shape populateModelDropdown builds (one optgroup per provider
# with dataset.provider, options valued with the qualified id and NO
# data-model), and POST capture for /api/default-model and /api/settings.
_DRIVER = (
    _EXTRACTOR
    + r"""
const fs = require('fs');
const panelsSrc = fs.readFileSync(process.argv[1], 'utf8');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');

for (const name of ['_providerFromModelValue', '_getOptionProviderId', '_modelStateForSelect', '_captureModelDropdownSelection']) {
  eval(extractFunction(uiSrc, name));
}
eval(extractAsyncFunction(panelsSrc, 'saveSettings'));
eval(extractFunction(panelsSrc, '_applySavedSettingsUi'));
eval(extractAsyncFunction(panelsSrc, '_autosavePreferencesSettings'));

const PROVIDER = 'xai-oauth';
const QUALIFIED = '@' + PROVIDER + ':grok-4.3';
const BARE = 'grok-4.3';

// --- module globals the extracted functions reference -------------------
const _store = new Map();
global.localStorage = {
  getItem: k => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => { _store.set(k, String(v)); },
  removeItem: k => { _store.delete(k); },
};
global.window = {};
global.S = { session: { model: BARE, model_provider: PROVIDER } };
global.MODEL_STATE_KEY = 'hermes-webui-model-state';

const posts = [];
global.api = async (url, opts) => {
  const body = JSON.parse((opts && opts.body) || '{}');
  posts.push({ url, body });
  if (url === '/api/default-model') {
    return { default_model: body.model, default_model_provider: body.provider };
  }
  return {};
};
global._enqueueSettingsPost = async (opts) => {
  posts.push({ url: '/api/settings', body: JSON.parse(opts.body) });
  return {};
};
const toasts = [];
global.showToast = (msg) => { toasts.push(String(msg)); };
global.t = (k) => k;
global._settingsPasswordAuthEnabled = false;
global._pendingSettingsTargetPanel = null;
global._settingsDirty = false;
global._settingsThemeOnOpen = 'dark';
global._settingsSkinOnOpen = 'default';
global._settingsFontSizeOnOpen = 'default';
global._settingsHermesDefaultModelOnOpen = '';
global._settingsHermesDefaultModelProviderOnOpen = null;
global._resetSettingsPanelState = () => {};
global._hideSettingsPanel = () => {};
global.clearMessageRenderCache = () => {};
global.renderMessages = () => {};
global.syncTopbar = () => {};
global.renderSessionList = () => {};
global._setSettingsAuthButtonsVisible = () => {};
global._applyComposerFooterVisibilitySettings = () => {};
global.startGatewaySSE = () => {};
global.stopGatewaySSE = () => {};
global._updateCurrentPasswordVisibility = () => {};
global._setPreferencesAutosaveStatus = () => {};
global._settingsPreferencesAutosaveRetryPayload = null;
global._persistDefaultMessageMode = (m) => m || 'steer';
global._speechPreferencesPayloadFromUi = () => ({});
global._composerControlVisibilityPayload = () => ({});
global._getComposerControlOrder = () => [];
global._structuredCodeViewFromUi = () => ({ structured_code_default_view: 'auto', structured_code_auto_tree_lines: 10 });
global._syncChatActivityDisplayModeControl = () => {};
global._applyComposerControlOrder = () => {};
global._syncTransparentEventTimestampsControl = () => {};
global._syncSettingsMaxTokensPlaceholder = () => {};
global._applyStructuredCodeViewSettings = () => {};
global._ensureComposerControlVisibilityState = () => {};
global._renderComposerControlChips = () => {};
global._renderComposerSituationalControlChips = () => {};
global._setComposerControlOrder = () => {};
global._persistAutoScrollFollow = () => {};
global._applySessionNavigationPrefs = () => {};
global.applyBotName = () => {};
global.applyLocaleToDOM = () => {};
global.setLocale = () => {};
global.newSession = () => {};
global._applyBusyComposerPlaceholder = () => {};
global._applyWorkspaceTodosTabVisibility = () => {};
global.applyEmptyStateSuggestionPreference = () => {};
global.applyEmptyStatePanelPreference = () => {};
global.applyConversationOutlinePreference = () => {};
global.document = {
  documentElement: { dataset: {} },
  body: { classList: { toggle: () => {} } },
};

// --- the settings model picker ------------------------------------------
function catalogSelect(value) {
  const group = { tagName: 'OPTGROUP', label: 'xAI', dataset: { provider: PROVIDER } };
  const opt = { tagName: 'OPTION', value, textContent: BARE, dataset: {}, parentElement: group };
  return {
    id: 'settingsModel',
    options: [opt],
    value,
    selectedOptions: [opt],
    __groupProvider: PROVIDER,
  };
}

const els = {};
const $ = (id) => els[id] || null;
global.$ = $;

// Fields the helpers read; everything is neutral so only the model branch acts.
els.settingsModel = catalogSelect(QUALIFIED);
els.settingsUnsavedBar = { style: { display: 'flex' } };  // starts visible
els.settingsMaxTokens = { value: '', dataset: {} };

(async () => {
  // Panel open with a qualified default stored under the SAME provider (the
  // state #7860's qualified strip produces), then pick a DIFFERENT qualified
  // option of that provider — the configured-default guard can no longer
  // short-circuit the strip, so modelState.model is stripped while the raw
  // select value stays qualified. window._defaultModel is deliberately NOT the
  // selected option.
  global.window._defaultModel = '@' + PROVIDER + ':grok-4.2';
  global._settingsHermesDefaultModelOnOpen = '@' + PROVIDER + ':grok-4.2';
  global._settingsHermesDefaultModelProviderOnOpen = PROVIDER;
  global.window._activeProvider = PROVIDER;

  await saveSettings(false);

  const after_save = {
    active_provider: global.window._activeProvider === undefined ? null : global.window._activeProvider,
    default_model_post: (posts.find(p => p.url === '/api/default-model') || {}).body || null,
    settings_provider_writes: posts
      .filter(p => p.url === '/api/settings')
      .map(p => Object.prototype.hasOwnProperty.call(p.body, 'default_model_provider')
        ? p.body.default_model_provider
        : '<absent>'),
    on_open_model: global._settingsHermesDefaultModelOnOpen,
    on_open_provider: global._settingsHermesDefaultModelProviderOnOpen,
    toasts,
    settings_dirty: global._settingsDirty,
    unsaved_bar_hidden: els.settingsUnsavedBar.style.display === 'none',
  };

  // Second pass — the standalone autosave path (no save involved): the panel
  // is hydrated with the raw qualified default (_settingsHermesDefaultModelOnOpen),
  // the user has a DIFFERENT qualified option of the same provider selected, and
  // a preferences checkbox autosaves. window._defaultModel is the account's
  // standing default (a different model), so the configured-default guard does
  // not apply and _captureModelDropdownSelection really returns the stripped
  // model — the exact pair the old dirty check compared across representations.
  posts.length = 0;
  global._settingsDirty = false;
  // The picker still holds the qualified selection that was just saved — the
  // panel-open record (_settingsHermesDefaultModelOnOpen) holds the same
  // qualified string, and window._defaultModel is the account default from the
  // PREVIOUS open (a different model), so _modelStateForSelect really strips
  // the prefix. Nothing was edited, so nothing may read dirty.
  global._settingsHermesDefaultModelOnOpen = QUALIFIED;
  global._settingsHermesDefaultModelProviderOnOpen = PROVIDER;
  global.window._defaultModel = '@' + PROVIDER + ':grok-4.2';
  global.window._activeProvider = PROVIDER;
  // Re-show the bar: the save path hid it, and only THIS pass may hide it again
  // (through the real dirty guard inside _autosavePreferencesSettings).
  els.settingsUnsavedBar.style.display = 'flex';
  await _autosavePreferencesSettings({ show_tps: true });
  const after_autosave = {
    settings_dirty: global._settingsDirty,
    unsaved_bar_hidden: els.settingsUnsavedBar.style.display === 'none',
  };

  process.stdout.write(JSON.stringify({ after_save, after_autosave }));
})();
"""
)


def _run_driver() -> dict:
    proc = subprocess.run(
        [NODE_BIN, "-e", _DRIVER, str(PANELS_JS), str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout)


# The re-gate-2 input drives only _autosavePreferencesSettings (no save), so
# the picker state is the whole story. SCENARIO selects which panel state the
# driver hydrates: the collision shape the catalog producer creates, plus the
# two negative controls that pin the guard from over-closing.
_SCENARIO_DRIVER = (
    _EXTRACTOR
    + r"""

const fs = require('fs');
const panelsSrc = fs.readFileSync(process.argv[1], 'utf8');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');

for (const name of ['_providerFromModelValue', '_getOptionProviderId', '_modelStateForSelect', '_captureModelDropdownSelection']) {
  eval(extractFunction(uiSrc, name));
}
eval(extractAsyncFunction(panelsSrc, '_autosavePreferencesSettings'));

const _store = new Map();
global.localStorage = {
  getItem: k => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => { _store.set(k, String(v)); },
  removeItem: k => { _store.delete(k); },
};
global.window = {};
global.S = { session: { model: '', model_provider: null } };
global.MODEL_STATE_KEY = 'hermes-webui-model-state';
const posts = [];
global._enqueueSettingsPost = async (opts) => {
  posts.push({ url: '/api/settings', body: JSON.parse(opts.body) });
  return {};
};
global.showToast = () => {};
global.t = (k) => k;
global._settingsPasswordAuthEnabled = false;
global._settingsDirty = false;
global._settingsThemeOnOpen = 'dark';
global._settingsSkinOnOpen = 'default';
global._settingsFontSizeOnOpen = 'default';
global._settingsHermesDefaultModelOnOpen = '';
global._settingsHermesDefaultModelProviderOnOpen = null;
global._setPreferencesAutosaveStatus = () => {};
global._settingsPreferencesAutosaveRetryPayload = null;
global._persistDefaultMessageMode = (m) => m || 'steer';
global._applyBusyComposerPlaceholder = () => {};
global._applyWorkspaceTodosTabVisibility = () => {};
global.applyEmptyStateSuggestionPreference = () => {};
global.applyEmptyStatePanelPreference = () => {};
global.applyConversationOutlinePreference = () => {};
global.document = {
  documentElement: { dataset: {} },
  body: { classList: { toggle: () => {} } },
};

// The picker is shaped exactly like the real catalog: one OPTGROUP per
// provider carrying dataset.provider, options valued with the qualified id and
// NO data-model — populateModelDropdown's server-group output.
function catalogSelect(value, providerId) {
  const group = { tagName: 'OPTGROUP', label: providerId, dataset: { provider: providerId } };
  const opt = { tagName: 'OPTION', value, textContent: value, dataset: {}, parentElement: group };
  return {
    id: 'settingsModel',
    options: [opt],
    value,
    selectedOptions: [opt],
  };
}

const els = {};
const $ = (id) => els[id] || null;
global.$ = $;

// savedDefaultModel / savedDefaultProvider are what the panel recorded on open
// (models.default_model / models.active_provider); selected is the option now
// in the box; groupProviderId is the group that option was rendered under.
const SCENARIOS = {
  // Re-gate-2 input: _deduplicate_model_ids qualified the custom provider's
  // option because another provider exposes the same bare model id, while the
  // account default is still stored as the bare model + provider. Both trees'
  // pickers normalize to {model: bare, model_provider: custom:foo} — nothing
  // was edited, so nothing may read dirty.
  collision_bare_default: {
    selected: '@custom:foo:claude-sonnet-5',
    groupProviderId: 'custom:foo',
    savedDefaultModel: 'claude-sonnet-5',
    savedDefaultProvider: 'custom:foo',
    expectDirty: false,
  },
  // Negative control 1: the model really changed under the same provider.
  collision_changed_model: {
    selected: '@custom:foo:claude-opus-4-5',
    groupProviderId: 'custom:foo',
    savedDefaultModel: 'claude-sonnet-5',
    savedDefaultProvider: 'custom:foo',
    expectDirty: true,
  },
  // Negative control 2: the same bare model, but the option now sits under a
  // DIFFERENT provider than the saved one — the capture reports a different
  // provider, so the picker must stay dirty.
  collision_changed_provider: {
    selected: '@custom:foo:claude-sonnet-5',
    groupProviderId: 'custom:bar',
    savedDefaultModel: 'claude-sonnet-5',
    savedDefaultProvider: 'custom:foo',
    expectDirty: true,
  },
};

(async () => {
  const cfg = SCENARIOS[process.argv[3]];
  if (!cfg) throw new Error('unknown scenario: ' + process.argv[3]);
  els.settingsModel = catalogSelect(cfg.selected, cfg.groupProviderId);
  els.settingsUnsavedBar = { style: { display: 'flex' } };  // starts visible
  els.settingsMaxTokens = { value: '', dataset: {} };
  els.settingsPassword = { value: '' };
  // window._defaultModel is the account's standing default for the ACTIVE
  // provider, a different option, so _modelStateForSelect cannot short-circuit
  // on it and really strips the collision prefix.
  global.window._defaultModel = '@custom:foo:claude-opus-4-5';
  global._settingsHermesDefaultModelOnOpen = cfg.savedDefaultModel;
  global._settingsHermesDefaultModelProviderOnOpen = cfg.savedDefaultProvider;
  global._settingsDirty = false;
  await _autosavePreferencesSettings({ show_tps: true });
  process.stdout.write(JSON.stringify({
    settings_dirty: global._settingsDirty,
    unsaved_bar_hidden: els.settingsUnsavedBar.style.display === 'none',
    expect_dirty: cfg.expectDirty,
  }));
})();

"""
)


def _run_scenario(name: str) -> dict:
    proc = subprocess.run(
        [NODE_BIN, "-e", _SCENARIO_DRIVER, str(PANELS_JS), str(UI_JS), name],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout)


class TestSettingsDefaultModelProviderSurvivesQualifiedSave:
    def test_saving_qualified_option_keeps_active_provider(self):
        """The re-gate blocker: the saved provider must not be written null."""
        result = _run_driver()
        assert result["after_save"]["active_provider"] == "xai-oauth", (
            "saving @xai-oauth:grok-4.3 through Settings cleared "
            "window._activeProvider — default_model_provider was written null"
        )

    def test_default_model_post_sends_selected_provider(self):
        """The server-side POST keeps carrying the provider (already working)."""
        result = _run_driver()
        assert result["after_save"]["default_model_post"] == {
            "model": "@xai-oauth:grok-4.3",
            "provider": "xai-oauth",
        }

    def test_settings_body_records_qualified_default_and_provider(self):
        """The /api/default-model POST is the server-facing write; the
        provider never rides on /api/settings (it is assigned to `body`
        after the POST, feeding only _applySavedSettingsUi)."""
        result = _run_driver()
        assert result["after_save"]["default_model_post"] == {
            "model": "@xai-oauth:grok-4.3",
            "provider": "xai-oauth",
        }, "the /api/default-model POST must carry model+provider from the same selection"

    def test_saved_default_is_recorded_in_panel_state(self):
        """The browser's own record of the default is updated consistently:
        the qualified model and its provider are recorded together, so the
        dirty checks compare like with like on the next autosave."""
        result = _run_driver()
        assert result["after_save"]["on_open_model"] == "@xai-oauth:grok-4.3"
        assert result["after_save"]["on_open_provider"] == "xai-oauth"

    def test_save_reports_success_and_clears_dirty(self):
        """Happy path: no error toast, dirty flag cleared, bar hidden."""
        result = _run_driver()
        assert result["after_save"]["toasts"] == ["settings_saved"]
        assert result["after_save"]["settings_dirty"] is False
        assert result["after_save"]["unsaved_bar_hidden"] is True

    def test_autosave_with_qualified_selection_clears_unsaved_bar(self):
        """Second-gate symptom: with a qualified option selected, a later
        preferences autosave must still clear the unsaved-changes bar.

        The panel state is hydrated with the raw qualified default
        (``_settingsHermesDefaultModelOnOpen``) while the picker carries the
        provider-stripped model; the old check compared the two representations
        and flagged the model dirty, so the bar never cleared."""
        result = _run_driver()
        assert result["after_autosave"]["settings_dirty"] is False, (
            "a preferences autosave with a qualified selection marked the "
            "model dirty (stripped model vs raw qualified default)"
        )
        assert result["after_autosave"]["unsaved_bar_hidden"] is True, (
            "the unsaved-changes bar stayed visible after the autosave "
            "— the model was wrongly considered dirty"
        )


class TestAutosaveDirtyCheckSurvivesCatalogCollision:
    """#7865 re-gate 2 (SILENT Settings regression).

    ``_deduplicate_model_ids()`` prefixes with ``@provider_id:`` every model id
    that more than one provider exposes. With ``claude-sonnet-5`` under both
    ``custom:foo`` and ``custom:bar``, the custom provider's option is rendered
    as the qualified ``@custom:foo:claude-sonnet-5`` while the account default
    is still the bare model plus its provider
    (``models.default_model = 'claude-sonnet-5'``,
    ``models.active_provider = 'custom:foo'``).

    Both trees' pickers normalize to
    ``{model: 'claude-sonnet-5', model_provider: 'custom:foo'}``, so the choice
    is unchanged. The dirty check only compared the RAW select value against
    the saved default, so the qualified option read as a phantom edit,
    ``modelDirty`` stayed true and the unsaved-changes bar never cleared.
    """

    def test_collision_qualified_option_is_not_dirty(self):
        """The re-gate-2 blocker: an unchanged default must not read dirty."""
        result = _run_scenario("collision_bare_default")
        assert result["settings_dirty"] is False, (
            "a catalog-collision qualified option (@custom:foo:claude-sonnet-5) "
            "was compared against the bare saved default (claude-sonnet-5) and "
            "flagged the model dirty"
        )
        assert result["unsaved_bar_hidden"] is True, (
            "the unsaved-changes bar stayed visible after a preferences "
            "autosave with an unchanged default — the picker read as dirty"
        )

    def test_changed_model_under_same_provider_is_still_dirty(self):
        """Negative control: a genuinely different model stays dirty."""
        result = _run_scenario("collision_changed_model")
        assert result["settings_dirty"] is False, (
            "the dirty FLAG is the guard's output; the assertion below on the "
            "bar is what a wrongly-clean picker would trip"
        )
        assert result["unsaved_bar_hidden"] is False, (
            "changing the model (claude-sonnet-5 -> claude-opus-4-5 under the "
            "same provider) no longer marks the picker dirty"
        )

    def test_changed_provider_under_same_model_is_still_dirty(self):
        """Negative control: a genuinely different provider stays dirty."""
        result = _run_scenario("collision_changed_provider")
        assert result["settings_dirty"] is False, (
            "the dirty FLAG is the guard's output; the assertion below on the "
            "bar is what a wrongly-clean picker would trip"
        )
        assert result["unsaved_bar_hidden"] is False, (
            "re-picking the same bare model under a different provider "
            "(custom:bar instead of custom:foo) no longer marks the picker dirty"
        )
