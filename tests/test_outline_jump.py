"""Run the real _jumpToMessage against a small deterministic DOM."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node required")

DRIVER = r"""
const fs=require('fs');
const source=fs.readFileSync(process.argv[1], 'utf8');
const scenario=JSON.parse(process.argv[2]);
function extract(name){
 const start=source.search(new RegExp('(?:async\\s+)?function\\s+'+name+'\\s*\\('));
 if(start<0) throw Error('missing '+name);
 let i=source.indexOf('{',start),depth=1; i++;
 while(depth&&i<source.length){if(source[i]==='{')depth++;if(source[i]==='}')depth--;i++;}
 return source.slice(start,i);
}
const events=[], frames=[], timers=[];
global.window=global;
global.S={session:{session_id:'a'},messages:[{role:'user',content:'tail',_turnUsage:7}]};
global._messagesTruncated=!!scenario.truncated;
global._scrollPinned=true;global._messageUserUnpinned=false;
global._messageRenderWindowSize=30;
let exists=!!scenario.row,settling=true;
const container={scrollTop:900};
const row={dataset:{sessionMsgIdx:scenario.stale?900:0},scrollIntoView:()=>{events.push(owned?'scroll':'scroll-unowned');container.scrollTop=100;},classList:{remove(){},add(){}},offsetWidth:1};
global.document={getElementById:id=>id==='messages'?container:(exists?row:null)};
global.requestAnimationFrame=f=>frames.push(f);
window.setTimeout=f=>timers.push(f);
// A load-time bottom-settle timer armed before the click writes the bottom unless cancelled.
timers.push(()=>{if(settling){container.scrollTop=900;events.push('settle-bottom');}});
global._cancelBottomSettle=()=>{settling=false;events.push('cancel');};
// Scroll ownership lapses when its settle timer fires (300 ms in ui.js); a long
// render outlasts it, so the jump must take ownership again before scrolling.
let owned=false;
global._beginMessageJumpScroll=()=>{owned=true;_scrollPinned=false;_messageUserUnpinned=true;events.push('begin');};
global._currentMessageRenderWindowSize=()=>_messageRenderWindowSize;
global._messageRenderableMessageCount=()=>130;
global._messageSessionIndexForRawIdx=n=>n;
// While bottom settling is armed, a render pulls the viewport back to the end.
global.renderMessages=()=>{events.push('render',_messageUserUnpinned?'unpinned':'pinned');exists=true;row.dataset.sessionMsgIdx=0;owned=false;if(settling)frames.push(()=>{container.scrollTop=900;events.push('bottom-retry');});};
global._flashRow=()=>{};
global._currentSid=()=>S.session.session_id;
global._ensureAllMessagesLoaded=async()=>{
 events.push('ensure');
 if(scenario.switchAwait){S.session={session_id:'b'};return;}
 if(_messagesTruncated){events.push('fetch');_messagesTruncated=false;}
};
global.api=async()=>{events.push('direct-api');return {session:{messages:[{role:'user',content:'first'}]}};};
for(const name of ['_expandOutlineRenderWindow','_jumpToMessage'])eval(extract(name));
(async()=>{
 await _jumpToMessage(0);
 if(scenario.switchFrame){S.session={session_id:'b'};container.scrollTop=777;}
 for(let i=0;i<8;i++){await Promise.resolve();while(frames.length)frames.shift()();while(timers.length)timers.shift()();}
 console.log(JSON.stringify({events,top:container.scrollTop}));
})().catch(e=>{console.error(e);process.exitCode=1});
"""


def run(**scenario):
    src = ROOT / "static" / "outline.js"
    result = subprocess.run([NODE, "-e", DRIVER, str(src), json.dumps(scenario)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("row", [False, True], ids=["row-off-screen", "row-on-screen"])
def test_jump_lands_despite_pending_bottom_settle(row):
    result = run(row=row)
    assert result["top"] == 100
    assert "pinned" not in result["events"]
    assert "bottom-retry" not in result["events"]
    assert "settle-bottom" not in result["events"]
    # The scroll happens while the jump owns scrolling (re-taken after a render).
    assert "scroll" in result["events"]
    assert "scroll-unowned" not in result["events"]


def test_loaded_history_is_not_downloaded_again():
    result = run()
    assert "direct-api" not in result["events"]
    assert "fetch" not in result["events"]
    assert "render" in result["events"]


def test_truncated_history_loads_through_the_shared_loader():
    result = run(truncated=True)
    assert "direct-api" not in result["events"]
    assert "fetch" in result["events"]
    assert result["top"] == 100


def test_tail_row_with_the_same_local_id_is_not_the_target():
    result = run(row=True, stale=True)
    assert result["events"].index("render") < result["events"].index("scroll")


def test_session_switch_leaves_the_new_session_alone():
    assert "render" not in run(truncated=True, switchAwait=True)["events"]
    assert run(switchFrame=True)["top"] == 777
