#!/usr/bin/env python3
"""Behavioral gate for the #7855 CORE defect: the goal-continuation ID must
belong to exactly ONE send() invocation.

Round 4 kept the ID in a shared module slot. The queue drain published it and
*any* concurrent ``send()`` read it, so a genuine user turn that happened to be
awaiting its upload posted the pending continuation's ID and consumed the goal.
Round 5 replaced the slot with a per-invocation parameter, but kept a second one
for the lock holder — and a genuine turn that arrived RE-ENTRANTLY (while the
continuation was parked in ``uploadPendingFiles``) read that slot, queued FIRST
with the continuation's 32-hex ID, and consumed the server's pending record
before the real continuation drained (maintainer review, round 6).

Round 6 removes the last shared slot. This gate drives the REAL shipped
``send()`` — extracted verbatim from ``static/messages.js`` and executed in a
Node VM with stubbed browser APIs, the same technique Codex used — and replays
each sequence:

  1. round 5: a genuine user send parks on its upload ``await`` while the
     continuation drains; what the parked turn POSTED must carry no ID;
  2. a continuation rejected with "session already has an active stream" is
     re-queued WITH its ID, so the retry stays a continuation;
  3. round 6 CORE: a continuation parked in ``uploadPendingFiles`` while a
     genuine re-entrant send queues FIRST — the genuine entry may carry only its
     OWN token, and the real continuation keeps its ID;
  4. round 6: a failed START restores the draft text AND its continuation ID, so
     the retry (or a refresh before it) still posts the token;
  5. round 6: both busy-queue modes keep the ID on a continuation entry and put
     none on a genuine message.

A shared slot makes a genuine turn post/queue the pending ID (these gates fail);
a per-invocation binding makes it post nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level function (and its whole body) out of the real source."""
    key = f"async function {name}(" if f"async function {name}(" in source else f"function {name}("
    start = source.index(key)
    body_start = source.index("){", start) + 1
    depth = 0
    for idx in range(body_start, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"{name} function body did not close")


# Stubbed browser surface. Anything the real send() touches that is not modelled
# here resolves to a no-op, so the extracted function runs unmodified.
_HARNESS = r"""
const vm = require('vm');
const SEND_SRC = __SEND_SRC__;
const HELPERS  = __HELPER_SRC__;
const HAS_SLOT = __HAS_SLOT__;
const MODE     = __MODE__;
let REJECT_START = false;

const POSTED = [], QUEUE = [];
let UPLOAD_GATE = null;
let GENERIC_ERR = false;
const el = () => ({ value:'', style:{}, dataset:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}},
  appendChild(){}, addEventListener(){}, setAttribute(){}, removeAttribute(){}, focus(){}, click(){},
  querySelector(){return null;}, querySelectorAll(){return [];}, children:[], textContent:'', innerHTML:'' });

const target = {
  console, setTimeout:()=>0, clearTimeout:()=>{}, setInterval:()=>0, clearInterval:()=>{},
  document:{ querySelector(){return null;}, querySelectorAll(){return [];}, getElementById(){return null;},
             createElement(){return el();}, addEventListener(){}, body:el(), documentElement:el() },
  localStorage:{ getItem(){return null;}, setItem(){}, removeItem(){} },
  history:{ replaceState(){}, pushState(){} },
  location:{ pathname:'/', search:'', href:'http://x/' },
  navigator:{ clipboard:{ writeText:async()=>{} } },
  S:{ session:{ session_id:'sid-1', model:'m', model_provider:'p', workspace:null, profile:null },
      activeProfile:'default', busy:false, messages:[], pendingFiles:[], _pendingSessionToolsets:null },
  INFLIGHT:{}, LIVE_STREAMS:{},
  _sendInProgress:false, _sendInProgressSid:null,
  _drainingGoalContinuationId:'', _pendingMoaConfig:null, _pendingSelections:[], _pendingPickMatch:null,
  api: async (url, opts) => {
    if (url === '/api/chat/start' && opts && opts.body) {
      POSTED.push(JSON.parse(opts.body));
      if (REJECT_START) {
        const err = new Error(GENERIC_ERR ? 'provider unreachable' : 'session already has an active stream');
        err.status = 409;
        throw err;
      }
    }
    return { stream_id:'st-1', session_id:'sid-1' };
  },
  uploadPendingFiles: async () => {
    // 'reject' returns immediately so the POST (and its requeue) is reached.
    // The round-6 modes need the same: a parked send would never POST, so the
    // failure/queue paths under test could never be observed.
    if (MODE === 'reject' || MODE === 'failedstart' || MODE === 'busy') return [];
    await new Promise(r => { UPLOAD_GATE = r; });
    return [];
  },
  queueSessionMessage: (sid, entry) => { QUEUE.push(entry); },
  shiftQueuedSessionMessage: () => null,
  updateQueueBadge(){}, renderTray(){}, autoResize(){}, renderMessages(){}, setBusy(){},
  showToast(){}, hideApprovalCard(){}, hideClarifyCard(){}, removeThinking(){}, setStatus(){},
  setComposerStatus(){}, stopApprovalPolling(){}, stopClarifyPolling(){}, syncModelChip(){},
  clearInflightState(){}, clearOptimisticSessionStreaming(){}, renderSessionList(){},
  _saveComposerDraftNow(){},
  _chatPayloadModelState: () => ({ model:'m', model_provider:'p' }),
  _composerTextWithPendingSelections: () => target.__msg.value,
  _clearComposerAfterQueuedSelectionSend(){}, _clearComposerDraft(){},
  attachLiveStream(){}, _flushSelectionBlocksToComposer(){},
  __msg: el(),
};
for (const k of ['Array','String','Object','Number','Boolean','JSON','Math','Date','Promise','RegExp',
                 'TypeError','Error','RangeError','Map','Set','WeakMap','Symbol','parseInt','parseFloat',
                 'isNaN','isFinite','encodeURIComponent','decodeURIComponent','structuredClone',
                 'TextEncoder','TextDecoder','Intl','BigInt','queueMicrotask','setImmediate','clearImmediate']) {
  if (k in globalThis) target[k] = globalThis[k];
}
target.$ = (id) => (id === 'msg' ? target.__msg : el());
target.window = target; target.globalThis = target;

const ctx = new Proxy(target, {
  has: (t, k) => k in t,
  get: (t, k) => (k in t ? t[k] : (t[k] = () => {})),
});

vm.createContext(ctx);
vm.runInContext(HELPERS, ctx);
vm.runInContext(SEND_SRC, ctx);

(async () => {
  if (MODE === 'reject') {
    // The reviewer's probe #2: a continuation whose /api/chat/start is rejected
    // with "session already has an active stream" must be re-queued WITH its ID.
    REJECT_START = true;
    target.__msg.value = 'continuation body';
    // Do NOT await: past the requeue, send() continues into session-reload work
    // that our timer stubs never resolve. The requeue itself is synchronous, so
    // a few event-loop turns are enough to observe it.
    target.send({ goalContinuationId: '0123cccc' }).catch(() => {});
    for (let i = 0; i < 200 && !QUEUE.length; i++) await new Promise(r => setImmediate(r));
    console.log('REQUEUED=' + JSON.stringify(QUEUE.map(e => e && e.goal_continuation_id).filter(Boolean)));
    return;
  }
  // The round-6 modes below drive send() themselves; running this race too would
  // park a send on the upload gate and leave _sendInProgress true, so every
  // later invocation would take the re-entrant guard instead of the path under
  // test. Keep the round-5 race scoped to its own mode.
  if (MODE !== 'race') return;

  target.__msg.value = 'genuine user turn';
  const genuine = target.send();                             // 1. parks on the upload await
  for (let i = 0; i < 200 && !UPLOAD_GATE; i++) await new Promise(r => setImmediate(r));
  if (!UPLOAD_GATE) { console.log('PARK_FAILED'); return; }

  if (HAS_SLOT) {
    ctx._setDrainingGoalContinuationId('0123cccc');           // old shape: published globally
  } else {
    await target.send({ goalContinuationId: '0123cccc' });   // new shape: this call's argument
  }

  const gate = UPLOAD_GATE; UPLOAD_GATE = null; gate();       // 3. release the genuine turn
  try { await genuine; } catch (e) {}

  const posted = POSTED.find(b => b.message === 'genuine user turn');
  console.log('POSTED_ID=' + JSON.stringify(posted ? (posted.goal_continuation_id || '') : 'NO_POST'));
})().catch(e => console.log('HARNESS_ERR ' + e.message));

// ── Round 6: the re-entrant GUARD branch ────────────────────────────────────
// The in-flight send is a parked continuation; the genuine turn arrives
// RE-ENTRANTLY and is queued first. It must carry only its own token.
(async () => {
  if (MODE !== 'requeue') return;
  REJECT_START = true;
  target.__msg.value = 'tokenized continuation body';
  const cont = target.send({ goalContinuationId: '0123dddd' });   // parks on uploadPendingFiles
  for (let i = 0; i < 200 && !UPLOAD_GATE; i++) await new Promise(r => setImmediate(r));
  if (!UPLOAD_GATE) { console.log('PARK_FAILED'); return; }

  target.__msg.value = 'genuine re-entrant turn';
  target.send();                                                   // takes the _sendInProgress guard
  const gate = UPLOAD_GATE; UPLOAD_GATE = null; gate();
  try { await cont; } catch (e) {}

  const entries = QUEUE.map(e => ({ text: e.text, id: e.goal_continuation_id || '' }));
  const genuine = entries.find(e => e.text === 'genuine re-entrant turn');
  const cont2 = entries.find(e => e.text === 'tokenized continuation body');
  console.log('GENUINE_ID=' + JSON.stringify(genuine ? genuine.id : 'NOT_QUEUED'));
  console.log('CONT_ID=' + JSON.stringify(cont2 ? cont2.id : 'NOT_QUEUED'));
  console.log('QUEUE_ORDER=' + JSON.stringify(QUEUE.map(e => e.text)));
})().catch(e => console.log('HARNESS_ERR ' + e.message));

// ── Round 6: a failed START must restore the text AND its ID ────────────────
(async () => {
  if (MODE !== 'failedstart') return;
  REJECT_START = true;
  GENERIC_ERR = true;                 // non-session_rotated failure → generic restore path
  target.__msg.value = 'restored continuation text';
  await target.send({ goalContinuationId: '0123eeee' }).catch(() => {});
  for (let i = 0; i < 500 && !target.__msg.value; i++) await new Promise(r => setImmediate(r));
  console.log('RESTORED_TEXT=' + JSON.stringify(target.__msg.value));
  console.log('RESTORED_ID=' + JSON.stringify(target.__msg.dataset.goalContinuationId || ''));
  // The token stays readable for exactly that text (one-shot read) ...
  // NOTE: the helpers live in the VM context, not this module scope — call them
  // through ctx so the real extracted function runs (a bare name is undefined here).
  const back = typeof ctx._takeRestoredDraftGoalContinuationId === 'function'
    ? ctx._takeRestoredDraftGoalContinuationId(target.__msg.value) : '';
  ctx.__msg.value = 'restored continuation text';
  console.log('READBACK_ID=' + JSON.stringify(back));
  // ... and a DIFFERENT text must fail closed (the reader is one-shot, so
  // re-seed the draft first to prove that test in isolation).
  ctx._setRestoredGoalContinuationDraft('0123eeee', 'restored continuation text');
  console.log('REPLACED_ID=' + JSON.stringify(
    ctx._takeRestoredDraftGoalContinuationId('my own new message')));
})().catch(e => console.log('HARNESS_ERR ' + e.message));

// ── Round 6: the busy QUEUE modes carry the ID ──────────────────────────────
(async () => {
  if (MODE !== 'busy') return;
  for (const mode of ['interrupt', 'queue']) {
    target.S.busy = true;
    target.window._defaultMessageMode = mode;
    target.S.activeStreamId = 'st-1';
    QUEUE.length = 0;
    target.__msg.value = 'continuation body ' + mode;
    await target.send({ goalContinuationId: '0123ffff' }).catch(() => {});
    target.__msg.value = 'plain user message ' + mode;
    await target.send().catch(() => {});
    const entries = QUEUE.map(e => ({ text: e.text, id: e.goal_continuation_id || '' }));
    console.log(mode.toUpperCase() + '_CONT=' + JSON.stringify(
      entries.find(e => e.text === 'continuation body ' + mode)?.id ?? 'NOT_QUEUED'));
    console.log(mode.toUpperCase() + '_GENUINE=' + JSON.stringify(
      entries.find(e => e.text === 'plain user message ' + mode)?.id ?? 'NOT_QUEUED'));
  }
})().catch(e => console.log('HARNESS_ERR ' + e.message));
"""

_HELPER_NAMES = (
    "_normalizeGoalContinuationId",
    "_setDrainingGoalContinuationId",
    "_readDrainingGoalContinuationId",
    "_takeRestoredDraftGoalContinuationId",
    "_setRestoredGoalContinuationDraft",
    "_clearRestoredGoalContinuationDraft",
    "_restoreComposerDraftAfterFailedSend",
)


def _run(source: str, tmp_path: Path, mode: str = "race") -> str:
    """Execute the real send() from `source` in the Node VM and return stdout."""
    send_src = _extract_function(source, "send")
    helpers = []
    for name in _HELPER_NAMES:
        try:
            helpers.append(_extract_function(source, name))
        except ValueError:
            pass
    script = (
        _HARNESS.replace("__SEND_SRC__", json.dumps(send_src))
        .replace("__HELPER_SRC__", json.dumps("\n".join(helpers) or "/* none */"))
        .replace("__HAS_SLOT__", json.dumps("_setDrainingGoalContinuationId" in source))
        .replace("__MODE__", json.dumps(mode))
    )
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr[:2000]}"
    return proc.stdout

def _field(stdout: str, key: str):
    for line in stdout.splitlines():
        if line.startswith(key + "="):
            return json.loads(line[len(key) + 1 :])
    raise AssertionError(f"harness produced no {key}: {stdout!r}")


def test_a_genuine_turn_never_consumes_a_pending_continuation(tmp_path):
    """The reviewer's probe #1, replayed against the real send()."""
    stdout = _run(MESSAGES_SRC, tmp_path)
    assert "PARK_FAILED" not in stdout, f"the send never parked on its upload await: {stdout!r}"
    posted_id = _field(stdout, "POSTED_ID")
    assert posted_id == "", (
        "a genuine user turn posted the pending continuation's ID "
        f"({posted_id!r}) — the ID is not bound to a single send() invocation "
        "(#7855 round 5, CORE)"
    )


def test_the_drain_requeue_keeps_the_continuation_id(tmp_path):
    """The reviewer's probe #2: a continuation rejected with "session already has
    an active stream" must be re-queued WITH its ID, so the retry stays a
    continuation instead of silently becoming a plain turn."""
    stdout = _run(MESSAGES_SRC, tmp_path, mode="reject")
    requeued = _field(stdout, "REQUEUED")
    assert "0123cccc" in requeued, (
        f"the blocked drain requeued without its continuation ID ({requeued!r}) — "
        "the retry would become an ordinary turn and end the goal loop"
    )


def test_the_harness_reproduces_the_shared_slot_defect(tmp_path):
    """Guard the guard: synthesize the round-4 shared-slot shape from the CURRENT
    source and prove this same harness reports the stolen ID. Without this, a
    green run could just mean the probe stopped exercising anything."""
    # Rebuild the old shape: a global slot the drain publishes and the POST reads.
    buggy = MESSAGES_SRC.replace(
        "goal_continuation_id:_goalContinuationId||undefined",
        "goal_continuation_id:_drainingGoalContinuationId||undefined",
        1,
    )
    assert buggy != MESSAGES_SRC, "could not synthesize the shared-slot shape"
    buggy = buggy.replace(
        "function _normalizeGoalContinuationId(id){return String(id||'').trim();}",
        "function _normalizeGoalContinuationId(id){return String(id||'').trim();}\n"
        "function _setDrainingGoalContinuationId(id){_drainingGoalContinuationId=String(id||'').trim();}",
        1,
    )
    stdout = _run(buggy, tmp_path)
    assert _field(stdout, "POSTED_ID") == "0123cccc", (
        "the harness did not reproduce the shared-slot defect, so the real-send "
        f"gate above is not evidence of anything: {stdout!r}"
    )


def test_a_reentrant_genuine_turn_does_not_inherit_the_parked_continuation_id(tmp_path):
    """Round 6 CORE: a genuine send that arrives while a continuation is parked in
    uploadPendingFiles is queued FIRST. It must carry only its own token, never
    the parked send's — otherwise it consumes the server's pending record before
    the real continuation drains."""
    stdout = _run(MESSAGES_SRC, tmp_path, mode="requeue")
    assert "PARK_FAILED" not in stdout, f"the parked continuation never stalled: {stdout!r}"
    genuine_id = _field(stdout, "GENUINE_ID")
    assert genuine_id == "", (
        "a genuine re-entrant turn was queued with the parked continuation's ID "
        f"({genuine_id!r}) — that turn drains first and consumes the pending record"
    )
    # The real continuation still drains with the token it owns.
    assert _field(stdout, "CONT_ID") == "0123dddd", (
        "the actual continuation lost its own ID on the requeue path"
    )
    # And the genuine turn is still queued first (order is unchanged by the fix).
    assert _field(stdout, "QUEUE_ORDER") == [
        "genuine re-entrant turn",
        "tokenized continuation body",
    ], "the re-entrant send must not be dropped or reordered by the fix"


def test_a_failed_start_restores_the_draft_text_and_its_id_together(tmp_path):
    """Round 6 item 2: a rejected POST never admitted the turn, so the continuation
    ID must be restored together with the draft text — otherwise the retry (and
    any refresh in between) posts an empty ID and the goal loop ends silently."""
    stdout = _run(MESSAGES_SRC, tmp_path, mode="failedstart")
    assert _field(stdout, "RESTORED_TEXT") == "restored continuation text", (
        "the failed-start restore must put the original draft text back"
    )
    assert _field(stdout, "RESTORED_ID") == "0123eeee", (
        "the restored draft lost the continuation ID — the retry would post an "
        "empty ID and the continuation would become an ordinary turn"
    )
    # The token survives a retry for exactly that text ...
    assert _field(stdout, "READBACK_ID") == "0123eeee", (
        "a retry of the restored text could not read its continuation ID back"
    )
    # ... and still fails closed for a different one.
    assert _field(stdout, "REPLACED_ID") == "", (
        "the restored draft leaked its ID onto unrelated text"
    )


def test_busy_queue_modes_carry_the_id_for_a_continuation_only(tmp_path):
    """Round 6 item 3: with a live stream, both busy modes queue the identified
    continuation text. Each entry must carry the token, and a genuine message in
    the SAME mode must carry none (#6885)."""
    stdout = _run(MESSAGES_SRC, tmp_path, mode="busy")
    for mode in ("INTERRUPT", "QUEUE"):
        assert _field(stdout, mode + "_CONT") == "0123ffff", (
            f"the {mode.lower()} busy-queue path dropped the continuation ID"
        )
        assert _field(stdout, mode + "_GENUINE") == "", (
            f"a genuine message queued in {mode.lower()} mode carried a continuation ID"
        )

