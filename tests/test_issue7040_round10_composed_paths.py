import json, subprocess, shutil, textwrap
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = REPO / "static" / "messages.js"
UI_JS = REPO / "static" / "ui.js"
NODE = shutil.which("node") or "/opt/homebrew/bin/node"

DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found in source');
  let cursor = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth && cursor < src.length) {
    if (src[cursor] === '{') depth++;
    else if (src[cursor] === '}') depth--;
    cursor++;
  }
  return src.slice(start, cursor);
}
function extractLine(re, label) {
  const m = src.match(re);
  if (!m) throw new Error(label + ' not found in source');
  return m[0];
}

// Replay idempotence is owned by durable event identity carried ON the tool
// call (_startEventId/_completeEventId), so there is no per-attach state for
// this driver to declare or reset. The behavioural assertions below are the
// oracle; nothing here asserts the mere presence of a mechanism.
const parts = [
  extractFunc('_coerceLiveToolCallSeq'),
  extractFunc('_coerceLiveToolCallSignature'),
  extractFunc('activityBurstFallbackFromCandidate'),
  extractFunc('activitySegmentSeqFallbackFromCandidate'),
  extractFunc('_stableStringify'),
  extractFunc('_hashString'),
  extractFunc('_toolCallSignature'),
  extractFunc('_liveToolTid'),
  extractFunc('_findPendingLiveToolCallIndex'),
  extractFunc('upsertLiveToolCall'),
];

// Minimal environment the producer touches.
const preamble = `
const activeSid = 'sess-1';
const uploaded = [];
const S = { toolCalls: [], messages: [], session: { session_id: activeSid } };
const INFLIGHT = {};
function persistInflightState(){}
let __anchor = { burstId: 7, segmentSeq: 2 };
function _currentLiveToolAnchor(){ return __anchor; }
`;

const mod = preamble + parts.join('\n') + `
module.exports = { upsertLiveToolCall, INFLIGHT, S, activeSid };
`;
const path = process.argv[3];
fs.writeFileSync(path, mod);
const M = require(path);

const ARGS = { path: '/tmp/x.txt' };
function ev(){ return { name: 'read_file', args: ARGS, preview: 'p' }; }

const out = {};

// --- Case A: two equal ID-less STARTs, then their two completions ---
M.upsertLiveToolCall(ev(), 'start');
M.upsertLiveToolCall(ev(), 'start');
let calls = M.INFLIGHT[M.activeSid].toolCalls;
out.startOnly_count = calls.length;
out.startOnly_tids = calls.map(c => c.tid);
out.startOnly_occurrences = calls.map(c => c._occurrence);

M.upsertLiveToolCall(Object.assign(ev(), { snippet: 'first-result' }), 'complete');
M.upsertLiveToolCall(Object.assign(ev(), { snippet: 'second-result' }), 'complete');
calls = M.INFLIGHT[M.activeSid].toolCalls;
out.afterComplete_count = calls.length;
out.afterComplete_done = calls.map(c => c.done === true);
out.afterComplete_snippets = calls.map(c => c.snippet);

// --- Case B: completion-only events (no preceding start) ---
delete M.INFLIGHT[M.activeSid];
M.upsertLiveToolCall(Object.assign(ev(), { snippet: 'c1' }), 'complete');
M.upsertLiveToolCall(Object.assign(ev(), { snippet: 'c2' }), 'complete');
const c2 = M.INFLIGHT[M.activeSid].toolCalls;
out.completeOnly_count = c2.length;
out.completeOnly_tids = c2.map(c => c.tid);
out.completeOnly_occurrences = c2.map(c => c._occurrence);
out.completeOnly_snippets = c2.map(c => c.snippet);

console.log(JSON.stringify(out));
"""


def _run(tmp_path):
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER)
    proc = subprocess.run(
        [NODE, str(driver), str(MESSAGES_JS), str(tmp_path / "mod.js")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError("node driver failed:\n" + proc.stdout + proc.stderr)
    return json.loads(proc.stdout)


@pytest.mark.skipif(not Path(NODE).exists(), reason="node not available")
def test_equal_idless_live_calls_get_distinct_occurrences_via_producer(tmp_path):
    """#7040 round 10 finding 2, driven through the PRODUCER.

    ``upsertLiveToolCall()`` looked a record up by signature/name *before* the
    occurrence was minted, so a second equal ID-less start -- or a second
    completion-only event -- reused the first record and never received its own
    occurrence/tid.

    The round-9 lifecycle test called ``_liveToolTid(..., 0/1)`` directly and
    hand-assembled the later stages, so it could not see this: the collapse
    happens in the producer, before any tid is computed. This test drives
    ``upsertLiveToolCall`` itself -- the outermost function the two SSE handlers
    (``tool`` / ``tool_complete``) actually call -- and asserts on the records
    the producer produced.
    """
    out = _run(tmp_path)

    # Two equal ID-less starts must be two records with distinct identities.
    assert out["startOnly_count"] == 2, (
        "two equal ID-less starts collapsed into %d record(s)" % out["startOnly_count"]
    )
    assert out["startOnly_occurrences"] == [0, 1], out["startOnly_occurrences"]
    assert len(set(out["startOnly_tids"])) == 2, (
        "aliased tids across two distinct calls: %r" % (out["startOnly_tids"],)
    )

    # Their completions must pair one-to-one, in order -- not both onto record 0.
    assert out["afterComplete_count"] == 2, out["afterComplete_count"]
    assert out["afterComplete_done"] == [True, True], out["afterComplete_done"]
    assert out["afterComplete_snippets"] == ["first-result", "second-result"], (
        "completions did not pair with starts in arrival order: %r"
        % (out["afterComplete_snippets"],)
    )

    # Completion-only events (no preceding start) must also not collapse.
    assert out["completeOnly_count"] == 2, (
        "two completion-only events collapsed into %d record(s)"
        % out["completeOnly_count"]
    )
    assert out["completeOnly_occurrences"] == [0, 1], out["completeOnly_occurrences"]
    assert len(set(out["completeOnly_tids"])) == 2, out["completeOnly_tids"]
    assert out["completeOnly_snippets"] == ["c1", "c2"], out["completeOnly_snippets"]


ANCHOR_DRIVER = r"""
const fs = require('fs');
const msgSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSrc  = fs.readFileSync(process.argv[3], 'utf8');

function cut(src, name, kw) {
  const re = new RegExp((kw || 'function') + '\\s+' + name + '\\s*[\\(=]');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let cursor = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth && cursor < src.length) {
    if (src[cursor] === '{') depth++;
    else if (src[cursor] === '}') depth--;
    cursor++;
  }
  return src.slice(start, cursor);
}
function line(src, re, label) {
  const m = src.match(re);
  if (!m) throw new Error(label + ' not found');
  return m[0];
}

// Real projector chain from ui.js.
const uiParts = [
  line(uiSrc, /^.*_TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT\s*=.*$/m, 'RUN_LIMIT'),
  line(uiSrc, /^.*_TRANSCRIPT_DISPLAY_OPAQUE_HEAD\s*=.*$/m, 'HEAD'),
  line(uiSrc, /^.*_TRANSCRIPT_DISPLAY_NOTICE\s*=.*$/m, 'NOTICE'),
  line(uiSrc, /^.*_TRANSCRIPT_DISPLAY_OPAQUE_DATA_RE\s*=.*$/m, 'DATA_RE'),
  line(uiSrc, /^.*_candidateScanRe\s*=.*$/m, 'candidateScanRe'),
  cut(uiSrc, '_isB64Code'),
  cut(uiSrc, '_scanBase64Gap'),
  cut(uiSrc, '_opaqueRunSpans'),
  cut(uiSrc, '_boundOpaqueRuns'),
  cut(uiSrc, '_createIncrementalOpaqueRunCache'),
  cut(uiSrc, '_projectTranscriptTextForDisplay'),
];

// Real anchor renderer from messages.js.
const msgParts = [
  line(msgSrc, /const _anchorProseSmdCache\s*=\s*new Map\(\);/, '_anchorProseSmdCache'),
  cut(msgSrc, '_finalizeAnchorProseIncrementalNode'),
  cut(msgSrc, '_anchorProseIncrementalNode'),
];

const stubs = `
// --- minimal DOM/smd environment ---
function mkEl(){
  return { className:'', classList:{toggle(){},add(){},remove(){}}, dataset:{},
           children:[], appendChild(c){this.children.push(c);},
           setAttribute(){}, querySelector(){ return this._body || null; } };
}
const document = { createElement(){ const e=mkEl(); e._body=mkEl(); return e; } };
const window = {
  smd: { parser(){ return {}; }, parser_write(){}, parser_end(){} },
};
function _safeSmdRenderer(){ return {}; }
function _smdRendererWithoutUnderscoreEmphasis(r){ return r; }
function _smdBindParserIdentity(){}
function _shouldUseLiveProseFade(){ return false; }
function _streamFadeRenderer(){ return {}; }
function _smdMediaTailFlush(){}
function _smdMediaTailClear(){}
function _smdClearParserIdentity(){}
function _sanitizeSmdLinks(){}
function enhanceMarkdownTables(){}
function _streamFadeMuteRenderedPrefix(){}
// --- instrumentation counters ---
const COUNT = { project: 0, scanBytes: 0 };
`;

// Wrap the two functions whose input size IS the work, after they are defined.
const instrument = `
const __proj = _projectTranscriptTextForDisplay;
_projectTranscriptTextForDisplay = function(t, o){ COUNT.project++; return __proj(t, o); };
const __bound = _boundOpaqueRuns;
_boundOpaqueRuns = function(raw, streaming){ COUNT.scanBytes += String(raw||'').length; return __bound(raw, streaming); };
const __spans = _opaqueRunSpans;
_opaqueRunSpans = function(raw){ COUNT.scanBytes += String(raw||'').length; return __spans(raw); };
`;

const mod = stubs + uiParts.join('\n') + '\n' + msgParts.join('\n') + '\n' + instrument + `
module.exports = { _anchorProseIncrementalNode, COUNT };
`;
const path = process.argv[4];
fs.writeFileSync(path, mod);
const M = require(path);

// Drive the ANCHOR renderer the way a live stream does: one repaint per chunk,
// text growing monotonically. Plain prose (no opaque runs) is the common case.
const CHUNK = 'lorem ipsum dolor sit amet consectetur adipiscing elit sed do. ';
const N = 120;
let text = '';
for (let i = 0; i < N; i++) {
  text += CHUNK;
  M._anchorProseIncrementalNode('anchor-key-1', text, {});
}
const finalLen = text.length;
console.log(JSON.stringify({
  repaints: N,
  finalLen,
  projectCalls: M.COUNT.project,
  scanBytes: M.COUNT.scanBytes,
}));
"""

def _run_anchor(tmp_path):
    driver = tmp_path / "anchor_driver.js"
    driver.write_text(ANCHOR_DRIVER)
    proc = subprocess.run(
        [NODE, str(driver), str(MESSAGES_JS), str(UI_JS), str(tmp_path / "anchor_mod.js")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError("node driver failed:\n" + proc.stdout + proc.stderr)
    return json.loads(proc.stdout)


@pytest.mark.skipif(not Path(NODE).exists(), reason="node not available")
def test_anchor_repaints_do_not_reproject_full_text(tmp_path):
    """#7040 round 10 finding 1, driven through the ANCHOR renderer.

    Round 9 gave the main live path an owner-scoped incremental projection
    cache, but ``_scheduleRender()`` also calls ``_upsertAnchorProcessProse()``
    on every repaint, which reaches ``_anchorProseIncrementalNode`` -- and that
    projected the FULL accumulated text with no ``liveCache``, then projected
    the result a second time for ``dataset.rawText``. So a long live payload
    still cost O(n) per update, O(n^2) cumulatively.

    The round-9 perf test drove ``_projectTranscriptTextForDisplay`` directly,
    so it could not see this composed cost. This test drives the Anchor renderer
    itself and accounts for the work two ways:

    * exactly one projector call per repaint (the second, ``dataset.rawText``
      projection is gone), and
    * total bytes handed to the underlying scanners stays near-linear in the
      final text length rather than quadratic in the repaint count.
    """
    out = _run_anchor(tmp_path)

    # One projection per repaint -- not two.
    assert out["projectCalls"] == out["repaints"], (
        "expected 1 projector call per repaint, got %d for %d repaints"
        % (out["projectCalls"], out["repaints"])
    )

    # Work accounting: a full re-scan every repaint is ~ n*final/2 bytes.
    quadratic = out["finalLen"] * out["repaints"] / 2.0
    assert out["scanBytes"] < quadratic / 10.0, (
        "anchor repaints scanned %d bytes; a full re-scan per repaint is ~%d, "
        "so this is still quadratic" % (out["scanBytes"], quadratic)
    )


REATTACH_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found in source');
  let cursor = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth && cursor < src.length) {
    if (src[cursor] === '{') depth++;
    else if (src[cursor] === '}') depth--;
    cursor++;
  }
  return src.slice(start, cursor);
}
function extractLine(re, label) {
  const m = src.match(re);
  if (!m) throw new Error(label + ' not found in source');
  return m[0];
}
// This schedule is reachable because a reload restores persisted toolCalls and
// a stale cursor; the durable event ids on those records are what must make the
// replay idempotent.
const parts = [
  extractFunc('_coerceLiveToolCallSeq'),
  extractFunc('_coerceLiveToolCallSignature'),
  extractFunc('activityBurstFallbackFromCandidate'),
  extractFunc('activitySegmentSeqFallbackFromCandidate'),
  extractFunc('_stableStringify'),
  extractFunc('_hashString'),
  extractFunc('_toolCallSignature'),
  extractFunc('_liveToolTid'),
  extractFunc('_findPendingLiveToolCallIndex'),
  extractFunc('upsertLiveToolCall'),
];

// Real persistence: saveInflightState round-trips through JSON exactly like
// the production localStorage path, so anything not serialized is genuinely
// lost across the simulated reload.
const preamble = `
const activeSid = 'sess-1';
const uploaded = [];
const S = { toolCalls: [], messages: [], session: { session_id: activeSid } };
let INFLIGHT = {};
const streamId = 'stream-1';
const STORE = {};
let _lastRunJournalSeq = 0;
let _lastRunJournalEventId = '';
function saveInflightState(sid, state){ STORE[sid] = JSON.stringify(state); }
function loadInflightState(sid){ return STORE[sid] ? JSON.parse(STORE[sid]) : null; }
function persistInflightState(){
  const inflight = INFLIGHT[activeSid];
  if (!inflight) return;
  saveInflightState(activeSid, {
    streamId,
    messages: inflight.messages || [],
    uploaded: inflight.uploaded || [],
    toolCalls: inflight.toolCalls || [],
    lastRunJournalSeq: inflight.lastRunJournalSeq || 0,
    lastRunJournalEventId: inflight.lastRunJournalEventId || '',
    journalReplayFromStart: !!inflight.journalReplayFromStart,
  });
}
let __anchor = { burstId: 7, segmentSeq: 2 };
function _currentLiveToolAnchor(){ return __anchor; }
`;

const mod = preamble + parts.join('\n') + `
// Rebuild INFLIGHT the way loadSession() -> attachLiveStream() does: restore
// the persisted entry, then apply the production per-attach claim reset.
function simulateReloadAndReattach(staleCursorSeq, staleCursorEventId){
  const stored = loadInflightState(activeSid);
  INFLIGHT = {};
  INFLIGHT[activeSid] = {
    streamId: String(stored.streamId || ''),
    messages: stored.messages || [],
    uploaded: stored.uploaded || [],
    toolCalls: stored.toolCalls || [],
    reattach: true,
    lastRunJournalSeq: Number(stored.lastRunJournalSeq || 0) || 0,
    lastRunJournalEventId: String(stored.lastRunJournalEventId || ''),
    journalReplayFromStart: !!stored.journalReplayFromStart,
  };
  // Deliberately stale cursor: the reviewer's window where the record was
  // saved synchronously but the cursor had not advanced yet.
  if (staleCursorSeq !== undefined) {
    INFLIGHT[activeSid].lastRunJournalSeq = staleCursorSeq;
    INFLIGHT[activeSid].lastRunJournalEventId = staleCursorEventId || '';
  }
  S.toolCalls = INFLIGHT[activeSid].toolCalls;
  return INFLIGHT[activeSid];
}
module.exports = {
  upsertLiveToolCall,
  simulateReloadAndReattach,
  get INFLIGHT(){ return INFLIGHT; },
  S, activeSid,
  get cursor(){ return { seq: _lastRunJournalSeq, id: _lastRunJournalEventId }; },
};
`;
const path = process.argv[3];
fs.writeFileSync(path, mod);
const M = require(path);

const ARGS = { path: '/tmp/x.txt' };
function ev(extra){ return Object.assign({ name: 'read_file', args: ARGS, preview: 'p' }, extra || {}); }
function calls(){ return M.INFLIGHT[M.activeSid].toolCalls; }
function snap(){
  return {
    count: calls().length,
    snippets: calls().map(c => c.snippet),
    dones: calls().map(c => c.done === true),
    tids: calls().map(c => c.tid),
    occurrences: calls().map(c => c._occurrence),
  };
}

const out = {};

// Two equal ID-less starts; only call 0 completes, via event E0 (seq 40).
M.upsertLiveToolCall(ev(), 'start', 'run:sess-1:10');
M.upsertLiveToolCall(ev(), 'start', 'run:sess-1:20');
M.upsertLiveToolCall(ev({ snippet: 'FIRST' }), 'complete', 'run:sess-1:40');
out.beforeReload = snap();
out.cursorAfterComplete = M.INFLIGHT[M.activeSid].lastRunJournalSeq;

// Reload + reattach with a DELIBERATELY STALE cursor (30 < 40), which is what
// makes the server replay E0 even though it was already applied.
M.simulateReloadAndReattach(30, 'run:sess-1:30');
out.afterReload = snap();

// 1) E0 REPLAYS FIRST while call 1 is still pending.
M.upsertLiveToolCall(ev({ snippet: 'FIRST' }), 'complete', 'run:sess-1:40');
out.afterE0Replay = snap();

// 2) Then E1 alone completes call 1.
M.upsertLiveToolCall(ev({ snippet: 'SECOND' }), 'complete', 'run:sess-1:50');
out.afterE1 = snap();

// 3) Repeat E1 after all calls are done -> idempotent.
M.upsertLiveToolCall(ev({ snippet: 'SECOND' }), 'complete', 'run:sess-1:50');
out.afterE1ReplayAllDone = snap();

// 4) Repeat E1 while ANOTHER equal call is pending -> must not consume it.
M.upsertLiveToolCall(ev(), 'start', 'run:sess-1:60');
M.upsertLiveToolCall(ev({ snippet: 'SECOND' }), 'complete', 'run:sess-1:50');
out.afterE1ReplayWithPending = snap();

// 5) The genuinely new completion for that third call still lands on it.
M.upsertLiveToolCall(ev({ snippet: 'THIRD' }), 'complete', 'run:sess-1:70');
out.afterThird = snap();

console.log(JSON.stringify(out));
"""


def _run_reattach(tmp_path):
    driver = tmp_path / "reattach_driver.js"
    driver.write_text(REATTACH_DRIVER)
    proc = subprocess.run(
        [NODE, str(driver), str(MESSAGES_JS), str(tmp_path / "reattach_mod.js")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError("node driver failed:\n" + proc.stdout + proc.stderr)
    return json.loads(proc.stdout)


@pytest.mark.skipif(not Path(NODE).exists(), reason="node not available")
def test_replayed_completion_does_not_consume_a_later_pending_call(tmp_path):
    """#7040 r12: an exact replay must never steal a pending occurrence.

    Round 11 searched unfinished records BEFORE checking whether the incoming
    event id already owned a completed record. So with call 0 completed by E0
    and an equal call 1 still pending, replaying E0 after a reload consumed
    call 1; the later real E1 then had no pending owner and minted a spurious
    completion-only occurrence.

    This drives the real schedule: persist through a JSON round trip, rebuild
    INFLIGHT the way loadSession() -> attachLiveStream() does (including the
    production per-attach claim-set reset), force a deliberately stale replay
    cursor, then replay E0 FIRST while call 1 is pending.
    """
    out = _run_reattach(tmp_path)

    # Baseline: two calls, only the first done.
    assert out["beforeReload"]["count"] == 2, out["beforeReload"]
    assert out["beforeReload"]["dones"] == [True, False], out["beforeReload"]
    # The cursor advanced from the same event that mutated the record.
    assert out["cursorAfterComplete"] == 40, out["cursorAfterComplete"]

    # State survives the reload byte-for-byte.
    assert out["afterReload"]["snippets"] == ["FIRST", ""], out["afterReload"]
    assert out["afterReload"]["dones"] == [True, False], out["afterReload"]

    # 1) Replaying E0 must be a no-op: call 1 stays pending, nothing is added.
    a = out["afterE0Replay"]
    assert a["count"] == 2, "E0 replay minted an occurrence: %r" % (a,)
    assert a["dones"] == [True, False], "E0 replay consumed the pending call: %r" % (a,)
    assert a["snippets"] == ["FIRST", ""], a

    # 2) E1 then completes call 1 — and only call 1.
    b = out["afterE1"]
    assert b["count"] == 2, b
    assert b["dones"] == [True, True], b
    assert b["snippets"] == ["FIRST", "SECOND"], b
    assert len(set(b["tids"])) == 2, b["tids"]
    assert b["occurrences"] == [0, 1], b["occurrences"]

    # 3) Replaying E1 with everything done is idempotent.
    assert out["afterE1ReplayAllDone"] == b, out["afterE1ReplayAllDone"]

    # 4) Replaying E1 while a third equal call is pending must not consume it.
    c = out["afterE1ReplayWithPending"]
    assert c["count"] == 3, c
    assert c["dones"] == [True, True, False], "E1 replay stole the pending call: %r" % (c,)
    assert c["snippets"] == ["FIRST", "SECOND", ""], c

    # 5) The third call's own completion still lands on it.
    e = out["afterThird"]
    assert e["count"] == 3, e
    assert e["dones"] == [True, True, True], e
    assert e["snippets"] == ["FIRST", "SECOND", "THIRD"], e
    assert len(set(e["tids"])) == 3, e["tids"]
