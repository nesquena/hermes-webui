"""Silent background-wakeup turns collapse at render time on exact match only.

A background-process wakeup starts an agent turn. When that turn's final
assistant reply is exactly ``[[SILENT]]`` (after trim), the wakeup row and the
turn collapse instead of rendering a visible bubble. Prose that merely contains
the token stays visible, turns not opened by a wakeup are untouched, and the raw
transcript (``S.messages``) is never mutated. The helpers are extracted from the
production ``static/ui.js`` and executed in Node.
"""

import copy
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
const payload = JSON.parse(process.argv[2]);
function extractFunc(name){
  const start = src.indexOf('function ' + name + '(');
  if(start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')'){ depth--; if(depth === 0){ close = i; break; } }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){ depth--; if(depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(name + ' body did not close');
}
function msgContent(m){
  if(!m) return '';
  if(typeof m.content === 'string') return m.content;
  if(Array.isArray(m.content)) return m.content.map(p => (p && (p.text || p.content)) || '').join('\n');
  return String(m.content || '');
}
function _isContextCompactionMessage(){ return false; }
function _isPreservedCompressionTaskListMessage(){ return false; }
function _isRecoveryControlMessage(){ return false; }
function _messageHasReasoningPayload(){ return false; }
function _assistantMessageHasVisibleContent(m){ return !!String(msgContent(m)).trim(); }
function _assistantVisibleContentForReasoningCompare(m){ return String((m && m.content) || ''); }

global.window = {_showBackgroundWakeups: payload.showBackgroundWakeups !== false};
global.S = {messages: []};
let _visWithIdxCache = null;
let _visWithIdxCacheLen = 0;
let _visWithIdxCacheSrc = null;
for(const name of [
  '_isSilentWakeupSentinelReply',
  '_computeSilentWakeupTurnIdxs',
  '_silentWakeupTurnHiddenIdxs',
  'clearVisibleMessageRowCache',
  '_hasHiddenProcessWakeupBoundaryBefore',
  '_messageIsRenderable',
  '_getVisibleMessagesWithIdx',
  '_assistantTurnFinalVisibleContentMap',
]) eval(extractFunc(name));

const out = {};
for(const [name, messages] of Object.entries(payload.cases)){
  S.messages = messages;
  clearVisibleMessageRowCache();
  const before = JSON.stringify(messages);
  const visible = _getVisibleMessagesWithIdx();
  const finals = {};
  for(const [k, v] of _assistantTurnFinalVisibleContentMap(visible).entries()) finals[k] = v;
  const boundaries = {};
  for(const entry of visible) boundaries[entry.rawIdx] = _hasHiddenProcessWakeupBoundaryBefore(entry.rawIdx);
  out[name] = {
    visible: visible.map(e => e.rawIdx),
    finals,
    boundaries,
    rawUnchanged: JSON.stringify(S.messages) === before && S.messages === messages,
  };
}

// Memo invalidation: an in-place edit keeps (reference, length); the shared
// visible-row cache reset must drop the silent-turn memo as well.
const edited = payload.cases.exact_silent;
S.messages = edited;
clearVisibleMessageRowCache();
const beforeEdit = _getVisibleMessagesWithIdx().map(e => e.rawIdx);
edited[edited.length - 1].content = 'Build finished: 3 artifacts uploaded.';
clearVisibleMessageRowCache();
const afterEdit = _getVisibleMessagesWithIdx().map(e => e.rawIdx);
// Appending a row changes the length key, so the memo recomputes by itself.
edited.push({role: 'user', content: '[IMPORTANT: Background process proc_9 completed.]', _source: 'process_wakeup'});
edited.push({role: 'assistant', content: '[[SILENT]]'});
_visWithIdxCache = null;
const afterAppend = _getVisibleMessagesWithIdx().map(e => e.rawIdx);
out.memo = {beforeEdit, afterEdit, afterAppend};
process.stdout.write(JSON.stringify(out));
"""


HUMAN = {"role": "user", "content": "Deploy the build and tell me when it is done."}
ANSWER = {"role": "assistant", "content": "Started the deploy in the background."}


def _wakeup(proc="proc_1"):
    return {
        "role": "user",
        "content": f"[IMPORTANT: Background process {proc} completed (exit_code=0).\nCommand: sleep 1\nOutput:\ndone]",
        "_source": "process_wakeup",
    }


def _assistant(content, **extra):
    return {"role": "assistant", "content": content, **extra}


CASES = {
    "exact_silent": [HUMAN, ANSWER, _wakeup(), _assistant("[[SILENT]]")],
    "whitespace_silent": [HUMAN, ANSWER, _wakeup(), _assistant("  \n[[SILENT]]\n ")],
    "prose_containing_token": [
        HUMAN,
        ANSWER,
        _wakeup(),
        _assistant("Nothing to report yet, so I would normally answer [[SILENT]] here."),
    ],
    "token_prefix_with_suffix": [HUMAN, ANSWER, _wakeup(), _assistant("[[SILENT]] deploy failed")],
    "lowercase_token": [HUMAN, ANSWER, _wakeup(), _assistant("[[silent]]")],
    "silent_after_tool_work": [
        HUMAN,
        ANSWER,
        _wakeup(),
        _assistant(
            "",
            tool_calls=[{"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}],
        ),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        _assistant("[[SILENT]]"),
    ],
    "silent_not_final": [HUMAN, ANSWER, _wakeup(), _assistant("[[SILENT]]"), _assistant("Deploy failed: exit 1.")],
    "human_turn_silent": [HUMAN, _assistant("[[SILENT]]")],
    "silent_then_visible_wakeup": [
        HUMAN,
        ANSWER,
        _wakeup("proc_1"),
        _assistant("[[SILENT]]"),
        _wakeup("proc_2"),
        _assistant("Both processes finished; the deploy is live."),
    ],
}


def _run(cases=CASES, show_background_wakeups=True):
    payload = {"cases": copy.deepcopy(cases), "showBackgroundWakeups": show_background_wakeups}
    result = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_exact_silent_wakeup_turn_collapses_with_its_wakeup_row():
    out = _run()
    for name in ("exact_silent", "whitespace_silent"):
        assert out[name]["visible"] == [0, 1], name
        assert out[name]["rawUnchanged"] is True, name
    # Tool-carrying rows of the same wakeup turn collapse with it.
    assert out["silent_after_tool_work"]["visible"] == [0, 1]
    assert out["silent_after_tool_work"]["rawUnchanged"] is True


def test_prose_or_near_miss_sentinel_is_never_collapsed():
    out = _run()
    for name in ("prose_containing_token", "token_prefix_with_suffix", "lowercase_token"):
        assert out[name]["visible"] == [0, 1, 2, 3], name
        assert out[name]["rawUnchanged"] is True, name
    # Only the FINAL reply decides: a later real answer keeps the turn visible.
    assert out["silent_not_final"]["visible"] == [0, 1, 2, 3, 4]
    # Turns not opened by a background wakeup are outside the contract.
    assert out["human_turn_silent"]["visible"] == [0, 1]


def test_collapsed_turn_stays_a_boundary_between_visible_turns():
    out = _run()
    case = out["silent_then_visible_wakeup"]
    assert case["visible"] == [0, 1, 4, 5]
    # The pre-wakeup answer keeps its own final text: the later wakeup answer is
    # not folded into it across the collapsed silent turn.
    assert case["finals"]["1"] == "Started the deploy in the background."
    assert case["finals"]["5"] == "Both processes finished; the deploy is live."
    assert case["boundaries"]["4"] is True

    hidden = _run(show_background_wakeups=False)["silent_then_visible_wakeup"]
    assert hidden["visible"] == [0, 1, 5]
    assert hidden["boundaries"]["5"] is True
    assert hidden["finals"]["1"] == "Started the deploy in the background."


def test_ordinary_transcripts_do_not_report_hidden_boundaries():
    out = _run()
    assert all(v is False for v in out["prose_containing_token"]["boundaries"].values())
    assert all(v is False for v in out["human_turn_silent"]["boundaries"].values())


def test_silent_memo_follows_edits_and_appends():
    memo = _run()["memo"]
    assert memo["beforeEdit"] == [0, 1]
    assert memo["afterEdit"] == [0, 1, 2, 3]
    assert memo["afterAppend"] == [0, 1, 2, 3]
