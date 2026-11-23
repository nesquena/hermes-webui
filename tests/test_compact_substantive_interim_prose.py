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
      {role:'prose', kind:'process_prose', source_event_type:'settled_message', text:SUBSTANTIVE + ' Bare string variant.', local_id:'prose-bare', row_id:'row-prose-bare', _inline_prose_visible:false},
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
      bareStringProseKept: kept('prose', 'prose-bare'),
      shortProseKept: kept('prose', 'prose-small'),
      finalDupKept: kept('prose', 'prose-final'),
      thinkingKept: kept('thinking', 'think-1'),
      toolKept: rows.some((row)=>row.role === 'tool' && row.tool_call_id === 'tool-1'),
      rowCount: rows.length,
    };
  }

  if(name === 'reloaded_compact'){
    const {message, text} = __RELOADED__;
    const scene = message._anchor_activity_scene;
    eval(extractFn(uiSrc, 'msgContent'));
    eval(extractFn(uiSrc, '_anchorSceneProseMatchesFinalAnswer'));
    eval(extractFn(uiSrc, '_anchorSceneProseDuplicatesFinalAnswer'));
    const rows = _anchorSceneRowsForRendering(scene, {settled:true});
    const matchingProse = (items)=>items.filter(row=>row.role === 'prose' && row.text.trim() === text).length;
    const inline = msgContent(message);
    return {
      hydratedCopies: matchingProse(scene.activity_rows),
      railCopies: matchingProse(rows),
      inlineCopy: inline.includes(text),
      finalInline: inline.includes('The document is ready.'),
      toolCopies: rows.filter(row=>row.role === 'tool').length,
    };
  }
  if(name.indexOf('render_')===0){
    // Drive the REAL settled renderers end-to-end over a fake DOM: the
    // compact renderer must spare substantive interim segments, its inline
    // twin (transparent branch) must keep folding them.
    const compact=name.indexOf('_compact')!==-1;
    const targetName=compact?'_renderSettledAnchorSceneForMessage':'_renderSettledAnchorSceneTransparentForMessage';
    eval(extractFn(uiSrc,targetName));
    function mkSeg(text){
      const classes=new Set();
      const attrs={};
      return {
        dataset:{rawText:text},
        hidden:false,
        classList:{add:(c)=>classes.add(c),remove:(c)=>classes.delete(c),contains:(c)=>classes.has(c)},
        getAttribute:(n)=>(n in attrs?attrs[n]:null),
        setAttribute:(n,v)=>{attrs[n]=String(v);},
        removeAttribute:(n)=>{delete attrs[n];},
      };
    }
    const segSubstantive=mkSeg(SUBSTANTIVE);
    const segShort=mkSeg(SHORT);
    const segs=[segSubstantive,segShort];
    const blocks={
      querySelectorAll(sel){
        if(sel.indexOf('.assistant-segment[data-msg-idx]')!==-1) return segs;
        return [];
      },
      insertBefore:(n)=>n,
      appendChild:(n)=>n,
    };
    const group={
      classList:{contains:()=>false,add:()=>{},remove:()=>{}},
      setAttribute:()=>{},
      removeAttribute:()=>{},
      _deferredWorklogRows:null,
    };
    const turn={
      querySelectorAll:()=>[],
      setAttribute:()=>{},
      removeAttribute:()=>{},
      getAttribute:()=>null,
      classList:{contains:()=>false,add:()=>{},remove:()=>{}},
    };
    const scene=buildScene();
    const message={role:'assistant',content:SUBSTANTIVE,_anchor_activity_scene:scene,_anchor_stream_id:'st-1',_turnDuration:12};
    const segment={dataset:{},closest:()=>turn,parentElement:blocks,setAttribute:()=>{},removeAttribute:()=>{},getAttribute:()=>null,classList:{add:()=>{},contains:()=>false,remove:()=>{}}};
    global._assistantTurnBlocks=()=>blocks;
    global.S={session:{session_id:'test-session'},activeStreamId:null};
    global._anchorSceneSceneHasWorklogWorthyRows=()=>true;
    global._anchorSceneRowsForRendering=(sc)=>sc.activity_rows;
    global.isTransparentStream=()=>!compact;
    global.isCompactWorklogMode=()=>compact;
    global._shouldKeepSettledWorklogOpenForStreamSettle=()=>false;
    global._readActivityDisclosureState=()=>undefined;
    global._copyActivityDisclosureState=()=>{};
    global._anchorSceneHasErroredTerminalState=()=>false;
    global._anchorSceneWorklogGroup=()=>group;
    global._renderAnchorSceneRowsIntoWorklog=()=>true;
    global._anchorSceneLastNonTerminalWorkRowIndex=()=>-1;
    global._assistantAnchorSceneFinalAnswerText=()=>FINAL;
    global.msgContent=()=>FINAL;
    global._transparentRevealKey=()=>'reveal-key';
    global._transparentRevealedTurns=new Set();
    global._TRANSPARENT_SETTLED_ROW_CAP=30;
    global._TRANSPARENT_SETTLED_ROW_CAP_SLACK=10;
    global._anchorSceneTransparentNodeForRow=()=>({setAttribute:()=>{}});
    global._syncTransparentEventControls=()=>{};
    let returned=null;
    let threw=null;
    try{
      returned=compact
        ?_renderSettledAnchorSceneForMessage(message,segment,2)
        :_renderSettledAnchorSceneTransparentForMessage(message,segment,2);
    }catch(err){
      threw=String((err&&err.message)||err);
    }
    const folded=(seg)=>!!(seg.hidden||seg.getAttribute('aria-hidden')==='true'||seg.classList.contains('assistant-segment-worklog-source'));
    return {
      target:targetName,
      threw,
      returned:returned===null?null:!!returned,
      substantiveFolded:folded(segSubstantive),
      shortFolded:folded(segShort),
    };
  }
  throw new Error('unknown case: ' + name);
}

const output = runCase("__CASE__");
console.log(JSON.stringify(output));
"""


def _run_scenario(case_name: str, reloaded: dict | None = None) -> dict:
    assert NODE is not None, "node is required for the substantive interim prose driver"
    script = (
        HARNESS_JS_TEMPLATE
        .replace("__UI_SRC__", json.dumps(str(ROOT / "static" / "ui.js")))
        .replace("__EXTRACT_FN_JS__", EXTRACT_FN_JS)
        .replace("__CASE__", str(case_name))
        .replace("__RELOADED__", json.dumps(reloaded))
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
def test_settled_compact_keeps_prose_with_no_inline_copy():
    """A prose row whose text never reaches the inline body (e.g. a bare string
    part, which msgContent() drops) must keep its rail row — dropping it would
    erase the only rendered copy. Rows known to be inline-visible still drop."""
    data = _run_scenario("rows_settled_compact")
    assert data["bareStringProseKept"] is True
    assert data["substantiveProseKept"] is False


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
@pytest.mark.parametrize("part_type", [None, "input_text", "output_text", "text"])
@pytest.mark.parametrize("saved_prose", [False, True])
def test_reloaded_compact_worklog_keeps_the_only_copy_of_long_prose(part_type, saved_prose):
    from api import routes

    text = "The inspection recorded which document entries were complete and which still needed work. " * 4
    text = text.strip()
    part = {"type": part_type, "text": text} if part_type is not None else text
    message = {
        "role": "assistant",
        "content": [part, {"type": "tool_use", "id": "read-1", "name": "read_file", "input": {}},
                    {"type": "text", "text": "The document is ready."}],
    }
    messages = [{"role": "user", "content": "Inspect the example document."}, message]
    ref = routes._assistant_anchor_scene_message_ref(message)
    rows = [
        {"role": "prose", "kind": "process_prose", "row_id": "recorded-prose", "text": text},
    ] if saved_prose else []
    records = {
        ref: {
            "message_ref": ref,
            "message_index": 1,
            "stream_id": "example-stream",
            "scene": {"version": "activity_scene_v1", "mode": "compact_worklog", "activity_rows": rows},
        },
    }
    reloaded = routes._hydrate_anchor_activity_scenes(messages, records)[1]
    output = _run_scenario("reloaded_compact", {"message": reloaded, "text": text})
    assert output["hydratedCopies"] == 1
    assert output["finalInline"] is True
    assert output["toolCopies"] == 1
    # Only the actual text part is visible inline; all other variants must be
    # present in the server-hydrated Worklog after the frontend filters it.
    inline = part_type == "text"
    assert output["inlineCopy"] is inline
    assert output["railCopies"] == (0 if inline else 1)


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


def test_content_part_rows_mark_inline_visibility():
    """Bare string and input_text/output_text parts never reach the inline body
    (msgContent() joins only `type==='text'`), so their rows must carry the
    marker the settled renderer keys on to keep the rail copy."""
    src = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    anchor = "const row=_anchorSceneProseRow(text,rows.length,messageIndex);"
    occurrences = src.count(anchor)
    assert occurrences == 2, occurrences
    first = src.index(anchor)
    assert "row._inline_prose_visible=false" in src[first:first + 500]
    second = src.index(anchor, first + 1)
    assert "if(part.type!=='text') row._inline_prose_visible=false" in src[second:second + 500]


def _js_function_body(source: str, name: str) -> str:
    """Slice a top-level `function name(...){...}` out of ui.js by brace
    matching — precise scoping without regex or nearby-context guesses."""
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"{name} body did not close")


def test_settled_rail_install_spares_substantive_segments():
    """The rail-install guard must live in _renderSettledAnchorSceneForMessage
    and NOWHERE else. Its inline twin (_renderSettledAnchorSceneTransparentForMessage)
    contains the same fold-loop text, so a short-context grep cannot tell the
    two apart — scope the assertion to each function body. The twin keeps
    folding every segment (the transparent rail owns the copies)."""
    source = _ui_js_source()
    anchor = "_interimProseIsSubstantive(segText)"
    assert source.count(anchor) == 1
    target = _js_function_body(source, "_renderSettledAnchorSceneForMessage")
    twin = _js_function_body(source, "_renderSettledAnchorSceneTransparentForMessage")
    assert anchor in target, "guard is not inside the settled compact renderer"
    assert "_interimProseIsSubstantive" not in twin, "guard leaked into the transparent twin"
    # The guard runs before the fold, inside the rawIdx loop.
    assert target.index(anchor) < target.index(
        "node.classList.add('assistant-segment-worklog-source')"
    )


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_settled_compact_renderer_spares_substantive_segment():
    """Drives the REAL settled compact renderer end-to-end over a fake DOM:
    a substantive interim segment must stay visible (its rail row is dropped),
    while short narration keeps folding behind the rail."""
    data = _run_scenario("render_settled_compact")
    assert data["threw"] is None, data["threw"]
    assert data["returned"] is True
    assert data["substantiveFolded"] is False
    assert data["shortFolded"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the substantive interim prose driver")
def test_transparent_twin_still_folds_substantive_segment():
    """The inline twin must keep folding every segment — the transparent rail
    owns the copies (chronological contract), so the guard has no business
    there. This is the red side of a guard mis-landed into the twin."""
    data = _run_scenario("render_settled_transparent")
    assert data["threw"] is None, data["threw"]
    assert data["returned"] is True
    assert data["substantiveFolded"] is True
    assert data["shortFolded"] is True
