"""Behavioural test for the #4650 reasoning-chip request-storm fix.

`syncTopbar()` calls `syncReasoningChip()` on every routine UI refresh, and
during streaming those fire at high frequency. Commit a9ce2889 made
`syncReasoningChip()` refetch `GET /api/reasoning` unconditionally, so ordinary
syncs became a network storm (one request per token -> ~2 tok/s).

The fix restores the pre-a9ce2889 cache short-circuit while keeping that
commit's intent (refresh supported-efforts after a model switch): fetch only
when nothing is cached yet OR the model/provider identity changed since the
last fetch. This test drives the ACTUAL functions from static/ui.js via node
and counts network calls, so the storm cannot silently come back.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function makeEl() {
  const attrs = {};
  return {
    style: {}, dataset: {}, title: '', textContent: '', value: '',
    classList: { add(){}, remove(){}, toggle(){}, contains(){return false} },
    setAttribute(name, value){ attrs[name] = String(value); },
    getAttribute(name){ return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null; },
    removeAttribute(name){ delete attrs[name]; },
    querySelectorAll(){return []}, querySelector(){return null},
    getBoundingClientRect(){return {left:0,top:0,width:0,height:0}},
  };
}

const els = {
  composerReasoningWrap: makeEl(),
  composerReasoningLabel: makeEl(),
  composerReasoningChip: makeEl(),
  composerReasoningDropdown: makeEl(),
  modelSelect: makeEl(),
};

// Mutable app state the reasoning helpers read from.
global.S = { session: { model: 'gpt-5', model_provider: 'openai' } };
global.window = {};
global.document = { createElement: makeEl, addEventListener(){}, querySelectorAll(){return []}, querySelector(){return null} };
global.$ = id => els[id] || null;

// Count every network call and remember the URL it hit.
let CALLS = [];
global.api = (url) => { CALLS.push(url); return { then: () => ({ catch: () => {} }), catch: () => {} }; };

// Helpers the reasoning code calls.
global._modelStateForSelect = () => ({ model: '', model_provider: null });
global._highlightReasoningOption = () => {};
global._applyReasoningOptions = () => {};

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++;
  }
  return src.slice(start, i);
}

// `let _currentReasoningEffort` / `_currentReasoningEffortsSupported` /
// `_lastReasoningFetchKey` are module-scope state the functions close over;
// declare them in this eval scope so the extracted functions can see them.
var _currentReasoningEffort = null;
var _currentReasoningEffortsSupported = null;
var _lastReasoningFetchKey = null;
var _reasoningFetchSeq = 0;

eval(extractFunc('_normalizeReasoningEffort'));
eval(extractFunc('_formatReasoningEffortLabel'));
eval(extractFunc('_reasoningEffortContext'));
eval(extractFunc('_reasoningEffortQuery'));
eval(extractFunc('_applyReasoningChip'));
eval(extractFunc('fetchReasoningChip'));
eval(extractFunc('syncReasoningChip'));

// ── Scenario ───────────────────────────────────────────────────────────────
const result = {};

// 0. COLD-CACHE in-flight burst: 10 syncs BEFORE the first GET resolves (no
//    _applyReasoningChip yet, so _currentReasoningEffort is still null). This is
//    the streaming storm's worst case — it must produce exactly ONE request,
//    relying on the optimistic key short-circuit, not on a resolved chip.
for (let i = 0; i < 10; i++) syncReasoningChip();
result.after_cold_inflight_burst = CALLS.length;

// 1. First sync with nothing cached -> must fetch.
syncReasoningChip();
result.after_first_sync = CALLS.length;

// Simulate the fetch resolving (what _applyReasoningChip does on response).
_applyReasoningChip('high', { supported_efforts: ['low','high'] });

// 2. Ten routine syncs with the SAME model (the streaming storm) -> no new fetch.
for (let i = 0; i < 10; i++) syncReasoningChip();
result.after_ten_same_model_syncs = CALLS.length;

// 3. Model switch -> exactly one more fetch.
global.S.session.model = 'claude-opus-4';
global.S.session.model_provider = 'anthropic';
syncReasoningChip();
result.after_model_switch = CALLS.length;

// 4. More routine syncs on the new model -> still no new fetch.
_applyReasoningChip('low', { supported_efforts: ['low','high'] });
for (let i = 0; i < 5; i++) syncReasoningChip();
result.after_new_model_syncs = CALLS.length;

// 5. OUT-OF-ORDER staleness guard: capture each fetch's success callback and
//    fire an OLDER fetch's response AFTER a newer dispatch. The stale response
//    must be ignored (generation guard), even though both share the same key
//    after a profile-switch-style cache reset. Swap in a capturing api() mock.
const HANDLERS = [];
global.api = (url) => ({
  then: (onOk) => { HANDLERS.push({ url, onOk }); return { catch: (onErr) => { HANDLERS[HANDLERS.length-1].onErr = onErr; } }; },
  catch: () => {},
});
_currentReasoningEffort = null; _lastReasoningFetchKey = null;
fetchReasoningChip();                 // dispatch #A (seq N)
fetchReasoningChip();                 // dispatch #B (seq N+1) — supersedes A
// Resolve the OLDER one (#A) last with one effort; must be ignored.
HANDLERS[0].onOk({ reasoning_effort: 'low', supported_efforts: ['low','high'] });
result.effort_after_stale_A = _currentReasoningEffort;   // expect null (ignored)
// Now resolve the NEWER one (#B) with a different effort; must apply.
HANDLERS[1].onOk({ reasoning_effort: 'high', supported_efforts: ['low','high'] });
result.effort_after_fresh_B = _currentReasoningEffort;   // expect 'high'
// A late FAILURE from the older dispatch must NOT hide the fresh chip.
if (HANDLERS[0].onErr) HANDLERS[0].onErr();
result.effort_after_stale_A_fail = _currentReasoningEffort; // expect still 'high'

result.calls = CALLS;
process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("reasoning_storm_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def outcome(driver_path):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_cold_inflight_burst_fetches_once(outcome):
    """The worst-case streaming storm: 10 syncs BEFORE the first GET resolves
    (cold cache, _currentReasoningEffort still null) must produce exactly ONE
    request. The optimistic key short-circuit — not a resolved chip — closes
    this in-flight window (#4650 review finding 2)."""
    assert outcome["after_cold_inflight_burst"] == 1, (
        "a cold-cache burst of syncs before the first response settles must fire "
        f"exactly one GET, not one-per-sync: {outcome['calls']}"
    )


def test_first_sync_fetches_once(outcome):
    assert outcome["after_first_sync"] == 1, (
        f"a further sync on the same in-flight key must not add a request: {outcome['calls']}"
    )


def test_repeated_syncs_same_model_do_not_refetch(outcome):
    """The core #4650 regression: 10 routine syncs on the same model must NOT
    issue 10 network calls — that was the per-token request storm."""
    assert outcome["after_ten_same_model_syncs"] == 1, (
        "routine topbar syncs on an unchanged model must serve the cache, not "
        f"refetch — request storm regression: {outcome['calls']}"
    )


def test_model_switch_triggers_one_refetch(outcome):
    """a9ce2889's intent must survive: switching models refreshes the chip's
    supported-efforts, so exactly one fetch fires on the switch."""
    assert outcome["after_model_switch"] == 2, (
        f"a model switch must trigger exactly one refetch: {outcome['calls']}"
    )


def test_syncs_after_switch_do_not_refetch(outcome):
    assert outcome["after_new_model_syncs"] == 2, (
        "routine syncs after a model switch must serve the cache again: "
        f"{outcome['calls']}"
    )


def test_stale_out_of_order_success_is_ignored(outcome):
    """#4650 review: an older in-flight /api/reasoning success that resolves AFTER
    a newer dispatch (same key, e.g. profile switch) must not poison the chip."""
    assert outcome["effort_after_stale_A"] is None, (
        "a superseded older fetch's success must be ignored (generation guard), "
        f"got {outcome['effort_after_stale_A']!r}"
    )
    assert outcome["effort_after_fresh_B"] == "high", (
        "the newest fetch's success must apply"
    )


def test_stale_out_of_order_failure_does_not_hide_fresh_chip(outcome):
    """A late failure from a superseded dispatch must not clear/hide the chip
    that a newer successful fetch already applied."""
    assert outcome["effort_after_stale_A_fail"] == "high", (
        "a stale failure must not overwrite the fresh chip state: "
        f"got {outcome['effort_after_stale_A_fail']!r}"
    )
