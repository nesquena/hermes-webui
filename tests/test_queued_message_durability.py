"""Regression tests: queued messages must be durable, ordered, exactly-once,
with visible lifecycle states (tickets queued-messages-disappear P1 and
no-progress-status P2, reported 26 Aug 2026).

Root causes fixed:

1. loadSession()'s restore kept only the FIRST persisted queue entry (as a
   composer draft) and cleared the persisted copy, so every other queued
   message vanished on refresh. Worse, its staleness filter dropped every
   entry whose ``_queued_at`` predates the last assistant reply, which is the
   NORMAL case for a message queued while the agent answered a previous turn.
2. setBusy(false)'s drain removed the entry from the queue and both storage
   layers BEFORE /api/chat/start durably accepted the turn, so a refresh,
   disconnect, or start failure inside that window silently lost the message.
3. No visible state existed beyond "N queued": failures dropped into a chat
   error bubble with the queue entry gone.

New design (static/ui.js): every entry carries ``_state``
('queued'|'running'|'failed') plus a stable ``_qid``. The drain marks the
entry 'running' in place (still persisted), and send() settles it through
_queueDrainEntryAccepted / _queueDrainEntryFailed / _queueDrainEntryRequeue.
Restore (restoreSessionQueueFromStorage) repopulates the FULL ordered queue
and drops entries only with positive transcript evidence the turn was sent.

This module verifies both the static wiring and, through node's ``vm``, the
REAL extracted queue functions across the four required scenarios:
refresh-with-queued-messages, disconnect during queue drain, failure surfaces
visibly, and order preserved.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    depth = 1
    i = brace + 1
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} braces unbalanced"
    return src[start:i]


# ---------------------------------------------------------------------------
# Static wiring assertions
# ---------------------------------------------------------------------------

class TestLifecycleWiring:
    def test_queue_entries_carry_state_and_qid(self):
        assert "_qid: payload._qid||_queueEntryId()" in UI_JS
        assert "_state:'queued'" in UI_JS
        assert "function _queueEntryState(entry)" in UI_JS

    def test_drain_marks_running_before_send_and_settles_after(self):
        body = _extract_function(UI_JS, "drainQueuedSessionMessage")
        mark = body.find("_setQueueEntryState(sid,next._qid,'running')")
        send = body.find("await send()")
        assert mark != -1 and send != -1 and mark < send, (
            "drain must mark the entry 'running' (still persisted) BEFORE send(); "
            "the old shift-before-send removed it from storage first and lost it on refresh"
        )
        # The entry is only removed in the finally fallback (send returned via a
        # non-chat path); the accepted callback owns the happy-path removal.
        assert "removeQueuedSessionMessage(sid,next._qid)" in body

    def test_send_settles_drained_entry_on_start_outcome(self):
        accepted = MESSAGES_JS.find("_queueDrainEntryAccepted(activeSid)")
        start_call = MESSAGES_JS.find("await api('/api/chat/start'")
        assert accepted != -1 and start_call != -1 and start_call < accepted, (
            "the accepted settle must run AFTER /api/chat/start durably accepted the turn"
        )
        assert "_queueDrainEntryFailed(activeSid,errMsg)" in MESSAGES_JS
        assert "_queueDrainEntryRequeue(activeSid)" in MESSAGES_JS
        assert "_queueDrainSessionGone(activeSid)" in MESSAGES_JS

    def test_failed_settle_runs_before_next_drain_kick(self):
        failed = MESSAGES_JS.find("_queueDrainEntryFailed(activeSid,errMsg)")
        drain_kick = MESSAGES_JS.find(
            "_queueDrainSid=activeSid;renderMessages();setBusy(false);setComposerStatus(`Error: ${errMsg}`);"
        )
        assert failed != -1 and drain_kick != -1 and failed < drain_kick, (
            "the entry must be marked 'failed' before setBusy(false) so the drain "
            "kicked there skips past it instead of re-sending or stalling"
        )

    def test_drain_originated_failure_skips_composer_restore(self):
        assert "if(!_drainSendFailed) _restoreComposerDraftAfterFailedSend(" in MESSAGES_JS, (
            "a drain-originated failure keeps its text as the visible failed queue "
            "entry; also restoring it into the composer would double-submit"
        )

    def test_conflict_requeue_does_not_duplicate_drained_entry(self):
        assert (
            "if(!(typeof _queueDrainEntryRequeue==='function'&&_queueDrainEntryRequeue(activeSid))){"
            in MESSAGES_JS
        )

    def test_setbusy_uses_lifecycle_drain(self):
        body = _extract_function(UI_JS, "setBusy")
        assert "drainQueuedSessionMessage(sid)" in body
        assert "shiftQueuedSessionMessage(sid)" not in body, (
            "setBusy must not shift the entry out of the persisted queue before the send is durable"
        )

    def test_states_are_visible_in_queue_card(self):
        chips = _extract_function(UI_JS, "_renderQueueChips")
        assert "queue-state-" in chips and "data-queue-state" in chips
        assert "queue-card-state-" in chips
        assert "'Sending'" in chips and "'Failed'" in chips
        assert "queue-card-retry" in chips, "failed rows need a retry action"
        # State changes must invalidate the render fingerprint.
        assert "_queueEntryState(e)" in chips

    def test_state_css_hooks_exist(self):
        assert ".queue-card-state-running" in STYLE_CSS
        assert ".queue-card-state-failed" in STYLE_CSS
        assert ".queue-card-row.queue-state-failed" in STYLE_CSS

    def test_restore_runs_in_both_loadsession_branches(self):
        assert SESSIONS_JS.count("restoreSessionQueueFromStorage(sid,S.messages,S.session)") == 2


# ---------------------------------------------------------------------------
# Behavioral tests: run the REAL extracted queue code under node vm
# ---------------------------------------------------------------------------

def _queue_core_source() -> str:
    start = UI_JS.find("function _getSessionQueue(sid, create=false){")
    end = UI_JS.find("function _compressionSessionLock(){")
    assert start != -1 and end != -1 and start < end
    return UI_JS[start:end]


def _run_queue_harness(scenario: str):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    core = _queue_core_source()
    drain = _extract_function(UI_JS, "drainQueuedSessionMessage")
    harness = """
'use strict';
const _store=()=>{const m=new Map();return{getItem:k=>m.has(k)?m.get(k):null,setItem:(k,v)=>m.set(k,String(v)),removeItem:k=>m.delete(k)};};
const sessionStorage=_store();
const localStorage=_store();
const SESSION_QUEUES={};
const _queueRenderKeys={};
const S={busy:false,session:{session_id:'s1'},pendingFiles:[]};
let _loadingSessionId=null;
const _msgEl={value:''};
const $=()=>_msgEl;
const autoResize=()=>{};
const renderTray=()=>{};
const updateQueueBadge=()=>{};
const scheduled=[];
const setTimeout=(fn)=>{scheduled.push(fn);return 0;};
let sendImpl=async()=>{};
async function send(){return sendImpl();}
""" + core + "\n" + drain + """
function snapshot(sid){
  return _getSessionQueue(sid,false).map(e=>({text:e.text,state:_queueEntryState(e),error:e._error||null}));
}
function persisted(sid){
  const raw=localStorage.getItem('hermes-queue-'+sid);
  return raw?JSON.parse(raw).map(e=>({text:e.text,state:e._state||'queued'})):[];
}
function reloadPage(){
  for(const k of Object.keys(SESSION_QUEUES)) delete SESSION_QUEUES[k];
  _drainingQueueEntry=null;
}
async function runScenario(name){
  const out={};
  if(name==='refresh_restores_full_queue'){
    // Queue three messages while the agent answers a PREVIOUS turn; the reply
    // lands AFTER they were queued (the old filter dropped all of them).
    queueSessionMessage('s1',{text:'m1'});
    queueSessionMessage('s1',{text:'m2'});
    queueSessionMessage('s1',{text:'m3'});
    const messages=[
      {role:'user',content:'earlier turn',timestamp:(Date.now()-60000)/1000},
      {role:'assistant',content:'reply to earlier turn',timestamp:(Date.now()+5000)/1000},
    ];
    reloadPage();
    const n=restoreSessionQueueFromStorage('s1',messages,{});
    out.restoredCount=n;
    out.queue=snapshot('s1');
    out.persisted=persisted('s1');
  }
  if(name==='processed_entry_dropped_once'){
    queueSessionMessage('s1',{text:'m1'});
    queueSessionMessage('s1',{text:'m2'});
    // m1 reached the transcript as a user turn sent after it was queued.
    const messages=[{role:'user',content:'m1',timestamp:(Date.now()+1000)/1000}];
    reloadPage();
    restoreSessionQueueFromStorage('s1',messages,{});
    out.queue=snapshot('s1');
  }
  if(name==='drain_failure_surfaces'){
    queueSessionMessage('s1',{text:'m1'});
    queueSessionMessage('s1',{text:'m2'});
    sendImpl=async()=>{_queueDrainEntryFailed('s1','network unreachable');};
    drainQueuedSessionMessage('s1');
    out.duringSettle=snapshot('s1');   // m1 must be persisted as running
    out.persistedDuringSettle=persisted('s1');
    await scheduled.shift()();
    out.afterFailure=snapshot('s1');
    out.persistedAfterFailure=persisted('s1');
    // Retry: back to queued in place, then a successful drain removes it.
    _setQueueEntryState('s1',_getSessionQueue('s1',false)[0]._qid,'queued');
    sendImpl=async()=>{_queueDrainEntryAccepted('s1');};
    drainQueuedSessionMessage('s1');
    await scheduled.shift()();
    out.afterRetry=snapshot('s1');
  }
  if(name==='disconnect_mid_drain_recovers'){
    queueSessionMessage('s1',{text:'m1'});
    drainQueuedSessionMessage('s1');
    // Browser dies before send() settles: the entry is still persisted as
    // 'running'. On reload without transcript evidence it reverts to queued.
    reloadPage();
    restoreSessionQueueFromStorage('s1',[],{});
    out.unsentRecovered=snapshot('s1');
    // Same crash, but the turn DID land server-side (pending user message):
    reloadPage();
    _persistSessionQueueStorage('s1',[{text:'m1',_queued_at:Date.now(),_qid:'x1',_state:'running'}]);
    restoreSessionQueueFromStorage('s1',[],{pending_user_message:'m1'});
    out.sentNotDuplicated=snapshot('s1');
  }
  if(name==='order_preserved'){
    queueSessionMessage('s1',{text:'m1'});
    queueSessionMessage('s1',{text:'m2'});
    queueSessionMessage('s1',{text:'m3'});
    // Busy-conflict requeue must keep m1 at the head, not push it to the back.
    sendImpl=async()=>{_queueDrainEntryRequeue('s1');};
    drainQueuedSessionMessage('s1');
    await scheduled.shift()();
    out.afterRequeue=snapshot('s1');
    // Successful drain removes exactly the head.
    sendImpl=async()=>{_queueDrainEntryAccepted('s1');};
    drainQueuedSessionMessage('s1');
    await scheduled.shift()();
    out.afterAccepted=snapshot('s1');
  }
  if(name==='switch_away_reverts_in_place'){
    queueSessionMessage('s1',{text:'m1'});
    queueSessionMessage('s1',{text:'m2'});
    drainQueuedSessionMessage('s1');
    S.session={session_id:'OTHER'};
    await scheduled.shift()();
    out.queue=snapshot('s1');
  }
  if(name==='hydration_during_drain'){
    queueSessionMessage('s1',{text:'replacement'});
    drainQueuedSessionMessage('s1');
    _loadingSessionId='s1';
    sendImpl=async()=>{if(!_loadingSessionId)_queueDrainEntryAccepted('s1');};
    await scheduled.shift()();
    out.pending=persisted('s1');out.composer=_msgEl.value;
    _loadingSessionId=null;
    drainQueuedSessionMessage('s1');
    if(scheduled.length) await scheduled.shift()();
    out.after=persisted('s1');
  }
  return out;
}
runScenario(process.argv[1]).then(o=>console.log(JSON.stringify(o))).catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    proc = subprocess.run(
        [node, "-e", harness, scenario],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestServerSideFailureVisibility:
    """P2 acceptance: failure state is enforced server-side. The streaming
    worker must persist the failure into the transcript (not just emit a
    transient SSE apperror) so a turn that fails while the browser is
    disconnected is still visible as a failed state after reload."""

    STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

    def test_terminal_failure_persists_visible_error_row(self):
        src = self.STREAMING_PY
        assert "_materialize_pending_user_turn_before_error(s)" in src, (
            "the pending user turn must be materialized so the failed turn's "
            "input is not lost with the pending state"
        )
        error_row = src.find("'_error': True,")
        append = src.find("s.messages.append(_error_message)")
        apperror = src.find("put('apperror', _error_payload)")
        assert error_row != -1 and append != -1 and apperror != -1
        assert append < apperror, (
            "the persisted transcript error row must be saved before the "
            "transient apperror SSE event, so disconnected clients still see "
            "the failure on reload"
        )


class TestRefreshWithQueuedMessages:
    def test_full_queue_survives_refresh_in_order(self):
        out = _run_queue_harness("refresh_restores_full_queue")
        assert out["restoredCount"] == 3
        assert [e["text"] for e in out["queue"]] == ["m1", "m2", "m3"]
        assert all(e["state"] == "queued" for e in out["queue"])
        # And the persisted mirror still holds all of them (no clear-on-load).
        assert [e["text"] for e in out["persisted"]] == ["m1", "m2", "m3"]

    def test_already_processed_entry_not_sent_twice(self):
        out = _run_queue_harness("processed_entry_dropped_once")
        assert [e["text"] for e in out["queue"]] == ["m2"], (
            "m1 already reached the transcript and must reconcile away; m2 must survive"
        )


class TestDisconnectDuringQueueDrain:
    def test_hydration_during_drain_keeps_replacement_until_ready(self):
        out = _run_queue_harness('hydration_during_drain')
        assert out['pending'] == [{'text': 'replacement', 'state': 'queued'}]
        assert out['composer'] == ''
        assert out['after'] == []

    def test_entry_stays_persisted_while_running(self):
        out = _run_queue_harness("drain_failure_surfaces")
        assert out["duringSettle"][0]["state"] == "running"
        assert [e["text"] for e in out["persistedDuringSettle"]] == ["m1", "m2"], (
            "the drained entry must stay in BOTH storage layers until /api/chat/start accepts"
        )

    def test_crash_mid_drain_recovers_without_loss_or_duplicate(self):
        out = _run_queue_harness("disconnect_mid_drain_recovers")
        assert out["unsentRecovered"] == [{"text": "m1", "state": "queued", "error": None}], (
            "a 'running' entry whose turn never landed must revert to 'queued', not vanish"
        )
        assert out["sentNotDuplicated"] == [], (
            "a 'running' entry whose turn DID land (pending_user_message) must not re-send"
        )


class TestFailureSurfacesVisibly:
    def test_failed_send_keeps_entry_with_error(self):
        out = _run_queue_harness("drain_failure_surfaces")
        assert out["afterFailure"][0] == {
            "text": "m1", "state": "failed", "error": "network unreachable",
        }
        assert [e["text"] for e in out["persistedAfterFailure"]] == ["m1", "m2"]
        assert out["afterRetry"] == [{"text": "m2", "state": "queued", "error": None}], (
            "retry must re-drain the failed entry exactly once and leave the rest queued"
        )


# ---------------------------------------------------------------------------
# D3: cross-tab ownership. The persisted queue is shared across tabs through
# localStorage; without claims, two tabs each restore an independent in-memory
# copy, both drain the SAME entry (double-send), and the memory-authoritative
# restore branch re-mirrors stale copies, resurrecting entries another tab
# already sent. The fix: a per-tab id, a _claimed_by/_claimed_at claim written
# into the PERSISTED entry at drain time (re-read before claiming, re-read in
# the settle callback; on a race the lowest tab id wins), claim expiry after a
# staleness window, and merge-by-_qid with storage as the source of truth for
# entries the tab does not own.
# ---------------------------------------------------------------------------

MULTITAB_HARNESS = """
'use strict';
const vm=require('vm');
const fs=require('fs');
const SRC=fs.readFileSync(0,'utf8');
const sharedMap=new Map();
const shared={getItem:k=>sharedMap.has(k)?sharedMap.get(k):null,setItem:(k,v)=>sharedMap.set(k,String(v)),removeItem:k=>sharedMap.delete(k)};
function freshStore(){const m=new Map();return{getItem:k=>m.has(k)?m.get(k):null,setItem:(k,v)=>m.set(k,String(v)),removeItem:k=>m.delete(k)};}
function makeTab(id){
  const sandbox={
    window:{__HERMES_QUEUE_TAB_ID:id},
    localStorage:shared,           // SHARED across tabs, like the browser
    sessionStorage:freshStore(),   // per-tab, like the browser
    SESSION_QUEUES:{},
    _queueRenderKeys:{},
    S:{busy:false,session:{session_id:'s1'},pendingFiles:[]},
    autoResize:()=>{},
    renderTray:()=>{},
    updateQueueBadge:()=>{},
  };
  sandbox.$=()=>({value:''});
  sandbox.scheduled=[];
  let timerSeq=0;
  sandbox.setTimeout=(fn,delay)=>{sandbox.scheduled.push({fn,delay:delay||0});return ++timerSeq;};
  sandbox.sends=[];
  vm.createContext(sandbox);
  vm.runInContext(SRC,sandbox);
  sandbox.send=async function(){sandbox.sends.push(1);sandbox.api._queueDrainEntryAccepted('s1');};
  return sandbox;
}
async function flush(tab){
  while(tab.scheduled.length){const t=tab.scheduled.shift();await t.fn();}
}
function readShared(){
  const raw=shared.getItem('hermes-queue-s1');
  return raw?JSON.parse(raw).map(e=>({text:e.text,state:e._state||'queued',claimedBy:e._claimed_by||null})):[];
}
async function run(name){
  const out={};
  if(name==='two_tabs_exactly_one_send'){
    const A=makeTab('tab-a');
    const B=makeTab('tab-b');
    A.api.queueSessionMessage('s1',{text:'hello'});
    B.api.restoreSessionQueueFromStorage('s1',[],{});
    out.bSeesEntry=B.api._getSessionQueue('s1',false).length;
    // Both tabs try to drain the same shared entry.
    A.api.drainQueuedSessionMessage('s1');
    B.api.drainQueuedSessionMessage('s1');
    // B saw A's persisted claim and must not have scheduled a settle/send.
    out.bScheduledSettle=B.scheduled.some(t=>t.delay===120);
    await flush(A);
    await flush(B);
    out.sends=A.sends.length+B.sends.length;
    out.shared=readShared();
    B.api.drainQueuedSessionMessage('s1');
    await flush(B);
    out.sendsAfterResync=A.sends.length+B.sends.length;
    out.bQueueAfterResync=B.api._getSessionQueue('s1',false).length;
  }
  if(name==='claim_race_loser_backs_off'){
    const Z=makeTab('tab-z');
    Z.api.queueSessionMessage('s1',{text:'race'});
    Z.api.drainQueuedSessionMessage('s1');  // claims as tab-z
    // localStorage has no CAS: simulate a competing tab whose claim write
    // landed AFTER ours. 'tab-a' is the LOWER id, so tab-a must win.
    const raw=JSON.parse(shared.getItem('hermes-queue-s1'));
    raw[0]._claimed_by='tab-a';raw[0]._claimed_at=Date.now();
    shared.setItem('hermes-queue-s1',JSON.stringify(raw));
    await flush(Z);  // settle re-read detects the lost race
    out.zSends=Z.sends.length;
    out.shared=readShared();
    out.zMem=Z.api._getSessionQueue('s1',false).map(e=>({state:Z.api._queueEntryState(e),claimedBy:e._claimed_by||null}));
  }
  if(name==='claim_race_winner_reasserts'){
    const A=makeTab('tab-a');
    A.api.queueSessionMessage('s1',{text:'race'});
    A.api.drainQueuedSessionMessage('s1');  // claims as tab-a
    // The competing claim came from the HIGHER tab id: we stay the winner.
    const raw=JSON.parse(shared.getItem('hermes-queue-s1'));
    raw[0]._claimed_by='tab-z';raw[0]._claimed_at=Date.now();
    shared.setItem('hermes-queue-s1',JSON.stringify(raw));
    await flush(A);
    out.aSends=A.sends.length;
    out.shared=readShared();
  }
  if(name==='claiming_tab_dies_entry_recovers'){
    const A=makeTab('tab-a');
    A.api.queueSessionMessage('s1',{text:'m1'});
    A.api.drainQueuedSessionMessage('s1');  // claims; settle never runs: tab dies
    const B=makeTab('tab-b');
    B.api.restoreSessionQueueFromStorage('s1',[],{});
    out.restoredState=B.api._getSessionQueue('s1',false).map(e=>B.api._queueEntryState(e));
    B.api.drainQueuedSessionMessage('s1');
    out.blockedWhileFresh=!B.scheduled.some(t=>t.delay===120);
    out.retryScheduled=B.scheduled.some(t=>t.delay>B.api.claimTtl);
    // The claiming tab is gone; age its claim past the staleness window.
    const raw=JSON.parse(shared.getItem('hermes-queue-s1'));
    raw[0]._claimed_at=Date.now()-(B.api.claimTtl+1000);
    shared.setItem('hermes-queue-s1',JSON.stringify(raw));
    await flush(B);  // retry re-check: stale claim expires, B drains and sends
    out.bSends=B.sends.length;
    out.shared=readShared();
    out.bQueue=B.api._getSessionQueue('s1',false).length;
  }
  if(name==='stale_memory_cannot_resurrect_sent_entry'){
    const A=makeTab('tab-a');
    const B=makeTab('tab-b');
    A.api.queueSessionMessage('s1',{text:'m1'});
    B.api.restoreSessionQueueFromStorage('s1',[],{});
    B.api.drainQueuedSessionMessage('s1');
    await flush(B);  // B sends the entry and removes it from the shared store
    out.bSends=B.sends.length;
    out.sharedAfterSend=readShared();
    // A still holds a stale in-memory copy. The old memory-authoritative
    // branch re-mirrored it into storage, resurrecting the sent entry.
    out.restoredInA=A.api.restoreSessionQueueFromStorage('s1',[],{});
    out.sharedAfterRestore=readShared();
    A.api.drainQueuedSessionMessage('s1');
    await flush(A);
    out.aSends=A.sends.length;
    out.sharedFinal=readShared();
  }
  return out;
}
run(process.argv[1]).then(o=>console.log(JSON.stringify(o))).catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""


def _run_multitab_harness(scenario: str):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    src = (
        _queue_core_source()
        + "\n"
        + _extract_function(UI_JS, "drainQueuedSessionMessage")
        + "\nthis.api={queueSessionMessage,drainQueuedSessionMessage,"
        "restoreSessionQueueFromStorage,removeQueuedSessionMessage,"
        "_getSessionQueue,_queueEntryState,_setQueueEntryState,"
        "_persistSessionQueueStorage,_queueDrainEntryAccepted,"
        "tabId:_QUEUE_TAB_ID,claimTtl:_QUEUE_CLAIM_TTL_MS};\n"
    )
    proc = subprocess.run(
        [node, "-e", MULTITAB_HARNESS, scenario],
        input=src, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node multitab harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestCrossTabWiring:
    def test_drain_claims_entry_in_persisted_copy(self):
        assert "_claimed_by=_QUEUE_TAB_ID" in UI_JS
        assert "_QUEUE_CLAIM_TTL_MS" in UI_JS
        body = _extract_function(UI_JS, "drainQueuedSessionMessage")
        # Re-read the shared store before claiming AND re-check in the settle
        # callback (CAS emulation; localStorage has no compare-and-set).
        assert "_syncSessionQueueWithStorage(sid)" in body
        assert "_readPersistedSessionQueue(sid)" in body

    def test_restore_merges_instead_of_mirroring_memory(self):
        body = _extract_function(UI_JS, "restoreSessionQueueFromStorage")
        assert "_mergeSessionQueueWithStorage(" in body
        assert "memory is authoritative; just re-mirror it" not in body, (
            "the memory-authoritative re-mirror resurrects entries another tab already sent (D3)"
        )

    def test_read_prefers_shared_localstorage(self):
        body = _extract_function(UI_JS, "_readPersistedSessionQueue")
        local = body.find("localStorage.getItem")
        session = body.find("sessionStorage.getItem")
        assert local != -1 and session != -1 and local < session, (
            "reads must prefer the SHARED localStorage copy; a stale per-tab "
            "sessionStorage mirror must never shadow another tab's removals"
        )


class TestCrossTabExactlyOnce:
    def test_two_tabs_one_queued_entry_exactly_one_send(self):
        out = _run_multitab_harness("two_tabs_exactly_one_send")
        assert out["bSeesEntry"] == 1
        assert out["bScheduledSettle"] is False, (
            "the second tab must see the first tab's persisted claim and not schedule a send"
        )
        assert out["sends"] == 1, "one queued entry across two tabs must send exactly once"
        assert out["shared"] == []
        assert out["sendsAfterResync"] == 1
        assert out["bQueueAfterResync"] == 0, (
            "after the entry was sent by the other tab, the resync must drop the local copy"
        )

    def test_claim_race_loser_backs_off_deterministically(self):
        out = _run_multitab_harness("claim_race_loser_backs_off")
        assert out["zSends"] == 0, "the higher tab id must back off when it loses the claim race"
        assert out["shared"] == [{"text": "race", "state": "running", "claimedBy": "tab-a"}], (
            "the loser must not touch the winner's persisted claim"
        )
        assert out["zMem"] == [{"state": "running", "claimedBy": "tab-a"}], (
            "the loser's memory must mirror the winning claim, not keep its own"
        )

    def test_claim_race_winner_reasserts_and_sends_once(self):
        out = _run_multitab_harness("claim_race_winner_reasserts")
        assert out["aSends"] == 1, "the lowest tab id must win the race and send"
        assert out["shared"] == []


class TestClaimingTabDies:
    def test_fresh_claim_blocks_then_stale_claim_recovers(self):
        out = _run_multitab_harness("claiming_tab_dies_entry_recovers")
        assert out["restoredState"] == ["running"], (
            "another tab's live claim must restore as running, not revert to queued"
        )
        assert out["blockedWhileFresh"] is True, (
            "a fresh foreign claim must block the drain in other tabs"
        )
        assert out["retryScheduled"] is True, (
            "a blocked drain must schedule a re-check after the claim staleness window"
        )
        assert out["bSends"] == 1, "the entry must recover and send after the dead tab's claim expires"
        assert out["shared"] == []
        assert out["bQueue"] == 0


class TestStaleMemoryCannotResurrect:
    def test_sent_entry_not_resurrected_by_stale_tab_memory(self):
        out = _run_multitab_harness("stale_memory_cannot_resurrect_sent_entry")
        assert out["bSends"] == 1
        assert out["sharedAfterSend"] == []
        assert out["restoredInA"] == 0, (
            "a stale in-memory copy of an entry another tab sent must reconcile away"
        )
        assert out["sharedAfterRestore"] == [], (
            "the restore must not re-mirror the stale copy into the shared store"
        )
        assert out["aSends"] == 0, "the already-sent entry must never send again from the stale tab"
        assert out["sharedFinal"] == []


class TestOrderPreserved:
    def test_requeue_and_accept_keep_fifo_order(self):
        out = _run_queue_harness("order_preserved")
        assert [e["text"] for e in out["afterRequeue"]] == ["m1", "m2", "m3"], (
            "a busy-conflict requeue must keep the entry at its original position"
        )
        assert [e["text"] for e in out["afterAccepted"]] == ["m2", "m3"]

    def test_session_switch_during_settle_reverts_in_place(self):
        out = _run_queue_harness("switch_away_reverts_in_place")
        assert [(e["text"], e["state"]) for e in out["queue"]] == [
            ("m1", "queued"), ("m2", "queued"),
        ], "switching sessions during the settle window must keep m1 at the head"
