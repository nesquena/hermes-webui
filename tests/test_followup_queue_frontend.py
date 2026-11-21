"""Browser side of the server-side follow-up FIFO: runs the real ui.js queue functions in node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MSG = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
CMD = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node required")


def _src():
    start = UI.index("function _getSessionQueue(")
    end = UI.index("function getQueuedSessionCount(")
    return UI[start:end]


def _run(script):
    prelude = """
const SESSION_QUEUES={}; const _queueRenderKeys={};
const store=()=>{const m={};return {getItem:k=>k in m?m[k]:null,setItem:(k,v)=>{m[k]=String(v)},removeItem:k=>{delete m[k]},_m:m}};
global.sessionStorage=store(); global.localStorage=store();
const toasts=[]; function showToast(m){toasts.push(m)}
let apiCalls=[]; let apiImpl=async()=>({items:[]});
async function api(p,o){apiCalls.push([p,o&&o.body?JSON.parse(o.body):null]);return apiImpl(p,o)}
function updateQueueBadge(){}
"""
    code = prelude + _src() + "\n(async()=>{" + script + "})().catch(e=>{console.log('ERR '+e.stack);process.exit(1)})"
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_server_entries_render_in_order_and_are_never_persisted_or_locally_drained():
    out = _run("""
apiImpl=async()=>({items:[{id:'a',text:'one',status:'queued',queued_at:1},{id:'b',text:'two',status:'processing',queued_at:2}]});
await syncServerQueue('s1');
queueSessionMessage('s1',{text:'with file',files:[{name:'f'}]});
const mirrored=SESSION_QUEUES.s1.map(e=>e.text+':'+(e._status||'local'));
const shifted=shiftQueuedSessionMessage('s1');  // only the browser-held entry may be shifted locally
const persisted=localStorage._m['hermes-queue-s1']||null;
console.log(JSON.stringify({mirrored,shifted:shifted&&shifted.text,remaining:SESSION_QUEUES.s1.map(e=>e.text),persisted,
  serverOwns:_hasServerQueueEntries('s1')}));
""")
    assert out["mirrored"] == ["one:queued", "two:processing", "with file:local"]
    assert out["shifted"] == "with file"
    assert out["remaining"] == ["one", "two"]
    assert out["persisted"] is None  # server-owned entries never hit localStorage (no double-run on reload)
    assert out["serverOwns"] is True


def test_enqueue_failure_is_reported_and_returns_false():
    out = _run("""
apiImpl=async()=>{const e=new Error('queue is full');throw e};
const ok=await enqueueServerFollowup('s1','x','cid');
console.log(JSON.stringify({ok,toast:toasts[0],q:SESSION_QUEUES.s1||null}));
""")
    assert out["ok"] is False and "queue is full" in out["toast"] and out["q"] is None


def test_enqueue_sends_idempotent_client_id_and_mirrors_backend_state():
    out = _run("""
apiImpl=async()=>({items:[{id:'z',text:'hi',status:'queued',queued_at:5}]});
const ok=await enqueueServerFollowup('s1','hi','cid-1');
console.log(JSON.stringify({ok,call:apiCalls[0],q:SESSION_QUEUES.s1.map(e=>e._server_id)}));
""")
    assert out["ok"] is True and out["call"][0] == "/api/chat/queue"
    assert out["call"][1] == {"session_id": "s1", "text": "hi", "client_id": "cid-1"}
    assert out["q"] == ["z"]


def test_reconnect_migration_hands_over_every_entry_not_just_the_first():
    out = _run("""
const entries=[{text:'A',_queued_at:10},{text:'B',_queued_at:11},{text:'C',_queued_at:12}];
const r=await migrateLocalQueueToServer('s1',entries);
console.log(JSON.stringify({n:r.accepted,rest:r.rest.length,texts:apiCalls.map(c=>c[1].text),ids:new Set(apiCalls.map(c=>c[1].client_id)).size}));
""")
    assert out["n"] == 3 and out["rest"] == 0 and out["texts"] == ["A", "B", "C"] and out["ids"] == 3


def test_partial_migration_reports_exactly_the_unaccepted_entries():
    out = _run("""
let n=0; apiImpl=async()=>{ if(++n===2) throw new Error('down'); return {items:[]}; };
const r=await migrateLocalQueueToServer('s1',[{text:'A',_queued_at:1},{text:'B',_queued_at:2},{text:'C',_queued_at:3}]);
console.log(JSON.stringify({accepted:r.accepted,rest:r.rest.map(e=>e.text)}));
""")
    # A is on the server; B (the failure) and C are returned so the caller keeps exactly those
    assert out == {"accepted": 1, "rest": ["B", "C"]}
    # sessions.js re-persists only `rest`, marks it so the staleness guard cannot drop it
    assert "_retry_migrate" in SESS and "e._retry_migrate" in SESS


def test_one_ordered_sequence_across_browser_and_server_queues():
    out = _run("""
apiImpl=async()=>({items:[{id:'a',text:'S1',status:'queued',queued_at:1}]});
await syncServerQueue('s1');
const afterServerOnly=_canServerQueue('s1',[],'');          // text, nothing browser-held ahead -> server
const withFile=_canServerQueue('s1',[{name:'f'}],'');       // attachment -> browser
const withToken=_canServerQueue('s1',[],'abc');             // goal continuation -> browser
queueSessionMessage('s1',{text:'F1',files:[{name:'f'}]});   // browser-held entry now waits
const behindBrowser=_canServerQueue('s1',[],'');            // later text must NOT jump ahead of F1
console.log(JSON.stringify({afterServerOnly,withFile,withToken,behindBrowser}));
""")
    assert out == {"afterServerOnly": True, "withFile": False, "withToken": False, "behindBrowser": False}


def test_failed_server_entries_do_not_block_browser_held_work():
    out = _run("""
apiImpl=async()=>({items:[{id:'f',text:'bad',status:'failed',queued_at:1,error:'x'}]});
await syncServerQueue('s1');
queueSessionMessage('s1',{text:'F1',files:[{name:'f'}]});
const owns=_hasServerQueueEntries('s1');
const shifted=shiftQueuedSessionMessage('s1');
console.log(JSON.stringify({owns,shifted:shifted&&shifted.text}));
""")
    assert out["owns"] is False and out["shifted"] == "F1"
    assert "!items.some(i=>i.status!=='failed')" in UI  # the setBusy hand-off uses the same rule


def test_send_captures_chat_draft_and_stream_before_the_enqueue_await():
    interrupt = MSG[MSG.index("defaultMessageMode==='interrupt'"):]
    assert interrupt.index("const _streamId=S.activeStreamId") < interrupt.index("await enqueueServerFollowup")
    assert "_cancelStreamById(_streamId)" in interrupt  # never cancels whatever chat is on screen now
    guard = MSG[MSG.index("function _clearComposerIfStillSubmitted"):MSG.index("async function _cancelStreamById")]
    assert "S.session.session_id===sid" in guard and "composer.value" in guard
    assert MSG.count("_clearComposerIfStillSubmitted(_sid,text)") == 2


def test_slash_commands_keep_the_text_when_queueing_fails():
    assert "_restoreSlashDraft(_sid,'/queue '+msg)" in CMD
    assert "_restoreSlashDraft(_sid,'/interrupt '+msg)" in CMD
    assert "_cancelStreamById(_streamId)" in CMD
    assert "String(inp.value||'').trim()!==''" in CMD  # never over a newer draft


def test_composer_only_clears_after_server_accepts_and_never_cancels_on_failure():
    # queue + interrupt branches bail out (return) before clearing/cancelling when enqueue fails
    assert MSG.count("if(!await enqueueServerFollowup(_sid,text)) return;") == 2
    interrupt = MSG[MSG.index("defaultMessageMode==='interrupt'"):]
    assert interrupt.index("enqueueServerFollowup") < interrupt.index("cancelStream('busy-interrupt')")
    assert CMD.count("if(!await enqueueServerFollowup(") == 2


def test_stop_stays_a_separate_action():
    ui = UI[UI.index("function getComposerPrimaryAction"):UI.index("function _applyBusyComposerPlaceholder")]
    assert "return 'stop'" in ui and "cancelStream" in ui
    queue_fn = UI[UI.index("async function enqueueServerFollowup"):UI.index("async function _serverQueueAction")]
    assert "cancel" not in queue_fn.lower()
