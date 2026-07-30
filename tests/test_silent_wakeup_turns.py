"""Regression coverage for the silent background-wakeup-turn contract.

An assistant reply whose entire trimmed content is the sentinel ``[[SILENT]]``
is the agent's way to acknowledge a background-process wakeup without speaking
to the user. Such replies must not render. When a wakeup turn (opened by a
``_source: "process_wakeup"`` user row) ends with the sentinel, the whole turn
collapses: the wakeup row itself and every assistant message of the turn,
including tool-carrying ones. Hidden rows remain true turn boundaries for
edit/regenerate ownership and Compact Worklog final semantics. The contract is
render-only and never mutates persisted session data.
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
const messages = JSON.parse(process.argv[2]);
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

global.window = {_showBackgroundWakeups: true};
global.S = {messages: messages};
let _visWithIdxCache = null;
let _visWithIdxCacheLen = 0;
let _visWithIdxCacheSrc = null;

// The sentinel const lives at module scope in production; eval it in the SAME
// scope as the two helpers so the const binding resolves (eval'd const does
// not leak across separate eval calls).
const sentinelStart = src.indexOf('const SILENT_TURN_SENTINEL');
if(sentinelStart === -1) throw new Error('SILENT_TURN_SENTINEL not found');
const sentinelEnd = src.indexOf('\n', sentinelStart);
eval(
  src.slice(sentinelStart, sentinelEnd) + '\n'
  + extractFunc('_isSilentSentinelContent') + '\n'
  + extractFunc('_computeSilentTurnHiddenIdxs') + '\n'
  + extractFunc('_isSilentSentinelStreamPrefix') + '\n'
  + extractFunc('_syncSilentLiveTurnSuppression')
);
if(src.indexOf('function _hasHiddenProcessWakeupBoundaryBefore') !== -1) eval(extractFunc('_hasHiddenProcessWakeupBoundaryBefore'));
function _assistantVisibleContentForReasoningCompare(m){ return String((m && m.content) || ''); }
eval(extractFunc('_assistantTurnFinalVisibleContentMap'));
eval(extractFunc('_stripWorkspaceDisplayPrefix'));
eval(extractFunc('_stripAttachedFilesMarkerForDisplay'));
eval(extractFunc('_messageIsRenderable'));
eval(extractFunc('_getVisibleMessagesWithIdx'));

const visible = _getVisibleMessagesWithIdx();
const hidden = Array.from(_computeSilentTurnHiddenIdxs(messages)).sort((a, b) => a - b);
const finalMap = _assistantTurnFinalVisibleContentMap(visible);
const finals = {};
for(const [k, v] of finalMap.entries()) finals[k] = v;
// Live-stream suppression semantics, exercised against a fake live-turn element.
const fakeTurn = {
  attrs: {},
  setAttribute(k, v){ this.attrs[k] = String(v); },
  removeAttribute(k){ delete this.attrs[k]; },
};
global.$ = (id) => (id === 'liveAssistantTurn' ? fakeTurn : null);
const suppression = {
  empty: _isSilentSentinelStreamPrefix(''),
  whitespaceOnly: _isSilentSentinelStreamPrefix('   \n '),
  partial: _isSilentSentinelStreamPrefix('[[SIL'),
  exact: _isSilentSentinelStreamPrefix('[[SILENT]]'),
  exactTrimmed: _isSilentSentinelStreamPrefix('  [[SILENT]]\n'),
  diverging: _isSilentSentinelStreamPrefix('[[SILENT]] et voici la suite'),
  unrelated: _isSilentSentinelStreamPrefix('Vu, je traite.'),
};
_syncSilentLiveTurnSuppression('[[SIL');
const attrAfterPrefix = fakeTurn.attrs['data-silent-pending'] || null;
_syncSilentLiveTurnSuppression('[[SILENT]]');
const attrAfterExact = fakeTurn.attrs['data-silent-pending'] || null;
_syncSilentLiveTurnSuppression('Résultat : tout est déployé.');
const attrAfterRealContent = fakeTurn.attrs['data-silent-pending'] || null;
process.stdout.write(JSON.stringify({
  visible: visible.map(e => ({rawIdx: e.rawIdx, role: e.m.role, source: e.m._source || '', text: String(e.m.content).slice(0, 40)})),
  hidden,
  boundaryBeforeLast: typeof _hasHiddenProcessWakeupBoundaryBefore === 'function'
    ? _hasHiddenProcessWakeupBoundaryBefore(messages.length - 1)
    : null,
  finals,
  suppression,
  attrAfterPrefix,
  attrAfterExact,
  attrAfterRealContent,
}));
"""


WAKEUP = {
    "role": "user",
    "content": "[IMPORTANT: Background process proc_1 completed (exit_code=0).\nCommand: sleep 1\nOutput:\ndone]",
    "_source": "process_wakeup",
    "timestamp": 1783405253.72,
}
WAKEUP2 = {
    "role": "user",
    "content": "[IMPORTANT: Background process proc_2 completed (exit_code=0).\nCommand: sleep 2\nOutput:\ndone]",
    "_source": "process_wakeup",
    "timestamp": 1783405260.72,
}


def _run_driver(messages):
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS_PATH), json.dumps(messages)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_silent_wakeup_turn_is_fully_hidden():
    result = _run_driver([
        {"role": "user", "content": "question humaine"},
        {"role": "assistant", "content": "réponse visible A1"},
        dict(WAKEUP),
        {"role": "assistant", "content": "[[SILENT]]"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [0, 1]
    assert result["hidden"] == [2, 3]


def test_silent_wakeup_turn_with_tool_calls_is_fully_hidden():
    result = _run_driver([
        dict(WAKEUP),
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "process", "arguments": "{}"}},
        ]},
        {"role": "tool", "content": "{\"status\": \"exited\"}"},
        {"role": "assistant", "content": "[[SILENT]]"},
    ])

    assert result["visible"] == []
    assert result["hidden"] == [0, 1, 3]


def test_concluding_wakeup_turn_stays_visible():
    result = _run_driver([
        dict(WAKEUP),
        {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 déploiement terminé"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [0, 1]
    assert result["hidden"] == []


def test_mixed_consecutive_wakeups_hide_only_the_silent_turn():
    result = _run_driver([
        dict(WAKEUP),
        {"role": "assistant", "content": "[[SILENT]]"},
        dict(WAKEUP2),
        {"role": "assistant", "content": "# CONCLUSION\n---\n> 🟢 tout est terminé"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [2, 3]
    assert result["hidden"] == [0, 1]


def test_sentinel_reply_to_a_human_hides_only_the_reply():
    result = _run_driver([
        {"role": "user", "content": "question humaine"},
        {"role": "assistant", "content": "[[SILENT]]"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [0]
    assert result["hidden"] == [1]


def test_sentinel_embedded_in_longer_text_hides_nothing():
    result = _run_driver([
        dict(WAKEUP),
        {"role": "assistant", "content": "Vu. [[SILENT]] ne s'applique pas ici"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [0, 1]
    assert result["hidden"] == []


def test_sentinel_with_surrounding_whitespace_is_hidden():
    result = _run_driver([
        {"role": "user", "content": "question humaine"},
        {"role": "assistant", "content": "  [[SILENT]]\n"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [0]
    assert result["hidden"] == [1]


def test_mid_turn_hidden_sentinel_remains_a_true_turn_boundary():
    # Same raw turn (no user boundary between the sentinel and the conclusion):
    # the turn ends with visible content, so the turn itself stays visible and
    # only the lone sentinel message collapses. That hidden message must still
    # act as a real turn boundary — otherwise the conclusion would fold into
    # the pre-wakeup assistant turn (Compact Worklog terminal semantics,
    # question mapping, regenerate ownership).
    result = _run_driver([
        {"role": "assistant", "content": "rapport précédent"},
        dict(WAKEUP),
        {"role": "assistant", "content": "[[SILENT]]"},
        {"role": "assistant", "content": "conclusion visible"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [0, 1, 3]
    assert result["hidden"] == [2]
    assert result["boundaryBeforeLast"] is True
    assert result["finals"].get("0") == "rapport précédent"
    assert result["finals"].get("3") == "conclusion visible"


def test_hidden_silent_turn_before_visible_turn_keeps_boundary():
    # Silent wakeup turn fully collapsed, followed by a concluding wakeup turn:
    # the visible conclusion attaches to ITS wakeup (visible boundary), never
    # to the hidden silent turn.
    result = _run_driver([
        {"role": "assistant", "content": "rapport précédent"},
        dict(WAKEUP),
        {"role": "assistant", "content": "[[SILENT]]"},
        dict(WAKEUP2),
        {"role": "assistant", "content": "conclusion visible"},
    ])

    assert [e["rawIdx"] for e in result["visible"]] == [0, 3, 4]
    assert result["hidden"] == [1, 2]
    assert result["finals"].get("0") == "rapport précédent"
    assert result["finals"].get("4") == "conclusion visible"


def test_live_stream_prefix_suppression_semantics():
    # The live bubble stays hidden while the streamed text is a prefix of the
    # sentinel (or the exact sentinel), and reappears as soon as the content
    # diverges into a real reply.
    result = _run_driver([{"role": "user", "content": "q"}])

    sup = result["suppression"]
    assert sup["empty"] is False
    assert sup["whitespaceOnly"] is False
    assert sup["partial"] is True
    assert sup["exact"] is True
    assert sup["exactTrimmed"] is True
    assert sup["diverging"] is False
    assert sup["unrelated"] is False
    assert result["attrAfterPrefix"] == "1"
    assert result["attrAfterExact"] == "1"
    assert result["attrAfterRealContent"] is None


def test_live_suppression_css_rule_and_token_wiring_present():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    messages_js = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

    assert '#liveAssistantTurn[data-silent-pending="1"]' in css
    assert "display: none" in css
    # The per-token/interim chokepoint must drive the suppression attribute.
    assert "_syncSilentLiveTurnSuppression(assistantText)" in messages_js
