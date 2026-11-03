"""#7860 defect 1: `_modelProviderForSend` must prefer the selected model's
provider over a stale `S.session.model_provider`.

Selecting a model from a non-default provider stores the session's model name
but leaves `session.model_provider` at the account default; every send then
routes the picked model through the old provider (429 / model-not-found).
The send path consults `_modelProviderForSend()`, which currently returns the
stale session provider before even looking at the dropdown the user just
changed.

Round 2 (#7865, maintainer): the dropdown's provider may only win over a loaded
session's provider when the picker's session-scoped "explicit pick" evidence
exists. A bare dropdown match is not evidence — after a session restore the
catalog repaint can leave another provider's identically-valued option selected
(e.g. `gpt-5.5` under both OpenAI and OpenAI Codex), which must NOT hijack the
provider the session record holds.

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
             '_modelStateForSelect', '_pickerExplicitPickKey',
             '_readExplicitPickerPick', '_rememberExplicitPickerPick',
             '_clearExplicitPickerPick', '_modelProviderForSend']
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
var _sessionStore = new Map();
global.sessionStorage = {
  getItem: k => (_sessionStore.has(k) ? _sessionStore.get(k) : null),
  setItem: (k, v) => { _sessionStore.set(k, String(v)); },
  removeItem: k => { _sessionStore.delete(k); },
};
global.window = {};
// ui.js reads S.session.<field> — wrap the scenario session object.
globalThis.__MOCK_S = { session: cfg.session || null };
// #7865 round-2: seed the session-scoped explicit-pick evidence exactly like
// the picker's change handler would (keyed by the ACTIVE session id).
if (cfg.explicitPick && cfg.session && cfg.session.session_id) {
  // explicitPickForSession lets a scenario attach the evidence to a DIFFERENT
  // session id than the active one, to prove the marker never leaks.
  const _pickSid = cfg.explicitPickForSession || cfg.session.session_id;
  _rememberExplicitPickerPick(_pickSid,
    cfg.explicitPick.value, cfg.explicitPick.model_provider);
}
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
            session={"session_id": "s1", "model": "gpt-4o", "model_provider": "openai-codex"},
            dropdown={
                "value": "claude-opus-5-5",
                "dataProvider": "claude-subscription-directsdk-experimental",
            },
            explicitPick={
                "value": "claude-opus-5-5",
                "model_provider": "claude-subscription-directsdk-experimental",
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

    def test_no_session_uses_dropdown_provider_when_picked(self):
        res = _run(_scenario(
            model="claude-sonnet-5",
            session=None,
            dropdown={
                "value": "claude-sonnet-5",
                "dataProvider": "claude-subscription-directsdk-experimental",
            },
        ))
        # No active session → no session provider to preserve → the dropdown's
        # provider is the only candidate (master parity for the empty composer).
        assert res == "claude-subscription-directsdk-experimental"

    def test_no_dropdown_no_session_returns_empty(self):
        res = _run(_scenario(model="some-model", session=None, dropdown=None))
        assert res == ""


class TestRestoredSessionKeepsItsOwnProviderWithoutExplicitPick:
    """#7865 round-2 (maintainer CORE fix): a matching dropdown option may NOT
    override a loaded session's provider unless the picker's session-scoped
    explicit-pick evidence says the user picked AFTER this session loaded.

    The real-world state: two providers offer the same bare id (`gpt-5.5` under
    both OpenAI and OpenAI Codex). A session restore runs `syncTopbar()` before
    the catalog refresh (`static/sessions.js` ~2535), so another provider's
    identically-valued option can be left selected while the session correctly
    holds `model_provider: "openai"`. Letting the dropdown win there sends the
    turn to a provider the user never picked.
    """

    def test_partial_catalog_restore_keeps_session_provider(self):
        # The exact case the reviewer named: session model gpt-5.5 on openai,
        # dropdown's selected gpt-5.5 option belongs to openai-codex, no pick.
        res = _run(_scenario(
            model="gpt-5.5",
            session={"session_id": "s1", "model": "gpt-5.5", "model_provider": "openai"},
            dropdown={"value": "gpt-5.5", "dataProvider": "openai-codex"},
        ))
        assert res == "openai"

    def test_explicit_pick_for_this_session_lets_dropdown_win(self):
        # Same shape, but WITH the picker evidence → dropdown provider wins
        # (that is the #7860 fix: a real pick must beat a stale session field).
        res = _run(_scenario(
            model="gpt-5.5",
            session={"session_id": "s1", "model": "gpt-5.5", "model_provider": "openai"},
            dropdown={"value": "gpt-5.5", "dataProvider": "openai-codex"},
            explicitPick={"value": "gpt-5.5", "model_provider": "openai-codex"},
        ))
        assert res == "openai-codex"

    def test_explicit_pick_from_another_session_does_not_authorize_override(self):
        # Evidence belongs to a different session id than the active one → it
        # must not leak an override across sessions.
        res = _run(_scenario(
            model="gpt-5.5",
            session={"session_id": "s2", "model": "gpt-5.5", "model_provider": "openai"},
            dropdown={"value": "gpt-5.5", "dataProvider": "openai-codex"},
            explicitPickForSession="s1",
            explicitPick={"value": "gpt-5.5", "model_provider": "openai-codex"},
        ))
        assert res == "openai"

    def test_stale_explicit_pick_for_a_different_value_does_not_authorize(self):
        # A pick recorded for another model value must not authorize an
        # override for the model actually being sent.
        res = _run(_scenario(
            model="gpt-5.5",
            session={"session_id": "s1", "model": "gpt-5.5", "model_provider": "openai"},
            dropdown={"value": "gpt-5.5", "dataProvider": "openai-codex"},
            explicitPick={"value": "gpt-4o", "model_provider": "openai-codex"},
        ))
        assert res == "openai"

    def test_qualified_model_branch_unaffected_without_any_pick(self):
        # The explicit @provider:model branch at the top is authoritative and
        # needs no picker evidence at all (maintainer: "fine as is").
        res = _run(_scenario(
            model="@openai:gpt-5.5",
            session={"session_id": "s1", "model": "gpt-5.5", "model_provider": "openai-codex"},
            dropdown={"value": "gpt-5.5", "dataProvider": "openai"},
        ))
        assert res == "openai"


class TestStaleMarkerAfterReloadCannotAuthorizeWrongProvider:
    """#7865 round-3 (maintainer CORE fix): the explicit-pick marker lives in
    `sessionStorage`, so it survives a page reload. Two halves must hold:

    1. `_modelProviderForSend` only lets the dropdown override when the
       pick's recorded provider equals the selected option's provider. A stale
       marker must not authorize a DIFFERENT provider's identically-valued
       option (the catalog repaint on restore can leave one selected).
    2. `loadSession` clears the marker of the session being LOADED, not just
       the one being left — on a fresh boot `S.session` is null, so
       `currentSid` is null and the restored session's marker was never
       cleared (the reload half of the same defect).
    """

    def test_stale_marker_with_mismatched_provider_does_not_authorize(self):
        # The marker's provider (openai) does NOT match the provider of the
        # option now selected (openai-codex), and the session's own provider is
        # a third one (anthropic). A stale marker must not authorize the
        # colliding option: the send must fall through to the session's own
        # provider. This is the exact shape the reviewer described — the same
        # `gpt-5.5` value offered by two providers, the pick never made for the
        # one the catalog repaint happened to leave selected.
        res = _run(_scenario(
            model="gpt-5.5",
            session={"session_id": "s1", "model": "gpt-5.5", "model_provider": "anthropic"},
            dropdown={"value": "gpt-5.5", "dataProvider": "openai-codex"},
            explicitPick={"value": "gpt-5.5", "model_provider": "openai"},
        ))
        assert res == "anthropic"

    def test_matching_marker_provider_still_authorizes_override(self):
        # Negative control for the test above: when the marker's provider DOES
        # match the selected option, the #7860 fix must still let the dropdown
        # win (otherwise round-3 over-closed the real fix).
        res = _run(_scenario(
            model="gpt-5.5",
            session={"session_id": "s1", "model": "gpt-5.5", "model_provider": "anthropic"},
            dropdown={"value": "gpt-5.5", "dataProvider": "openai-codex"},
            explicitPick={"value": "gpt-5.5", "model_provider": "openai-codex"},
        ))
        assert res == "openai-codex"


def test_driver_smoke():
    res = _run(_scenario(model="", session=None, dropdown=None))
    assert res == ""
