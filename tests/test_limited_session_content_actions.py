"""Regression coverage for clipped paginated session content actions.

The paginated session API may return a display-only preview marked with
``_content_truncated``. Frontend actions that can mutate durable session state
must first replace those previews with the authoritative full transcript.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = REPO / "static" / "sessions.js"
UI_JS = REPO / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER = r"""
const fs = require('fs');
const sessions = fs.readFileSync(process.argv[1], 'utf8');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

function sliceBetween(src, startNeedle, endNeedle) {
  const start = src.indexOf(startNeedle);
  if (start < 0) throw new Error('start not found: ' + startNeedle);
  const end = src.indexOf(endNeedle, start);
  if (end < 0) throw new Error('end not found: ' + endNeedle);
  return src.slice(start, end);
}

(async () => {
  if (scenario === 'ensure') {
    const fnSrc = sliceBetween(
      sessions,
      'async function _ensureAllMessagesLoaded()',
      '\n\nconst SESSION_ARCHIVED_PAGE_SIZE'
    );
    let apiCalls = 0;
    let _messagesTruncated = false;
    let _loadingOlder = false;
    let _loadingSessionId = null;
    let _oldestIdx = 0;
    let _messagesGeneration = 0;
    const S = {
      session: {session_id: 's1'},
      messages: [{role: 'user', content: 'PREVIEW', _content_truncated: true}],
    };
    const window = {};
    function _bumpMessagesGeneration(){ _messagesGeneration += 1; }
    function _syncToolCallsForLoadedMessages(){}
    async function api(url) {
      apiCalls += 1;
      if (url.includes('msg_limit=')) throw new Error('full reload must not use msg_limit');
      return {session: {
        messages: [{role: 'user', content: 'FULL-ORIGINAL'}],
        message_count: 1,
        tool_calls: [],
      }};
    }
    eval(fnSrc);
    await _ensureAllMessagesLoaded();
    process.stdout.write(JSON.stringify({
      apiCalls,
      content: S.messages[0].content,
      marker: Boolean(S.messages[0]._content_truncated),
      oldestIdx: _oldestIdx,
    }));
    return;
  }

  if (scenario === 'regenerate') {
    const fnSrc = sliceBetween(
      ui,
      'async function regenerateResponse(',
      '\n\n// postProcessRenderedMessages()'
    );
    const S = {
      busy: false,
      session: {session_id: 's1'},
      messages: [
        {role: 'user', content: 'PREVIEW', _content_truncated: true},
        {role: 'assistant', content: 'answer'},
      ],
    };
    let _oldestIdx = 0;
    let composer = {value: ''};
    let sentText = null;
    let truncateBody = null;
    const btn = {closest(){ return {dataset: {msgIdx: '1'}}; }};
    function msgContent(m){ return String((m && m.content) || ''); }
    async function _ensureAllMessagesLoaded(){
      S.messages = [
        {role: 'user', content: 'FULL-ORIGINAL'},
        {role: 'assistant', content: 'answer'},
      ];
    }
    async function api(_url, options){ truncateBody = JSON.parse(options.body); }
    function renderMessages(){}
    function $(id){ if (id !== 'msg') throw new Error('unexpected element'); return composer; }
    async function send(){ sentText = composer.value; }
    function setStatus(){}
    function t(key){ return key; }
    eval(fnSrc);
    await regenerateResponse(btn);
    process.stdout.write(JSON.stringify({sentText, truncateBody}));
    return;
  }

  if (scenario === 'edit') {
    let start = ui.indexOf('async function editMessage(');
    if (start < 0) start = ui.indexOf('function editMessage(');
    if (start < 0) throw new Error('editMessage not found');
    const end = ui.indexOf('\n\nfunction cancelEdit(', start);
    if (end < 0) throw new Error('editMessage end not found');
    const fnSrc = ui.slice(start, end);
    const S = {
      busy: false,
      session: {session_id: 's1'},
      messages: [{role: 'user', content: 'PREVIEW', _content_truncated: true}],
    };
    let _oldestIdx = 0;
    let ensureCalls = 0;
    let textarea = null;
    const body = {replaceWith(){}};
    function makeRow(rawText) {
      return {
        dataset: {msgIdx: '0', rawText},
        querySelector(selector) {
          if (selector === '.msg-body') return body;
          return null;
        },
      };
    }
    const initialRow = makeRow('PREVIEW');
    const refreshedRow = makeRow('FULL-ORIGINAL');
    const btn = {closest(){ return initialRow; }};
    const document = {
      querySelector(){ return refreshedRow; },
      createElement(tag) {
        if (tag === 'textarea') {
          textarea = {
            className: '', value: '', style: {}, scrollHeight: 0,
            addEventListener(){}, after(){}, focus(){}, setSelectionRange(){},
          };
          return textarea;
        }
        return {
          className: '', innerHTML: '', remove(){},
          querySelector(){ return {}; },
        };
      },
    };
    function msgContent(m){ return String((m && m.content) || ''); }
    async function _ensureAllMessagesLoaded(){
      ensureCalls += 1;
      S.messages = [{role: 'user', content: 'FULL-ORIGINAL'}];
    }
    function renderMessages(){}
    function requestAnimationFrame(cb){ cb(); }
    function autoResizeTextarea(){}
    async function submitEdit(){}
    eval(fnSrc);
    await editMessage(btn);
    process.stdout.write(JSON.stringify({ensureCalls, value: textarea && textarea.value}));
    return;
  }

  throw new Error('unknown scenario: ' + scenario);
})().catch(err => { console.error(err.stack || err); process.exit(1); });
"""


def _run(scenario: str) -> dict:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(SESSIONS_JS), str(UI_JS), scenario],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_ensure_all_messages_reloads_content_truncated_preview_without_row_truncation():
    result = _run("ensure")

    assert result == {
        "apiCalls": 1,
        "content": "FULL-ORIGINAL",
        "marker": False,
        "oldestIdx": 0,
    }


def test_regenerate_reads_last_user_text_after_authoritative_full_reload():
    result = _run("regenerate")

    assert result["sentText"] == "FULL-ORIGINAL"
    assert result["truncateBody"] == {"session_id": "s1", "keep_count": 1}


def test_edit_loads_authoritative_content_before_populating_textarea():
    result = _run("edit")

    assert result == {"ensureCalls": 1, "value": "FULL-ORIGINAL"}
