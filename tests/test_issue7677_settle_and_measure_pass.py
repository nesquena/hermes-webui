"""Regression coverage for #7677 — "up to 45 full-transcript rebuilds per turn".

Two independent defects from the report are locked here:

1. Terminal settlement (report items 9/10). STREAM_DONE renders the just-settled
   turn with the keep-open token armed, disarms, then collapses it in place — or
   falls back to a SECOND full transcript rebuild. The in-place path keys off the
   settled-scene OWNER group, which only the NON-transparent settled renderer
   stamps, so transparent streams always paid the second rebuild (~11 s of every
   turn on the reported 969-message session) even when the armed render already
   produced exactly what an unarmed rebuild would.

2. Geometry read/write ordering (report item 3). `_updateMessageVirtualMeasurements`
   interleaved `getBoundingClientRect()` reads with `contain-intrinsic-size`
   style writes, so row N's read forced a fresh full-document layout after row
   N-1's write — N rows cost N full layouts on a 474,671 px transcript.

Both are node-harness tests over the real functions (same style as the sibling
virtualization suites).
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS_PATH = ROOT / "static" / "ui.js"
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    # Hardened brace-matching extractor (skips string/template/regex literals and
    # comments), shared with the sibling vscroll / virtualization suites.
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  let str = null;
  let inLine = false;
  let inBlock = false;
  let inRegex = false;
  let prev = '';
  while (depth > 0 && i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i++; } i++; continue; }
    if (str) {
      if (c === '\\') { i += 2; continue; }
      if (c === str) str = null;
      i++; continue;
    }
    if (inRegex) {
      if (c === '/') inRegex = false;
      i++; continue;
    }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; i++; continue; }
    if (c === '/' && !'})]0123456789'.includes(prev) && !/[A-Za-z_$]/.test(prev)) {
      inRegex = true; i++; continue;
    }
    if (c === '{') depth++;
    else if (c === '}') depth--;
    if (c.trim()) prev = c;
    i++;
  }
  return src.slice(start, i);
}"""
    return prelude + body


def _transparent_cap_consts() -> tuple[int, int]:
    """Pin the harness to the REAL cap constants so a tunable change cannot
    silently turn these assertions into no-ops."""
    cap = re.search(r"const _TRANSPARENT_SETTLED_ROW_CAP=(\d+);", UI_JS)
    slack = re.search(r"const _TRANSPARENT_SETTLED_ROW_CAP_SLACK=(\d+);", UI_JS)
    assert cap and slack, "transparent settled row-cap constants not found in static/ui.js"
    return int(cap.group(1)), int(slack.group(1))


# ── 2. measurement pass: read/write separation ──────────────────────────────


def _measurement_harness(row_count: int) -> str:
    heights = ",".join(str(100 + i * 50) for i in range(row_count))
    template = _extract_func_script(UI_JS) + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_measureMessageVirtualRow'));
eval(extractFunc('_updateMessageVirtualMeasurements'));
// Module-level store the extracted helpers close over (mirrors ui.js).
var _userRowIntrinsicHeightBySessionIdx = Object.create(null);
// Instrumented rows: a getBoundingClientRect() logs a READ, a
// contain-intrinsic-size assignment logs a WRITE. Any write that lands before a
// later read is a forced synchronous layout (the #7677 thrash).
const events=[];
function makeUserRow(rawIdx, height){
  const row={
    dataset:{ role:'user', sessionMsgIdx:String(rawIdx) },
    classList:{ contains(){ return false; } },
    style:{},
  };
  row.getBoundingClientRect=function(){ events.push('read:'+rawIdx); return {height:height}; };
  let cis='';
  Object.defineProperty(row.style,'containIntrinsicSize',{
    get(){ return cis; },
    set(v){ events.push('write:'+rawIdx); cis=v; },
  });
  return row;
}
const rows={};
const heights=[__HEIGHTS__];
for(let i=0;i<heights.length;i++) rows[i]=makeUserRow(i, heights[i]);
const inner={};
inner.querySelector=function(sel){
  const m=/\[data-msg-idx="(\d+)"\]/.exec(sel);
  return (m&&rows[m[1]])?rows[m[1]]:null;
};
function $(id){ return id==='msgInner'?inner:null; }
let _messageVirtualHeightCache=[];
let _messageVirtualEstimatedRowHeight=140;
let refreshCalls=0, settledCalls=0;
function _scheduleMessageVirtualMeasurementRefresh(){ refreshCalls++; }
function _markMessageVirtualMeasurementsSettled(){ settledCalls++; }
const vis=Array.from({length:heights.length},(_,i)=>({rawIdx:i}));
const visIdxs=Array.from({length:heights.length},(_,i)=>i);
_updateMessageVirtualMeasurements(vis, visIdxs, {virtualized:true});
const writes=events.filter(e=>e.indexOf('write:')===0);
const reads=events.filter(e=>e.indexOf('read:')===0);
const lastReadIdx=events.reduce((acc,e,i)=>e.indexOf('read:')===0?i:acc,-1);
const firstWriteIdx=events.findIndex(e=>e.indexOf('write:')===0);
console.log(JSON.stringify({
  reads:reads.length,
  writes:writes.length,
  firstWriteIdx:firstWriteIdx,
  lastReadIdx:lastReadIdx,
  cache:_messageVirtualHeightCache,
  inline:rows[0].style.containIntrinsicSize,
  settledCalls:settledCalls,
  refreshCalls:refreshCalls,
}));
"""
    return template.replace("__HEIGHTS__", heights)


def test_measurement_pass_writes_intrinsic_size_only_after_all_geometry_reads():
    """Report item 3: the measurement pass must not interleave a style write
    between two geometry reads. Every `contain-intrinsic-size` write has to land
    AFTER the whole row set has been measured, so the pass shares one layout
    instead of forcing one full-document layout per row."""
    m = json.loads(_run_node(_measurement_harness(4)))
    assert m["reads"] == 4, f"expected one geometry read per row; got {m['reads']}"
    assert m["writes"] == 4, f"expected one intrinsic-size write per user row; got {m['writes']}"
    assert m["firstWriteIdx"] > m["lastReadIdx"], (
        "the virtual measurement pass still interleaves reads and writes: a "
        "contain-intrinsic-size write at events[{}] precedes the last geometry read at "
        "events[{}]. Reads must all run before any write (#7677 layout thrash: "
        "N rows => N full-document layouts). events={}".format(
            m["firstWriteIdx"], m["lastReadIdx"], m
        )
    )


def test_measurement_pass_still_reserves_and_remembers_measured_heights():
    """The batching must not drop the #5638 contract: measured user heights are
    still written inline and remembered in the height cache."""
    m = json.loads(_run_node(_measurement_harness(3)))
    assert m["inline"] == "auto 100px", (
        "the deferred write phase must still stamp the measured height inline on "
        f"the user row; got {m['inline']!r}"
    )
    assert m["cache"] == [100, 150, 200], f"height cache must hold measured heights; got {m['cache']}"
    assert m["refreshCalls"] == 1, (
        "a first measurement pass learns the row heights and schedules exactly one "
        f"measurement refresh (not a rebuild storm); got refreshCalls={m['refreshCalls']}"
    )


def test_measure_row_writes_immediately_without_a_pending_buffer():
    """Back-compat contract for direct callers of `_measureMessageVirtualRow`
    (the #5638 suite drives it with two arguments): no pending buffer supplied
    means write inline right away."""
    source = _extract_func_script(UI_JS) + r"""
var _userRowIntrinsicHeightBySessionIdx = Object.create(null);
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_measureMessageVirtualRow'));
const events=[];
const row={
  dataset:{ role:'user', sessionMsgIdx:'7' },
  classList:{ contains(){ return false; } },
  style:{},
};
row.getBoundingClientRect=function(){ events.push('read'); return {height:2400}; };
let cis='';
Object.defineProperty(row.style,'containIntrinsicSize',{
  get(){ return cis; },
  set(v){ events.push('write'); cis=v; },
});
const inner={ querySelector(){ return row; } };
const pending=[];
const deferredHeight=_measureMessageVirtualRow(inner,{rawIdx:1},pending);
const inlineAfterDeferred=_rowInline(row);
const h=_measureMessageVirtualRow(inner,{rawIdx:1});
console.log(JSON.stringify({
  deferredHeight:deferredHeight,
  inlineAfterDeferred:inlineAfterDeferred,
  pendingLen:pending.length,
  pendingRowIsMeasured:pending.length===1&&pending[0].row===row,
  inlineAfterPlainCall:_rowInline(row),
}));
function _rowInline(r){ return r.style.containIntrinsicSize; }
"""
    m = json.loads(_run_node(source))
    assert m["inlineAfterDeferred"] == "", (
        "passing a pending buffer must DEFER the inline write; got "
        f"{m['inlineAfterDeferred']!r}"
    )
    assert m["pendingLen"] == 1 and m["pendingRowIsMeasured"], (
        "the deferred write must be queued for the measured row; got "
        f"pendingLen={m['pendingLen']} pendingRowIsMeasured={m['pendingRowIsMeasured']}"
    )
    assert m["inlineAfterPlainCall"] == "auto 2400px", (
        "a plain two-argument call must keep writing inline immediately "
        f"(#5638 contract); got {m['inlineAfterPlainCall']!r}"
    )


# ── 1. STREAM_DONE settlement: transparent streams ──────────────────────────


def _transp_settle_harness(
    row_count: int,
    transparent: bool,
    owner_stream_id: str,
    queried_stream_id: str | None = None,
    owner_has_scene: bool = True,
    scene_row_count: int | None = None,
    revealed: bool = False,
    session_id: str | None = None,
) -> str:
    cap, slack = _transparent_cap_consts()
    assert row_count in (0, cap + slack, cap + slack + 1)
    owner_idx = 5
    queried = queried_stream_id or owner_stream_id
    # `row_count` drives the RENDERED NODES left behind by the armed render;
    # `scene_row_count` drives the scene's own row list
    # (`_anchorSceneRowsForRendering`) — the two differ whenever a row renders no
    # node at all (hidden reasoning). The unarmed rebuild caps on the ROW LIST,
    # never on the node count (#7677 review P1).
    scen_rows = row_count if scene_row_count is None else scene_row_count
    owner_scene = (
        f" _anchor_activity_scene:{{ rows:new Array({scen_rows}) }},"
        if owner_has_scene
        else ""
    )
    template = _extract_func_script(UI_JS) + r"""
const _TRANSPARENT_SETTLED_ROW_CAP=__CAP__;
const _TRANSPARENT_SETTLED_ROW_CAP_SLACK=__SLACK__;
// Review-fix collaborators (#7677): only eval'd once they exist in the source,
// so the pre-fix RED run keeps the original assertions green.
if(src.search(/function\s+_transparentSettledTurnWouldBeCapped\s*\(/)>=0){
  eval(extractFunc('_transparentSettledTurnWouldBeCapped'));
}
if(src.search(/function\s+_cacheSettledTranscriptHtml\s*\(/)>=0){
  eval(extractFunc('_cacheSettledTranscriptHtml'));
}
eval(extractFunc('_transparentSettledTurnSecondRenderIsRedundant'));
eval(extractFunc('_collapseJustSettledWorklogInPlace'));
// The compact settled-scene OWNER group is a NON-transparent artifact: a
// transparent settled scene renders per-row nodes stamped with the owner index.
const ownerNodes=Array.from({length:__ROWCOUNT__},()=>({
  getAttribute(k){
    if(k==='data-anchor-owner-idx') return '5';
    if(k==='data-anchor-settled-scene-row') return '1';
    return null;
  },
}));
const inner={innerHTML:'<button onclick="liveCopy()">copy</button><div data-tree-init="1">tree</div>'};
inner.querySelectorAll=function(sel){
  if(sel.indexOf('data-anchor-settled-scene-owner')!==-1) return [];   // transparent: no group
  if(sel.indexOf('data-anchor-owner-idx')!==-1) return ownerNodes;
  return [];
};
function $(id){ return id==='msgInner'?inner:null; }
function isTransparentStream(){ return __TRANSPARENT__; }
// Stubbable collaborator: a scene that is worklog-worthy must fail closed when
// none of its rows are in the rendered transcript.
function _anchorSceneSceneHasWorklogWorthyRows(scene){ return !!(scene&&scene.rows); }
// The renderer's row source: the UNARMED rebuild caps on this length, so the
// skip predicate has to read the same list.
function _anchorSceneRowsForRendering(scene,opts){ return (scene&&Array.isArray(scene.rows))?scene.rows:[]; }
// Revealed-state (the persistent set is authoritative, per #5966 Codex F3).
const _transparentRevealedTurns=new Set();
function _transparentRevealKey(sessionId,ownerIdx){ return String(sessionId)+':'+String(ownerIdx); }
// _sessionHtmlCache snapshot collaborators (mirrors renderMessages' own gates).
const _sessionHtmlCache=new Map();
var _sessionHtmlCacheSid=null;
// The keep-open settle render's PRE-INIT snapshot, exactly as renderMessages
// stashes it (cacheHtml is taken before highlightCode/addCopyButtons/
// initTreeViews, so it carries no onclick handlers and no data-tree-init flags).
var _settleKeepOpenCacheStash=__STASH__;
const INFLIGHT={};
const window={};
function _getVisibleMessagesWithIdx(){ return []; }
function _messageVirtualKeepTailCount(){ return 0; }
function _currentMessageVirtualWindow(vis,keep){ return {start:0,end:0,topPad:0,bottomPad:0,total:0}; }
function _messageVirtualWindowKeyFor(m){ return 'wk'; }
function _messageRenderCacheSignature(){ return 'sig1'; }
const S={ messages:new Array(__OWNERIDX__ + 1).fill(null)__SESSION__ };
S.messages[__OWNERIDX__]={ role:'assistant', _anchor_stream_id:__OWNERSTREAM__,__OWNERSCENE__ };
// Same key expression the impl and _revealTransparentEarlierSteps use.
if(__REVEALED__) _transparentRevealedTurns.add(_transparentRevealKey(S&&S.session&&S.session.session_id,__OWNERIDX__));
console.log(JSON.stringify({
  redundant:_transparentSettledTurnSecondRenderIsRedundant(__QUERIED__),
  collapsed:_collapseJustSettledWorklogInPlace(__QUERIED__),
  cacheEntry:_sessionHtmlCache.has('sess-1') ? _sessionHtmlCache.get('sess-1') : null,
  cacheSid:_sessionHtmlCacheSid,
}));
"""
    return (
        template.replace("__CAP__", str(cap))
        .replace("__SLACK__", str(slack))
        .replace("__ROWCOUNT__", str(row_count))
        .replace("__TRANSPARENT__", str(transparent).lower())
        .replace("__OWNERIDX__", str(owner_idx))
        .replace("__OWNERSTREAM__", json.dumps(owner_stream_id))
        .replace("__OWNERSCENE__", owner_scene)
        .replace("__QUERIED__", json.dumps(queried))
        .replace("__REVEALED__", str(revealed).lower())
        .replace("__STASH__", _keep_open_stash_js(session_id, owner_idx + 1))
        .replace(
            "__SESSION__",
            f", session:{{ session_id:{json.dumps(session_id)} }}" if session_id else "",
        )
    )


def _keep_open_stash_js(session_id: str | None, msg_count: int) -> str:
    """The PRE-INIT snapshot renderMessages stashes while the keep-open token is
    armed — the only HTML the settle skip is allowed to publish (#7677 review
    r2 P1: a post-init innerHTML snapshot keeps the copy buttons / tree views but
    not their onclick handlers, so restored controls would be dead)."""
    if not session_id:
        return "null"
    return (
        "{ sid:%s, msgCount:%d, renderWindowKey:'wk', signature:'sig1', "
        "html:'<div>pre-init</div>' }" % (json.dumps(session_id), msg_count)
    )


def test_transparent_settle_within_row_cap_skips_the_second_transcript_rebuild():
    """Report items 9/10: when the just-settled transparent turn is within the
    row cap, the armed settle render is already identical to the unarmed one, so
    STREAM_DONE must report "collapsed in place" and skip the second full
    transcript rebuild."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(_run_node(_transp_settle_harness(cap + slack, True, "stream-1")))
    assert m["redundant"] is True, (
        "a just-settled transparent turn within the row cap needs no re-cap, so a "
        "second render is redundant; got redundant=False"
    )
    assert m["collapsed"] is True, (
        "_collapseJustSettledWorklogInPlace must claim the transparent settle case "
        "(nothing to collapse) so the STREAM_DONE handler skips its second full "
        f"transcript rebuild; got collapsed={m['collapsed']}"
    )


def test_transparent_settle_over_row_cap_still_pays_the_second_rebuild():
    """Fail closed: a turn whose rows EXCEED the cap slack is re-capped by an
    unarmed rebuild, so the second render must stay."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(_run_node(_transp_settle_harness(cap + slack + 1, True, "stream-1")))
    assert m["redundant"] is False
    assert m["collapsed"] is False, (
        "an over-cap transparent turn is re-capped by the unarmed rebuild, so the "
        "second render must NOT be skipped; got collapsed=True"
    )


def test_transparent_settle_plain_reply_without_activity_rows_skips_the_second_render():
    """The common case: a transparent turn with NO worklog-worthy scene (plain
    reply, no tool/thinking rows) renders zero settled rows, so an unarmed rebuild
    cannot change it either — the second full rebuild must be skipped there too."""
    m = json.loads(
        _run_node(_transp_settle_harness(0, True, "stream-1", owner_has_scene=False))
    )
    assert m["redundant"] is True, (
        "a plain transparent reply renders no activity rows, so the second "
        f"STREAM_DONE rebuild is redundant; got redundant={m['redundant']}"
    )
    assert m["collapsed"] is True


def test_non_transparent_settle_keeps_the_existing_fallback():
    """The non-transparent path is owned by the settled-scene owner group: with
    no matching group the helper must keep returning False (unchanged behavior,
    the second render stays the fail-closed fallback)."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(_run_node(_transp_settle_harness(cap + slack, False, "stream-1")))
    assert m["redundant"] is False
    assert m["collapsed"] is False


def test_transparent_settle_fails_closed_when_scene_rows_are_not_rendered():
    """A worklog-worthy scene whose rows are NOT in the rendered transcript
    (virtualization window / cache divergence) cannot be proven equal to the
    unarmed rebuild, so the second render must stay."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(
        _run_node(
            _transp_settle_harness(
                0, True, "stream-1", owner_has_scene=True
            )
        )
    )
    assert m["redundant"] is False
    assert m["collapsed"] is False


def test_transparent_settle_fails_closed_when_the_turn_is_unknown():
    """No owner message for the settling stream id (virtualized out / scene
    stripped) => cannot prove the renders match => keep the second render."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(
        _run_node(_transp_settle_harness(cap + slack, True, "stream-1", queried_stream_id="stream-unknown"))
    )
    assert m["redundant"] is False
    assert m["collapsed"] is False


def test_hidden_reasoning_rows_do_not_mask_an_over_cap_turn():
    """#7677 review P1 — the skip predicate must decide with the SAME inputs the
    unarmed renderer uses: the scene's row list, not the node count left by the
    ARMED render. Hidden reasoning rows are in `rows` but never produce a node, so
    a node count reads `cap+slack` while `rows.length` is `cap+slack+5`: the
    unarmed rebuild WOULD re-cap that turn to its last `cap` rows plus a
    "Show earlier steps" affordance, and skipping it would leave a permanently
    expanded turn."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(
        _run_node(
            _transp_settle_harness(
                cap + slack,
                True,
                "stream-1",
                scene_row_count=cap + slack + 5,
            )
        )
    )
    assert m["redundant"] is False, (
        "an over-cap scene row list must keep the second render even when the "
        "armed render left only cap+slack nodes behind (hidden reasoning rows "
        f"render no node); got redundant={m['redundant']}"
    )
    assert m["collapsed"] is False


def test_revealed_over_cap_turn_still_skips_the_second_render():
    """Mirror of P1 in the other direction: a turn the reader already revealed is
    never re-capped by the unarmed rebuild, so the second render stays redundant
    even with an over-cap row list."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(
        _run_node(
            _transp_settle_harness(
                cap + slack,
                True,
                "stream-1",
                scene_row_count=cap + slack + 5,
                revealed=True,
            )
        )
    )
    assert m["redundant"] is True
    assert m["collapsed"] is True


def test_transparent_settle_skip_still_writes_the_session_html_cache():
    """#7677 review P2 — the unarmed second render was the pass that populated
    `_sessionHtmlCache` for transparent turns (the armed render is deliberately
    uncached, #5260). Skipping it must not leave the session uncached, or
    switching away and back pays a full rebuild."""
    cap, slack = _transparent_cap_consts()
    m = json.loads(
        _run_node(
            _transp_settle_harness(cap + slack, True, "stream-1", session_id="sess-1")
        )
    )
    assert m["collapsed"] is True
    entry = m["cacheEntry"]
    assert entry is not None, (
        "the skipped second render must be replaced by a direct snapshot of the "
        "already-settled DOM in _sessionHtmlCache; got no cache entry"
    )
    assert entry["msgCount"] == 6
    assert entry["renderWindowKey"] == "wk"
    assert entry["signature"] == "sig1"
    assert entry["html"] == "<div>pre-init</div>", (
        "the published entry must carry the stashed PRE-INIT snapshot, not the "
        f"live post-init DOM; got {entry['html'][:60]!r}"
    )
    assert m["cacheSid"] == "sess-1"


# ── 3. cache snapshot gates (review P2) ─────────────────────────────────────


def _cache_snapshot_harness(inflight: bool, html_len: int) -> str:
    template = _extract_func_script(UI_JS) + r"""
const _sessionHtmlCache=new Map();
var _sessionHtmlCacheSid=null;
const INFLIGHT = __INFLIGHT__;
const window = { _compressionUi:null, _handoffUi:null };
// The LIVE DOM at settle time is POST-init: addCopyButtons()/initTreeViews()
// already ran, and innerHTML cannot carry their onclick handlers — so a live
// snapshot would restore dead controls (#7677 review r2 P1).
const inner = { html:'<button onclick="liveCopy()">copy</button><div data-tree-init="1">tree</div>' };
Object.defineProperty(inner,'innerHTML',{ get(){ return this.html; } });
function $(id){ return id==='msgInner'?inner:null; }
function _getVisibleMessagesWithIdx(){ return []; }
function _messageVirtualKeepTailCount(){ return 0; }
function _currentMessageVirtualWindow(vis,keep){ return {start:0,end:0}; }
function _messageVirtualWindowKeyFor(m){ return 'wk'; }
function _messageRenderCacheSignature(){ return 'sig1'; }
// The PRE-INIT snapshot the keep-open settle render stashed in renderMessages().
var _settleKeepOpenCacheStash = {
  sid:'sess-1', msgCount:3, renderWindowKey:'wk', signature:'sig1',
  html:'x'.repeat(__HTMLLEN__),
};
const S = { session:{session_id:'sess-1'}, messages:[1,2,3] };
if(src.search(/function\s+_cacheSettledTranscriptHtml\s*\(/)>=0){
  eval(extractFunc('_cacheSettledTranscriptHtml'));
}
let ok=null, err=null;
if(typeof _cacheSettledTranscriptHtml==='function'){
  try{ ok=_cacheSettledTranscriptHtml(); }catch(e){ err=String(e); }
}else{ err='missing'; }
console.log(JSON.stringify({
  ok, err,
  entry:_sessionHtmlCache.has('sess-1')?_sessionHtmlCache.get('sess-1'):null,
  stashHtml:_settleKeepOpenCacheStash?_settleKeepOpenCacheStash.html.length:null,
}));
"""
    return template.replace(
        "__INFLIGHT__", "{'sess-1':true}" if inflight else "{}"
    ).replace("__HTMLLEN__", str(html_len))


def test_cache_snapshot_writes_a_cacheable_entry():
    m = json.loads(_run_node(_cache_snapshot_harness(inflight=False, html_len=64)))
    assert m["err"] is None, m["err"]
    assert m["ok"] is True, (
        "the settle-skip path must publish the keep-open render's PRE-INIT "
        f"snapshot into _sessionHtmlCache; got ok={m['ok']}"
    )
    assert m["entry"] is not None
    assert m["entry"]["msgCount"] == 3
    assert m["entry"]["html"] == "x" * 64, (
        "the cached HTML must be the stashed PRE-INIT snapshot, never the live "
        f"post-init DOM; got {m['entry']['html'][:40]!r}"
    )
    assert m["stashHtml"] is None, "the stash must be consumed once published"


def test_cache_snapshot_respects_the_inflight_and_size_gates():
    """Same gates as renderMessages' own cache write: an in-flight stream and an
    oversized transcript must never be cached."""
    busy = json.loads(_run_node(_cache_snapshot_harness(inflight=True, html_len=64)))
    assert busy["ok"] is False
    assert busy["entry"] is None, "an in-flight session must not be cached"
    huge = json.loads(_run_node(_cache_snapshot_harness(inflight=False, html_len=300_000)))
    assert huge["ok"] is False
    assert huge["entry"] is None, "transcripts >=300KB must not be cached"


def test_stream_done_handler_wires_the_in_place_collapse_before_the_fallback():
    """Source lock (#4970 gate, kept intact): the settle chain still prefers the
    in-place collapse and only falls back to the scroll-preserving second
    render, and both stay before the follower re-settle."""
    import tests.test_issue4970_stream_done_shrink_regression as gate  # noqa: F401

    messages_js = gate.MESSAGES_JS
    disarm_idx = messages_js.index("_disarmKeepSettledWorklogOpen()")
    after = messages_js[disarm_idx : disarm_idx + 1100]
    assert "&& _collapseJustSettledWorklogInPlace(_settledStreamId)" in after
    assert "_renderMessagesWithScrollSnapshot({_prescrollSnapshot:_doneLiveScrollSnapshot})" in after
