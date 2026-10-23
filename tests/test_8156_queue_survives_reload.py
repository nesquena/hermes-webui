#!/usr/bin/env python3
"""Behavioral gate for the reload half of the #8156 stale-bundle recovery.

A stale-bundle rejection asks the user to reload with follow-ups still queued
(and, when newer typing blocked the restore, the rejected turn queued first).
The queue restore in ``loadSession`` used to put the first persisted entry in
an empty composer and drop every other one; the saved draft, restored further
down, then replaced that first entry too. This replays the REAL restore block
(extracted verbatim from ``static/sessions.js``) with the REAL queue helpers
from ``static/ui.js`` in a Node VM backed by in-memory storage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

START = "    // Restore any queued message that survived page refresh or tab restore.\n"
END = "    // Reconstruct tool calls from message metadata"


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    body_start = source.index("){", start) + 1
    depth = 0
    for idx in range(body_start, len(source)):
        if source[idx] == "{":
            depth += 1
        elif source[idx] == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"{name} did not close")


def _restore_block(source: str) -> str:
    start = source.index(START)
    return source[start : source.index(END, start)]


def _pre_fix_block(block: str) -> str:
    """The restore as master shipped it: first entry into an empty composer
    regardless of a saved draft, everything else dropped."""
    old = block.replace("if(_fresh.length&&!_queueHasDraft){", "if(_fresh.length){", 1)
    cut = old.index("          const _requeue=")
    end = old.index("          }\n", old.index("showToast(`${_requeue.length}", cut)) + len("          }\n")
    old = old[:cut] + old[end:]
    assert old != block and "_requeue" not in old
    return old


_HARNESS = r"""
const vm = require('vm');
const SRC = __SRC__;
const SCENARIO = __SCENARIO__;
const mkStore = () => { const m = new Map(); return {
  getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; };
const msg = { value:'' };
const GOAL_DRAFTS = [], TOASTS = [];
const target = {
  console, JSON, Array, String, Object, Date, Math, Number, Boolean, Map, Set,
  sessionStorage: mkStore(), localStorage: mkStore(),
  S: { session:{ session_id:'sid-1', composer_draft:null },
       messages:[{ role:'user', content:'q' }, { role:'assistant', content:'a', timestamp:1000 }] },
  $: (id) => (id === 'msg' ? msg : null),
  _setRestoredGoalContinuationDraft: (id, text) => GOAL_DRAFTS.push({ id, text }),
  autoResize(){}, showToast: (t) => TOASTS.push(t),
};
target.window = target;
vm.createContext(target);
vm.runInContext(SRC, target);
const later = 2000 * 1000;  // queued after the last assistant message (ms)
const entries = [
  { text:'FOLLOWUP one', files:[{}], goal_continuation_id:'0123bbbb', model:'m', _queued_at: later },
  { text:'FOLLOWUP two', files:[], model:'m', _queued_at: later + 1 },
];
if (SCENARIO === 'draft') target.S.session.composer_draft = { text:'ORIGINAL message', files:[] };
if (SCENARIO === 'filedraft') target.S.session.composer_draft = { text:'', files:[{ name:'original.txt', size:16, path:'' }] };
if (SCENARIO === 'old') entries.forEach(e => { e._queued_at = 1; });  // before the last reply
const key = 'hermes-queue-sid-1';
target.localStorage.setItem(key, JSON.stringify(entries));
if (SCENARIO === 'switch') {
  // Same page, session switch: the in-memory queue already holds the entries.
  target.SESSION_QUEUES['sid-1'] = entries.map(e => ({ ...e }));
}
vm.runInContext('__restore("sid-1")', target);
const live = (target.SESSION_QUEUES['sid-1'] || []);
const persisted = JSON.parse(target.localStorage.getItem(key) || '[]');
console.log('RESULT=' + JSON.stringify({
  composer: msg.value,
  live: live.map(e => ({ text:e.text, files:(e.files || []).length, contId:e.goal_continuation_id || '' })),
  persisted: persisted.map(e => e.text),
  goalDrafts: GOAL_DRAFTS, toasts: TOASTS,
}));
"""


def _run(tmp_path: Path, scenario: str, block: str | None = None) -> dict:
    helpers = [
        "var SESSION_QUEUES={};",
        *(_extract_function(UI_SRC, n) for n in (
            "_getSessionQueue", "_queueStorageKey", "_clearPersistedSessionQueue",
            "_persistSessionQueueStorage", "_readPersistedSessionQueue",
            "queueSessionMessage", "getQueuedSessionCount")),
        "function __restore(sid){\n" + (block or _restore_block(SESSIONS_SRC)) + "\n}",
    ]
    script = (_HARNESS.replace("__SRC__", json.dumps("\n".join(helpers)))
              .replace("__SCENARIO__", json.dumps(scenario)))
    path = tmp_path / f"restore_{scenario}.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT="):
            return json.loads(line[len("RESULT="):])
    raise AssertionError(proc.stdout + proc.stderr)


def test_with_a_saved_draft_every_queued_message_stays_queued(tmp_path):
    """The stale-bundle reload: the draft holds the original, so both follow-ups
    stay queued (in memory and persisted) with their continuation ID."""
    r = _run(tmp_path, "draft")
    assert r["composer"] == "", r  # the draft restore fills it later
    assert r["live"] == [
        {"text": "FOLLOWUP one", "files": 0, "contId": "0123bbbb"},
        {"text": "FOLLOWUP two", "files": 0, "contId": ""},
    ], r
    assert r["persisted"] == ["FOLLOWUP one", "FOLLOWUP two"], r


def test_draft_gate_reproduces_the_dropped_follow_ups_on_the_pre_fix_block(tmp_path):
    r = _run(tmp_path, "draft", _pre_fix_block(_restore_block(SESSIONS_SRC)))
    # Master put FOLLOWUP one in the composer (the draft restore then replaces
    # it) and dropped FOLLOWUP two.
    assert r["composer"] == "FOLLOWUP one" and r["live"] == [] and r["persisted"] == [], r


def test_without_a_draft_the_first_goes_to_the_composer_and_the_rest_stay_queued(tmp_path):
    r = _run(tmp_path, "nodraft")
    assert r["composer"] == "FOLLOWUP one", r
    assert r["goalDrafts"] == [{"id": "0123bbbb", "text": "FOLLOWUP one"}], r
    assert r["live"] == [{"text": "FOLLOWUP two", "files": 0, "contId": ""}], r
    old = _run(tmp_path, "nodraft", _pre_fix_block(_restore_block(SESSIONS_SRC)))
    assert old["composer"] == "FOLLOWUP one" and old["live"] == [], old


def test_an_attachment_only_draft_also_keeps_the_queue(tmp_path):
    """Codex on 8c05f45: an attachment-only send leaves a draft with empty text
    and file metadata; its restore clears the composer too."""
    r = _run(tmp_path, "filedraft")
    assert r["composer"] == "", r
    assert [e["text"] for e in r["live"]] == ["FOLLOWUP one", "FOLLOWUP two"], r
    assert r["persisted"] == ["FOLLOWUP one", "FOLLOWUP two"], r


def test_a_session_switch_keeps_the_in_memory_queue_durable(tmp_path):
    """Codex on 8c05f45: switching away and back must not leave the queue only in
    memory, or the reload the 409 asks for loses it. No duplicates either."""
    r = _run(tmp_path, "switch")
    assert [e["text"] for e in r["live"]] == ["FOLLOWUP one", "FOLLOWUP two"], r
    assert r["persisted"] == ["FOLLOWUP one", "FOLLOWUP two"], r
    old = _run(tmp_path, "switch", _pre_fix_block(_restore_block(SESSIONS_SRC)))
    assert old["persisted"] == [], f"harness did not reproduce the cleared durable copy: {old}"


def test_entries_older_than_the_last_reply_are_still_dropped(tmp_path):
    r = _run(tmp_path, "old")
    assert r["composer"] == "" and r["live"] == [] and r["persisted"] == [], r
