"""Regression coverage for process-wakeup transcript rendering.

A background-process wakeup is stored as a synthetic user turn
(`_source: "process_wakeup"`).  It must be visible by default, but it must not
look like a human-authored chat bubble.  Keeping it in the visible message list
also preserves the user-turn boundary between the assistant response that
preceded the notification and the assistant response produced by the wakeup.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
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

global.window = {};
global.S = {messages: []};
let _visWithIdxCache = null;
let _visWithIdxCacheLen = 0;
let _visWithIdxCacheSrc = null;

const heightStart = src.indexOf('const MESSAGE_RENDER_WINDOW_DEFAULT');
const heightEnd = src.indexOf('const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS', heightStart);
if(heightStart !== -1 && heightEnd !== -1) eval(src.slice(heightStart, heightEnd));
if(src.indexOf('function _isProcessWakeupMessage') !== -1) eval(extractFunc('_isProcessWakeupMessage'));
eval(extractFunc('_stripWorkspaceDisplayPrefix'));
eval(extractFunc('_stripAttachedFilesMarkerForDisplay'));
eval(extractFunc('_messageIsRenderable'));
eval(extractFunc('_getVisibleMessagesWithIdx'));
eval(extractFunc('_messageVirtualRoleForEntry'));

const wakeup = {
  role: 'user',
  content: '[IMPORTANT: Background process proc_123 completed (exit_code=0).\nCommand: sleep 1\nOutput:\ndone]',
  _source: 'process_wakeup',
  timestamp: 1783405253.72,
};
S.messages = [
  {role: 'assistant', content: 'previous assistant report', timestamp: 1783405252.05},
  wakeup,
  {role: 'assistant', content: 'assistant response to wakeup', timestamp: 1783405254.10},
];

const visible = _getVisibleMessagesWithIdx();
const turns = [];
let current = [];
for(const entry of visible){
  const source = entry.m._source || '';
  if(entry.m.role === 'user'){
    if(current.length) turns.push(current);
    turns.push(['user:' + source + ':' + String(entry.m.content).slice(0, 35)]);
    current = [];
  }else if(entry.m.role === 'assistant'){
    current.push('assistant:' + entry.m.content);
  }
}
if(current.length) turns.push(current);
const virtualRole = _messageVirtualRoleForEntry({m: wakeup});
const virtualHeight = typeof _messageVirtualDefaultHeightForRole === 'function'
  ? _messageVirtualDefaultHeightForRole(virtualRole)
  : null;
const attachmentOnlyWakeup = {
  role: 'user',
  content: '',
  _source: 'process_wakeup',
  attachments: [{name: 'result.txt'}],
};
const markerWakeupContent = [
  '[Workspace::v1: /tmp/hermes]',
  'Visible wakeup text',
  '',
  '[Attached files: result.txt]',
].join(String.fromCharCode(10));

process.stdout.write(JSON.stringify({
  visible: visible.map(e => ({rawIdx: e.rawIdx, role: e.m.role, source: e.m._source || '', text: String(e.m.content).slice(0, 32)})),
  turns,
  virtualRole,
  virtualHeight,
  attachmentOnlyRenderable: _messageIsRenderable(attachmentOnlyWakeup),
  strippedWakeupDisplay: _stripAttachedFilesMarkerForDisplay(_stripWorkspaceDisplayPrefix(markerWakeupContent)),
}));
"""


def _run_driver():
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS_PATH)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_process_wakeup_message_stays_visible_and_preserves_turn_boundary():
    result = _run_driver()

    assert result["visible"] == [
        {"rawIdx": 0, "role": "assistant", "source": "", "text": "previous assistant report"},
        {"rawIdx": 1, "role": "user", "source": "process_wakeup", "text": "[IMPORTANT: Background process p"},
        {"rawIdx": 2, "role": "assistant", "source": "", "text": "assistant response to wakeup"},
    ]
    assert result["turns"] == [
        ["assistant:previous assistant report"],
        ["user:process_wakeup:[IMPORTANT: Background process proc"],
        ["assistant:assistant response to wakeup"],
    ]


def test_process_wakeup_has_its_own_virtual_height_role():
    result = _run_driver()

    assert result["virtualRole"] == "process_wakeup"
    assert isinstance(result["virtualHeight"], int)
    assert 1 <= result["virtualHeight"] <= 120


def test_attachment_only_process_wakeup_is_visible_and_display_markers_are_stripped():
    result = _run_driver()

    assert result["attachmentOnlyRenderable"] is True
    assert result["strippedWakeupDisplay"] == "Visible wakeup text"


def test_process_wakeup_uses_compact_status_row_not_normal_user_bubble():
    ui = UI_JS_PATH.read_text(encoding="utf-8")
    marker = "const isProcessWakeup="
    marker_idx = ui.find(marker)
    assert marker_idx != -1, "render loop must classify process-wakeup messages"
    process_branch_idx = ui.find("if(isProcessWakeup)", marker_idx)
    user_branch_idx = ui.find("if(isUser)", marker_idx)

    assert process_branch_idx != -1, "process-wakeup render branch missing"
    assert user_branch_idx != -1, "normal user render branch missing"
    assert process_branch_idx < user_branch_idx, (
        "process-wakeup rows must render through the compact status branch "
        "before the normal user-bubble branch"
    )
    process_branch = ui[process_branch_idx:user_branch_idx]
    assert "process-wakeup-row" in process_branch
    assert "process-wakeup-notice" in process_branch
    assert "data-role='process_wakeup'" in process_branch or "dataset.role='process_wakeup'" in process_branch
    assert "${filesHtml}" in process_branch
    assert "const rowDisplayContent=displayContent;" in ui
    assert "const rowDisplayContent=isProcessWakeup?content:displayContent;" not in ui

    assert ".process-wakeup-row" in STYLE_CSS
    assert ".process-wakeup-notice" in STYLE_CSS
    assert ".process-wakeup-text" in STYLE_CSS
    notice_rule = STYLE_CSS[
        STYLE_CSS.index(".process-wakeup-notice{") : STYLE_CSS.index(".process-wakeup-label{")
    ]
    assert "margin:8px 0 8px var(--msg-rail)" in notice_rule
    assert "max-width:min(var(--msg-max),760px)" in notice_rule
    assert "margin-left:30px" not in notice_rule
    assert "max-width:680px" not in notice_rule
    assert "@media(max-width:700px){.process-wakeup-notice{margin-left:0;}}" in STYLE_CSS
