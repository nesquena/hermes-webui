"""Browser side of the server-side follow-up FIFO: runs the real ui.js queue functions in node."""
import json
import re
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
const n=await migrateLocalQueueToServer('s1',entries);
console.log(JSON.stringify({n,texts:apiCalls.map(c=>c[1].text),ids:new Set(apiCalls.map(c=>c[1].client_id)).size}));
""")
    assert out["n"] == 3 and out["texts"] == ["A", "B", "C"] and out["ids"] == 3


def test_migration_failure_keeps_browser_copy_for_retry():
    out = _run("""
apiImpl=async()=>{throw new Error('down')};
console.log(JSON.stringify({n:await migrateLocalQueueToServer('s1',[{text:'A',_queued_at:1}])}));
""")
    assert out["n"] == -1
    assert "queue-migration-deferred" in SESS  # sessions.js keeps persisted copy on this signal


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
