"""Regression coverage: restored live turns must honor [[SILENT]] suppression.

A process-wakeup turn observed through session sync (api_server flow) is
rendered by re-injecting the persisted inflight ``liveTurnHtml`` snapshot via
``restoreLiveTurnHtmlForSession()``. That path bypasses both the
persisted-message filter (``_computeSilentTurnHiddenIdxs``) and the per-token
streaming hook that sets ``data-silent-pending``. A wakeup turn whose
accumulated assistant text is the ``[[SILENT]]`` sentinel (or a streaming
prefix of it) therefore re-appeared on every live-turn restore until the turn
fully settled.

The restore path must re-apply ``_syncSilentLiveTurnSuppression`` from the
restored DOM text so the CSS rule
``#liveAssistantTurn[data-silent-pending="1"] { display: none !important; }``
keeps the sentinel turn hidden.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extractFunc(name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')'){
      depth--;
      if(depth === 0){ close = i; break; }
    }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){
      depth--;
      if(depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' body did not close');
}

// The sentinel const and the helpers are eval'd in one scope so the const
// binding is visible to the eval'd functions.
const sentinelStart = src.indexOf('const SILENT_TURN_SENTINEL');
if(sentinelStart === -1) throw new Error('SILENT_TURN_SENTINEL not found');
const sentinelEnd = src.indexOf('\n', sentinelStart);
eval(
  src.slice(sentinelStart, sentinelEnd) + '\n'
  + extractFunc('_isSilentSentinelContent') + '\n'
  + extractFunc('_isSilentSentinelStreamPrefix') + '\n'
  + extractFunc('_syncSilentLiveTurnSuppression') + '\n'
  + extractFunc('restoreLiveTurnHtmlForSession')
);
// Called unconditionally by restoreLiveTurnHtmlForSession but returns
// immediately when there is no pre-existing live turn (our scenario).
function _mergeRestoredLiveAssistantSegment(){}

// ── Minimal DOM stub ─────────────────────────────────────────────────────
const registry = {};
function makeSegment(text){
  const body = {textContent: text};
  return {
    querySelector(sel){ return sel === '.msg-body' ? body : null; },
  };
}
function makeRestored(liveTexts){
  const segments = liveTexts.map(makeSegment);
  return {
    id: '',
    dataset: {},
    _attrs: {},
    setAttribute(k, v){ this._attrs[k] = v; },
    getAttribute(k){ return (k in this._attrs) ? this._attrs[k] : null; },
    removeAttribute(k){ delete this._attrs[k]; },
    querySelector(){ return null; },
    querySelectorAll(sel){ return sel === '[data-live-assistant="1"]' ? segments : []; },
  };
}
let presetRestored = null;
const msgInner = {
  appendChild(child){ if(child.id) registry[child.id] = child; },
};
registry['msgInner'] = msgInner;
global.document = {
  createElement(tag){
    if(tag !== 'template') throw new Error('unexpected element ' + tag);
    return {
      set innerHTML(_v){ this.content = {firstElementChild: presetRestored}; },
    };
  },
};
global.window = {};
global.S = {session: {session_id: 'sid-restore'}};
global.INFLIGHT = {'sid-restore': {liveTurnHtml: '<div>snapshot</div>'}};
global.requestAnimationFrame = function(){ /* do not invoke the callback */ };
function $(id){ return registry[id] || null; }
global.$ = $;

function restoreWith(liveTexts){
  for(const k of Object.keys(registry)) delete registry[k];
  registry['msgInner'] = msgInner;
  presetRestored = makeRestored(liveTexts);
  const ok = restoreLiveTurnHtmlForSession('sid-restore');
  const turn = registry['liveAssistantTurn'] || null;
  return {
    ok,
    silentPending: turn ? (turn._attrs['data-silent-pending'] || null) : 'NO-TURN',
  };
}

process.stdout.write(JSON.stringify({
  sentinel: restoreWith(['[[SILENT]]']),
  prefix: restoreWith(['[[SIL']),
  normal: restoreWith(['Réponse normale pour Anthony.']),
  segmented: restoreWith(['', '[[SILENT]]']),
}));
"""


def _run_restore_probe():
    assert NODE is not None  # guarded by pytestmark skipif
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node driver failed:\n{proc.stderr[-800:]}"
    return json.loads(proc.stdout)


def test_restored_silent_live_turn_gets_suppression_attribute():
    out = _run_restore_probe()
    assert out["sentinel"]["ok"] is True
    assert out["sentinel"]["silentPending"] == "1", (
        "restored [[SILENT]] live turn must carry data-silent-pending=1 "
        "so the CSS suppression keeps it hidden"
    )


def test_restored_silent_prefix_live_turn_gets_suppression_attribute():
    out = _run_restore_probe()
    assert out["prefix"]["silentPending"] == "1", (
        "a streaming prefix of the sentinel must stay hidden while restoring"
    )


def test_restored_normal_live_turn_is_not_suppressed():
    out = _run_restore_probe()
    assert out["normal"]["ok"] is True
    assert out["normal"]["silentPending"] is None, (
        "normal restored turns must not be suppressed"
    )


def test_restored_silent_text_in_later_segment_is_detected():
    out = _run_restore_probe()
    assert out["segmented"]["silentPending"] == "1", (
        "sentinel text split across live segments must still be detected"
    )
