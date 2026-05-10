"""
Regression tests for issue #1771: switching sessions with missing/stale model
metadata must not leave the composer model picker on the previously viewed
chat's model.

These tests execute the real static/ui.js syncTopbar() path in Node with a tiny
DOM/select shim so the behavioral contract is protected without needing a full
browser harness.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name, opts = {}) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) {
    if (opts.optional) return '';
    throw new Error(name + ' not found');
  }
  let i = ui.indexOf('{', start);
  let depth = 1;
  i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}

const calls = {syncModelChip: 0, renderModelDropdown: 0, positionModelDropdown: 0, fetches: []};
let modelSelect;
let dropdownOpen = false;
const dropdown = {classList: {contains: (name) => name === 'open' && dropdownOpen}};

function makeSelect(options, initialValue) {
  const sel = {id: 'modelSelect', options: [], selectedIndex: -1, selectedOptions: []};
  Object.defineProperty(sel, 'value', {
    get() { return this._value || ''; },
    set(v) {
      this._value = v;
      const idx = this.options.findIndex(o => o.value === v);
      this.selectedIndex = idx;
      this.selectedOptions = idx >= 0 ? [this.options[idx]] : [];
    }
  });
  sel.querySelector = function(_selector) { return this.options[0] || null; };
  for (const item of options) {
    const group = {tagName: 'OPTGROUP', dataset: {provider: item.provider || ''}};
    const opt = {value: item.value, textContent: item.label || item.value, parentElement: group, dataset: {}};
    sel.options.push(opt);
  }
  sel.value = initialValue || '';
  return sel;
}

function $(id) {
  if (id === 'modelSelect') return modelSelect;
  if (id === 'composerModelDropdown') return dropdown;
  return {textContent: '', style: {}, classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}}, appendChild(){}, appendChildNode(){}};
}
function t(key) { return key; }
function syncModelChip() { calls.syncModelChip++; }
function renderModelDropdown() { calls.renderModelDropdown++; }
function _positionModelDropdown() { calls.positionModelDropdown++; }
function syncAppTitlebar() {}
function syncWorkspaceDisplays() {}
function syncReasoningChip() {}
function syncToolsetsChip() {}
function syncTerminalButton() {}
function _syncHermesPanelSessionActions() {}
function _latestGatewayRoutingForSession() { return null; }
function getModelLabel(v) { return v; }
function _formatGatewayModelLabel(_v, text) { return text; }
const _liveModelFetchPending = new Set();
const document = {
  title: '',
  baseURI: 'http://127.0.0.1/hermes/',
  createElement(tag) { return {tagName: tag.toUpperCase(), className: '', textContent: '', appendChild(){}}; },
  createTextNode(text) { return {textContent: text}; },
};
const window = { _botName: 'Hermes', _defaultModel: null, _activeProvider: null };
function fetch(url, opts) { calls.fetches.push({url: String(url), body: opts && opts.body || ''}); return Promise.resolve({ok: true}); }

for (const name of [
  '_getOptionProviderId', '_providerFromModelValue', '_modelStateForSelect',
  '_findModelInDropdown', '_refreshOpenModelDropdown', '_applyModelToDropdown',
  '_modelStateFromAppliedDropdown', '_persistSessionModelCorrection',
  '_applySessionModelFallback', 'syncTopbar'
]) {
  const src = extractFunc(name, {optional: name !== 'syncTopbar'});
  if (src) eval(src);
}

const args = JSON.parse(process.argv[3]);
modelSelect = makeSelect(args.options, args.initialValue);
dropdownOpen = !!args.dropdownOpen;
window._defaultModel = args.defaultModel || null;
window._activeProvider = args.activeProvider || null;
var S = {
  session: {
    session_id: 'session-b',
    id: 'session-b',
    title: 'Session B',
    model: args.sessionModel,
    model_provider: args.sessionProvider || null,
    messages: [],
    _modelResolutionDeferred: !!args.modelResolutionDeferred,
  },
  messages: [],
  activeProfile: 'default',
};

syncTopbar();

process.stdout.write(JSON.stringify({
  selectValue: modelSelect.value,
  sessionModel: S.session.model,
  sessionProvider: S.session.model_provider,
  calls,
}));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("issue1771_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run_sync(
    driver_path,
    *,
    session_model,
    initial_value="@expensive:gpt-5.5",
    default_model="@safe:gpt-4o-mini",
    dropdown_open=False,
    model_resolution_deferred=False,
):
    payload = {
        "sessionModel": session_model,
        "sessionProvider": None,
        "initialValue": initial_value,
        "defaultModel": default_model,
        "activeProvider": "safe",
        "dropdownOpen": dropdown_open,
        "modelResolutionDeferred": model_resolution_deferred,
        "options": [
            {
                "provider": "expensive",
                "value": "@expensive:gpt-5.5",
                "label": "GPT-5.5",
            },
            {"provider": "safe", "value": "@safe:gpt-4o-mini", "label": "GPT-4o mini"},
        ],
    }
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    return json.loads(result.stdout)


def test_sync_topbar_missing_model_falls_back_to_configured_default_not_previous_chat(
    driver_path,
):
    got = _run_sync(driver_path, session_model="")

    assert got["selectValue"] == "@safe:gpt-4o-mini"
    assert got["sessionModel"] == "@safe:gpt-4o-mini"
    assert got["sessionProvider"] == "safe"
    assert got["selectValue"] != "@expensive:gpt-5.5"


def test_sync_topbar_unknown_model_falls_back_to_configured_default_not_first_option(
    driver_path,
):
    got = _run_sync(driver_path, session_model="unknown")

    assert got["selectValue"] == "@safe:gpt-4o-mini"
    assert got["sessionModel"] == "@safe:gpt-4o-mini"
    assert got["sessionProvider"] == "safe"


def test_sync_topbar_rerenders_open_visible_model_dropdown_after_session_model_change(
    driver_path,
):
    got = _run_sync(driver_path, session_model="", dropdown_open=True)

    assert got["selectValue"] == "@safe:gpt-4o-mini"
    assert got["calls"]["renderModelDropdown"] >= 1
    assert got["calls"]["positionModelDropdown"] >= 1


def test_sync_topbar_does_not_persist_correction_while_model_resolution_deferred(
    driver_path,
):
    """Regression for stage-310 Opus review: the !hasSessionModel branch must
    skip the network write + state mutation while sessions.js has set
    _modelResolutionDeferred=true between the fast-path session render and
    the resolve_model=1 round-trip.

    Without this guard, every fast-path session view of an empty/unknown-model
    session fires a /api/session/update POST that races _resolveSessionModelForDisplaySoon
    and thrashes imported/read-only CLI sessions whose model field reads "unknown"
    (#1778 introduced exactly that surface in v0.51.16).
    """
    got_empty = _run_sync(driver_path, session_model="", model_resolution_deferred=True)
    # Visible UX still happens (sel.value gets the safe default) ...
    assert got_empty["selectValue"] == "@safe:gpt-4o-mini"
    # ... but session state is NOT mutated and NO POST is issued.
    assert got_empty["sessionModel"] == "", (
        "S.session.model must not be mutated while resolution is deferred"
    )
    update_calls = [
        c
        for c in got_empty["calls"]["fetches"]
        if "session" in c["url"] and "update" in c["url"]
    ]
    assert update_calls == [], (
        f"no /api/session/update POSTs while deferred (got {update_calls})"
    )

    got_unknown = _run_sync(
        driver_path, session_model="unknown", model_resolution_deferred=True
    )
    assert got_unknown["selectValue"] == "@safe:gpt-4o-mini"
    assert got_unknown["sessionModel"] == "unknown"
    update_calls_u = [
        c
        for c in got_unknown["calls"]["fetches"]
        if "session" in c["url"] and "update" in c["url"]
    ]
    assert update_calls_u == [], (
        "imported/read-only CLI session with model=unknown must not be silently written"
    )
