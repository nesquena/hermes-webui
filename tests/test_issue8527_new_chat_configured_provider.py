"""#8527 — the new-chat configured default must keep its configured provider.

Reproduction (nesquena/hermes-webui#8527): with a named provider in
``config.yaml`` → ``providers:`` pointing at ``https://openrouter.ai/api/v1``
under its own ``key_env`` (and ``OPENROUTER_API_KEY`` also set for the
built-in video_gen plugin), every new WebUI chat is created with
``model_provider: openrouter`` and the turn is billed to the built-in key
instead of the named provider's key.

Maintainer diagnosis (nesquena-hermes 2026-10-26): the configured-default
branch of the browser new-chat path keeps only the model —
``newModelState={model:window._defaultModel,model_provider:null}`` — although
``populateModelDropdown`` stores ``_activeProvider`` alongside
``_defaultModel`` (both come from the ``/api/models`` payload). The later
fallback is restricted to models without a ``/`` or ``@`` qualifier, so a
slash-qualified configured default (``anthropic/claude-sonnet-5.5``) leaves
this path with no provider pinned, and the server falls back to the
built-in route. Prescription: preserve the configured model/provider pair
when the configured-default branch wins, while retaining the guards against
attaching the active provider to a stale model from another session; do not
treat sharing an endpoint as sharing provider identity or credentials.

This drives the REAL ``newSession`` from ``static/sessions.js`` under node
(same lexical-extraction pattern as
``tests/test_7865_settings_default_provider_survives_qualified_save.py``):
the function is extracted verbatim, its module globals are supplied as
mocks, and the POST ``/api/session/new`` payload is captured and asserted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
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


def _run_driver(scenario: dict) -> dict:
    """Extract the real newSession and drive one scenario; return the captured payload."""
    driver = (
        _EXTRACTOR
        + r"""
const fs = require('fs');
// node -e puts the first user argument at process.argv[1].
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = JSON.parse(process.argv[2]);

// ---- module globals newSession touches (mocks; inference/storage only) ----
const captured = {urls: [], bodies: []};
const noop = () => {};
const promiseNoop = () => Promise.resolve();

const S = Object.assign({
  activeProfile: 'default',
  session: null,
  messages: [],
  toolCalls: [],
  _profileSwitchWorkspace: null,
  _profileDefaultWorkspace: null,
  _pendingSessionToolsets: null,
}, scenario.S || {});

let _modelSelect = scenario.modelSelect || null;

let ctx = {
  S,
  $: (id) => (id === 'modelSelect' ? _modelSelect : null),
  api: (url, opts) => {
    captured.urls.push(url);
    let body = null;
    try { body = JSON.parse((opts && opts.body) || 'null'); } catch (_) { body = null; }
    captured.bodies.push(body);
    return Promise.resolve({session: {session_id: 'new-sid', messages: []}});
  },
  window: {
    _defaultModel: scenario.defaultModel,
    _activeProvider: scenario.activeProvider,
    _clearPendingSelections: noop,
  },
  showToast: noop,
  updateQueueBadge: noop,
  clearLiveToolCards: noop,
  syncTopbar: noop,
  renderMessages: noop,
  loadDir: () => ({catch: noop}),
  refreshSessionList: promiseNoop,
  _allProjects: [],
  _activeProject: scenario.activeProject || null,
  NO_PROJECT_FILTER: '__no_project__',
  _newSessionInFlight: null,
  _newSessionPendingText: () => '',
  _setNewSessionPending: noop,
  _messagesTruncated: false,
  _oldestIdx: 0,
  _projectBindingsForNewSession: () => ({}),
  _profileMatchesActiveProfile: () => true,
  _readEmptyComposerModelOverride: () => scenario.explicitOverride || null,
  _modelStateForSelect: (sel, value) => {
    const opt = (sel && sel.options || []).find((o) => o.value === value);
    const provider = opt ? opt['data-provider'] : null;
    return {model: value, model_provider: provider || null};
  },
  _readPersistedModelState: () => null,
  _clearExplicitPickerPick: noop,
  _clearEmptyComposerModelOverride: noop,
  _adoptRegenerationRevision: noop,
  _rememberNewChatDraftSession: noop,
  _setActiveSessionUrl: noop,
  _setLiveAssistantTps: noop,
  _setSessionViewedCount: noop,
  _syncCtxIndicator: noop,
  _applyModelToDropdown: noop,
  _applyReasoningChip: noop,
  _ensureModelOptionInDropdown: noop,
  _hydrateTodosFromSession: noop,
  _announceNewSessionWorkspace: noop,
  _deferWorkspaceRefreshForSession: noop,
  _retireProjectPickerForExplicitRepaint: noop,
  _loadSessionGeneration: 0,
  _sessionSourceFilter: null,
  // JS built-ins must pass through the Proxy below verbatim: the proxy
  // becomes the VM global, and returning a no-op for `Object`/`JSON`/...
  // would break every extracted expression that uses them.
  Object, JSON, Promise, Array, String, Number, Boolean, Math, RegExp, Date,
  Error, TypeError, console, setTimeout, isNaN, parseInt, parseFloat,
};

// Proxy fallback: any module-level helper the extraction references but this
// driver did not enumerate resolves to a safe no-op instead of crashing the
// run. Explicitly defined bindings (S, $, api, window, data flags) pass through.
const capturedCtx = ctx;
ctx = new Proxy(capturedCtx, {
  get(target, prop) {
    if (prop in target) return target[prop];
    return noop;
  },
  has() { return true; },
});

const fnSrc = extractAsyncFunction(src, 'newSession');
const vm = require('vm');
const context = vm.createContext(ctx);
vm.runInContext(fnSrc, context);

context.newSession(false, {}).then(() => {
  const body = captured.bodies[0] || {};
  process.stdout.write(JSON.stringify({ok: true, body: body}));
}).catch((err) => {
  process.stdout.write(JSON.stringify({ok: false, error: String(err && err.stack || err)}));
});
"""
    )
    proc = subprocess.run(
        [NODE_BIN, "-e", driver, str(SESSIONS_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise AssertionError(f"driver produced no output; stderr={proc.stderr[:2000]}")
    return json.loads(out.splitlines()[-1])


# The reproduction config: configured default is a slash-qualified model served
# by a NAMED provider that shares the openrouter endpoint with the built-in one.
_QUALIFIED_DEFAULT = "anthropic/claude-sonnet-5.5"
_NAMED_PROVIDER = "or-general"


def test_configured_default_keeps_its_configured_provider():
    """Fresh boot, no pick: the configured pair must reach /api/session/new.

    RED on master: the branch pins ``model_provider:null`` and the later
    fallback is restricted to unqualified models, so the payload carries no
    provider and the server falls back to the built-in openrouter route
    (billing the shared key).
    """
    result = _run_driver(
        {
            "defaultModel": _QUALIFIED_DEFAULT,
            "activeProvider": _NAMED_PROVIDER,
            "S": {"session": None},
        }
    )
    assert result["ok"], result.get("error")
    body = result["body"]
    assert body.get("model") == _QUALIFIED_DEFAULT, (
        f"the configured default model must be sent; got {body.get('model')!r}"
    )
    assert body.get("model_provider") == _NAMED_PROVIDER, (
        "the configured default's own provider must travel with the model on "
        f"the new-chat payload; got {body.get('model_provider')!r} — without it "
        "the server falls back to the built-in route sharing the endpoint and "
        "bills the shared key (#8527)"
    )


def test_configured_default_wins_over_stale_picker_without_its_provider():
    """#4728's stale-picker case must keep winning — with the CONFIGURED provider.

    Deleting the last session leaves S.session null plus a stale picker value
    from another session's backend. The configured default must win (model
    AND provider), so the stale picker's provider is never attached.
    """
    result = _run_driver(
        {
            "defaultModel": _QUALIFIED_DEFAULT,
            "activeProvider": _NAMED_PROVIDER,
            "S": {"session": None},
            "modelSelect": {
                "value": "gpt-5.1",
                "options": [
                    {"value": "gpt-5.1", "data-provider": "openai"},
                ],
            },
        }
    )
    assert result["ok"], result.get("error")
    body = result["body"]
    assert body.get("model") == _QUALIFIED_DEFAULT, (
        f"the configured default must still win over the stale picker; got {body.get('model')!r}"
    )
    assert body.get("model_provider") == _NAMED_PROVIDER, (
        "the stale picker's provider must never be attached to the configured "
        f"default; got {body.get('model_provider')!r}"
    )


def test_explicit_pick_override_keeps_its_pair():
    """Control: an explicit empty-composer pick still travels verbatim.

    The empty-composer override branch consumes the pair the change handler
    derived together; this repair must not disturb it (regression control).
    """
    result = _run_driver(
        {
            "defaultModel": _QUALIFIED_DEFAULT,
            "activeProvider": _NAMED_PROVIDER,
            "S": {"session": None},
            "explicitOverride": {"model": "grok-4.3", "model_provider": "xai-oauth"},
        }
    )
    assert result["ok"], result.get("error")
    body = result["body"]
    assert body.get("model") == "grok-4.3"
    assert body.get("model_provider") == "xai-oauth", (
        f"an explicit pick must keep its own provider; got {body.get('model_provider')!r}"
    )


def test_configured_default_without_provider_stays_null():
    """Control: no configured provider (older catalogs) must not invent one.

    ``_activeProvider`` is null when the catalog payload carries none; the
    payload must stay provider-less exactly as before so the server's
    slow-path normalization still owns the resolution.
    """
    result = _run_driver(
        {
            "defaultModel": _QUALIFIED_DEFAULT,
            "activeProvider": None,
            "S": {"session": None},
        }
    )
    assert result["ok"], result.get("error")
    body = result["body"]
    assert body.get("model_provider") in (None, ""), (
        f"no configured provider must stay unresolved; got {body.get('model_provider')!r}"
    )


# ---------------------------------------------------------------------------
# Maintainer re-gate (2026-10-29): the configured globals are not always a
# profile-owned pair.
#
# Both profile-switch writers (_switchProfileForSessionLoad in sessions.js,
# switchToProfile in panels.js) used to replace only window._defaultModel when
# the destination returned ``default_model_provider:null``, retaining the
# PREVIOUS profile's window._activeProvider. newSession() then sent the
# destination's default model with the old profile's provider — a
# wrong-provider client payload (the canonical switch auto-creates the new
# session BEFORE its normal catalog refresh; the later refresh clears the
# stale global but cannot retract the request already sent).
#
# The writers now adopt the model AND (re)bind the provider from the SAME
# destination response — replace or clear, never keep the previous profile's
# provider when the destination has none. These tests compose the REAL
# switchToProfile → REAL newSession chain (plus the _switchProfileForSessionLoad
# writer) under node, with the ordinary post-switch refresh ordering recorded,
# and cover both the null-provider and explicit-provider destination arms.
# ---------------------------------------------------------------------------

PANELS_JS = ROOT / "static" / "panels.js"

_SWITCH_DRIVER_BODY = r"""
const fs = require('fs');
const srcSessions = fs.readFileSync(process.argv[1], 'utf8');
const srcPanels = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);

const calls = [];
const bodies = {};
const noop = () => {};
const promiseNoop = () => Promise.resolve();

const S = Object.assign({
  activeProfile: 'default',
  activeProfileIsDefault: true,
  session: scenario.session || null,
  messages: scenario.messages || [],
  toolCalls: [],
  _profileSwitchWorkspace: null,
  _profileDefaultWorkspace: null,
  _pendingSessionToolsets: null,
}, scenario.S || {});

let _modelSelect = null;

const ctx = {
  S: S,
  $: (id) => (id === 'modelSelect' ? _modelSelect : null),
  api: (url, opts) => {
    calls.push(url);
    if (url === '/api/profile/switch') {
      return Promise.resolve(scenario.switchData);
    }
    if (url === '/api/session/new') {
      let body = null;
      try { body = JSON.parse((opts && opts.body) || 'null'); } catch (_) { body = null; }
      bodies['/api/session/new'] = body;
      return Promise.resolve({
        session: {session_id: 'new-sid', messages: [], model: (body && body.model) || null, model_provider: (body && body.model_provider) || null},
      });
    }
    return Promise.resolve({});
  },
  window: {
    _defaultModel: scenario.defaultModel,
    _activeProvider: scenario.activeProvider,
    _clearPendingSelections: noop,
    invalidateSlashSkillCaches: undefined,
  },
  document: { title: '', createElement: () => ({dataset: {}, textContent: '', value: '', appendChild() {}, querySelectorAll: () => []}) },
  localStorage: { removeItem() {}, setItem() {} },
  showToast: noop,
  t: (v) => v,
  updateQueueBadge: noop,
  clearLiveToolCards: noop,
  syncTopbar: noop,
  renderMessages: noop,
  loadDir: promiseNoop,
  refreshSessionList: noop,
  _allProjects: [],
  _activeProject: scenario.activeProject || null,
  NO_PROJECT_FILTER: '__no_project__',
  _newSessionInFlight: null,
  _newSessionPendingText: () => '',
  _setNewSessionPending: noop,
  _messagesTruncated: false,
  _oldestIdx: 0,
  _projectBindingsForNewSession: () => ({}),
  _profileMatchesActiveProfile: () => true,
  _readEmptyComposerModelOverride: () => null,
  _modelStateForSelect: (sel, value) => {
    const opt = (sel && sel.options || []).find((o) => o.value === value);
    return {model: value, model_provider: (opt ? opt['data-provider'] : null) || null};
  },
  _readPersistedModelState: () => null,
  _clearExplicitPickerPick: noop,
  _clearEmptyComposerModelOverride: noop,
  _adoptRegenerationRevision: noop,
  _rememberNewChatDraftSession: noop,
  _setActiveSessionUrl: noop,
  _setLiveAssistantTps: noop,
  _setSessionViewedCount: noop,
  _syncCtxIndicator: noop,
  _applyModelToDropdown: () => null,
  _applyReasoningChip: noop,
  _ensureModelOptionInDropdown: noop,
  _hydrateTodosFromSession: noop,
  _announceNewSessionWorkspace: noop,
  _deferWorkspaceRefreshForSession: noop,
  _retireProjectPickerForExplicitRepaint: noop,
  _loadSessionGeneration: 0,
  _sessionSourceFilter: null,
  _clearRestoredGoalContinuationDraft: noop,
  updateSendBtn: noop,
  setStatus: noop,
  setComposerStatus: noop,
  startSessionStream: noop,
  // switchToProfile module vars + stubs (typeof-guarded in source).
  _profileSwitchGeneration: 0,
  _profileSwitchInFlight: 0,
  _profileSwitchOpeningExistingSession: false,
  _workspacePanelMode: 'closed',
  _skillsData: null,
  _workspaceList: null,
  _tabVisReconcilePending: 0,
  _renamingSid: null,
  _sessionListSkeletonActive: false,
  _invalidateSessionListRenders: noop,
  _setProfileSwitchListEmbargo: noop,
  showSessionListSkeleton: noop,
  bumpWorkspaceTreeGen: noop,
  showWorkspaceTreeSkeleton: noop,
  clearWorkspaceTreeSkeleton: noop,
  _resetCronUnreadForProfileSwitch: noop,
  _clearPersistedModelState: noop,
  startGatewaySSE: noop,
  applyBotName: noop,
  refreshProfileTransitionReasoningChip: noop,
  animateNextSessionListRefresh: noop,
  renderSessionList: promiseNoop,
  _openProfileSwitchSessionBrowser: noop,
  _profileSwitchPanelLoad: promiseNoop,
  closeSessionActionMenu: noop,
  Object, JSON, Promise, Array, String, Number, Boolean, Math, RegExp, Date,
  Error, TypeError, console, setTimeout, isNaN, parseInt, parseFloat,
};
ctx.refreshRan = null;
// The ordinary post-switch catalog refresh (populateModelDropdown path) clears
// the stale globals in the real flow; it runs AFTER the auto-created session's
// POST and records its position so the test can prove the payload was already
// correct before it ran — the refresh can never retract a sent request.
ctx._refreshProfileSwitchBackground = () => {
  ctx.refreshRan = calls.length;
  ctx.window._activeProvider = null;
};

const proxy = new Proxy(ctx, {
  get(target, prop) { if (prop in target) return target[prop]; return noop; },
  has() { return true; },
});
const vm = require('vm');
const context = vm.createContext(proxy);
vm.runInContext(extractAsyncFunction(srcPanels, 'switchToProfile'), context);
vm.runInContext(extractAsyncFunction(srcSessions, 'newSession'), context);
vm.runInContext(extractAsyncFunction(srcSessions, '_switchProfileForSessionLoad'), context);

const run = scenario.flow === 'loadSwitch'
  ? context._switchProfileForSessionLoad(scenario.switchName)
  : context.switchToProfile(scenario.switchName);
run.then(() => {
  process.stdout.write(JSON.stringify({
    ok: true,
    calls: calls,
    bodies: bodies,
    activeProvider: ctx.window._activeProvider,
    defaultModel: ctx.window._defaultModel,
    refreshRan: ctx.refreshRan,
  }));
}).catch((err) => {
  process.stdout.write(JSON.stringify({ok: false, error: String(err && err.stack || err)}));
});
"""


def _run_switch_driver(scenario: dict) -> dict:
    """Extract the real switchToProfile + real newSession (or the real
    _switchProfileForSessionLoad writer) and drive one scenario."""
    driver = _EXTRACTOR + _SWITCH_DRIVER_BODY
    proc = subprocess.run(
        [NODE_BIN, "-e", driver, str(SESSIONS_JS), str(PANELS_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise AssertionError(f"switch driver produced no output; stderr={proc.stderr[:2000]}")
    return json.loads(out.splitlines()[-1])


_OLD_SESSION = {
    "session_id": "old-sid",
    "model": "old-model",
    "model_provider": "old-route",
    "profile": "old-profile",
}
_OLD_MESSAGES = [{"role": "user", "content": "existing conversation"}]
_DEST_DEFAULT = "google/gemini-2.5-pro"
_SWITCH_NAME = "new-profile"


def _switch_scenario(switch_data: dict) -> dict:
    """An existing conversation on the OLD profile; the switch is about to
    auto-create a new session for the destination profile."""
    return {
        "flow": "switch",
        "switchName": _SWITCH_NAME,
        "switchData": switch_data,
        # Stale globals from the previous profile, hydrated before the switch.
        "defaultModel": _DEST_DEFAULT,
        "activeProvider": "old-route",
        "session": _OLD_SESSION,
        "messages": _OLD_MESSAGES,
    }


def test_profile_switch_new_session_drops_previous_provider_when_destination_has_none():
    """A destination returning ``default_model_provider:null`` must NOT carry
    the previous profile's provider onto the auto-created session's payload.

    The canonical switch (real switchToProfile) adopts the destination's
    default model and then auto-creates the new session (real newSession)
    BEFORE its normal catalog refresh — which is exactly why the payload must
    already be correct at POST time: the refresh clears the stale global but
    cannot retract the request already sent.
    """
    result = _run_switch_driver(_switch_scenario({
        "active": _SWITCH_NAME,
        "is_default": False,
        "default_model": _DEST_DEFAULT,
        "default_model_provider": None,
    }))
    assert result["ok"], result.get("error")
    body = result["bodies"].get("/api/session/new") or {}
    assert body.get("model") == _DEST_DEFAULT, (
        f"the destination default model must be sent; got {body.get('model')!r}"
    )
    assert body.get("model_provider") in (None, ""), (
        "a destination with no configured provider must send model_provider=null on the "
        f"auto-created session — retaining the previous profile's provider "
        f"({body.get('model_provider')!r}) would bill the new profile's turn to the "
        "old route (#8527 profile-switch re-gate)"
    )
    # Ordinary refresh ordering: the catalog refresh runs AFTER the POST (its
    # position is recorded), so it can never be the mechanism that fixed the
    # payload — the writers must have bound the pair from the switch response.
    assert result["refreshRan"] is not None, "the post-switch refresh must have run"
    assert result["calls"].index("/api/session/new") < result["refreshRan"], (
        "the auto-created session's POST must precede the ordinary catalog refresh"
    )


def test_profile_switch_new_session_keeps_destination_provider():
    """Control arm: a destination WITH a configured provider sends it verbatim."""
    result = _run_switch_driver(_switch_scenario({
        "active": _SWITCH_NAME,
        "is_default": False,
        "default_model": _DEST_DEFAULT,
        "default_model_provider": "new-route",
    }))
    assert result["ok"], result.get("error")
    body = result["bodies"].get("/api/session/new") or {}
    assert body.get("model") == _DEST_DEFAULT
    assert body.get("model_provider") == "new-route", (
        f"the destination's own provider must travel with its model; got {body.get('model_provider')!r}"
    )


def _load_switch_scenario(switch_data: dict) -> dict:
    return {
        "flow": "loadSwitch",
        "switchName": _SWITCH_NAME,
        "switchData": switch_data,
        "defaultModel": _DEST_DEFAULT,
        "activeProvider": "old-route",
        "session": None,
        "messages": [],
    }


def test_session_load_switch_clears_stale_provider_when_destination_has_none():
    """The sessions.js profile-switch writer (_switchProfileForSessionLoad)
    must clear the previous profile's provider when the destination has none."""
    result = _run_switch_driver(_load_switch_scenario({
        "active": _SWITCH_NAME,
        "is_default": False,
        "default_model": _DEST_DEFAULT,
        "default_model_provider": None,
    }))
    assert result["ok"], result.get("error")
    assert result["defaultModel"] == _DEST_DEFAULT
    assert result["activeProvider"] in (None, ""), (
        "window._activeProvider must be cleared from the same destination response "
        f"when default_model_provider is null; retained {result['activeProvider']!r} "
        "from the previous profile (#8527 profile-switch re-gate)"
    )


def test_session_load_switch_adopts_destination_provider():
    """Control arm: the sessions.js writer adopts a configured destination provider."""
    result = _run_switch_driver(_load_switch_scenario({
        "active": _SWITCH_NAME,
        "is_default": False,
        "default_model": _DEST_DEFAULT,
        "default_model_provider": "new-route",
    }))
    assert result["ok"], result.get("error")
    assert result["defaultModel"] == _DEST_DEFAULT
    assert result["activeProvider"] == "new-route", (
        f"the destination's configured provider must be adopted; got {result['activeProvider']!r}"
    )
