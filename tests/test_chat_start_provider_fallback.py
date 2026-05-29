"""Regression coverage for browser chat model-provider fallback.

The browser send path may fall back to the model dropdown for the model ID on a
fresh session. The provider must follow only when that dropdown/persisted state
describes the same model being sent.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")


def test_messages_payloads_use_model_tied_provider_helper():
    ui_src = UI_JS_PATH.read_text(encoding="utf-8")
    messages_src = MESSAGES_JS_PATH.read_text(encoding="utf-8")

    assert "function _modelProviderForSend" in ui_src
    assert "function _chatPayloadModelState" in messages_src
    assert "_modelProviderForSend(model)" in messages_src

    chat_start_idx = messages_src.find("api('/api/chat/start'")
    assert chat_start_idx >= 0, "could not find /api/chat/start POST in messages.js"
    payload_block = messages_src[chat_start_idx:chat_start_idx + 500]
    assert "model:_modelState.model" in payload_block
    assert "model_provider:_modelState.model_provider" in payload_block
    assert "model_provider:S.session.model_provider||null" not in payload_block

    for idx in [m.start() for m in __import__("re").finditer("queueSessionMessage\\(", messages_src)]:
        block = messages_src[idx:idx + 260]
        if "model_provider:" in block:
            assert "S.session.model_provider||null" not in block


_DRIVER_SRC = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
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

let modelSelect;
function $(id) { return id === 'modelSelect' ? modelSelect : null; }

function makeSelect(options, initialValue) {
  const sel = {options: [], selectedIndex: -1, selectedOptions: []};
  Object.defineProperty(sel, 'value', {
    get() { return this._value || ''; },
    set(v) {
      this._value = v;
      const idx = this.options.findIndex(o => o.value === v);
      this.selectedIndex = idx;
      this.selectedOptions = idx >= 0 ? [this.options[idx]] : [];
    }
  });
  for (const item of options) {
    const group = {tagName: 'OPTGROUP', dataset: {provider: item.provider || ''}};
    const opt = {value: item.value, parentElement: group, dataset: {}};
    if (item.optionProvider) opt.dataset.provider = item.optionProvider;
    sel.options.push(opt);
  }
  sel.value = initialValue || '';
  return sel;
}

const store = new Map();
const localStorage = {
  getItem(k) { return store.has(k) ? store.get(k) : null; },
  setItem(k, v) { store.set(k, String(v)); },
  removeItem(k) { store.delete(k); },
};
const MODEL_STATE_KEY = 'hermes-webui-model-state';

for (const name of [
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_modelStateForSelect',
  '_readPersistedModelState',
  '_modelProviderForSend',
]) {
  eval(extractFunc(name));
}

const args = JSON.parse(process.argv[3]);
modelSelect = makeSelect(args.options || [], args.initialValue || '');
if (args.persisted) localStorage.setItem(MODEL_STATE_KEY, JSON.stringify(args.persisted));
var S = {session: {model_provider: args.sessionProvider || null}};

process.stdout.write(JSON.stringify({provider: _modelProviderForSend(args.model)}));
"""

node_test = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("chat_provider_fallback_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run_helper(driver_path, payload):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)["provider"]


@node_test
def test_model_provider_for_send_preserves_session_provider(driver_path):
    provider = _run_helper(driver_path, {
        "model": "grok-4.3",
        "sessionProvider": "session-provider",
        "initialValue": "grok-4.3",
        "options": [{"provider": "xai-oauth", "value": "grok-4.3"}],
    })

    assert provider == "session-provider"


@node_test
def test_model_provider_for_send_falls_back_to_matching_dropdown(driver_path):
    provider = _run_helper(driver_path, {
        "model": "grok-4.3",
        "initialValue": "grok-4.3",
        "options": [{"provider": "xai-oauth", "value": "grok-4.3"}],
    })

    assert provider == "xai-oauth"


@node_test
def test_model_provider_for_send_does_not_steal_unrelated_dropdown_provider(driver_path):
    provider = _run_helper(driver_path, {
        "model": "grok-4.3",
        "initialValue": "claude-sonnet-4.6",
        "options": [
            {"provider": "anthropic", "value": "claude-sonnet-4.6"},
            {"provider": "xai-oauth", "value": "grok-4.3"},
        ],
    })

    assert provider is None


@node_test
def test_model_provider_for_send_uses_only_matching_persisted_state(driver_path):
    matching = _run_helper(driver_path, {
        "model": "grok-4.3",
        "initialValue": "",
        "persisted": {"model": "grok-4.3", "model_provider": "xai-oauth"},
    })
    unrelated = _run_helper(driver_path, {
        "model": "grok-4.3",
        "initialValue": "",
        "persisted": {"model": "claude-sonnet-4.6", "model_provider": "anthropic"},
    })

    assert matching == "xai-oauth"
    assert unrelated is None
