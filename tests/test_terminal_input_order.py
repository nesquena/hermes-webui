"""Terminal keystrokes must reach the shell in the order they were typed.

xterm.js calls ``onData`` once per key (or once per paste). Each call used to
start its own ``/api/terminal/input`` POST without waiting for the previous one.
The server handles requests on separate threads, so those POSTs could reach the
shell out of order and a fast-typed command arrived scrambled. The terminal now
keeps one input request in flight per session and sends keys typed meanwhile
together in the next request, in pieces within the server's input limit.

These tests drive the real ``static/terminal.js`` in Node with a fake xterm and
an ``api()`` whose requests resolve only when the test says so.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TERMINAL_JS = (REPO / "static" / "terminal.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

HARNESS = r"""
const calls=[];
let onData=null;
class FakeTerminal {
  constructor(){ this.cols=80; this.rows=24; this.options={}; }
  loadAddon(){}
  open(){}
  onData(cb){ onData=cb; }
  dispose(){}
}
global.window={addEventListener(){},Terminal:FakeTerminal};
global.document={
  documentElement:{classList:{contains(){return false;}}},
  getElementById(){return null;},
};
global.getComputedStyle=()=>({getPropertyValue(){return '';}});
global.$=(id)=>id==='terminalSurface'?{textContent:''}:null;
global.requestAnimationFrame=()=>1;
global.cancelAnimationFrame=()=>{};
global.setTimeout=()=>1;
global.clearTimeout=()=>{};
global.S={session:{session_id:'s1',workspace:'/tmp/ws'}};
global.t=(key)=>key;
const toasts=[];
global.showToast=(msg)=>toasts.push(msg);
global.syncTerminalButton=()=>{};
global.api=(path,opts)=>{
  if(path!=='/api/terminal/input')return Promise.resolve({});
  let resolve,reject;
  const p=new Promise((res,rej)=>{resolve=res;reject=rej;});
  const body=JSON.parse(opts.body);
  const {body:_b,method:_m,...rest}=opts;
  calls.push({sid:body.session_id,data:body.data,opts:rest,resolve,reject});
  return p;
};
""" + TERMINAL_JS + r"""
async function settle(){ for(let i=0;i<5;i++) await Promise.resolve(); }
function sent(){ return calls.map(c=>c.data); }
// Settle request i if it was made (on a broken build it may not exist).
function res(i){ if(calls[i])calls[i].resolve({}); }
function rej(i,e){ if(calls[i])calls[i].reject(e); }
(async()=>{
  const out={};
  _ensureXterm();
  TERMINAL_UI.sessionId='s1';

  // Keys typed while the first request is in flight wait for it.
  'echo hi'.split('').forEach(ch=>onData(ch));
  await settle();
  out.while_first_in_flight=sent();
  out.first_opts=calls[0].opts;
  res(0);
  await settle();
  out.after_first_done=sent();
  res(1);
  await settle();
  out.after_second_done=sent();

  // A failed request drops the keys queued behind it (they would leave a gap in
  // the line), and keys typed afterwards go through.
  onData('\r');
  onData('x');
  onData('y');
  await settle();
  rej(2,new Error('boom'));
  await settle();
  out.after_failure=sent();
  out.toasts=toasts.slice();
  onData('q');
  await settle();
  out.after_failure_next_key=sent();
  res(3);
  await settle();

  // Keys joined while waiting go out in pieces within the server's
  // 8192-character limit.
  calls.length=0;
  onData('k');
  onData('a'.repeat(5000));
  onData('b'.repeat(5000));
  onData('c');
  onData('d'.repeat(3000));
  await settle();
  for(let i=0;i<3;i++){ res(i); await settle(); }
  out.chunk_lengths=calls.map(c=>c.data.length);
  out.chunks_joined=calls.map(c=>c.data).join('')==='k'+'a'.repeat(5000)+'b'.repeat(5000)+'c'+'d'.repeat(3000);

  // A single paste over the limit is refused whole, as the server refuses it,
  // and keys around it still go through.
  calls.length=0;
  toasts.length=0;
  onData('k');
  onData('d'.repeat(9000));
  onData('l');
  await settle();
  for(let i=0;i<2;i++){ res(i); await settle(); }
  out.oversized_sent=sent();
  out.oversized_toasts=toasts.slice();

  // The limit counts code points, as the server does: 5000 emoji (10000 UTF-16
  // units) are accepted, and go out in pieces that keep each pair whole.
  calls.length=0;
  toasts.length=0;
  onData('\u{1F600}'.repeat(5000));
  await settle();
  for(let i=0;i<2;i++){ res(i); await settle(); }
  out.emoji_lengths=calls.map(c=>c.data.length);
  out.emoji_toasts=toasts.slice();

  // A piece never ends between the two halves of a surrogate pair.
  calls.length=0;
  onData('k');
  onData('x'.repeat(8191));
  onData('\u{1F600}y');
  await settle();
  for(let i=0;i<3;i++){ res(i); await settle(); }
  out.surrogate_lengths=calls.map(c=>c.data.length);
  out.surrogate_second_ends=calls[1]?calls[1].data.charCodeAt(calls[1].data.length-1):null;

  // Closing drops unsent keys. A reopened shell of the same session still waits
  // for the request already in flight, so its keys can't overtake it.
  calls.length=0;
  onData('a');
  onData('b');
  await settle();
  await closeComposerTerminal('s1',{skipApi:true});
  TERMINAL_UI.sessionId='s1';
  onData('z');
  await settle();
  out.after_reopen=sent();
  res(0);
  await settle();
  out.after_reopen_settled=sent();
  res(1);
  await settle();

  // Restarting drops unsent keys too, the same way.
  calls.length=0;
  onData('m');
  onData('n');
  await settle();
  TERMINAL_UI.open=true;
  TERMINAL_UI.collapsed=false;
  _startComposerTerminal=async()=>{};
  await restartComposerTerminal();
  onData('o');
  await settle();
  out.after_restart=sent();
  res(0);
  await settle();
  out.after_restart_settled=sent();
  res(1);
  await settle();

  // A failed request of a closed shell does not drop the keys typed into the
  // reopened one, and says nothing about it.
  calls.length=0;
  toasts.length=0;
  onData('g');
  await settle();
  await closeComposerTerminal('s1',{skipApi:true});
  TERMINAL_UI.sessionId='s1';
  onData('h');
  await settle();
  rej(0,new Error('terminal not running'));
  await settle();
  out.after_stale_failure=sent();
  out.stale_failure_toasts=toasts.slice();
  res(1);
  await settle();

  // Another session's terminal does not wait for this session's request.
  calls.length=0;
  onData('s');
  await settle();
  TERMINAL_UI.sessionId='s2';
  onData('t');
  await settle();
  out.other_session=calls.map(c=>[c.sid,c.data]);
  calls.forEach((_,i)=>res(i));
  await settle();
  out.queues_left=TERMINAL_UI.inputQueues?TERMINAL_UI.inputQueues.size:null;
  process.stdout.write(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1);});
"""


@pytest.fixture(scope="module")
def result():
    if NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [NODE, "-e", HARNESS], capture_output=True, text=True, timeout=30, cwd=REPO
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_keys_typed_during_a_request_wait_for_it(result):
    assert result["while_first_in_flight"] == ["e"]
    assert result["after_first_done"] == ["e", "cho hi"]
    assert result["after_second_done"] == ["e", "cho hi"]


def test_input_requests_keep_the_api_defaults(result):
    # api()'s network-error retries recover from a stale idle connection.
    assert result["first_opts"] == {}


def test_a_failed_request_drops_the_keys_queued_behind_it(result):
    assert result["after_failure"] == ["e", "cho hi", "\r"]
    assert result["toasts"] == ["terminal_input_failedboom"]
    assert result["after_failure_next_key"] == ["e", "cho hi", "\r", "q"]


def test_input_is_sent_in_pieces_within_the_server_limit(result):
    assert result["chunk_lengths"] == [1, 8192, 4809]
    assert result["chunks_joined"] is True


def test_a_single_paste_over_the_limit_is_refused_whole(result):
    assert result["oversized_sent"] == ["k", "l"]
    assert result["oversized_toasts"] == ["terminal_input_failedinput too large"]


def test_the_paste_limit_counts_code_points_like_the_server(result):
    assert result["emoji_lengths"] == [8192, 1808]
    assert result["emoji_toasts"] == []


def test_a_piece_does_not_split_a_surrogate_pair(result):
    assert result["surrogate_lengths"] == [1, 8191, 3]
    assert result["surrogate_second_ends"] == ord("x")


def test_closing_drops_unsent_keys_and_the_reopened_shell_waits(result):
    assert result["after_reopen"] == ["a"]
    assert result["after_reopen_settled"] == ["a", "z"]


def test_restarting_drops_unsent_keys_and_the_new_shell_waits(result):
    assert result["after_restart"] == ["m"]
    assert result["after_restart_settled"] == ["m", "o"]


def test_a_closed_shells_failure_keeps_the_new_shells_keys(result):
    assert result["after_stale_failure"] == ["g", "h"]
    assert result["stale_failure_toasts"] == []


def test_sessions_do_not_wait_for_each_other(result):
    assert result["other_session"] == [["s1", "s"], ["s2", "t"]]
    assert result["queues_left"] == 0
