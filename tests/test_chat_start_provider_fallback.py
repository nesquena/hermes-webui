"""Regression coverage for browser chat model-provider fallback.

The browser send path may fall back to the model dropdown for the model ID on a
fresh session. The provider must follow only when that dropdown/persisted state
describes the same model being sent.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")


def _function_source(source: str, marker: str) -> str:
    start = source.find(marker)
    assert start >= 0, f"{marker!r} not found"
    body_start = source.find("{", source.find(")", start))
    assert body_start >= 0, f"{marker!r} has no body"
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    i = body_start
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"{marker!r} body is not balanced")


def _balanced_call(source: str, marker: str, start: int = 0) -> tuple[str, int]:
    call_start = source.find(marker, start)
    assert call_start >= 0, f"{marker!r} not found"
    open_at = source.find("(", call_start)
    assert open_at >= 0, f"{marker!r} has no opening parenthesis"
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    for i in range(open_at, len(source)):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch == "/" and nxt == "/":
            line_comment = True
        elif ch == "/" and nxt == "*":
            block_comment = True
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[call_start : i + 1], i + 1
    raise AssertionError(f"{marker!r} call is not balanced")


def _calls(source: str, marker: str) -> list[str]:
    calls = []
    start = 0
    while True:
        marker_pos = source.find(marker, start)
        if marker_pos < 0:
            return calls
        call, start = _balanced_call(source, marker, marker_pos)
        calls.append(call)
    return calls


def test_messages_payloads_use_model_tied_provider_helper():
    ui_src = UI_JS_PATH.read_text(encoding="utf-8")
    messages_src = MESSAGES_JS_PATH.read_text(encoding="utf-8")

    assert "function _modelProviderForSend" in ui_src
    assert "function _chatPayloadModelState" in messages_src
    assert "_modelProviderForSend(model)" in messages_src

    send_src = _function_source(messages_src, "async function send(")
    snapshot = "const _submittedModelState={..._chatPayloadModelState()};"
    snapshot_pos = send_src.index(snapshot)
    upload_pos = send_src.index("await uploadPendingFiles")
    forced_skill_pos = send_src.index("await _pending.promise")
    chat_start_call, _ = _balanced_call(send_src, "await api('/api/chat/start'")
    chat_start_pos = send_src.index(chat_start_call)
    assert snapshot_pos < upload_pos < forced_skill_pos < chat_start_pos
    assert not re.search(r"_submittedModelState\.(?:model|model_provider)\s*=", send_src[snapshot_pos:])

    assert "model:_submittedModelState.model" in chat_start_call
    assert "model_provider:_submittedModelState.model_provider" in chat_start_call
    assert "_chatPayloadModelState()" not in chat_start_call
    assert "_modelState.model" not in chat_start_call
    assert "_modelState.model_provider" not in chat_start_call

    queue_calls = _calls(messages_src, "queueSessionMessage(")
    assert queue_calls, "messages.js must retain synchronous queue-path coverage"
    for queue_call in queue_calls:
        model = re.search(r"\bmodel:\s*([A-Za-z_$][\w$]*)\.model\b", queue_call)
        provider = re.search(r"\bmodel_provider:\s*([A-Za-z_$][\w$]*)\.model_provider\b", queue_call)
        assert model and provider, queue_call
        assert model.group(1) == provider.group(1), queue_call


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

if (args.mode === 'modelState') {
  process.stdout.write(JSON.stringify(_modelStateForSelect(modelSelect, args.model)));
} else {
  process.stdout.write(JSON.stringify({provider: _modelProviderForSend(args.model)}));
}
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
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)["provider"]


def _run_model_state_helper(driver_path, payload):
    payload = {"mode": "modelState", **payload}
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


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


@node_test
def test_model_state_reads_live_custom_provider_from_option_metadata(driver_path):
    state = _run_model_state_helper(driver_path, {
        "model": "llama-3.3-custom",
        "initialValue": "llama-3.3-custom",
        "options": [{
            "provider": "custom:lab",
            "optionProvider": "custom:lab",
            "value": "llama-3.3-custom",
        }],
    })

    assert state == {"model": "llama-3.3-custom", "model_provider": "custom:lab"}


@node_test
def test_model_state_does_not_use_unrelated_selected_option_provider(driver_path):
    # If the requested model is not in the options and differs from the currently
    # selected option, it should return model_provider = null instead of the selected option's provider.
    state = _run_model_state_helper(driver_path, {
        "model": "kilo/minimax/minimax-m3",
        "initialValue": "qwen3.6:27b-mlx",
        "options": [{
            "provider": "ollama",
            "optionProvider": "ollama",
            "value": "qwen3.6:27b-mlx",
        }],
    })

    assert state == {"model": "kilo/minimax/minimax-m3", "model_provider": None}



def test_live_custom_models_are_tagged_with_provider_metadata():
    ui_src = UI_JS_PATH.read_text(encoding="utf-8")
    start = ui_src.index("function _addLiveModelsToSelect")
    body = ui_src[start:ui_src.index("async function _fetchLiveModels", start)]

    assert "providerGroup.dataset.provider=provider;" in body
    assert "opt.dataset.provider=provider;" in body


def test_new_session_does_not_fallback_to_stale_named_custom_provider():
    sessions_src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = sessions_src.index("async function newSession(")
    body = sessions_src[start:sessions_src.index("const data=await api('/api/session/new'", start)]
    assignment = body[body.index("reqBody.model_provider="):].split(";", 1)[0]

    assert "const _fallbackIsNamedCustom=String(_fallbackProvider||'').toLowerCase().startsWith('custom:');" in body
    assert "newModelState.model_provider" in assignment
    assert "!_familyMismatch" in assignment
    assert "!_fallbackIsNamedCustom" in assignment
    assert "_fallbackProvider||null" in assignment
