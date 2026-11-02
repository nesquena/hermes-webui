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
# The built-in provider that shares the same endpoint — a DIFFERENT credential
# (OPENROUTER_API_KEY vs the named provider's own key_env). Picking it on
# purpose must stay distinguishable from the configured named route.
_BUILTIN_PROVIDER = "openrouter"


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


def test_deliberate_builtin_route_pick_keeps_the_builtin_provider():
    """The configured pair must not hijack a DELIBERATE built-in pick.

    Issue #8527's verification criteria ask for the built-in route to be
    coverable too: when the user explicitly selects the built-in openrouter
    entry for the very same model id the configured default advertises, that
    pick must travel verbatim. Sharing an endpoint must not collapse the two
    provider identities — and the configured-default repair must never
    overwrite a pick the user actually made.

    This is the guard on the fix itself: it fails if the configured-default
    branch is allowed to win over an explicit empty-composer pick.
    """
    result = _run_driver(
        {
            "defaultModel": _QUALIFIED_DEFAULT,
            "activeProvider": _NAMED_PROVIDER,
            "S": {"session": None},
            "explicitOverride": {
                "model": _QUALIFIED_DEFAULT,
                "model_provider": _BUILTIN_PROVIDER,
            },
        }
    )
    assert result["ok"], result.get("error")
    body = result["body"]
    assert body.get("model") == _QUALIFIED_DEFAULT
    assert body.get("model_provider") == _BUILTIN_PROVIDER, (
        "a deliberate built-in pick must keep the built-in provider; got "
        f"{body.get('model_provider')!r} — the configured default must not "
        "hijack a pick the user actually made"
    )


def test_new_chat_after_existing_session_keeps_the_configured_pair():
    """Starting another chat after an existing session keeps the configured pair.

    Issue #8527's verification criteria ask for the "another chat after an
    existing session" case. The previous session's provider must not leak
    into the new session: the configured default wins, and it wins WITH its
    own configured provider (not the previous session's route).
    """
    result = _run_driver(
        {
            "defaultModel": _QUALIFIED_DEFAULT,
            "activeProvider": _NAMED_PROVIDER,
            "S": {
                "session": {
                    "session_id": "old-sid",
                    "model": _QUALIFIED_DEFAULT,
                    "model_provider": _BUILTIN_PROVIDER,
                }
            },
        }
    )
    assert result["ok"], result.get("error")
    body = result["body"]
    assert body.get("model") == _QUALIFIED_DEFAULT
    assert body.get("model_provider") == _NAMED_PROVIDER, (
        "the previous session's provider must not leak into the new session; "
        f"got {body.get('model_provider')!r}"
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

let _modelSelect = scenario.modelSelect || null;

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
// The ordinary post-switch catalog refresh (populateModelDropdown path) re-derives
// BOTH globals from the destination's /api/models payload and releases the
// one-shot profile-switch model reset; it runs AFTER the auto-created session's
// POST and records its position so the test can prove the payload was already
// correct before it ran — the refresh can never retract a sent request.
ctx._refreshProfileSwitchBackground = () => {
  ctx.refreshRan = calls.length;
  ctx.window._activeProvider = null;
  ctx.window._defaultModel = null;
  ctx.S._profileSwitchModelReset = false;
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
// Optional: drive the REAL newSession after the switch writer completes — the
// maintainer's repro chain for the sessions.js writer
// (_switchProfileForSessionLoad -> newSession), whose caller loads an existing
// session afterwards instead of auto-creating one.
const settled = scenario.chainNewSession
  ? run.then(() => context.newSession(false, {}))
  : run;
settled.then(() => {
  process.stdout.write(JSON.stringify({
    ok: true,
    calls: calls,
    bodies: bodies,
    activeProvider: ctx.window._activeProvider,
    defaultModel: ctx.window._defaultModel,
    profileSwitchModelReset: !!ctx.S._profileSwitchModelReset,
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


# ---------------------------------------------------------------------------
# Maintainer re-gate (2026-10-31): a destination WITHOUT a configured model
# must own the default state.
#
# api/profiles.py's per-client switch reads the destination's raw config.yaml
# and legitimately returns ``default_model:null`` (key present, value null),
# omits the key entirely, or returns an empty string for a model-less profile
# (api/profiles.py:1986-1993,2045-2051). Both switch writers used to gate BOTH
# default assignments behind ``if (data.default_model)``, so in that case the
# PREVIOUS profile's ``window._defaultModel`` / ``window._activeProvider``
# survived — and the canonical switch auto-creates a session BEFORE its normal
# catalog refresh, so the configured-default branch of newSession() explicitly
# posted the stale pair under the NEW profile:
#
#   {"profile":"new-profile","model":"google/gemini-2.5-pro","model_provider":"old-route"}
#
# Clearing the pair alone is not enough: with ``window._defaultModel`` null,
# newSession() would fall through to the still-pre-refresh picker (same model
# id, old provider) or the persisted pair. The writers now replace-or-clear
# the pair unconditionally and arm a one-shot reset gate
# (``S._profileSwitchModelReset``) that blocks those two fallbacks until
# populateModelDropdown rehydrates the destination's catalog.
#
# These tests compose the REAL caller chains — switchToProfile -> newSession
# (the canonical switch auto-creates the session) and
# _switchProfileForSessionLoad -> newSession (the maintainer's second repro
# chain) — seed the old profile's pair, and assert the request that actually
# went out: caller completion (the POST happened) and request scope (profile
# field, no stale model/provider).
# ---------------------------------------------------------------------------

# The three model-absent destination shapes api/profiles.py can return.
_MODEL_ABSENT_VARIANTS = {
    "null": {"default_model": None},
    "absent": {},
    "empty": {"default_model": ""},
}

# A stale same-model picker: the dropdown still holds the PREVIOUS profile's
# selection (same model id) with the previous profile's provider, because the
# catalog refresh runs AFTER the auto-created session's POST. The option
# carries a pre-set dataset.provider so the writer's option-tagging path is a
# no-op (mirrors a hydrated dropdown from the previous profile's catalog).
_STALE_PICKER = {
    "value": _DEST_DEFAULT,
    "options": [
        {
            "value": _DEST_DEFAULT,
            "data-provider": "old-route",
            "dataset": {"provider": "old-route"},
        },
    ],
}


def _assert_model_absent_new_session(result):
    """The auto-created / chained session must carry NO model or provider."""
    assert result["ok"], result.get("error")
    assert "/api/session/new" in result["calls"], (
        "the switch caller must actually complete the new session it owns; "
        f"calls={result['calls']!r}"
    )
    body = result["bodies"].get("/api/session/new") or {}
    assert body.get("profile") == _SWITCH_NAME, (
        f"the request must be scoped to the destination profile; got {body.get('profile')!r}"
    )
    assert body.get("model") in (None, ""), (
        "a destination with no configured model must not inherit the previous "
        f"profile's model on the new session; got {body.get('model')!r} (#8527 "
        "profile-switch re-gate)"
    )
    assert body.get("model_provider") in (None, ""), (
        "a destination with no configured model must not pin the previous "
        f"profile's provider; got {body.get('model_provider')!r} (#8527 "
        "profile-switch re-gate)"
    )


def _canonical_switch_scenario(switch_data, model_select=None):
    scenario = _switch_scenario(switch_data)
    if model_select is not None:
        scenario["modelSelect"] = model_select
    return scenario


def _load_switch_chain_scenario(switch_data, model_select=None):
    scenario = _load_switch_scenario(switch_data)
    scenario["chainNewSession"] = True
    if model_select is not None:
        scenario["modelSelect"] = model_select
    return scenario


@pytest.mark.parametrize("variant", sorted(_MODEL_ABSENT_VARIANTS))
def test_canonical_switch_new_session_drops_stale_pair_when_destination_model_is(variant):
    """switchToProfile -> newSession: a model-absent destination sends no pair.

    RED on the reviewed head: both writers skip the assignments, so the
    configured-default branch posts the previous profile's model + provider
    under the new profile's name.
    """
    result = _run_switch_driver(_canonical_switch_scenario({
        "active": _SWITCH_NAME,
        "is_default": False,
        **_MODEL_ABSENT_VARIANTS[variant],
    }))
    _assert_model_absent_new_session(result)
    # Ordinary refresh ordering: the POST precedes the catalog refresh, so the
    # refresh cannot be the mechanism that fixed the payload.
    assert result["refreshRan"] is not None, "the post-switch refresh must have run"
    assert result["calls"].index("/api/session/new") < result["refreshRan"], (
        "the auto-created session's POST must precede the ordinary catalog refresh"
    )


@pytest.mark.parametrize("variant", sorted(_MODEL_ABSENT_VARIANTS))
def test_session_load_switch_new_session_drops_stale_pair_when_destination_model_is(variant):
    """_switchProfileForSessionLoad -> newSession: same contract, other writer.

    RED on the reviewed head: the sessions.js writer leaves the previous
    profile's pair in place and newSession posts it under the new profile.
    """
    result = _run_switch_driver(_load_switch_chain_scenario({
        "active": _SWITCH_NAME,
        "is_default": False,
        **_MODEL_ABSENT_VARIANTS[variant],
    }))
    _assert_model_absent_new_session(result)
    # The writer itself must have cleared the pair — nothing refreshes it in
    # this flow before the chained newSession.
    assert result["defaultModel"] in (None, ""), (
        f"window._defaultModel must be cleared for a model-less destination; "
        f"retained {result['defaultModel']!r}"
    )
    assert result["activeProvider"] in (None, ""), (
        f"window._activeProvider must be cleared for a model-less destination; "
        f"retained {result['activeProvider']!r}"
    )


def test_canonical_switch_stale_same_model_picker_is_not_the_fallback():
    """The stale picker must not become the fallback after the reset.

    The dropdown still holds the previous profile's selection (same model id,
    old provider) because the catalog refresh runs after the auto-created
    session's POST. Clearing the globals alone would fall through to it; the
    one-shot reset gate is what keeps this request model-less.
    """
    result = _run_switch_driver(_canonical_switch_scenario(
        {"active": _SWITCH_NAME, "is_default": False, "default_model": None},
        model_select=_STALE_PICKER,
    ))
    _assert_model_absent_new_session(result)


def test_session_load_switch_stale_same_model_picker_is_not_the_fallback():
    """Same stale-picker contract for the _switchProfileForSessionLoad chain."""
    result = _run_switch_driver(_load_switch_chain_scenario(
        {"active": _SWITCH_NAME, "is_default": False, "default_model": None},
        model_select=_STALE_PICKER,
    ))
    _assert_model_absent_new_session(result)


def test_profile_switch_with_destination_model_keeps_pair_over_stale_picker():
    """Control: a destination WITH a configured pair still wins the stale picker.

    The gate must only fire for model-less destinations; a configured
    destination keeps the present-model/null-provider repair intact and the
    stale picker (same model id, old provider) is never attached.
    """
    result = _run_switch_driver(_canonical_switch_scenario(
        {
            "active": _SWITCH_NAME,
            "is_default": False,
            "default_model": _DEST_DEFAULT,
            "default_model_provider": "new-route",
        },
        model_select=_STALE_PICKER,
    ))
    assert result["ok"], result.get("error")
    body = result["bodies"].get("/api/session/new") or {}
    assert body.get("model") == _DEST_DEFAULT
    assert body.get("model_provider") == "new-route", (
        "the destination's own provider must travel with its model; the stale "
        f"picker's provider must never be attached; got {body.get('model_provider')!r}"
    )
    assert not result["profileSwitchModelReset"], (
        "the reset gate must stay disarmed when the destination configures a model"
    )


def test_new_session_without_switch_reset_still_uses_the_picker():
    """Control: outside a profile switch the picker fallback is untouched.

    No configured default and no reset gate — the ordinary picker fallback
    (and its provider) must still reach /api/session/new exactly as before.
    """
    result = _run_driver({
        "defaultModel": None,
        "activeProvider": None,
        "S": {"session": None},
        "modelSelect": {
            "value": "gpt-5.1",
            "options": [{"value": "gpt-5.1", "data-provider": "openai"}],
        },
    })
    assert result["ok"], result.get("error")
    body = result["body"]
    assert body.get("model") == "gpt-5.1", (
        f"the picker fallback must still win with no configured default; got {body.get('model')!r}"
    )
    assert body.get("model_provider") == "openai", (
        f"the picker's own provider must still travel; got {body.get('model_provider')!r}"
    )


# ---------------------------------------------------------------------------
# Maintainer re-gate (2026-10-31, round 2): the one-shot reset gate must
# survive the catalog hydration that follows a model-less profile switch.
#
# populateModelDropdown() released ``S._profileSwitchModelReset`` the moment
# the /api/models payload arrived (static/ui.js, right after the payload is
# parsed) — BEFORE it knew whether the picker could actually be rebuilt. The
# supported no-groups/no-configured-fallback branch then returned before the
# ``sel.innerHTML=''`` clear, so the PREVIOUS profile's option stayed in the
# picker with the gate already released, and the next newSession()
# (static/sessions.js, picker fallback) adopted that stale pair:
#
#   1. switch to new-profile with no configured model -> model-less POST ✓
#   2. catalog returns {active_provider:null, default_model:'',
#      configured_model_badges:{}, groups:[]} -> gate released, option kept ✗
#   3. New Chat again -> {profile:'new-profile', model:'old/model',
#      model_provider:'old-route'} ✗
#
# The tests below drive the REAL switch writer, the REAL populateModelDropdown
# (composed with the REAL picker identity/provider/application helpers
# extracted verbatim from static/ui.js) and the REAL newSession, so the whole
# guard/picker lifecycle is observed instead of a refresh stub that clears the
# globals directly.
# ---------------------------------------------------------------------------

UI_JS = ROOT / "static" / "ui.js"

# The picker helpers populateModelDropdown composes with — extracted verbatim
# so option identity, provider provenance and the selection reconciliation are
# the production implementations rather than test doubles.
_UI_PICKER_FUNCTIONS = [
    "_getOptionProviderId",
    "_providerFromModelValue",
    "_modelPickerOptionIdentity",
    "_deduplicateModelPickerOptions",
    "_modelStateForSelect",
    "_captureModelDropdownSelection",
    "_findModelInDropdown",
    "_refreshOpenModelDropdown",
    "_applyModelToDropdown",
    "_ensureModelOptionInDropdown",
    "_reconcileModelDropdownSelection",
]

_REFRESH_DRIVER_BODY = r"""
const fs = require('fs');
const vm = require('vm');
const srcSessions = fs.readFileSync(process.argv[1], 'utf8');
const srcPanels = fs.readFileSync(process.argv[2], 'utf8');
const srcUi = fs.readFileSync(process.argv[3], 'utf8');
const scenario = JSON.parse(process.argv[4]);

const calls = [];
const newSessionBodies = [];
const noop = () => {};
const promiseNoop = () => Promise.resolve();

// ---- minimal <select id="modelSelect"> stand-in ---------------------------
function makeSelect(groups, initialValue) {
  const sel = {id: 'modelSelect', tagName: 'SELECT', options: [], selectedIndex: -1, selectedOptions: []};
  Object.defineProperty(sel, 'value', {
    get() { return this._value || ''; },
    set(v) {
      this._value = v;
      const idx = this.options.findIndex((o) => o.value === v);
      this.selectedIndex = idx;
      this.selectedOptions = idx >= 0 ? [this.options[idx]] : [];
    }
  });
  Object.defineProperty(sel, 'innerHTML', {
    get() { return ''; },
    set(_v) { this.options = []; this._groups = []; this.value = ''; }
  });
  sel._groups = [];
  sel.appendChild = function(node) {
    if (node && node.tagName === 'OPTGROUP') {
      this._groups.push(node);
      for (const opt of node.children || []) {
        opt.parentElement = node;
        this.options.push(opt);
      }
    } else if (node) {
      node.parentElement = null;
      this.options.push(node);
    }
    if (!this.options.length) return;
    if (this.selectedIndex < 0) this.value = this.options[0].value;
    else if (this._value) this.value = this._value;
  };
  sel.querySelector = function(selector) {
    return String(selector).includes('option') ? (this.options[0] || null) : null;
  };
  sel.querySelectorAll = function(selector) {
    return String(selector) === 'optgroup' ? (this._groups || []) : [];
  };
  for (const group of groups || []) {
    const og = {
      tagName: 'OPTGROUP',
      label: group.provider || '',
      dataset: {provider: group.provider_id || ''},
      children: [],
      appendChild(opt) { opt.parentElement = this; this.children.push(opt); },
      removeChild(opt) {
        const idx = this.children.indexOf(opt);
        if (idx >= 0) this.children.splice(idx, 1);
        const oi = sel.options.indexOf(opt);
        if (oi >= 0) {
          sel.options.splice(oi, 1);
          sel.value = sel.value;
        }
      },
    };
    for (const item of group.options || []) {
      og.children.push({
        tagName: 'OPTION',
        value: item.value,
        textContent: item.label || item.value,
        title: '',
        dataset: Object.assign({}, item.dataset || {}),
      });
    }
    sel.appendChild(og);
  }
  sel.value = initialValue || '';
  return sel;
}

const _modelSelect = makeSelect(
  scenario.modelSelect && scenario.modelSelect.groups,
  scenario.modelSelect && scenario.modelSelect.value
);

const document = {
  baseURI: 'http://127.0.0.1/hermes/',
  title: '',
  createElement(tag) {
    const upper = String(tag).toUpperCase();
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
const localStorage = {getItem() { return null; }, setItem() {}, removeItem() {}};

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
      newSessionBodies.push(body);
      return Promise.resolve({
        session: {
          session_id: 'new-sid',
          messages: [],
          model: (body && body.model) || null,
          model_provider: (body && body.model_provider) || null,
        },
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
  document: document,
  localStorage: localStorage,
  fetch: async (url) => {
    const href = String(url);
    if (href.includes('api/models')) {
      return {json: async () => scenario.modelsPayload};
    }
    throw new Error('unexpected fetch ' + href);
  },
  URL: URL,
  Map: Map,
  Set: Set,
  Symbol: Symbol,
  showToast: noop,
  t: (v) => v,
  updateQueueBadge: noop,
  clearLiveToolCards: noop,
  syncTopbar: noop,
  renderMessages: noop,
  loadDir: () => ({catch: noop}),
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
  _readEmptyComposerModelOverride: () => scenario.explicitOverride || null,
  _readPersistedModelState: () => null,
  _clearExplicitPickerPick: noop,
  _clearEmptyComposerModelOverride: noop,
  _adoptRegenerationRevision: noop,
  _rememberNewChatDraftSession: noop,
  _setActiveSessionUrl: noop,
  _setLiveAssistantTps: noop,
  _setSessionViewedCount: noop,
  _syncCtxIndicator: noop,
  _applyReasoningChip: noop,
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
  // switchToProfile / _switchProfileForSessionLoad module vars + stubs.
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
  // populateModelDropdown collaborators (the picker helpers themselves are
  // extracted verbatim below; these are the leaf DOM/fetch stubs).
  getModelLabel: (v) => v,
  syncModelChip: () => { calls.push('syncModelChip'); },
  _fetchLiveModels: () => { calls.push('_fetchLiveModels'); },
  _redirectIfUnauth: () => false,
  _sortModelPickerEntries: (values) => values,
  renderModelDropdown: noop,
  _positionModelDropdown: noop,
  // The ordinary post-switch background refresh only RECORDS its position —
  // the real populateModelDropdown driven below owns the gate, so this stub
  // must never touch the globals (that indirection is what let the premature
  // release hide).
  _refreshProfileSwitchBackground: () => { calls.push('_refreshProfileSwitchBackground'); },
  Object, JSON, Promise, Array, String, Number, Boolean, Math, RegExp, Date,
  Error, TypeError, console, setTimeout, isNaN, parseInt, parseFloat,
};

const proxy = new Proxy(ctx, {
  get(target, prop) { if (prop in target) return target[prop]; return noop; },
  has() { return true; },
});
const context = vm.createContext(proxy);
vm.runInContext('var _dynamicModelLabels = {};', context);
for (const name of __PICKER_FNS__) {
  vm.runInContext(extractFunction(srcUi, name), context);
}
vm.runInContext(extractAsyncFunction(srcUi, 'populateModelDropdown'), context);
vm.runInContext(extractAsyncFunction(srcSessions, 'newSession'), context);
vm.runInContext(extractAsyncFunction(srcPanels, 'switchToProfile'), context);
vm.runInContext(extractAsyncFunction(srcSessions, '_switchProfileForSessionLoad'), context);

const snapshot = () => JSON.parse(vm.runInContext(`JSON.stringify({
  guard: !!S._profileSwitchModelReset,
  defaultModel: window._defaultModel,
  activeProvider: window._activeProvider,
  pickerValue: $('modelSelect').value,
  pickerOptions: $('modelSelect').options.map((o) => ({value: o.value, provider: _getOptionProviderId(o)})),
})`, context));

const switched = scenario.flow === 'loadSwitch'
  ? context._switchProfileForSessionLoad(scenario.switchName)
  : (scenario.flow === 'switch' ? context.switchToProfile(scenario.switchName) : Promise.resolve());

switched
  // The real post-switch catalog hydration (same call shape the session-load
  // refresh uses).
  .then(() => context.populateModelDropdown({freshness: 'session_visit'}))
  .then(() => {
    const afterRefresh = snapshot();
    // "Create another conversation" — the caller that exposed the stale pair.
    return context.newSession(false, scenario.newSessionOptions || {}).then(() => afterRefresh);
  })
  .then((afterRefresh) => {
    process.stdout.write(JSON.stringify({
      ok: true,
      calls: calls,
      newSessionBodies: newSessionBodies,
      afterRefresh: afterRefresh,
      afterNewSession: snapshot(),
    }));
  })
  .catch((err) => {
    process.stdout.write(JSON.stringify({ok: false, error: String((err && err.stack) || err)}));
  });
"""


def _run_refresh_driver(scenario: dict) -> dict:
    """Drive the REAL profile-switch writer, the REAL populateModelDropdown and
    the REAL newSession in that order; report the guard/picker state after the
    catalog hydration plus every ``/api/session/new`` payload."""
    driver = (
        _EXTRACTOR
        + _REFRESH_DRIVER_BODY.replace("__PICKER_FNS__", json.dumps(_UI_PICKER_FUNCTIONS))
    )
    proc = subprocess.run(
        [NODE_BIN, "-e", driver, str(SESSIONS_JS), str(PANELS_JS), str(UI_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise AssertionError(f"refresh driver produced no output; stderr={proc.stderr[:2000]}")
    return json.loads(out.splitlines()[-1])


# A model-less destination: api/profiles.py legitimately returns a null
# default_model for a profile that configures none.
_MODEL_LESS_SWITCH_DATA = {"active": _SWITCH_NAME, "is_default": False, "default_model": None}

# The empty catalog the backend ships for such a profile: no groups, no
# configured fallback, no active provider.
_EMPTY_CATALOG_PAYLOAD = {
    "active_provider": None,
    "default_model": "",
    "configured_model_badges": {},
    "groups": [],
}

# A populated fallback-model response: no server groups, but the destination's
# configured badges synthesize a group (the supported configured-fallback
# branch). Same model id as the stale picker option, DIFFERENT provider — the
# identity collision where a same-value option must not keep the previous
# profile's provenance.
_POPULATED_FALLBACK_PAYLOAD = {
    "active_provider": None,
    "default_model": "",
    "configured_model_badges": {"gpt-5.1": {"provider": "new-route"}},
    "groups": [],
}

# A stale same-model picker: the dropdown still holds the PREVIOUS profile's
# selection with the previous profile's provider (the catalog refresh has not
# rebuilt it yet), exactly as it looks when the switch's auto-created session
# POST goes out.
_STALE_SAME_MODEL_PICKER = {
    "value": _DEST_DEFAULT,
    "groups": [
        {
            "provider": "Old route",
            "provider_id": "old-route",
            "options": [
                {
                    "value": _DEST_DEFAULT,
                    "label": "Gemini 2.5 Pro",
                    "dataset": {"provider": "old-route"},
                },
            ],
        },
    ],
}

_STALE_BARE_PICKER = {
    "value": "gpt-5.1",
    "groups": [
        {
            "provider": "Old route",
            "provider_id": "old-route",
            "options": [
                {"value": "gpt-5.1", "label": "GPT-5.1", "dataset": {"provider": "old-route"}},
            ],
        },
    ],
}


def _model_less_switch_scenario(flow, models_payload, model_select, **extra):
    """A tab on the OLD profile (session + picker hydrated from its catalog)
    switching to a model-less destination, followed by the catalog hydration
    and another New Chat.

    ``flow='switch'`` is the canonical switchToProfile chain (it auto-creates
    the first session BEFORE the refresh); ``flow='loadSwitch'`` is the
    sessions.js writer chain the maintainer reproduced.
    """
    scenario = {
        "flow": flow,
        "switchName": _SWITCH_NAME,
        "switchData": _MODEL_LESS_SWITCH_DATA,
        "modelsPayload": models_payload,
        # Stale globals + picker from the previous profile.
        "defaultModel": _DEST_DEFAULT,
        "activeProvider": "old-route",
        "session": _OLD_SESSION if flow == "switch" else None,
        "messages": _OLD_MESSAGES if flow == "switch" else [],
        "modelSelect": model_select,
    }
    scenario.update(extra)
    return scenario


@pytest.mark.parametrize("flow", ["switch", "loadSwitch"])
def test_empty_catalog_after_model_less_switch_keeps_the_gate_and_drops_the_stale_picker(flow):
    """An empty catalog must not release the reset gate NOR keep the stale option.

    RED on the reviewed head: populateModelDropdown released the gate as soon
    as the payload arrived and returned before clearing the <select>, so the
    next New Chat posted the previous profile's pair
    (``model:'google/gemini-2.5-pro', model_provider:'old-route'``) under the
    new profile's name.
    """
    result = _run_refresh_driver(_model_less_switch_scenario(
        flow, _EMPTY_CATALOG_PAYLOAD, _STALE_SAME_MODEL_PICKER,
    ))
    assert result["ok"], result.get("error")
    bodies = result["newSessionBodies"]
    assert bodies, "the chained New Chat must actually POST /api/session/new"
    if flow == "switch":
        assert len(bodies) == 2, (
            f"the canonical switch auto-creates one session and the chained New Chat "
            f"creates the second; got {len(bodies)} POSTs"
        )
        first = bodies[0]
        assert first.get("model") in (None, ""), (
            "the switch's own auto-created session must stay model-less; got "
            f"{first.get('model')!r}"
        )
    second = bodies[-1]
    assert second.get("profile") == _SWITCH_NAME, (
        f"the request must be scoped to the destination profile; got {second.get('profile')!r}"
    )
    assert second.get("model") in (None, ""), (
        "an empty catalog (no groups, no configured fallback) must not let the next "
        f"New Chat fall back to the previous profile's picker; got {second.get('model')!r} "
        "(#8527 profile-switch re-gate)"
    )
    assert second.get("model_provider") in (None, ""), (
        "an empty catalog must not pin the previous profile's provider on the next "
        f"New Chat; got {second.get('model_provider')!r} (#8527 profile-switch re-gate)"
    )
    after = result["afterRefresh"]
    assert after["guard"], (
        "the one-shot reset gate must stay armed through empty/failed hydration — "
        "releasing it before the picker is rebuilt is the lifecycle gap"
    )
    assert after["pickerOptions"] == [], (
        "the previous profile's options must be quarantined when the destination "
        f"catalog is empty; retained {after['pickerOptions']!r}"
    )
    assert after["pickerValue"] in (None, ""), (
        f"the picker must not keep a selectable stale value; got {after['pickerValue']!r}"
    )


@pytest.mark.parametrize("flow", ["switch", "loadSwitch"])
def test_populated_fallback_model_releases_the_gate_with_fresh_provenance(flow):
    """A populated fallback-model response must rebuild the picker and only THEN
    release the gate, so the next New Chat carries the DESTINATION's pair.

    RED on the reviewed head: the gate was released before the rebuild and the
    reconciliation re-applied the previous profile's selection, so the same
    model id came back with the OLD provider ('old-route').
    """
    result = _run_refresh_driver(_model_less_switch_scenario(
        flow, _POPULATED_FALLBACK_PAYLOAD, _STALE_BARE_PICKER,
    ))
    assert result["ok"], result.get("error")
    bodies = result["newSessionBodies"]
    second = bodies[-1]
    assert second.get("model") == "gpt-5.1", (
        f"the destination's configured fallback model must be sent; got {second.get('model')!r}"
    )
    assert second.get("model_provider") == "new-route", (
        "after the destination catalog rehydrates, the picker fallback must carry "
        f"the DESTINATION's provider; got {second.get('model_provider')!r} — the "
        "previous profile's 'old-route' must not survive a same-model rebuild "
        "(#8527 profile-switch re-gate)"
    )
    after = result["afterRefresh"]
    assert not after["guard"], (
        "the reset gate must be released once the current profile's picker has "
        "actually been rebuilt/reconciled"
    )
    providers = sorted({str(o.get("provider") or "") for o in after["pickerOptions"]})
    assert "old-route" not in providers, (
        f"the stale option must not be reconciled back into the picker; got {after['pickerOptions']!r}"
    )
    assert after["pickerValue"] == "gpt-5.1", (
        f"the rebuilt picker must select the destination's model; got {after['pickerValue']!r}"
    )


def test_empty_catalog_still_admits_a_fresh_explicit_composer_pick():
    """A FRESH empty-composer pick is not stale state: it must survive the armed
    gate (newSession consults it ahead of the picker/persisted fallbacks).

    Control arm for the quarantine above — an over-broad fix that blocked
    every fallback during empty hydration would break the user's explicit
    choice.
    """
    result = _run_refresh_driver(_model_less_switch_scenario(
        "loadSwitch",
        _EMPTY_CATALOG_PAYLOAD,
        _STALE_SAME_MODEL_PICKER,
        explicitOverride={"model": "fresh/pick", "model_provider": "fresh-route"},
    ))
    assert result["ok"], result.get("error")
    bodies = result["newSessionBodies"]
    second = bodies[-1]
    assert second.get("model") == "fresh/pick", (
        f"a fresh explicit composer pick must survive the armed gate; got {second.get('model')!r}"
    )
    assert second.get("model_provider") == "fresh-route", (
        f"the explicit pick's own provider must travel; got {second.get('model_provider')!r}"
    )


def test_ordinary_refresh_without_a_pending_reset_still_preserves_the_selection():
    """Control: outside a profile switch the rebuild keeps reconciling the
    user's in-page selection (#4363) — the reset-aware skip is scoped to the
    armed gate only.
    """
    result = _run_refresh_driver({
        "flow": "none",
        "modelsPayload": {
            "active_provider": "openai",
            "default_model": "",
            "configured_model_badges": {},
            "groups": [
                {
                    "provider": "OpenAI",
                    "provider_id": "openai",
                    "models": [
                        {"id": "gpt-5.1", "label": "GPT-5.1"},
                        {"id": "gpt-5.1-mini", "label": "GPT-5.1 mini"},
                    ],
                },
            ],
        },
        "defaultModel": None,
        "activeProvider": "openai",
        "session": None,
        "messages": [],
        "modelSelect": {
            "value": "gpt-5.1",
            "groups": [
                {
                    "provider": "OpenAI",
                    "provider_id": "openai",
                    "options": [
                        {"value": "gpt-5.1", "label": "GPT-5.1", "dataset": {"provider": "openai"}},
                    ],
                },
            ],
        },
    })
    assert result["ok"], result.get("error")
    bodies = result["newSessionBodies"]
    assert len(bodies) == 1, f"expected exactly one New Chat POST; got {len(bodies)}"
    body = bodies[0]
    assert body.get("model") == "gpt-5.1", (
        f"the ordinary picker fallback must still win; got {body.get('model')!r}"
    )
    assert body.get("model_provider") == "openai", (
        f"the picker's own provider must still travel; got {body.get('model_provider')!r}"
    )
    after = result["afterRefresh"]
    assert after["pickerValue"] == "gpt-5.1", (
        f"the rebuild must preserve the in-page selection; got {after['pickerValue']!r}"
    )
    assert after["pickerOptions"] == [
        {"value": "gpt-5.1", "provider": "openai"},
        {"value": "gpt-5.1-mini", "provider": "openai"},
    ], f"the rebuilt catalog must be intact; got {after['pickerOptions']!r}"
