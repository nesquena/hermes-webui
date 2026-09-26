"""#7860 defect 1: `_modelProviderForSend` must prefer the selected model's
provider over a stale `S.session.model_provider`.

Selecting a model from a non-default provider stores the session's model name
but leaves `session.model_provider` at the account default; every send then
routes the picked model through the old provider (429 / model-not-found).
The send path consults `_modelProviderForSend()`, which currently returns the
stale session provider before even looking at the dropdown the user just
changed.

Drives the ACTUAL function from static/ui.js: the function (and its helpers)
are extracted via balanced-brace matching, their module-global references
(`S`, `$`) renamed to dedicated mock hooks, everything written to a temp JS
file as plain top-level code (no eval — the declared functions and `var`
mock bindings share one module scope) and executed under node.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync('__UI_JS_PATH__', 'utf8');

function extract(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(src);
  if (!m) throw new Error('function not found: ' + name);
  let i = src.indexOf('{', m.index);
  let depth = 0, j = i;
  for (; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(m.index, j + 1);
}

let funcs = ['_providerFromModelValue', '_getOptionProviderId',
             '_modelStateForSelect', '_modelProviderForSend']
  .map(extract).join('\n');

// Rename ui.js module globals (S, $) to explicit globalThis hooks so the
// extracted copy reads the scenario state below from any scope.
funcs = funcs
  .replace(/\bS\b(?![\w$])/g, 'globalThis.__MOCK_S')
  .replace(/\$(?!\w)/g, 'globalThis.__MOCK_DOLLAR');

const scenario = process.argv[2] || '{}';
const cfg = JSON.parse(scenario);

// Declared as plain top-level code (no eval): the extracted functions
// resolve globalThis.__MOCK_* at call time regardless of their own scope.
eval(funcs);

var _store = new Map();
global.localStorage = {
  getItem: k => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => { _store.set(k, String(v)); },
  removeItem: k => { _store.delete(k); },
};
global.window = {};
// ui.js reads S.session.<field> — wrap the scenario session object.
globalThis.__MOCK_S = { session: cfg.session || null };
globalThis.__MOCK_DOLLAR = () => null;
globalThis.MODEL_STATE_KEY = 'hermes-webui-model-state';
if (cfg.dropdown) {
  var opt = {
    value: cfg.dropdown.value,
    dataset: { model: (cfg.dropdown.dataModel || undefined),
               provider: (cfg.dropdown.dataProvider || undefined) },
  };
  globalThis.__MOCK_DOLLAR = id => id === 'modelSelect' ? {
    value: cfg.dropdown.value,
    selectedOptions: [opt],
    options: [opt],
  } : null;
}

const out = _modelProviderForSend(cfg.model);
process.stdout.write(String(out === null || out === undefined ? '' : out));
"""

_MOCK_SRC = r"""
var _store = new Map();
global.localStorage = {
  getItem: k => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => { _store.set(k, String(v)); },
  removeItem: k => { _store.delete(k); },
};
global.window = {};
var __MOCK_S = cfg.session || null;
var __MOCK_DOLLAR = () => null;
var MODEL_STATE_KEY = 'hermes-webui-model-state';
if (cfg.dropdown) {
  var opt = {
    value: cfg.dropdown.value,
    dataset: { model: (cfg.dropdown.dataModel || undefined),
               provider: (cfg.dropdown.dataProvider || undefined) },
  };
  __MOCK_DOLLAR = id => id === 'modelSelect' ? {
    value: cfg.dropdown.value,
    selectedOptions: [opt],
    options: [opt],
  } : null;
}
"""


def _run(scenario):
    driver = _DRIVER_SRC.replace("__UI_JS_PATH__", str(UI_JS_PATH))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(driver)
        tmp = f.name
    try:
        proc = subprocess.run(
            [str(NODE), tmp, json.dumps(scenario)],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        Path(tmp).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(f"node driver failed: {proc.stderr[:800]}")
    return proc.stdout.strip()


def _scenario(**kw):
    return kw


class TestSelectedModelProviderBeatsStaleSession:
    def test_picked_nondefault_model_uses_its_provider(self):
        res = _run(_scenario(
            model="claude-opus-5-5",
            session={"model": "gpt-4o", "model_provider": "openai-codex"},
            dropdown={
                "value": "claude-opus-5-5",
                "dataProvider": "claude-subscription-directsdk-experimental",
            },
        ))
        assert res == "claude-subscription-directsdk-experimental"

    def test_picked_qualified_custom_model_uses_its_provider(self):
        res = _run(_scenario(
            model="@custom:chimaera:claude-sonnet-5",
            session={"model": "gpt-5", "model_provider": "openai-codex"},
            dropdown={
                "value": "@custom:chimaera:claude-sonnet-5",
                "dataProvider": "custom:chimaera",
            },
        ))
        assert res == "custom:chimaera"

    def test_dropdown_mismatch_falls_back_to_session_provider(self):
        res = _run(_scenario(
            model="gpt-4o",
            session={"model": "gpt-4o", "model_provider": "openai-codex"},
            dropdown={"value": "claude-sonnet-5", "dataProvider": "claude-x"},
        ))
        assert res == "openai-codex"

    def test_no_session_uses_dropdown_provider(self):
        res = _run(_scenario(
            model="claude-sonnet-5",
            session=None,
            dropdown={
                "value": "claude-sonnet-5",
                "dataProvider": "claude-subscription-directsdk-experimental",
            },
        ))
        assert res == "claude-subscription-directsdk-experimental"

    def test_no_dropdown_no_session_returns_empty(self):
        res = _run(_scenario(model="some-model", session=None, dropdown=None))
        assert res == ""


def test_driver_smoke():
    res = _run(_scenario(model="", session=None, dropdown=None))
    assert res == ""
