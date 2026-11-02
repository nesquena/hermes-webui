"""Regression coverage: substantive interim prose stays visible in Compact Worklog.

Upstream #3401 folds every interim assistant segment of a settled turn into the
collapsed worklog rail. When an interim message carries content-bearing prose
(a checklist, a table, a direct answer emitted ahead of a tool call) the content
disappears behind the rail and the final reply can only point at it — #7894
documents the same failure class from the model-guidance side, and #3875
established that folding a visible answer is a defect.

The fix keeps short progress narration folded and exempts only substantive
prose (>= _INTERIM_PROSE_SUBSTANTIVE_MIN_CHARS trimmed characters) at three
seams. These tests drive the REAL functions sliced out of static/ui.js in
node — never a reimplementation.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

EXTRACT_FN_JS = """
function extractFn(src, name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const paramsStart = src.indexOf('(', start);
  let depth = 0;
  let paramsEnd = -1;
  for(let i = paramsStart; i < src.length; i++){
    if(src[i] === '(') depth += 1;
    else if(src[i] === ')'){
      depth -= 1;
      if(depth === 0){ paramsEnd = i; break; }
    }
  }
  const braceStart = src.indexOf('{', paramsEnd);
  if(braceStart === -1) throw new Error(name + ' body not found');
  depth = 0;
  for(let i = braceStart; i < src.length; i++){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}'){
      depth -= 1;
      if(depth === 0){
        return src.slice(start, i + 1);
      }
    }
  }
  throw new Error(name + ' body did not close');
}
"""

HARNESS_JS_TEMPLATE = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(__UI_SRC__, 'utf8');
__EXTRACT_FN_JS__

const SUBSTANTIVE = 'Full inventory of the sixteen auxiliary slots, grouped by '
  + 'cost tier, with provider, model, trigger frequency, timeout and the three '
  + 'recommended changes; each row also lists the timeout and the observed '
  + 'weekly call volume so the reader can weigh the tradeoffs unaided.';
const SHORT = 'Checking the current config first.';
const FINAL = 'Done - the list is above.';

function makeMessage(text, extra){
  const m = {
    role: 'assistant',
    content: text,
    tool_calls: [{id: 'call-1', function: {name: 'clarify', arguments: '{}'}}],
  };
  return Object.assign(m, extra || {});
}

function buildScene(){
  return {
    version: 'activity_scene_v1',
    mode: 'compact_worklog',
    final_answer: FINAL,
    activity_rows: [
      {role:'prose', kind:'process_prose', source_event_type:'settled_message', text:SUBSTANTIVE, local_id:'prose-big', row_id:'row-prose-big'},
      {role:'prose', kind:'process_prose', source_event_type:'settled_message', text:SHORT, local_id:'prose-small', row_id:'row-prose-small'},
      {role:'thinking', kind:'reasoning', source_event_type:'reasoning', text:'Plan the audit', local_id:'think-1', row_id:'row-think-1'},
      {role:'tool', kind:'tool_completed', source_event_type:'tool_complete', text:'', tool_call_id:'tool-1', tool:{id:'tool-1', name:'clarify'}, row_id:'row-tool-1'},
      {role:'prose', kind:'process_prose', source_event_type:'settled_message', text:FINAL, local_id:'prose-final', row_id:'row-prose-final'},
    ],
  };
}

function runCase(name){
  const transparent = name.indexOf('transparent') !== -1;
  global.window = { _chatActivityDisplayMode: transparent ? 'transparent_stream' : 'compact_worklog' };
  global._anchorSceneToolRowLogicalKey = (row)=>String((row && row.tool_call_id) || (row && row.row_id) || '');
  global._anchorSceneMergeToolRows = (existing)=>existing;
  global._anchorSceneProseDuplicatesFinalAnswer = (text, final)=>String(final || '').indexOf(String(text || '').trim()) !== -1;

  const constIdx = uiSrc.indexOf('const _INTERIM_PROSE_SUBSTANTIVE_MIN_CHARS=');
  if(constIdx !== -1){
    const constEnd = uiSrc.indexOf(';', constIdx);
    eval(uiSrc.slice(constIdx, constEnd + 1));
  }
  if(uiSrc.indexOf('function _interimProseIsSubstantive(') !== -1){
    eval(extractFn(uiSrc, '_interimProseIsSubstantive'));
  }
  eval(extractFn(uiSrc, 'chatActivityMode'));
  eval(extractFn(uiSrc, '_messageHasReasoningPayload'));
  eval(extractFn(uiSrc, '_isAssistantEmptyPlaceholderContent'));
  eval(extractFn(uiSrc, '_assistantMessageBelongsInWorklog'));
  eval(extractFn(uiSrc, '_anchorSceneIsSettledSuccessfulCompression'));
  eval(extractFn(uiSrc, '_anchorSceneRowsForRendering'));

  if(name.indexOf('belongs_') === 0){
    const live = name.indexOf('_live_') !== -1;
    const turnFinal = name.indexOf('_turn_final') !== -1;
    const text = name.indexOf('_textless') !== -1 ? ''
      : (name.indexOf('_short') !== -1 ? SHORT : SUBSTANTIVE);
    const m = makeMessage(text, live ? {_live: true} : {});
    const rawIdx = 2;
    const idxs = new Set([rawIdx]);
    const belongs = _assistantMessageBelongsInWorklog(
      m, rawIdx, idxs, text, {isTurnFinalAssistant: turnFinal},
    );
    return { belongs: !!belongs, textLen: text.trim().length };
  }

  if(name.indexOf('rows_') === 0){
    const settled = name.indexOf('_live') === -1;
    const rows = _anchorSceneRowsForRendering(buildScene(), {settled});
    const kept = (role, localId)=>rows.some((row)=>row.role === role && row.local_id === localId);
    return {
      substantiveProseKept: kept('prose', 'prose-big'),
      shortProseKept: kept('prose', 'prose-small'),
      finalDupKept: kept('prose', 'prose-final'),
      thinkingKept: kept('thinking', 'think-1'),
      toolKept: rows.some((row)=>row.role === 'tool' && row.tool_call_id === 'tool-1'),
      rowCount: rows.length,
    };
  }

  throw new Error('unknown case: ' + name);
}

const output = runCase("__CASE__");
console.log(JSON.stringify(output));
"""


def _run_scenario(case_name: str) -> dict:
    assert NODE is not None, "node is required for the substantive interim prose driver"
    script = (
        HARNESS_JS_TEMPLATE
        .replace("__UI_SRC__", json.dumps(str(ROOT / "static" / "ui.js")))
        .replace("__EXTRACT_FN_JS__", EXTRACT_FN_JS)
        .replace("__CASE__", str(case_name))
    )
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)  # node SIGABRTs on exit when this leaks into the child
    result = subprocess.run(
        [NODE, "-e", script], cwd=str(ROOT), text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def _ui_js_source() -> str:
    return (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_live_interim_substantive_prose_not_folded():
    data = _run_scenario("belongs_live_substantive")
    assert data["textLen"] >= 200
    assert data["belongs"] is False


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_live_interim_short_narration_still_folds():
    data = _run_scenario("belongs_live_short")
    assert data["textLen"] < 200
    assert data["belongs"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_live_turn_final_with_text_stays_visible():
    # #3875 control: the turn-final exemption is length-independent.
    data = _run_scenario("belongs_live_short_turn_final")
    assert data["belongs"] is False


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_live_interim_textless_tool_message_still_folds():
    data = _run_scenario("belongs_live_textless")
    assert data["belongs"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_settled_compact_rail_drops_substantive_prose_row():
    data = _run_scenario("rows_settled_compact")
    assert data["substantiveProseKept"] is False
    assert data["thinkingKept"] is True
    assert data["toolKept"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_settled_compact_rail_keeps_short_narration_row():
    data = _run_scenario("rows_settled_compact")
    assert data["shortProseKept"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_settled_transparent_keeps_substantive_prose_row():
    data = _run_scenario("rows_settled_transparent")
    assert data["substantiveProseKept"] is True
    assert data["shortProseKept"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_live_render_keeps_prose_rows():
    data = _run_scenario("rows_live_compact")
    assert data["substantiveProseKept"] is True
    assert data["shortProseKept"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_settled_final_answer_duplicate_row_still_dropped():
    # #6948 control must survive the new exemption.
    data = _run_scenario("rows_settled_compact")
    assert data["finalDupKept"] is False


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_threshold_helper_defaults_fail_closed():
    # Sliced drivers that do not carry the const must still classify, and the
    # constant must sit next to the helper in the source.
    source = _ui_js_source()
    assert "const _INTERIM_PROSE_SUBSTANTIVE_MIN_CHARS=200;" in source
    assert source.count("function _interimProseIsSubstantive(") == 1


def test_settled_rail_install_spares_substantive_segments():
    """The rail-install loop in _renderSettledAnchorSceneForMessage must skip
    substantive segments; its inline twin (the transparent branch loop) must
    stay untouched."""
    source = _ui_js_source()
    anchor = "_interimProseIsSubstantive(segText)"
    assert source.count(anchor) == 1
    i = source.index(anchor)
    loop_start = source.rindex("if(Number.isFinite(idx)&&idx<rawIdx){", 0, i)
    window = source[loop_start:i + 400]
    assert window.index(anchor) < window.index(
        "node.classList.add('assistant-segment-worklog-source')"
    )
