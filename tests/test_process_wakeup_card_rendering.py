"""#6345 — collapsed summary-card rendering for process-wakeup messages.

Behavioral coverage for the three ui.js helpers behind the card:
``_parseProcessWakeupBody`` (client inverse of ``format_wakeup_prompt``),
``_processWakeupInfo`` (server ``_wakeup_meta`` merged over the client parse),
and ``_processWakeupCardHtml`` (the ``<details>`` card markup). Structural
integration with the render loop is pinned by
tests/test_process_wakeup_rendering.py.
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
function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function li(name, size){ return '<svg data-icon="' + name + '"></svg>'; }
function t(key, ...args){
  let out = key;
  if(args.length) out += ':' + args.join(',');
  return out;
}

eval(extractFunc('_parseProcessWakeupBody'));
eval(extractFunc('_processWakeupInfo'));
eval(extractFunc('_processWakeupCardHtml'));

const okBody = '[IMPORTANT: Background process proc_1 completed (exit_code=0).\nCommand: npm run build\nOutput:\nall good]';
const failBody = '[IMPORTANT: Background process proc_2 completed (exit_code=3).\nCommand: pytest -q\nOutput:\n1 failed]';
const signalBody = '[IMPORTANT: Background process proc_3 completed (exit_code=-9).\nCommand: sleep 999\nOutput:\n]';
const watchBody = '[IMPORTANT: Background process w1 matched watch pattern "ERROR.*timeout".\nCommand: tail -f app.log\nMatched output:\nERROR request timeout\n(3 earlier matches were suppressed by rate limit)]';
const htmlBody = '[IMPORTANT: Background process p completed (exit_code=0).\nCommand: echo "<script>alert(1)</script>"\nOutput:\n<b>bold</b>]';

const okInfo = _processWakeupInfo({}, okBody);
const failInfo = _processWakeupInfo({}, failBody);
const signalInfo = _processWakeupInfo({}, signalBody);
const watchInfo = _processWakeupInfo({}, watchBody);
const htmlInfo = _processWakeupInfo({}, htmlBody);
const metaOnlyInfo = _processWakeupInfo(
  {_wakeup_meta: {type: 'completion', task_id: 'srv_1', command: 'cargo test', exit_code: 1}},
  'some future format the client parser does not know'
);
const metaOverParse = _processWakeupInfo(
  {_wakeup_meta: {type: 'completion', task_id: 'authoritative', command: 'npm run build', exit_code: 0}},
  okBody
);

const extras = {timeHtml: '<span class="msg-time">14:32</span>', filesHtml: '', footHtml: '<div class="msg-foot"></div>'};

process.stdout.write(JSON.stringify({
  okInfo, failInfo, watchInfo, metaOnlyInfo,
  metaOverParseTaskId: metaOverParse.taskId,
  unparseableIsNull: _processWakeupInfo({}, 'plain text') === null,
  emptyIsNull: _processWakeupInfo({content: ''}, '') === null,
  okCard: _processWakeupCardHtml(okInfo, okBody, extras),
  failCard: _processWakeupCardHtml(failInfo, failBody, extras),
  signalCard: _processWakeupCardHtml(signalInfo, signalBody, extras),
  watchCard: _processWakeupCardHtml(watchInfo, watchBody, extras),
  htmlCard: _processWakeupCardHtml(htmlInfo, htmlBody, extras),
  metaOnlyCard: _processWakeupCardHtml(metaOnlyInfo, 'some future format the client parser does not know', extras),
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


def test_client_parser_mirrors_the_two_structured_wakeup_shapes():
    result = _run_driver()

    ok = result["okInfo"]
    assert ok["type"] == "completion"
    assert ok["taskId"] == "proc_1"
    assert ok["command"] == "npm run build"
    assert ok["exitCode"] == "0"
    assert ok["output"] == "all good"

    watch = result["watchInfo"]
    assert watch["type"] == "watch_match"
    assert watch["pattern"] == "ERROR.*timeout"
    assert watch["command"] == "tail -f app.log"
    assert watch["output"] == "ERROR request timeout"
    assert watch["suppressed"] == 3

    assert result["unparseableIsNull"] is True
    assert result["emptyIsNull"] is True


def test_server_meta_is_authoritative_and_covers_unparseable_bodies():
    result = _run_driver()

    meta_only = result["metaOnlyInfo"]
    assert meta_only["taskId"] == "srv_1"
    assert meta_only["command"] == "cargo test"
    assert meta_only["exitCode"] == 1
    assert meta_only["output"] is None

    assert result["metaOverParseTaskId"] == "authoritative"

    # With no parsable output section the detail falls back to the raw body.
    assert "some future format" in result["metaOnlyCard"]


def test_card_markup_collapsed_by_default_with_exit_chip():
    result = _run_driver()

    ok_card = result["okCard"]
    assert ok_card.startswith('<details class="process-wakeup-card">')
    assert "open" not in ok_card.split(">", 1)[0]
    assert 'class="process-wakeup-chip ok"' in ok_card
    assert "exit 0" in ok_card
    assert "npm run build" in ok_card
    assert "[IMPORTANT" not in ok_card
    assert 'data-icon="chevron-right"' in ok_card
    assert 'data-icon="terminal"' in ok_card
    assert "process_wakeup_label" in ok_card
    assert '<span class="msg-time">14:32</span>' in ok_card

    fail_card = result["failCard"]
    assert 'class="process-wakeup-chip fail"' in fail_card
    assert "exit 3" in fail_card

    # Signal-killed processes report negative returncodes; still a failure.
    signal_card = result["signalCard"]
    assert 'class="process-wakeup-chip fail"' in signal_card
    assert "exit -9" in signal_card

    watch_card = result["watchCard"]
    assert 'class="process-wakeup-chip watch"' in watch_card
    assert "ERROR.*timeout" in watch_card
    assert "process_wakeup_suppressed:3" in watch_card
    # The truncatable pattern <code> must expose the full pattern on hover.
    assert '<code title="ERROR.*timeout">' in watch_card


def test_card_escapes_command_and_output():
    result = _run_driver()

    html_card = result["htmlCard"]
    assert "<script>" not in html_card
    assert "&lt;script&gt;" in html_card
    assert "<b>bold</b>" not in html_card
    assert "&lt;b&gt;bold&lt;/b&gt;" in html_card


def test_render_branch_and_css_wire_the_card_variant():
    ui = UI_JS_PATH.read_text(encoding="utf-8")
    branch_start = ui.find("if(isProcessWakeup){")
    branch_end = ui.find("if(isUser){", branch_start)
    assert branch_start != -1 and branch_end != -1
    branch = ui[branch_start:branch_end]

    assert "_processWakeupInfo(m, processText)" in branch
    assert "process-wakeup-notice-card" in branch
    assert "process-wakeup-fail" in branch
    # The raw-notice fallback must survive for unparseable bodies.
    assert "process-wakeup-text" in branch
    assert "t('process_wakeup_label')" in branch

    assert ".process-wakeup-card{" in STYLE_CSS
    assert ".process-wakeup-chip.ok{" in STYLE_CSS
    assert ".process-wakeup-chip.fail{" in STYLE_CSS
    assert ".process-wakeup-notice.process-wakeup-fail{" in STYLE_CSS
    assert ".process-wakeup-detail pre.process-wakeup-text{max-height" in STYLE_CSS
