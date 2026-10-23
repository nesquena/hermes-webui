#!/usr/bin/env python3
"""Behavioral gate for the #8156 stale-bundle recovery (re-gate of 2026-10-19).

A page running an older bundle gets ``409 stale_client_bundle`` from
``/api/chat/start``. The reviewer reproduced three ways the recovery lost the
user's message where master would have sent it; each is replayed here against
the REAL shipped ``send()`` (plus ``_restoreComposerDraftAfterFailedSend`` and
``ui.js``'s ``setBusy``) extracted verbatim and run in a Node VM with stubbed
browser APIs, the technique ``test_7855_goal_continuation_binding.py`` uses:

  1. typing during the held start: the rejected message must stay reachable
     (the composer keeps the newer text, so the transcript keeps the bubble);
  2. follow-ups queued during the held start: one ``/api/chat/start`` only, the
     composer holds the original text + file, both follow-ups stay queued;
  3. a rejected send with attachments: the status says files must be re-added
     after the reload the same message asks for (a reload restores text only).

Each gate also runs against the pre-fix branch shape (synthesized from the
current source) to prove the harness reproduces the reviewer's symptom.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
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


_HARNESS = r"""
const vm = require('vm');
const SRC  = __SRC__;
const MODE = __MODE__;

const POSTED = [], QUEUE = [], SAVED = [], STATUS = [];
let HOLD = null, HELD_ONCE = false;
const el = () => ({ value:'', style:{}, dataset:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}},
  appendChild(){}, addEventListener(){}, setAttribute(){}, removeAttribute(){}, focus(){}, click(){},
  querySelector(){return null;}, querySelectorAll(){return [];}, children:[], textContent:'', innerHTML:'' });
const staleError = () => {
  const err = new Error('This page is running an older WebUI build than the server; reload to continue.');
  err.status = 409;
  err.body = JSON.stringify({ type:'stale_client_bundle', retryable:true });
  return err;
};

const target = {
  console, setTimeout, clearTimeout, setInterval:()=>0, clearInterval:()=>{},
  document:{ querySelector(){return null;}, querySelectorAll(){return [];}, getElementById(){return null;},
             createElement(){return el();}, addEventListener(){}, body:el(), documentElement:el() },
  localStorage:{ getItem(){return null;}, setItem(){}, removeItem(){} },
  sessionStorage:{ getItem(){return null;}, setItem(){}, removeItem(){} },
  history:{ replaceState(){}, pushState(){} },
  location:{ pathname:'/', search:'', href:'http://x/', reload(){ target.RELOADED = true; } },
  navigator:{ clipboard:{ writeText:async()=>{} } },
  S:{ session:{ session_id:'sid-1', model:'m', model_provider:'p', workspace:null, profile:null },
      activeProfile:'default', busy:false, activeStreamId:null, pendingFiles:[], _pendingSessionToolsets:null,
      messages:[{role:'user',content:'earlier question'},{role:'assistant',content:'earlier answer'}] },
  INFLIGHT:{}, LIVE_STREAMS:{}, COMMANDS:[], _AGENT_COMMANDS_RUN_ON_WEBUI:new Set(),
  _sendInProgress:false, _sendInProgressSid:null, _queueDrainSid:null, _pendingSelections:[],
  _forcedSkillDirectivePending:null, _pendingMoaConfig:null, _approvalSessionId:null, _clarifySessionId:null,
  _projectPickerOpenEpoch:0, RELOADED:false,
  t: (k) => k,
  api: async (url, opts) => {
    if (url === '/api/chat/start' && opts && opts.body) {
      POSTED.push(JSON.parse(opts.body));
      if (!HELD_ONCE) { HELD_ONCE = true; await new Promise(r => { HOLD = r; }); }
      throw staleError();
    }
    return {};
  },
  uploadPendingFiles: async ({files}) => (files || []).map(f => ({ name:f.name, path:'/attachments/sid-1/' + f.name })),
  queueSessionMessage: (sid, entry, opts) => { if (opts && opts.front) QUEUE.unshift(entry); else QUEUE.push(entry); },
  shiftQueuedSessionMessage: () => QUEUE.shift() || null,
  _saveComposerDraftNow: (sid, text, files, contId) => {
    SAVED.push({ sid, text, files:(files||[]).map(f => f.name), ...(contId ? { contId } : {}) }); },
  setComposerStatus: (s) => { STATUS.push(String(s || '')); },
  _chatPayloadModelState: () => ({ model: target.__model || 'm', model_provider:'p' }),
  _composerTextWithPendingSelections: () => target.__msg.value,
  _normalizeGoalContinuationId: (id) => String(id || '').trim(),
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
// Anything send() touches that is not modelled above resolves to a no-op.
const ctx = new Proxy(target, {
  has: (t, k) => k in t,
  get: (t, k) => (k in t ? t[k] : (t[k] = () => {})),
});
vm.createContext(ctx);
vm.runInContext(SRC, ctx);

const tick = async (n) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
const report = () => {
  console.log('TRANSCRIPT=' + JSON.stringify(target.S.messages.map(m => m.role + ':' + m.content)));
  console.log('COMPOSER=' + JSON.stringify(target.__msg.value));
  console.log('TRAY=' + JSON.stringify((target.S.pendingFiles || []).map(f => f.name)));
  console.log('QUEUE=' + JSON.stringify(QUEUE.map(e => e.text)));
  console.log('QUEUE_HEAD=' + JSON.stringify(QUEUE.length ? { text:QUEUE[0].text,
    files:(QUEUE[0].files || []).map(f => f.name), contId:QUEUE[0].goal_continuation_id || '',
    model:QUEUE[0].model } : null));
  console.log('STARTS=' + JSON.stringify(POSTED.length));
  console.log('SAVED=' + JSON.stringify(SAVED));
  console.log('STATUS=' + JSON.stringify(STATUS.filter(Boolean).pop() || ''));
  console.log('BUSY=' + JSON.stringify(!!target.S.busy));
  console.log('RELOADED=' + JSON.stringify(!!target.RELOADED));
};

(async () => {
  target.__msg.value = 'ORIGINAL message';
  target.S.pendingFiles = MODE === 'nofiles' ? [] : [{ name:'original.txt', size:16, type:'text/plain' }];
  const first = (MODE === 'goal' || MODE === 'goaltyping')
    ? target.send({ goalContinuationId:'0123aaaa' }) : target.send();
  for (let i = 0; i < 500 && !HOLD; i++) await tick(1);
  if (!HOLD) { console.log('HOLD_FAILED'); report(); return; }
  if (MODE === 'typing' || MODE === 'goaltyping') {
    target.__msg.value = 'NEWER draft';
    target.__model = 'm2';  // and picks another model for it while the start is held
  } else if (MODE === 'switch') {
    // The user opens another session that is still running and has its own queue.
    target.S.session = { session_id:'sid-2', model:'m', model_provider:'p', workspace:null, profile:null };
    target.S.messages = [{ role:'user', content:'B question' }];
    target.S.busy = true;
    QUEUE.push({ text:'B queued', files:[] });
    target.__msg.value = '';
  } else if (MODE === 'queue') {
    for (const text of ['FOLLOWUP one', 'FOLLOWUP two']) {
      target.__msg.value = text;
      await target.send();
      target.__msg.value = '';
    }
  }
  HOLD();
  try { await first; } catch (e) { console.log('SEND_THREW ' + e.message); }
  // Let setBusy's 120 ms queue drain (and anything it sends) run its course.
  await new Promise(r => setTimeout(r, 600));
  await tick(50);
  report();
})().catch(e => console.log('HARNESS_ERR ' + (e && e.stack || e)));
"""


def _run(tmp_path: Path, mode: str, messages_src: str = MESSAGES_SRC, ui_src: str = UI_SRC) -> dict:
    src = "\n".join([
        _extract_function(messages_src, "_restoreComposerDraftAfterFailedSend"),
        _extract_function(messages_src, "_isStaleClientBundleError"),
        _extract_function(ui_src, "setBusy"),
        _extract_function(messages_src, "send"),
    ])
    script = _HARNESS.replace("__SRC__", json.dumps(src)).replace("__MODE__", json.dumps(mode))
    path = tmp_path / f"harness_{mode}.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=120)
    out = proc.stdout
    assert "HARNESS_ERR" not in out and "HOLD_FAILED" not in out and "SEND_THREW" not in out, out + proc.stderr
    # Some Node builds abort (SIGABRT) during teardown after printing everything;
    # accept that only when every probe line was printed.
    assert proc.returncode == 0 or (proc.returncode == -6 and "RELOADED=" in out), proc.stderr[:2000] or out
    fields = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isupper():
            fields[key] = json.loads(value)
    assert "RELOADED" in fields, out
    return fields


def _pre_fix(messages_src: str, ui_src: str) -> tuple[str, str]:
    """The branch shape the 2026-10-19 re-gate tested: bubble removed before the
    restore, setBusy(false) drains the queue, one status for every case."""
    i = messages_src.index("if(e&&e.status===409&&_isStaleClientBundleError(e)){")
    anchor = "stopClarifyPollingForSession(activeSid);\n"
    start = messages_src.index(anchor, i) + len(anchor)
    end = messages_src.index("      if(typeof renderSessionList==='function') void renderSessionList();\n", start)
    old_branch = (
        "      removeThinking();\n"
        "      if((S.session&&S.session.session_id)===activeSid){\n"
        "        S.messages=S.messages.filter(m=>m!==userMsg);\n"
        "        if(typeof renderMessages==='function') renderMessages();\n"
        "      }\n"
        "      setBusy(false);\n"
        "      _restoreComposerDraftAfterFailedSend(_failedSendDraftText, _failedSendFilesSnapshot, activeSid, _composerDraftClearPromise);\n"
        "      showToast(t('stale_bundle_refresh'),8000);\n"
        "      setComposerStatus(t('stale_bundle_refresh'));\n"
    )
    old_messages = messages_src[:start] + old_branch + messages_src[end:]
    old_ui = ui_src.replace("    if(opts&&opts.drain===false) return;\n", "", 1)
    assert old_messages != messages_src and old_ui != ui_src, "could not synthesize the pre-fix shape"
    return old_messages, old_ui


def test_typing_during_the_send_keeps_the_rejected_message(tmp_path):
    """Re-gate case 1: newer typing keeps the composer, so the rejected message
    goes back on the queue, ahead of anything else, with its attachment."""
    r = _run(tmp_path, "typing")
    assert r["COMPOSER"] == "NEWER draft", r  # newer typing is never clobbered
    assert r["QUEUE_HEAD"] == {"text": "ORIGINAL message", "files": ["original.txt"], "contId": "", "model": "m"}, r  # posted model
    assert r["TRANSCRIPT"] == ["user:earlier question", "assistant:earlier answer"], r
    assert r["STARTS"] == 1 and r["RELOADED"] is False, r


def test_a_goal_continuation_blocked_by_newer_typing_is_queued_with_its_id(tmp_path):
    """Codex thread on 53dd9a0: a continuation rejected while the user types a
    new draft must keep its ID (the server never consumed it)."""
    r = _run(tmp_path, "goaltyping")
    assert r["COMPOSER"] == "NEWER draft", r
    assert r["QUEUE_HEAD"] == {"text": "ORIGINAL message", "files": ["original.txt"], "contId": "0123aaaa", "model": "m"}, r
    old = _run(tmp_path, "goaltyping", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert old["QUEUE"] == [] and all("contId" not in x for x in old["SAVED"]), (
        f"harness did not reproduce the dropped continuation: {old}")


def test_typing_gate_reproduces_the_lost_message_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "typing", *_pre_fix(MESSAGES_SRC, UI_SRC))
    reachable = [m for m in r["TRANSCRIPT"] + [r["COMPOSER"]] + [s["text"] for s in r["SAVED"]]
                 if "ORIGINAL message" in m]
    assert reachable == [], f"harness did not reproduce the reviewer's loss: {r}"


def test_queued_follow_ups_stay_queued_and_the_original_is_restored(tmp_path):
    """Re-gate case 2: every queued follow-up would hit the same 409, so the
    queue must not drain into the restored composer."""
    r = _run(tmp_path, "queue")
    assert r["STARTS"] == 1, r
    assert r["COMPOSER"] == "ORIGINAL message", r
    assert r["TRAY"] == ["original.txt"], r
    assert r["QUEUE"] == ["FOLLOWUP one", "FOLLOWUP two"], r
    assert not any("ORIGINAL message" in m for m in r["TRANSCRIPT"]), r  # restored, so the bubble goes


def test_queue_gate_reproduces_the_drain_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "queue", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert r["STARTS"] == 3 and r["QUEUE"] == [] and r["COMPOSER"] == "FOLLOWUP two", (
        f"harness did not reproduce the reviewer's queue collapse: {r}")


def test_rejected_attachments_get_the_re_attach_instruction(tmp_path):
    """Re-gate case 3: a reload restores the draft text but not the File objects,
    so a rejected send with attachments says they must be re-added."""
    r = _run(tmp_path, "files")
    assert r["COMPOSER"] == "ORIGINAL message" and r["TRAY"] == ["original.txt"], r
    assert r["STATUS"] == "stale_bundle_refresh_files", r
    assert r["SAVED"] and r["SAVED"][-1]["text"] == "ORIGINAL message", r  # text survives the reload
    plain = _run(tmp_path, "nofiles")
    assert plain["STATUS"] == "stale_bundle_refresh", plain
    assert not any("ORIGINAL message" in m for m in plain["TRANSCRIPT"]), plain
    assert plain["TRANSCRIPT"] == ["user:earlier question", "assistant:earlier answer"], plain


def test_switching_away_leaves_the_visible_session_alone(tmp_path):
    """Codex thread on dcc62d6c: a 409 for session A that lands while session B
    is shown must not clear B's busy state, drain B's queue or touch B's
    transcript; A's original is persisted as A's draft instead."""
    r = _run(tmp_path, "switch")
    assert r["BUSY"] is True, r
    assert r["QUEUE"] == ["B queued"] and r["STARTS"] == 1, r
    assert r["TRANSCRIPT"] == ["user:B question"], r
    assert {"sid": "sid-1", "text": "ORIGINAL message", "files": ["original.txt"]} in r["SAVED"], r


def test_switch_gate_reproduces_the_cross_session_cleanup_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "switch", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert r["BUSY"] is False or r["QUEUE"] != ["B queued"], (
        f"harness did not reproduce the cross-session cleanup: {r}")


def test_a_goal_continuation_keeps_its_id_through_the_stale_restore(tmp_path):
    """Codex thread on a079e29: the server rejects before consuming the pending
    continuation, so the restored draft must be persisted WITH its ID (#7855) or
    the resend after the reload is scored as an ordinary turn."""
    r = _run(tmp_path, "goal")
    assert r["COMPOSER"] == "ORIGINAL message", r
    assert r["SAVED"] and r["SAVED"][-1] == {
        "sid": "sid-1", "text": "ORIGINAL message", "files": ["original.txt"], "contId": "0123aaaa"}, r


def test_goal_gate_reproduces_the_dropped_id_on_the_pre_fix_shape(tmp_path):
    r = _run(tmp_path, "goal", *_pre_fix(MESSAGES_SRC, UI_SRC))
    assert all("contId" not in s for s in r["SAVED"]), f"harness did not reproduce the dropped ID: {r}"


def test_new_stale_bundle_strings_exist_in_every_locale():
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_blocks = i18n.count("regen_failed:")
    assert locale_blocks >= 15
    assert i18n.count("stale_bundle_refresh_files:") == locale_blocks
