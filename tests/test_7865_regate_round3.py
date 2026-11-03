"""#7865 re-gate round 3 — the two findings both reviewers raised.

Both are in the same area as the fixes that just landed, and both were found by
driving the real picker / persistence / autosave functions rather than by reading
them.

## [CORE] A session whose provider was removed stops recovering

``static/ui.js`` stripped the leading ``@<provider>:`` prefix for ANY routed
provider, on the reasoning that the prefix was then a duplication of
``model_provider``. It is not: for a non-custom provider the qualified form IS
the session's model. Stripping it persisted the pair ``mistral-large`` /
``removed`` — a model id no provider owns, plus a provider the account no longer
has. The server fast path (``api/routes.py:7981``) accepts that pair unchanged,
and the installed Agent then raises ``AuthError: Unknown provider 'removed'`` on
the next send.

Only a CUSTOM provider's qualified id is a genuine duplication: the custom
namespace encodes the provider inside the model id, so keeping both repeats it
and the upstream answers 404 Model-not-found. The strip is now limited to that
case, and non-custom qualifiers stay in session state.

## [SHOULD-FIX] Save, then re-open Settings: the phantom unsaved bar returns

The re-gate-2 fix made ``_modelStateForSelect`` skip the strip for a configured
default. That is correct for the picker, but it changed what the autosave dirty
check sees: after "Save Settings" the server stores the bare model while the
default-model global still holds the raw qualified value the picker rendered.
Re-opening Settings therefore shows the qualified option, captured and raw are
both ``@custom:foo:claude-sonnet-5``, saved-on-open is bare ``claude-sonnet-5`` —
and neither of the two comparisons matched, so the bar reappeared on the next
autosaved edit.

``_autosavePreferencesSettings`` now accepts a third form: the raw value with the
captured provider's own ``@<provider>:`` prefix removed. It stays anchored on the
captured provider, so a same-model/different-provider re-pick keeps reading
dirty.

These tests drive the real functions from ``static/ui.js`` and
``static/panels.js`` under node, using the same lexical-extraction pattern as
``tests/test_7865_settings_default_provider_survives_qualified_save.py``.
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
    if (source[i] === '\\\\') { i += 1; continue; }
    if (source[i] === quote) return i;
  }
  throw new Error('unterminated string literal');
}
function readBalanced(source, start) {
  const open = source[start];
  const close = open === '(' ? ')' : open === '{' ? '}' : ']';
  let depth = 0;
  for (let i = start; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '"' || ch === "'" || ch === '`') { i = skipQuotedLiteral(source, i, ch); continue; }
    if (ch === '/' && canStartRegexLiteral(source, i)) {
      let j = i + 1, inClass = false;
      while (j < source.length) {
        if (source[j] === '\\\\') { j += 2; continue; }
        if (source[j] === '[') inClass = true;
        else if (source[j] === ']') inClass = false;
        else if (source[j] === '/' && !inClass) break;
        j += 1;
      }
      i = j;
      continue;
    }
    if (ch === open) depth += 1;
    else if (ch === close) {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced ' + open);
}
function extractDecl(source, keyword, name) {
  const patterns = [keyword + ' ' + name + '(', keyword + ' ' + name + ' =', keyword + ' ' + name + ':'];
  for (const p of patterns) {
    const idx = source.indexOf(p);
    if (idx < 0) continue;
    const paren = source.indexOf('(', idx);
    if (paren < 0) continue;
    const args = readBalanced(source, paren);
    let i = paren + args.length;
    while (i < source.length && /\s/.test(source[i])) i += 1;
    if (source[i] !== '{') return source.slice(idx, i) + ';';
    return source.slice(idx, i) + readBalanced(source, i);
  }
  throw new Error('declaration not found: ' + name);
}
function extractFunction(src, name) {
  return extractDecl(src, 'function', name) || extractDecl(src, 'async function', name);
}
function extractAsyncFunction(src, name) {
  return extractDecl(src, 'async function', name) || extractDecl(src, 'function', name);
}
"""


# ── CORE: a non-custom qualified id keeps its prefix in session state ────────

_CORE_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

// _modelStateForSelect is the function under test. It is extracted with the
// same lexical reader the sibling suite uses, so it runs VERBATIM.
""" + _EXTRACTOR + r"""

// ── the DOM/option shape _modelStateForSelect reads ────────────────────────
function optionEl(value, provider, model) {
  const group = { tagName: 'OPTGROUP', label: provider, dataset: { provider } };
  return {
    tagName: 'OPTION',
    value,
    textContent: model || value,
    dataset: { model: model || '', provider },
    parentElement: group,
  };
}

function makeSelect(value, provider, model) {
  const opt = optionEl(value, provider, model);
  return {
    id: 'model',
    options: [opt],
    value,
    selectedOptions: [opt],
  };
}

// _getOptionProviderId reads the option's authoritative data-provider.
global._getOptionProviderId = (opt) => {
  if (!opt) return '';
  const ds = opt.dataset || {};
  if (ds.provider) return String(ds.provider);
  const parent = opt.parentElement;
  if (parent && parent.dataset && parent.dataset.provider) {
    return String(parent.dataset.provider);
  }
  return '';
};

// The functions under test, extracted VERBATIM from static/ui.js.
for (const name of ['_providerFromModelValue', '_getOptionProviderId', '_modelStateForSelect']) {
  eval(extractFunction(uiSrc, name));
}

global.window = { _defaultModel: '' };
global.document = { createElement: () => ({ style: {} }) };

const cases = [
  // A provider the account no longer has: the qualified form must SURVIVE.
  {
    label: 'removed_provider_qualified',
    value: '@removed:mistral-large',
    provider: 'removed',
    model: '',
    expectModel: '@removed:mistral-large',
    expectProvider: 'removed',
  },
  // A native provider's qualified id: also must survive (#7860's real case).
  {
    label: 'native_provider_qualified',
    value: '@xai-oauth:grok-4.3',
    provider: 'xai-oauth',
    model: '',
    expectModel: '@xai-oauth:grok-4.3',
    expectProvider: 'xai-oauth',
  },
  // A real provider namespace: preserved (#1771).
  {
    label: 'namespace_qualified',
    value: '@safe:gpt-4o-mini',
    provider: 'safe',
    model: '',
    expectModel: '@safe:gpt-4o-mini',
    expectProvider: 'safe',
  },
  // A CUSTOM provider's qualified id IS a duplication and must still strip.
  {
    label: 'custom_provider_qualified',
    value: '@custom:foo:claude-sonnet-5',
    provider: 'custom:foo',
    model: '',
    expectModel: 'claude-sonnet-5',
    expectProvider: 'custom:foo',
  },
  // A bare model under any provider: untouched.
  {
    label: 'bare_model',
    value: 'grok-4.3',
    provider: 'xai-oauth',
    model: 'grok-4.3',
    expectModel: 'grok-4.3',
    expectProvider: 'xai-oauth',
  },
];

const out = [];
for (const c of cases) {
  global.window._defaultModel = '';  // never the configured default here
  const sel = makeSelect(c.value, c.provider, c.model);
  // The second argument is the model id under test; the select only supplies
  // the option metadata (provider, data-model).
  const state = _modelStateForSelect(sel, c.value);
  out.push({
    label: c.label,
    model: state.model,
    model_provider: state.model_provider,
    expectModel: c.expectModel,
    expectProvider: c.expectProvider,
  });
}
process.stdout.write(JSON.stringify(out));
"""


def _run_core() -> list[dict]:
    proc = subprocess.run(
        [NODE_BIN, "-e", _CORE_DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[:3000]
    return json.loads(proc.stdout)


class TestNonCustomQualifierSurvivesInSessionState:
    """The CORE finding: only a custom provider's prefix is a duplication."""

    def test_a_removed_providers_qualified_id_is_not_stripped(self):
        rows = {r["label"]: r for r in _run_core()}
        row = rows["removed_provider_qualified"]
        assert row["model"] == "@removed:mistral-large", (
            "a non-custom qualified id was stripped to the bare model, which "
            f"persists the pair {row['model']!r} / {row['model_provider']!r} — "
            "the installed Agent then raises AuthError: Unknown provider on the "
            "next send, so the session stops recovering"
        )
        assert row["model_provider"] == "removed"

    def test_a_native_providers_qualified_id_is_not_stripped(self):
        rows = {r["label"]: r for r in _run_core()}
        row = rows["native_provider_qualified"]
        assert row["model"] == "@xai-oauth:grok-4.3", (
            "#7860's real case regressed: a native provider's qualified id must "
            "stay in session state"
        )

    def test_a_provider_namespace_is_preserved(self):
        rows = {r["label"]: r for r in _run_core()}
        row = rows["namespace_qualified"]
        assert row["model"] == "@safe:gpt-4o-mini", (
            "a real provider namespace (#1771) was stripped, which would "
            "silently re-route the selection to the group's provider"
        )

    def test_a_custom_providers_qualified_id_is_still_stripped(self):
        """Negative control: the strip the PR exists to perform still happens."""
        rows = {r["label"]: r for r in _run_core()}
        row = rows["custom_provider_qualified"]
        assert row["model"] == "claude-sonnet-5", (
            "a custom provider's qualified id is no longer stripped, so the "
            "provider is persisted twice and the upstream answers 404 "
            "Model-not-found (#6884)"
        )
        assert row["model_provider"] == "custom:foo"

    def test_a_bare_model_is_untouched(self):
        rows = {r["label"]: r for r in _run_core()}
        row = rows["bare_model"]
        assert row["model"] == "grok-4.3"
        assert row["model_provider"] == "xai-oauth"


# ── SHOULD-FIX: Save → re-open → autosave keeps the bar clear ───────────────

_AUTOSAVE_DRIVER = r"""
const fs = require('fs');
const panelsSrc = fs.readFileSync(process.argv[1], 'utf8');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
""" + _EXTRACTOR + r"""
// ── module globals the extracted helpers touch ─────────────────────────────
const posts = [];
global.api = async (url, opts) => {
  posts.push({ url, body: (opts && opts.body) || null });
  return { ok: true, json: async () => ({}) };
};
global.showToast = () => {};
global.t = (k) => k;
global.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};
global.document = {
  documentElement: { dataset: {} },
  body: { classList: { toggle: () => {} } },
  createElement: () => ({ style: {} }),
};
global.window = { _defaultModel: '' };
global.S = { activeProfile: 'default', session_id: null, messages: [] };
global.renderMessages = () => {};
global.syncTopbar = () => {};
global.renderSessionList = () => {};
global.clearMessageRenderCache = () => {};
global.applyLocaleToDOM = () => {};
global.setLocale = () => {};
global.applyBotName = () => {};
global.newSession = () => {};
global._hideSettingsPanel = () => {};
global._resetSettingsPanelState = () => {};
global._setSettingsAuthButtonsVisible = () => {};
global._applyComposerFooterVisibilitySettings = () => {};
global._startSettingsSaveButtonWatcher = () => {};
global._setPreferencesAutosaveStatus = () => {};
global.startGatewaySSE = () => {};
global.stopGatewaySSE = () => {};
global._updateCurrentPasswordVisibility = () => {};
global._settingsPasswordAuthEnabled = false;
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
global._applyBusyComposerPlaceholder = () => {};
global._applyWorkspaceTodosTabVisibility = () => {};
global.applyEmptyStateSuggestionPreference = () => {};
global.applyEmptyStatePanelPreference = () => {};
global.applyConversationOutlinePreference = () => {};
global._enqueueSettingsPost = async (url, body) => { posts.push({ url, body }); return true; };
global._settingsDirty = false;
global._settingsThemeOnOpen = 'dark';
global._settingsSkinOnOpen = 'default';
global._settingsFontSizeOnOpen = 'default';
global._settingsHermesDefaultModelOnOpen = '';
global._settingsHermesDefaultModelProviderOnOpen = null;
global._pendingSettingsTargetPanel = null;

const els = {};
const $ = (id) => els[id] || null;
global.$ = $;

function catalogSelect(value, provider) {
  const group = { tagName: 'OPTGROUP', label: provider, dataset: { provider } };
  const opt = {
    tagName: 'OPTION', value, textContent: value,
    dataset: { provider }, parentElement: group,
  };
  return { id: 'settingsModel', options: [opt], value, selectedOptions: [opt] };
}

// The functions under test, extracted VERBATIM from both sources.
for (const name of ['_providerFromModelValue', '_getOptionProviderId', '_modelStateForSelect', '_captureModelDropdownSelection']) {
  eval(extractFunction(uiSrc, name));
}
eval(extractFunction(panelsSrc, '_applySavedSettingsUi'));
eval(extractAsyncFunction(panelsSrc, '_autosavePreferencesSettings'));

// ── the sequence the reviewer described ───────────────────────────────────
// 1. collision catalog + the default saved BARE: opening Settings re-selects
//    the qualified option.
// 2. "Save Settings" POSTs the qualified id; the server stores the BARE form
//    while panels.js writes the raw qualified value into the default-model
//    global.
// 3. close, re-open, toggle any autosaved preference -> the bar must stay clear.
(async () => {
  const QUALIFIED = '@custom:foo:claude-sonnet-5';
  const BARE = 'claude-sonnet-5';
  const PROVIDER = 'custom:foo';

  // Step 1: open with the bare default stored, picker shows the qualified one.
  global.window._defaultModel = '@custom:foo:claude-opus-4-5';
  global._settingsHermesDefaultModelOnOpen = BARE;
  global._settingsHermesDefaultModelProviderOnOpen = PROVIDER;
  els.settingsModel = catalogSelect(QUALIFIED, PROVIDER);
  els.settingsUnsavedBar = { style: { display: 'flex' } };
  els.settingsMaxTokens = { value: '', dataset: {} };

  await global._autosavePreferencesSettings({ show_tps: true });
  const after_first_autosave = {
    dirty: global._settingsDirty,
    bar_hidden: els.settingsUnsavedBar.style.display === 'none',
  };

  // Step 2: "Save Settings". The server echoes the bare form back, and
  // _applySavedSettingsUi writes the RAW qualified value into the global.
  global._settingsHermesDefaultModelOnOpen = BARE;      // what the server stored
  global.window._defaultModel = QUALIFIED;              // what the panel wrote

  // Step 3: re-open and autosave again — the phantom-bar case.
  els.settingsUnsavedBar = { style: { display: 'flex' } };
  await global._autosavePreferencesSettings({ show_tps: true });
  const after_reopen = {
    dirty: global._settingsDirty,
    bar_hidden: els.settingsUnsavedBar.style.display === 'none',
  };

  process.stdout.write(JSON.stringify({
    after_first_autosave,
    after_reopen,
    qualified: QUALIFIED,
    bare: BARE,
  }));
})();
"""


def _run_autosave() -> dict:
    proc = subprocess.run(
        [NODE_BIN, "-e", _AUTOSAVE_DRIVER, str(PANELS_JS), str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[:3000]
    return json.loads(proc.stdout)


class TestSaveReopenAutosaveKeepsTheBarClear:
    """The SHOULD-FIX finding: the phantom unsaved bar after Save → re-open."""

    def test_the_bar_stays_clear_across_save_and_reopen(self):
        result = _run_autosave()
        assert result["after_first_autosave"]["bar_hidden"] is True, (
            "the FIRST autosave with an unchanged default left the "
            "unsaved-changes bar visible"
        )
        assert result["after_reopen"]["bar_hidden"] is True, (
            "after Save → re-open → autosave the phantom unsaved bar returned: "
            f"captured and raw are both {result['qualified']!r} while "
            f"saved-on-open is the bare {result['bare']!r}, and neither "
            "comparison matched"
        )

    def test_the_dirty_flag_is_not_set_by_the_sequence(self):
        result = _run_autosave()
        assert result["after_reopen"]["dirty"] is False, (
            "an unchanged default marked the settings dirty after re-opening "
            "the panel"
        )


# ── source-level guard: the third comparison form exists ───────────────────


def test_the_autosave_accepts_the_own_prefix_stripped_form():
    """The fix is a third accepted form, not a widened first one.

    Accepting the raw value unconditionally would stop a genuinely changed model
    from reading dirty, so the third form must be anchored on the captured
    provider's own prefix.
    """
    src = PANELS_JS.read_text(encoding="utf-8")
    idx = src.find("const modelUnchanged=")
    assert idx > 0, "the autosave dirty check's modelUnchanged is gone"
    window = src[idx : idx + 900]
    assert "prefixStrippedValue" in window, (
        "the autosave dirty check does not accept the raw value with the "
        "captured provider's own prefix removed, so Save → re-open → autosave "
        "still shows the phantom unsaved bar (#7865 SHOULD-FIX)"
    )
    # The stripped form must be derived from the CAPTURED provider, not from a
    # hardcoded or last-colon parse.
    assert "model_provider" in src[max(0, idx - 900) : idx], (
        "the prefix is not taken from the captured provider, so a "
        "same-model/different-provider re-pick would stop reading dirty"
    )
