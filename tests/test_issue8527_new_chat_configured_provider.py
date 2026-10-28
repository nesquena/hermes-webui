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
