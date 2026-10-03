#!/usr/bin/env python3
"""Behavioral gate for the #7855 round-5 CORE defect: the goal-continuation ID
must belong to ONE send() invocation.

Round 4 kept the ID in a shared module slot (``static/messages.js``). The queue
drain published it and *any* concurrent ``send()`` read it, so a genuine user
turn that happened to be awaiting its upload posted the pending continuation's
ID and consumed the goal. The maintainer's probe showed the posted body
containing the other turn's ID.

This gate drives the REAL shipped ``send()`` — extracted verbatim from
``static/messages.js`` and executed in a Node VM with stubbed browser APIs, the
same technique Codex used — and replays the reviewer's exact sequence:

  1. a genuine user send starts and parks on its upload ``await``;
  2. a goal continuation is drained while it is parked;
  3. the parked send is released, and we assert what *it* posted.

A shared slot makes the genuine turn post the pending ID (this gate fails); a
per-invocation binding makes it post nothing.
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
  _sendInProgress:false, _sendInProgressSid:null, _sendInProgressGoalContinuationId:'',
  _drainingGoalContinuationId:'', _pendingMoaConfig:null, _pendingSelections:[], _pendingPickMatch:null,
  api: async (url, opts) => {
    if (url === '/api/chat/start' && opts && opts.body) {
      POSTED.push(JSON.parse(opts.body));
      if (REJECT_START) {
        const err = new Error('session already has an active stream');
        err.status = 409;
        throw err;
      }
    }
    return { stream_id:'st-1', session_id:'sid-1' };
  },
  uploadPendingFiles: async () => {
    if (MODE === 'reject') return [];           // no parking: we want the POST
    await new Promise(r => { UPLOAD_GATE = r; });
    return [];
  },
  queueSessionMessage: (sid, entry) => { QUEUE.push(entry); },
  shiftQueuedSessionMessage: () => null,
  updateQueueBadge(){}, renderTray(){}, autoResize(){}, renderMessages(){}, setBusy(){},
  showToast(){}, hideApprovalCard(){}, hideClarifyCard(){}, removeThinking(){}, setStatus(){},
  setComposerStatus(){}, stopApprovalPolling(){}, stopClarifyPolling(){}, syncModelChip(){},
  clearInflightState(){}, clearOptimisticSessionStreaming(){}, renderSessionList(){},
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
"""

_HELPER_NAMES = (
    "_normalizeGoalContinuationId",
    "_setDrainingGoalContinuationId",
    "_readDrainingGoalContinuationId",
    "_takeRestoredDraftGoalContinuationId",
    "_setRestoredGoalContinuationDraft",
    "_clearRestoredGoalContinuationDraft",
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
