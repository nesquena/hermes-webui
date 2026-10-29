"""Tool replay must not serialize the entire live turn once per SSE event.

Execute the production tool listeners and snapshot scheduler with a virtual
clock. Presentation helpers are stubbed; snapshotLiveTurnHtmlForSession is the
real ui.js function. This measures the serialization seam, not scene painting.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _function(source, name):
    start = source.index(f"function {name}(")
    # Parameters may contain a default object literal.
    params = source.index("(", start)
    depth = 1
    pos = params + 1
    while depth:
        depth += (source[pos] == "(") - (source[pos] == ")")
        pos += 1
    brace = source.index("{", pos)
    depth = 1
    pos = brace + 1
    while depth:
        depth += (source[pos] == "{") - (source[pos] == "}")
        pos += 1
    return source[start:pos]


def _listener(source, event):
    marker = f"source.addEventListener('{event}',e=>{{"
    start = source.index(marker)
    brace = start + len(marker) - 1
    depth = 1
    pos = brace + 1
    while depth:
        depth += (source[pos] == "{") - (source[pos] == "}")
        pos += 1
    return source[start:pos] + ");"


def _harness(messages=None):
    if messages is None:
        messages = (ROOT / "static/messages.js").read_text()
    ui = (ROOT / "static/ui.js").read_text()
    scheduler = messages[
        messages.index("  let _snapshotLiveTurnTimer=null;"):
        messages.index("  // Throttled variant for token-by-token updates.")
    ]
    dispose_start = messages.index("LIVE_STREAMS[activeSid]={streamId,source,dispose:")
    dispose_end = messages.index("    _bindSidebarIdleRecovery", dispose_start)
    script = """
const activeSid='session-a', streamId='stream-a';
let _streamFinalized=false, _terminalStateReached=false, _freshSegment=false, assistantText='', segmentStart=0;
let assistantRow=null, assistantBody=null;
const S={session:{session_id:activeSid},activeStreamId:streamId};
const INFLIGHT={[activeSid]:{}}, LIVE_STREAMS={};
let snapshots=0, html='initial';
const turn={dataset:{sessionId:activeSid},querySelectorAll:()=>[],
  get outerHTML(){snapshots++;return html;}};
function $(id){return id==='liveAssistantTurn'?turn:null;}
let nextTimer=1;
const timers=new Map(), cancelled=[];
function setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{fn,delay});return id;}
function clearTimeout(id){if(timers.has(id))cancelled.push(timers.get(id).fn);timers.delete(id);}
function drain(){const pending=[...timers.values()];timers.clear();pending.forEach(t=>t.fn());}
let observed=[];
function upsertLiveToolCall(d,phase){observed.push([d.tid,phase]);return {...d};}
function _completeAutomaticCompressionOnLiveProgress(){}
function _parseStreamState(){return {displayText:''};}
function _stripXmlToolCalls(s){return s;}
function _applyToAnchor(){}
function _flushPendingSegmentRender(){}
function appendLiveToolCard(){}
function _smdEndParser(){}
function _resetAssistantSegment(){}
function scrollIfPinned(){}
function _maybeNotifyPersistentStateSaved(){}
function _cancelAnimationFramePendingStreamRender(){}
function _resumeSessionStreamAfterLiveChat(){}
const source={listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}};
function emit(name,i){source.listeners[name]({data:JSON.stringify({name:'terminal',tid:'tool-'+i})});}
"""
    script += _function(ui, "snapshotLiveTurnHtmlForSession")
    script += _function(messages, "snapshotLiveTurn") + scheduler
    script += _function(messages, "closeLiveStream")
    script += messages[dispose_start:dispose_end]
    script += _listener(messages, "tool") + _listener(messages, "tool_complete")
    return script


def _run(case):
    if not NODE:
        pytest.skip("node is required")
    script = "const assert=require('node:assert/strict');\n" + _harness() + case
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_tool_burst_keeps_every_event_but_takes_one_latest_snapshot():
    result = _run("""
for(let i=0;i<500;i++){
  html='tool-'+i;
  emit('tool',i);emit('tool_complete',i);
}
const immediate=snapshots, queued=timers.size;
html='latest-rendered-state';drain();
console.log(JSON.stringify({immediate,queued,snapshots,saved:INFLIGHT[activeSid].liveTurnHtml,events:observed.length}));
""")
    assert result == {
        "immediate": 0, "queued": 1, "snapshots": 1,
        "saved": "latest-rendered-state", "events": 1000,
    }


def test_snapshot_throttle_is_not_a_resetting_debounce():
    result = _run("""
emit('tool',1);const timer=[...timers.keys()][0];
emit('tool_complete',1);
assert.equal([...timers.keys()][0],timer);
drain();emit('tool',2);drain();
console.log(JSON.stringify({snapshots}));
""")
    assert result["snapshots"] == 2


@pytest.mark.parametrize("reconnecting", [False, True])
def test_dispose_cancels_snapshot_even_if_cancelled_callback_runs(reconnecting):
    result = _run(f"""
_throttledSnapshotLiveTurn();
const old=[...timers.values()].map(t=>t.fn);
LIVE_STREAMS[activeSid].dispose({str(reconnecting).lower()});
html='replacement-turn';
old.forEach(fn=>fn());
console.log(JSON.stringify({{queued:timers.size,snapshots,saved:INFLIGHT[activeSid].liveTurnHtml||null}}));
""")
    assert result == {"queued": 0, "snapshots": 0, "saved": None}


def test_session_switch_still_captures_current_dom_synchronously():
    result = _run("""
emit('tool',1);
html='exact-state-at-switch';closeLiveStream(activeSid,streamId,source);
S.session={session_id:'session-b'};turn.dataset.sessionId='session-b';
drain();
console.log(JSON.stringify({snapshots,saved:INFLIGHT[activeSid].liveTurnHtml}));
""")
    assert result == {"snapshots": 1, "saved": "exact-state-at-switch"}


def test_cancelled_snapshot_cannot_clear_or_run_a_rearmed_timer():
    result = _run("""
_throttledSnapshotLiveTurn();const old=[...timers.values()][0].fn;
_cancelThrottledSnapshotTimer();_throttledSnapshotLiveTurn();
old();const before=snapshots;drain();
console.log(JSON.stringify({before,snapshots}));
""")
    assert result == {"before": 0, "snapshots": 1}


def test_finalized_stream_does_not_schedule_a_snapshot():
    result = _run("""
_streamFinalized=true;_throttledSnapshotLiveTurn();
console.log(JSON.stringify({queued:timers.size}));
""")
    assert result == {"queued": 0}
