"""Streaming frame budget: the live activity scene must not repaint unchanged.

`renderLiveAnchorActivityScene` (and its transparent_stream sibling) repaint the
live worklog by tearing the whole row list down and rebuilding every row,
bracketed by `_captureMessageScrollSnapshot()` before and
`_restoreMessageScrollSnapshotSameFrame()` after. Both brackets read layout
(`scrollHeight`/`clientHeight`/`getBoundingClientRect`) right around those DOM
writes, so each repaint forces a synchronous layout of the ENTIRE transcript —
cost proportional to the whole session, not to what changed.

During a live turn the scene is *requested* far more often than it changes: one
`_doRender` asks twice (`_renderLiveThinking` -> `appendThinking`, then
`_upsertAnchorProcessProse` -> `_renderAnchorLiveScene`) and every `reasoning`
SSE event asks again. Measured on a 2000-message session with a live streaming
turn, that was 26.4 repaints/s against a 15 fps render throttle and ~49% of
wall-clock, with roughly half of the repaints producing byte-identical DOM.

These tests drive the REAL production renderers over a mock DOM and count the
expensive work. They fail on the pre-fix code (every request repaints) and pass
only when an unchanged scene is skipped — while still repainting the moment the
scene, the mode, the stream, or the painted DOM changes (fail-closed).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
UI_JS_PATH = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Helpers introduced by the fix. They are extracted when present; when absent
# (pre-fix tree) the harness substitutes inert stubs so the production renderer
# still runs and the test fails on the repaint COUNT rather than on an import
# error.
MEMO_HELPERS = (
    "_liveAnchorSceneRepaintKey",
    "_liveAnchorSceneValueUnchanged",
    "_liveAnchorSceneRepaintSkip",
    "_liveAnchorSceneRepaintRemember",
)
RENDERERS = (
    "renderLiveAnchorActivityScene",
    "_renderLiveAnchorActivitySceneTransparent",
)


def _run_node(source: str) -> dict:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip())


def _extract_js_function(js: str, name: str) -> str | None:
    """Extract a top-level production function body without eval'ing the file."""
    marker = f"function {name}("
    start = js.find(marker)
    if start < 0:
        return None
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    index = js.index("{", start)
    while index < len(js):
        char = js[index]
        next_char = js[index + 1] if index + 1 < len(js) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return js[start : index + 1]
        index += 1
    raise AssertionError(f"JavaScript function {name} did not close")


_STUBS = {
    "_liveAnchorSceneRepaintKey": "function _liveAnchorSceneRepaintKey(){return '';}",
    "_liveAnchorSceneValueUnchanged": "function _liveAnchorSceneValueUnchanged(){return false;}",
    "_liveAnchorSceneRepaintSkip": "function _liveAnchorSceneRepaintSkip(){return undefined;}",
    "_liveAnchorSceneRepaintRemember": (
        "function _liveAnchorSceneRepaintRemember(k,r,s,t,c,result){return result;}"
    ),
}


def _production() -> str:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    parts = []
    for name in MEMO_HELPERS:
        extracted = _extract_js_function(js, name)
        parts.append(extracted if extracted else _STUBS[name])
    for name in RENDERERS:
        extracted = _extract_js_function(js, name)
        assert extracted, f"{name} missing from static/ui.js"
        parts.append(extracted)
    return "\n".join(parts)


_HARNESS = r"""
let _liveAnchorSceneRepaintMemo=null;

// ---- minimal DOM ----------------------------------------------------------
let nodeSeq=0;
function el(tag){
  const node={
    tag, id:'', _id:++nodeSeq, isConnected:true, children:[], parent:null,
    attrs:Object.create(null), dataset:{}, style:{}, hidden:false,
    classList:{add(){},remove(){},toggle(){},contains(){return false;}},
    setAttribute(k,v){this.attrs[k]=String(v);},
    getAttribute(k){return Object.prototype.hasOwnProperty.call(this.attrs,k)?this.attrs[k]:null;},
    removeAttribute(k){delete this.attrs[k];},
    appendChild(child){child.parent=this;this.children.push(child);return child;},
    removeChild(child){const i=this.children.indexOf(child);if(i>=0)this.children.splice(i,1);child.isConnected=false;},
    remove(){if(this.parent)this.parent.removeChild(this);this.isConnected=false;},
    querySelector(){return null;},
    querySelectorAll(){return [];},
    closest(){return null;},
    matches(){return false;},
    get childElementCount(){return this.children.length;},
  };
  return node;
}
const messagesEl=el('div');
const msgInner=el('div');
const emptyState=el('div');
const turn=el('div');
turn.id='liveAssistantTurn';
const blocks=el('div');
const group=el('div');
const worklogList=el('div');
group.appendChild(worklogList);
msgInner.appendChild(turn);
turn.appendChild(blocks);
function $(id){
  if(id==='messages')return messagesEl;
  if(id==='msgInner')return msgInner;
  if(id==='emptyState')return emptyState;
  if(id==='liveAssistantTurn')return turn;
  return null;
}
const window={};
const document={getElementById:$};

// ---- instrumented seams (what the fix is supposed to stop paying for) -----
const counters={rebuilds:0,captures:0,restores:0,transparentRows:0};
function _captureMessageScrollSnapshot(){counters.captures++;return {pinned:true,bottom:0,top:0};}
function _restoreMessageScrollSnapshotSameFrame(){counters.restores++;}
function _prepareLiveAnchorScrollRebuildGuard(){return {readerAwayFromBottom:false,release:null};}
function _restoreLiveAnchorScrollSnapshotAfterRebuild(){}
function _renderAnchorSceneRowsIntoWorklog(g,rows){
  counters.rebuilds++;
  worklogList.children.length=0;
  for(const row of rows) worklogList.appendChild(el('div'));
  return true;
}
function _toolWorklogListEl(){return worklogList;}
function _anchorSceneWorklogGroup(){blocks.children.includes(group)||blocks.appendChild(group);return group;}
function _assistantTurnBlocks(){return blocks;}
function _createAssistantTurn(){return turn;}
function _captureWorklogDetailDisclosureState(){return null;}
function _restoreWorklogDetailDisclosureState(){}
function _startActivityElapsedTimer(){}
function _dedupeLiveProcessedWorklogAnchors(){}
function _moveLiveRunStatusToTurnEnd(){}
function _syncToolCallGroupSummary(){}
function _syncTransparentEventControls(){}
function _resetMismatchedLiveAssistantTurnForSession(){return true;}
function _worklogDetailsExpandedDefault(){return false;}
function scrollIfPinned(){}
function isSimplifiedToolCalling(){return true;}
function _anchorSceneRowsForRendering(scene){return (scene&&scene.activity_rows)||[];}
function _transparentLiveRowKey(){return '';}
function _transparentLiveRowsCompatible(){return false;}
function _refreshTransparentLiveRow(existing){return existing;}
function _anchorSceneRowTimestampSeconds(){return null;}
function _anchorSceneTransparentNodeForRow(){counters.transparentRows++;return el('div');}

let MODE='compact_worklog';
function chatActivityMode(){return MODE;}

const S={session:{session_id:'sid-1',pending_started_at:11},activeStreamId:'stream-1'};

// A fresh-but-equal projection of the same scene, exactly like the real
// projectAssistantTurnAnchorActivityScene() which rebuilds frozen row objects
// on every call. Reference equality must NOT be what makes the memo hit.
function scene(proseText, toolDone){
  return {activity_rows:[
    {row_id:'r-prose', local_id:'live-prose:stream-1:1', role:'prose', kind:'process_prose',
     status:'running', source_event_type:'token', display_hint:'main_prose',
     text:proseText, thinking:null, tool:null,
     payload:{text:proseText, activitySegmentSeq:1, activityBurstId:0},
     group:{group_key:'segment:1', activity_burst_id:0, activity_segment_seq:1, assistant_msg_idx:null}},
    {row_id:'r-tool', local_id:'live-tool:1', role:'tool', kind:'tool_started',
     status: toolDone?'completed':'running', source_event_type:'tool', display_hint:'tool_row',
     text:'', thinking:null,
     tool:{id:'t1', name:'read_file', args:{path:'a.js'}, preview:'reading a.js',
           snippet:'', done:!!toolDone, is_error:false, duration:null,
           started_at:5, signature:'read_file|t1|{"path":"a.js"}'},
     payload:{name:'read_file', tid:'t1', args:{path:'a.js'}},
     group:{group_key:'segment:1', activity_burst_id:0, activity_segment_seq:1, assistant_msg_idx:null}},
  ]};
}
function render(sc){return renderLiveAnchorActivityScene('stream-1',sc,{sessionId:'sid-1'});}
"""


def _script(body: str) -> str:
    return _production() + _HARNESS + body


def test_identical_scene_is_not_repainted_and_still_reports_rendered():
    """Two back-to-back requests for the SAME scene must repaint once, not twice.

    The second call is what `_doRender` issues via `_renderLiveThinking` when no
    reasoning arrived; pre-fix it tore the worklog down and rebuilt it, paying a
    second full-transcript capture/restore reflow pair for identical DOM.
    """
    out = _run_node(
        _script(
            """
const first=render(scene('hello world'));
const second=render(scene('hello world'));
const third=render(scene('hello world'));
console.log(JSON.stringify({first,second,third,...counters,
  paintedRows:worklogList.childElementCount}));
"""
        )
    )
    assert out["first"] is True
    # Callers (e.g. _upsertAnchorReasoning) branch on this boolean to decide
    # whether to fall back to the legacy thinking card — a skip must not lie.
    assert out["second"] is True
    assert out["third"] is True
    assert out["rebuilds"] == 1
    assert out["captures"] == 1
    assert out["restores"] == 1
    assert out["paintedRows"] == 2


def test_changed_prose_still_repaints():
    """The memo must never freeze a live turn: new streamed text repaints."""
    out = _run_node(
        _script(
            """
render(scene('hello'));
render(scene('hello'));
render(scene('hello world'));
render(scene('hello world'));
render(scene('hello world again'));
console.log(JSON.stringify({...counters}));
"""
        )
    )
    assert out["rebuilds"] == 3
    assert out["captures"] == 3


def test_tool_completion_repaints():
    """A row field change other than text (tool done/status) must repaint."""
    out = _run_node(
        _script(
            """
render(scene('x'));
render(scene('x'));
render(scene('x', true));
console.log(JSON.stringify({...counters}));
"""
        )
    )
    assert out["rebuilds"] == 2


def test_dom_tampered_behind_the_memo_repaints_fail_closed():
    """If anything removes painted rows, an identical scene must repaint."""
    out = _run_node(
        _script(
            """
render(scene('x'));
worklogList.children.pop();          // e.g. removeThinking() / a dedupe pass
const second=render(scene('x'));
console.log(JSON.stringify({second,...counters,paintedRows:worklogList.childElementCount}));
"""
        )
    )
    assert out["second"] is True
    assert out["rebuilds"] == 2
    assert out["paintedRows"] == 2


def test_detached_turn_repaints_fail_closed():
    """A live turn replaced behind our back (reload/session restore) repaints."""
    out = _run_node(
        _script(
            """
render(scene('x'));
turn.isConnected=false;              // e.g. renderMessages() rebuilt #msgInner
render(scene('x'));
console.log(JSON.stringify({...counters}));
"""
        )
    )
    assert out["rebuilds"] == 2


def test_stream_switch_repaints():
    """A different stream id must not inherit the previous stream's paint."""
    out = _run_node(
        _script(
            """
render(scene('x'));
S.activeStreamId='stream-2';
renderLiveAnchorActivityScene('stream-2',scene('x'),{sessionId:'sid-1'});
console.log(JSON.stringify({...counters}));
"""
        )
    )
    assert out["rebuilds"] == 2


def test_session_switch_repaints():
    """A different session must not inherit the previous session's paint."""
    out = _run_node(
        _script(
            """
render(scene('x'));
S.session={session_id:'sid-2',pending_started_at:11};
renderLiveAnchorActivityScene('stream-1',scene('x'),{sessionId:'sid-2'});
console.log(JSON.stringify({...counters}));
"""
        )
    )
    assert out["rebuilds"] == 2


def test_transparent_stream_mode_is_guarded_too():
    """The sibling renderer pays the same reflow pair and needs the same guard."""
    out = _run_node(
        _script(
            """
MODE='transparent_stream';
const first=render(scene('hello'));
const second=render(scene('hello'));
const third=render(scene('hello there'));
console.log(JSON.stringify({first,second,third,...counters}));
"""
        )
    )
    assert out["first"] is True
    assert out["second"] is True
    assert out["third"] is True
    # Two paints for three requests: the identical middle request is skipped.
    assert out["captures"] == 2
    assert out["restores"] == 2


def test_mode_switch_repaints():
    """Switching activity display mode must repaint, not reuse the other mode."""
    out = _run_node(
        _script(
            """
render(scene('x'));
MODE='transparent_stream';
render(scene('x'));
console.log(JSON.stringify({...counters}));
"""
        )
    )
    assert out["captures"] == 2
