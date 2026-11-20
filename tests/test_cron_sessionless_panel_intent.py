"""Behavioural coverage for the sessionless cron notification's panel intent.

#7652 review round 4: routing a sessionless completion to the app root was not
enough. On a plain root load, boot restores ``localStorage['hermes-webui-session']``
— the last chat the user had open — so clicking the alert about a cron run
opened the chat instead of the Tasks panel the run belongs to. The fix carries an
explicit panel intent (``?panel=tasks``) in the URL and has boot honor it *ahead
of* the saved-chat restore.

These are executed tests, not source-string assertions: the intent parser and
its consumer are the real functions from ``static/sessions.js``, and the boot
ordering is asserted by running the real boot branch from ``static/boot.js``
against stubs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = REPO / "static" / "sessions.js"
BOOT_JS_PATH = REPO / "static" / "boot.js"
MESSAGES_JS_PATH = REPO / "static" / "messages.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# The boot branch the PR added, sliced by markers so the test tracks the real
# code (a rename or a move out of the pre-restore position fails here).
_BOOT_BRANCH_START = "const panelIntent=(typeof _panelQueryIntentFromLocation"
_BOOT_BRANCH_END = "const _profileQueryBlocksSavedLocal="


def _boot_branch() -> str:
    """The panel-intent branch as it appears in static/boot.js (without the
    `return` statement, which the harness supplies through a wrapper)."""
    src = BOOT_JS_PATH.read_text(encoding="utf-8")
    start = src.find(_BOOT_BRANCH_START)
    end = src.find(_BOOT_BRANCH_END)
    assert start != -1, "boot.js lost the panel-intent branch"
    assert end != -1 and end > start, "boot.js lost the block the branch precedes"
    return src[start:end]


def _run_node(body: str) -> dict:
    """Run a harness script from a temp file: the JS sources are read with fs
    rather than passed through argv, which would blow the arg-length limit."""
    harness = (
        "const fs = require('fs');\n"
        "const SESSIONS_SRC = fs.readFileSync(process.env.SESSIONS_JS_PATH, 'utf8');\n"
        + body
    )
    script = REPO / ".tmp-panel-intent-harness.cjs"
    script.write_text(harness, encoding="utf-8")
    try:
        result = subprocess.run(
            [NODE, str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO),
            env={**os.environ, "SESSIONS_JS_PATH": str(SESSIONS_JS_PATH)},
        )
        assert result.returncode == 0, f"node harness failed: {result.stderr}"
        return json.loads(result.stdout)
    finally:
        script.unlink(missing_ok=True)


_EXTRACT_FN_JS = """
function extractFn(name, src) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('missing function ' + name);
  let depth = 0, i = src.indexOf('(', start);
  for (; i < src.length; i++) {
    if (src[i] === '(') depth++;
    else if (src[i] === ')') { depth--; if (depth === 0) break; }
  }
  const brace = src.indexOf('{', i);
  depth = 0;
  for (i = brace; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('function did not close: ' + name);
}
function evalFn(name) { globalThis[name] = (0, eval)('(' + extractFn(name, SESSIONS_SRC) + ')'); }
// #7652 review round 5: _panelQueryIntentFromLocation now validates the name
// against the real panel set, so the helper it calls has to be evaluated too —
// otherwise every parse falls into the catch and reports "no intent".
function evalIntentFns() { evalFn('_knownPanelNames'); evalFn('_panelQueryIntentFromLocation'); }
"""

# Runs the REAL boot panel branch with the last chat still in localStorage and
# every boot collaborator stubbed, then reports what boot actually did. The
# branch source is injected with .replace() so this stays a plain (non-f)
# string and no JS brace ever needs escaping.
_HONOUR_STUB_JS = """
const branch = __BRANCH__;
const calls = [];
const switchPanelCalls = [];
global.S = { session: null, _bootReady: false };
global.syncTopbar = () => { calls.push('syncTopbar'); };
global.syncWorkspacePanelState = () => { calls.push('syncWorkspacePanelState'); };
global.renderSessionList = async () => { calls.push('renderSessionList'); };
global.startGatewaySSE = () => { calls.push('startGatewaySSE'); };
global._finalizeComposerPrefillOnBoot = async () => { calls.push('prefill'); };
global.switchPanel = async (name) => { switchPanelCalls.push(name); calls.push('switchPanel:' + name); };
global.localStorage = {
  store: { 'hermes-webui-session': 'last-open-chat' },
  getItem(k) { return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null; },
  setItem(k, v) { this.store[k] = String(v); },
  removeItem(k) { delete this.store[k]; },
};
global.window = {
  location: { search: '?panel=tasks', href: 'https://x.test/?panel=tasks' },
  history: { replaceState(state, title, url) {
    window.location.search = url.startsWith('?') ? url : url.slice(url.indexOf('?'));
    window.location.href = 'https://x.test/' + url;
  } },
};
evalIntentFns();
evalFn('_consumePanelQueryParamFromLocation');

(async () => {
  const urlSession = null;
  const prefillIntent = null;
  // #7652 review round 5: the branch only *decides* now. It hands the honored
  // panel to the restore path, which applies it after the saved session and its
  // in-flight recovery have run. An early `return` (the round-4 shape) never
  // reaches this read, so the intent reads back as null and the ordering
  // assertion below fails on behavior rather than on a ReferenceError.
  const NL = String.fromCharCode(10);
  const declaredIntent = await eval(
    '(async () => { ' + branch + NL
    + 'return (typeof pendingPanelIntent === "undefined") ? undefined : pendingPanelIntent; })()'
  );
  if (declaredIntent) {
    const saved = localStorage.getItem('hermes-webui-session');
    if (saved) {
      calls.push('loadSession:' + saved);
      S.session = { session_id: saved };
      S._bootReady = true;
      syncTopbar(); syncWorkspacePanelState();
      await renderSessionList();
      startGatewaySSE();
      calls.push('checkInflight:' + saved);
    }
    await switchPanel(declaredIntent);
  }
  console.log(JSON.stringify({
    calls,
    switchPanelCalls,
    declaredIntent: declaredIntent === undefined ? null : declaredIntent,
    bootReady: S._bootReady,
    restoredSession: S.session ? S.session.session_id : null,
    remainingSearch: window.location.search,
    savedSessionUntouched: localStorage.getItem('hermes-webui-session'),
  }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""

# Same stubs, but a URL session owns the view: the panel branch must decline.
_SESSION_STUB_JS = """
const branch = __BRANCH__;
global.S = { session: null, _bootReady: false };
global.syncTopbar = () => {};
global.syncWorkspacePanelState = () => {};
global.renderSessionList = async () => {};
global.startGatewaySSE = () => {};
global._finalizeComposerPrefillOnBoot = async () => {};
const switchPanelCalls = [];
global.switchPanel = async (name) => { switchPanelCalls.push(name); };
global.localStorage = {
  store: {},
  getItem() { return null; },
  setItem(k, v) {},
  removeItem(k) {},
};
// A session deep link: the panel intent is present but a session owns the view.
global.window = {
  location: { search: '?panel=tasks', href: 'https://x.test/session/abc?panel=tasks' },
  history: { replaceState(state, title, url) {
    window.location.search = url.startsWith('?') ? url : url.slice(url.indexOf('?'));
  } },
};
evalIntentFns();
evalFn('_consumePanelQueryParamFromLocation');

(async () => {
  const urlSession = 'abc';
  const prefillIntent = null;
  await eval('(async () => { ' + branch + ' })()');
  console.log(JSON.stringify({ switchPanelCalls, remainingSearch: window.location.search }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_panel_intent_parses_valid_panel_name():
    out = _run_node(
        _EXTRACT_FN_JS
        + """
global.window = { location: { search: '?panel=tasks' } };
evalIntentFns();
console.log(JSON.stringify(_panelQueryIntentFromLocation()));
"""
    )
    assert out == {"hasParam": True, "valid": True, "name": "tasks"}


def test_panel_intent_rejects_malformed_and_absent_values():
    out = _run_node(
        _EXTRACT_FN_JS
        + """
evalIntentFns();
function read(search) {
  global.window = { location: { search } };
  return _panelQueryIntentFromLocation();
}
const result = {
  absent: read(''),
  slashy: read('?panel=../../etc'),
  emptyVal: read('?panel='),
  chat: read('?panel=chat'),
};
console.log(JSON.stringify(result));
"""
    )
    # A value that cannot be a rendered panel must not be treated as an intent.
    assert out["slashy"] == {"hasParam": True, "valid": False, "name": "../../etc"}
    assert out["emptyVal"] == {"hasParam": True, "valid": False, "name": ""}
    # No parameter at all means "no intent", not "invalid intent".
    assert out["absent"] == {"hasParam": False, "valid": False, "name": ""}
    # 'chat' parses as valid (it is the default view) but boot ignores it.
    assert out["chat"] == {"hasParam": True, "valid": True, "name": "chat"}


def test_consuming_the_panel_param_strips_only_that_param():
    out = _run_node(
        _EXTRACT_FN_JS
        + """
const applied = [];
global.window = {
  location: { search: '?panel=tasks&q=hello', href: 'https://x.test/?panel=tasks&q=hello' },
  history: { replaceState(state, title, url) { applied.push(url); } },
};
evalFn('_consumePanelQueryParamFromLocation');
_consumePanelQueryParamFromLocation();
console.log(JSON.stringify({ applied }));
"""
    )
    # The other query params survive: the prefill `q=` is consumed by its own
    # helper later, and a panel intent must not eat it here.
    assert out["applied"] == ["/?q=hello"], (
        "the panel param must be dropped while the prefill param survives"
    )


def test_boot_honours_panel_intent_for_a_sessionless_cron_notification():
    """The intent must be honored, but as an overlay — not a replacement. The
    real boot branch runs with the last chat still in localStorage: it consumes
    the intent and hands it to the restore path, which restores that chat and
    its in-flight recovery first, then shows Tasks on top.

    Round 4 switched the panel inside the branch and returned, so the chat the
    user had open — and any live stream still running in it — stayed detached.
    """
    branch = _boot_branch()
    out = _run_node(
        _EXTRACT_FN_JS + _HONOUR_STUB_JS.replace("__BRANCH__", json.dumps(branch))
    )
    assert out["declaredIntent"] == "tasks", (
        "the panel intent must be handed to the restore path so the click lands "
        "on the panel the run belongs to"
    )
    assert out["switchPanelCalls"] == ["tasks"], (
        "the panel intent must land the user on the Tasks panel"
    )
    # The ordering is the fix: restore first, in-flight recovery next, panel last.
    assert out["restoredSession"] == "last-open-chat", (
        "the chat the user had open must still be restored under the panel"
    )
    calls = out["calls"]
    assert calls.index("loadSession:last-open-chat") < calls.index(
        "checkInflight:last-open-chat"
    ) < calls.index("switchPanel:tasks"), (
        "the saved-session restore and its in-flight recovery must both run "
        "before Tasks is shown, or the user's chat is left detached"
    )
    assert out["bootReady"] is True
    # The intent is consumed so a refresh does not re-run the panel launch, and
    # the saved chat is left alone for the next plain boot.
    assert "panel" not in out["remainingSearch"], (
        "the panel param must be consumed so a reload does not re-trigger"
    )
    assert out["savedSessionUntouched"] == "last-open-chat"


def test_boot_ignores_a_panel_intent_when_the_url_names_a_session():
    """A deep link that already carries a session must keep restoring it: the
    panel intent is for sessionless surfaces only."""
    branch = _boot_branch()
    out = _run_node(
        _EXTRACT_FN_JS + _SESSION_STUB_JS.replace("__BRANCH__", json.dumps(branch))
    )
    assert out["switchPanelCalls"] == [], (
        "a URL session must keep its own restore instead of being diverted"
    )
    assert "panel=tasks" in out["remainingSearch"], (
        "the param must not be consumed when the intent was not honored"
    )
