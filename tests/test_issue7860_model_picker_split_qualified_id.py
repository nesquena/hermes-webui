"""Model picker stores a malformed (model, model_provider) pair (#7860).

Two distinct defects produce it, and the second is masked by the first:

1. ``_modelProviderForSend`` consults the stored session provider *before*
   anything else, so a session created under one provider keeps sending there
   regardless of what is later selected in the picker — a Claude model routed
   to Codex, surfacing as a bogus "quota exhausted (429)".
2. ``_modelStateForSelect`` falls back to the raw option value when the
   option carries no ``data-model`` (which only the fallback injection path
   sets), and the ``@provider:`` prefix strip is gated to *custom* providers
   only (#1771). A catalog option for a non-custom provider therefore stores
   ``@claude-…:claude-sonnet-5`` as the *model name* — provider specified
   twice, and the upstream answers 404 ``HERMES_MODEL_ADMISSION_CONSUMED``.

Both are exercised here through the real functions, driven by node against
``static/ui.js`` (same driver pattern as #6131), because the bug lives in the
browser and the catalog data is correct on the way IN.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")


# Shared lexical extractor — same guard set as the #6131 driver so brace
# counting survives strings, templates, regex literals and comments.
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
  if (end < 0) throw new Error('unterminated block comment');
  return end + 1;
}
function skipRegexLiteral(source, index) {
  let inClass = false;
  for (let i = index + 1; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === '\\') { i += 1; continue; }
    if (ch === '[') inClass = true;
    else if (ch === ']') inClass = false;
    else if (ch === '/' && !inClass) {
      while (/[A-Za-z]/.test(source[i + 1] || '')) i += 1;
      return i;
    }
  }
  throw new Error('unterminated regex literal');
}
function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
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
  throw new Error('unterminated: ' + name);
}
"""


# The driver builds a catalog exactly the way populateModelDropdown does for
# server groups: one <optgroup> per provider carrying provider_id, and
# <option value="<qualified id>"> entries with NO data-model (the gap behind
# defect 2). Then it exercises the three functions that own the stored pair.
_DRIVER = (
    _EXTRACTOR
    + r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

for (const name of ['_providerFromModelValue', '_getOptionProviderId', '_modelStateForSelect', '_captureModelDropdownSelection', '_modelProviderForSend']) {
  eval(extractFunction(uiSrc, name));
}

const PROVIDER = 'claude-subscription-directsdk-experimental';
const QUALIFIED = '@' + PROVIDER + ':claude-sonnet-5[1m]';
const BARE = 'claude-sonnet-5[1m]';

function catalogSelect(value, groupProvider) {
  const group = {
    tagName: 'OPTGROUP',
    label: 'Claude',
    dataset: { provider: groupProvider || PROVIDER },
  };
  const opt = {
    tagName: 'OPTION',
    value,
    textContent: BARE,
    dataset: {},
  };
  const options = [opt];
  const select = {
    id: 'modelSelect',
    options,
    value,
    selectedOptions: [opt],
    __groupProvider: groupProvider || PROVIDER,
  };
  return group, select;
}

// _getOptionProviderId walks up to the optgroup; make that reachable.
function withGroups(select) {
  select.options[0].parentElement = { tagName: 'OPTGROUP', dataset: { provider: select.__groupProvider || PROVIDER } };
  return select;
}

const catalog = withGroups(catalogSelect(QUALIFIED));
const state = _modelStateForSelect(catalog, QUALIFIED);
const captured = _captureModelDropdownSelection(catalog);

// A session created under a different provider must not pin the send.
globalThis.S = { session: { model: BARE, model_provider: 'openai-codex' } };
const sendProvider = _modelProviderForSend(QUALIFIED);

// The picker's own current selection is the newer intent.
const picker = withGroups(catalogSelect(QUALIFIED));
const $ = function () { return picker; };
globalThis.$ = $;
const sendProviderWithPicker = _modelProviderForSend(QUALIFIED);

// #1771 negative control: a qualified id whose prefix belongs to a DIFFERENT
// provider than the option's own metadata is a real namespace, not a
// duplication — it must survive with the prefix intact. Built as the
// configured-default fallback shape (option absent from the catalog), which
// is the state that regressed when the strip was broadened.
globalThis.window = { _defaultModel: '@safe:gpt-4o-mini' };
const namespaceSelect = withGroups(catalogSelect('@safe:gpt-4o-mini'));
const namespaceState = _modelStateForSelect(namespaceSelect, '@safe:gpt-4o-mini');

// The same namespace with the option ABSENT from the catalog: routedProvider
// is empty, so the strip may only fire through the value's own explicit
// provider. A namespace prefix still must not be stripped on the strength of
// the value alone — the broad-strip regression surfaced exactly here.
globalThis.window = {};
const orphanSelect = {
  id: 'modelSelect',
  options: [],
  value: '@safe:gpt-4o-mini',
  selectedOptions: [],
  __groupProvider: PROVIDER,
};
const orphanState = _modelStateForSelect(orphanSelect, '@safe:gpt-4o-mini');

// The configured default is NOT a catalog group option even when its prefix
// happens to match the routed group provider: stripping it would rewrite the
// account's standing default.
const routedGroup = withGroups(catalogSelect('@' + PROVIDER + ':' + BARE));
const groupDefault = withGroups(catalogSelect('@' + PROVIDER + ':claude-sonnet-5[1m]'));
globalThis.window = { _defaultModel: '@' + PROVIDER + ':claude-sonnet-5[1m]' };
const configuredDefaultState = _modelStateForSelect(groupDefault, '@' + PROVIDER + ':claude-sonnet-5[1m]');

// #6221 colon-safety: the prefix comes from the option metadata, never from a
// last-colon re-parse, so a colon-bearing model id strips from the right edge.
const colonSelect = withGroups(catalogSelect('@custom:backup:model-a:free', 'custom:backup'));
const colonState = _modelStateForSelect(colonSelect, '@custom:backup:model-a:free');
globalThis.window = {};

process.stdout.write(JSON.stringify({
  stored_model: state.model,
  stored_provider: state.model_provider,
  captured,
  send_provider_from_stale_session: sendProvider,
  send_provider_when_picker_takes_priority: sendProviderWithPicker,
  namespace_model: namespaceState.model,
  namespace_provider: namespaceState.model_provider,
  orphan_namespace_model: orphanState.model,
  orphan_namespace_provider: orphanState.model_provider,
  configured_default_model: configuredDefaultState.model,
  configured_default_provider: configuredDefaultState.model_provider,
  colon_model: colonState.model,
  colon_provider: colonState.model_provider,
}));
"""
)


def _run_driver() -> dict:
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_qualified_catalog_id_is_split_into_a_bare_model_and_provider():
    """Defect 2: selecting ``@provider:model[suffix]`` must store the bare name."""
    result = _run_driver()

    assert result["stored_model"] == "claude-sonnet-5[1m]", (
        "the model name must not keep the @provider: prefix — the provider "
        "would be specified twice and the upstream 404s"
    )
    assert result["stored_provider"] == "claude-subscription-directsdk-experimental"
    assert result["captured"] == {
        "model": "claude-sonnet-5[1m]",
        "model_provider": "claude-subscription-directsdk-experimental",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_session_provider_does_not_hijack_a_later_selection():
    """Defect 1: a later picker selection must override a stale session provider.

    The session was created under openai-codex; the picker now shows a Claude
    catalog option. Sending must go to the Claude provider — the stale value is
    what produced the misleading "Codex quota exhausted (429)".
    """
    result = _run_driver()

    assert result["send_provider_from_stale_session"] == (
        "claude-subscription-directsdk-experimental"
    ), "a stale session provider must not pin a model to the provider it never belonged to"
    assert result["send_provider_when_picker_takes_priority"] == (
        "claude-subscription-directsdk-experimental"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_foreign_namespace_prefix_is_preserved():
    """#1771: a qualified id from a DIFFERENT provider namespace is kept intact.

    Broadening the strip to every provider would silently re-route a genuine
    ``@safe:`` namespace selection to the routed group's provider, so the
    prefix must stay part of the model id.
    """
    result = _run_driver()

    assert result["namespace_model"] == "@safe:gpt-4o-mini", (
        "a qualified id whose prefix is not the option's own provider is a "
        "real provider namespace (#1771) and must be preserved verbatim"
    )
    assert result["namespace_provider"] == "claude-subscription-directsdk-experimental"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_orphan_namespace_value_is_not_stripped_on_the_value_alone():
    """#1771 + #6884: an unmatched namespace value keeps its prefix.

    The option is not in the catalog, so no option metadata backs the prefix.
    A custom qualified id is still stripped there (the #6884 value-encoded
    variant), but a *foreign* namespace prefix is never justification enough
    by itself — otherwise the broad strip silently rewrites ``@safe:`` values.
    """
    result = _run_driver()

    assert result["orphan_namespace_model"] == "@safe:gpt-4o-mini", (
        "an unmatched value whose prefix is not the routed provider's is a "
        "real namespace and must keep its prefix (#1771)"
    )
    assert result["orphan_namespace_provider"] == "safe"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_configured_default_keeps_its_qualifying_prefix():
    """The account default is not a catalog option, so its prefix stays.

    ``window._defaultModel`` is the standing default of the active provider —
    the session model used on a missing/unknown-model fallback. Its prefix
    matches the routed group provider only by coincidence, so it must not be
    stripped like a duplicated catalog value.
    """
    result = _run_driver()

    assert result["configured_default_model"] == (
        "@claude-subscription-directsdk-experimental:claude-sonnet-5[1m]"
    ), "the configured default must keep its @provider: prefix"
    assert result["configured_default_provider"] == (
        "claude-subscription-directsdk-experimental"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_colon_bearing_model_id_strips_from_the_metadata_boundary():
    """#6221: a colon inside the model name must not confuse the strip.

    The prefix is taken from the option's authoritative provider metadata, so
    ``@custom:backup:model-a:free`` keeps ``model-a:free`` instead of being
    re-parsed down to ``free``.
    """
    result = _run_driver()

    assert result["colon_model"] == "model-a:free"
    assert result["colon_provider"] == "custom:backup"
