import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    m = re.search(rf"function\s+{re.escape(name)}\b", src)
    assert m, f"{name} not found"
    start = src.index("{", m.end())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): i + 1]
    raise AssertionError(f"unterminated {name}")


def test_session_switch_stabilization_behavior_and_mutation_sensitivity():
    functions = "\n".join(
        _function_body(UI_JS, name)
        for name in (
            "_endSessionSwitchLayoutStabilization",
            "_beginSessionSwitchLayoutStabilization",
            "_finishSessionSwitchLayoutStabilization",
            "_scheduleSessionSwitchLayoutQuietCheck",
            "_beginSessionSwitchLayoutPostProcess",
            "_endSessionSwitchLayoutPostProcess",
            "_settleSessionSwitchLayoutStabilization",
        )
    )
    script = f"""
const assert=require('assert');
let _sessionSwitchLayoutStabilizationSid='';
let _sessionSwitchLayoutLoadGeneration=null;
let _sessionSwitchLayoutStabilizationToken=0;
let _sessionSwitchLayoutStabilizationTimer=0;
let _sessionSwitchLayoutStabilizationObserver=null;
let _sessionSwitchLayoutWatchdogTimer=0;
let _sessionSwitchLayoutPostProcessPending=0;
let _sessionSwitchLayoutSettleRequested=false;
let _messageUserUnpinned=false;
let _scrollPinned=true;
let _bottomSettleToken=0;
let _settleRO=null;
let _settleTimer=0;
let _settleFinalTimer=0;
let _settleRAF=0;
let _programmaticScroll=true;
let bottomCalls=0;
let raf=[];
let timers=[];
const classes=new Set();
const inner={{}};
const el={{classList:{{add:x=>classes.add(x),remove:x=>classes.delete(x)}}}};
function $(id){{return id==='messages'?el:(id==='msgInner'?inner:null);}}
const document={{getElementById:id=>id==='msgInner'?inner:null}};
class ResizeObserver{{constructor(cb){{this.cb=cb;}}observe(){{}}disconnect(){{}}}}
function scrollToBottom(){{bottomCalls++;}}
function requestAnimationFrame(fn){{raf.push(fn);return raf.length;}}
function cancelAnimationFrame(){{}}
function setTimeout(fn){{timers.push(fn);return timers.length;}}
function clearTimeout(){{}}
{functions}
_beginSessionSwitchLayoutStabilization('incoming',1);
assert(classes.has('session-switch-layout-stabilizing'));
_settleSessionSwitchLayoutStabilization('incoming',1);
// settle schedules the quiet-check THEN arms the watchdog, so the quiet-check
// is the second-to-last timer. Fire it (not the watchdog) to settle normally.
timers.at(-2)();
while(raf.length) raf.shift()();
assert(!classes.has('session-switch-layout-stabilizing'));
assert.strictEqual(bottomCalls,1);
_beginSessionSwitchLayoutStabilization('incoming-2',2);
_messageUserUnpinned=true;
_settleSessionSwitchLayoutStabilization('incoming-2',2);
timers.at(-2)();
while(raf.length) raf.shift()();
assert.strictEqual(bottomCalls,1);
console.log(JSON.stringify({{bottomCalls,hasClass:classes.has('session-switch-layout-stabilizing')}}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"bottomCalls": 1, "hasClass": False}

    # Mutation: removing the CSS class add must make the same behavior test fail.
    mutated = functions.replace(
        "if(el&&el.classList) el.classList.add('session-switch-layout-stabilizing');",
        "// mutation: class add removed",
    )
    mutated_result = subprocess.run(
        ["node", "-e", script.replace(functions, mutated)], capture_output=True, text=True
    )
    assert mutated_result.returncode != 0


def test_session_switch_wires_begin_before_reset_and_settle_after_both_render_branches():
    load = _function_body(SESSIONS_JS, "loadSession")
    begin = "window._beginSessionSwitchLayoutStabilization(sid, _loadGeneration);"
    reset = "window._resetScrollDirectionTracker();"
    # The streaming branch opts out of the ResizeObserver (third arg `true`); the
    # idle branch keeps the default. Both still settle after their render.
    settle_streaming = "window._settleSessionSwitchLayoutStabilization(sid, _loadGeneration, true);"
    settle_idle = "window._settleSessionSwitchLayoutStabilization(sid, _loadGeneration);"
    assert begin in load
    assert load.index(begin) < load.index(reset)
    # Two streaming settles now: the INFLIGHT streaming-restore branch and the
    # post-await activeStreamId attach branch. Both opt out of the observer.
    assert load.count(settle_streaming) == 2
    assert load.count(settle_idle) == 1
    for marker, settle in (
        ("if(activeStreamId){", settle_streaming),
        ("}else{\n      S.busy=false;", settle_idle),
    ):
        branch = load[load.index(marker):]
        assert branch.index("renderMessages(") < branch.index(settle)


def test_live_anchor_scroll_guard_restores_after_release_layout_frame():
    for name in ("renderLiveAnchorActivityScene", "_renderLiveAnchorActivitySceneTransparent"):
        body = _function_body(UI_JS, name)
        compact = re.sub(r"\s+", "", body)
        release = "scrollRebuildGuard.release();"
        # release() must be called UNCONDITIONALLY here (not gated on the render
        # generation). Ownership is enforced INSIDE release() via a separate
        # min-height cleanup owner token, so gating the call on the render
        # generation would strand min-height set whenever a no-guard successor
        # advanced the generation without taking cleanup ownership (the leak the
        # maintainer flagged). The following-frame snapshot restore stays gated.
        assert release in compact
        gated_release = "_liveAnchorScrollRebuildGuardCurrent(scrollRebuildIdentity))return;scrollRebuildGuard.release();"
        assert gated_release not in compact, f"{name}: release() must NOT be render-generation gated"
        nested = "requestAnimationFrame(()=>{if(scrollRebuildIdentity&&typeof_liveAnchorScrollRebuildGuardCurrent==='function'&&!_liveAnchorScrollRebuildGuardCurrent(scrollRebuildIdentity))return;if(_messageUserUnpinned)_restoreMessageScrollSnapshotSameFrame(scrollSnapshot);});"
        assert nested in compact
        assert compact.index(release) < compact.index(nested)


def test_min_height_guard_release_ownership_survives_no_guard_successor():
    """The min-height cleanup guard must own its release via a token tracked
    SEPARATELY from the render/session generation. Every rebuild advances the
    render generation, but only a guard-installing rebuild takes cleanup
    ownership — so a later no-guard rebuild (reader re-pinned / session switch)
    can't invalidate an older release and strand msgInner.style.minHeight set
    (the permanent-frozen-layout leak, which could regress desktop too).
    """
    prep = _function_body(UI_JS, "_prepareLiveAnchorScrollRebuildGuard")
    # A dedicated owner counter, distinct from _liveAnchorScrollRebuildGeneration.
    assert "_liveAnchorMinHeightGuardOwner" in prep
    assert "const guardOwnerToken=++_liveAnchorMinHeightGuardOwner;" in prep
    # release() no-ops unless THIS guard is still the current cleanup owner.
    assert "if(released||guardOwnerToken!==_liveAnchorMinHeightGuardOwner) return;" in prep
    # Only a guard-installing prepare bumps the owner: the early no-guard returns
    # must happen BEFORE the owner is taken.
    assert prep.index("return {readerAwayFromBottom:false,release:null};") < prep.index("++_liveAnchorMinHeightGuardOwner")
    assert prep.index("return {readerAwayFromBottom:true,release:null};") < prep.index("++_liveAnchorMinHeightGuardOwner")
    # The owner global is declared once.
    assert UI_JS.count("let _liveAnchorMinHeightGuardOwner=0;") == 1


def test_inflight_streaming_restore_settles_stabilization():
    """The INFLIGHT streaming-restore branch renders + reattaches and returns
    without falling through to the idle/attach settle, so it must settle
    stabilization itself (with the streaming flag) — otherwise begin=1/settle=0/
    end=0 leaves the transcript forced-visible for the whole session.
    """
    load = _function_body(SESSIONS_JS, "loadSession")
    inflight = load[load.index("if(INFLIGHT[sid]){"):load.index("}else{\n    // Phase 2b")]
    assert "window._settleSessionSwitchLayoutStabilization(sid, _loadGeneration, true);" in inflight, \
        "INFLIGHT streaming-restore branch must settle with the streaming flag"


def test_same_session_force_reload_force_retires_prior_stabilization():
    """A same-session force reload skips _begin (currentSid===sid) but is the
    authoritative current load, so it must force-retire + invalidate any prior
    stabilization left active by an older cross-session load, or the transcript
    can stay forced-visible / min-height-pinned.
    """
    load = _function_body(SESSIONS_JS, "loadSession")
    # The else-branch of the _begin gate handles same-session force reload.
    assert "} else if (sameSessionForceReload && typeof window !== 'undefined' && typeof window._endSessionSwitchLayoutStabilization === 'function') {" in load
    assert "window._endSessionSwitchLayoutStabilization(_loadGeneration, undefined, true);" in load


def test_authoritative_same_sid_load_force_retires_armed_stabilization():
    """Rapid A→B→A→B: a same-SID non-force load re-selecting the already-open
    session (the early-return no-op) is still the authoritative current load. If
    an abandoned switch left stabilization armed (a B load that never became
    current and whose stale return was gated out by a newer generation), this
    path must force-retire it — otherwise `session-switch-layout-stabilizing`
    stays armed forever and silently disables mobile user-row virtualization.
    """
    load = _function_body(SESSIONS_JS, "loadSession")
    early = load[load.index("if(currentSid===sid && !forceReload"):load.index("return;\n  }\n")]
    assert "window._endSessionSwitchLayoutStabilization(undefined, undefined, true);" in early, \
        "authoritative same-SID re-select must force-retire any armed stabilization"


def test_settle_arms_bounded_watchdog_that_force_retires_stuck_processor():
    """The quiet-check waits on the pending-work count, so a single async
    processor that never resolves keeps pending>0 and leaves stabilization armed
    for the whole tab. _settle must arm an INDEPENDENT absolute-cap watchdog that
    force-retires the owning token after a bound even when a waiter never
    releases. It's cleared on _end so a normal settle doesn't fire it late.
    """
    settle = _function_body(UI_JS, "_settleSessionSwitchLayoutStabilization")
    end = _function_body(UI_JS, "_endSessionSwitchLayoutStabilization")
    assert "_sessionSwitchLayoutWatchdogTimer=setTimeout(" in settle
    # Watchdog force-retires (force=true) the exact token/generation it armed for.
    assert "_endSessionSwitchLayoutStabilization(loadGeneration,token,true);" in settle
    assert ",15000);" in settle
    # Token guard so a stale watchdog can't clear a newer stabilization.
    assert "if(token!==_sessionSwitchLayoutStabilizationToken) return;" in settle
    # Cleared by _end so a completed settle never fires the watchdog afterward.
    assert "clearTimeout(_sessionSwitchLayoutWatchdogTimer);" in end
    assert UI_JS.count("let _sessionSwitchLayoutWatchdogTimer=0;") == 1


def test_watchdog_retires_stabilization_when_processor_never_resolves():
    """Behavioral: drive the real begin/settle/postprocess/end helpers with a
    pending marker that NEVER releases, advance a fake clock past the cap, and
    assert the stabilizing class is removed anyway (the silent-leak the
    maintainer flagged). Uses node with a controllable timer queue.
    """
    fns = "\n".join(_function_body(UI_JS, n) for n in (
        "_endSessionSwitchLayoutStabilization",
        "_beginSessionSwitchLayoutStabilization",
        "_finishSessionSwitchLayoutStabilization",
        "_scheduleSessionSwitchLayoutQuietCheck",
        "_beginSessionSwitchLayoutPostProcess",
        "_endSessionSwitchLayoutPostProcess",
        "_settleSessionSwitchLayoutStabilization",
    ))
    script = f"""
const assert=require('assert');
// Controllable clock: collect timers, fire by advancing a virtual now.
let _now=0; const _timers=[];
global.setTimeout=(fn,ms)=>{{const id={{fn,at:_now+(ms||0),cancelled:false}};_timers.push(id);return id;}};
global.clearTimeout=(id)=>{{if(id&&typeof id==='object')id.cancelled=true;}};
global.requestAnimationFrame=(fn)=>{{_timers.push({{fn,at:_now,cancelled:false}});return 0;}};
global.cancelAnimationFrame=()=>{{}};
function advance(ms){{_now+=ms;let ran=true;while(ran){{ran=false;_timers.slice().sort((a,b)=>a.at-b.at).forEach(t=>{{if(!t.cancelled&&t.at<=_now){{t.cancelled=true;ran=true;t.fn();}}}});}}}}
// Minimal DOM: a messages element whose classList tracks the stabilizing class.
let _classes=new Set();
const messagesEl={{classList:{{add:c=>_classes.add(c),remove:c=>_classes.delete(c),contains:c=>_classes.has(c)}}}};
function $(id){{return id==='messages'?messagesEl:null;}}
function document_getElementById(){{return null;}}
const document={{getElementById:()=>null}};
let _scrollPinned=false,_messageUserUnpinned=true;
function scrollToBottom(){{}}
// Stabilization state globals.
let _sessionSwitchLayoutStabilizationSid='';
let _sessionSwitchLayoutLoadGeneration=null;
let _sessionSwitchLayoutStabilizationToken=0;
let _sessionSwitchLayoutStabilizationTimer=0;
let _sessionSwitchLayoutStabilizationObserver=null;
let _sessionSwitchLayoutWatchdogTimer=0;
let _sessionSwitchLayoutPostProcessPending=0;
let _sessionSwitchLayoutSettleRequested=false;
let _bottomSettleToken=0,_settleRO=null,_settleTimer=0,_settleFinalTimer=0,_settleRAF=0,_programmaticScroll=false;
// No ResizeObserver in this harness -> settle takes the non-observer path.
{fns}
// Switch INTO a session (idle path, observer would arm but is absent here).
_beginSessionSwitchLayoutStabilization('sidA', 1);
assert.strictEqual(_classes.has('session-switch-layout-stabilizing'), true, 'armed on begin');
_settleSessionSwitchLayoutStabilization('sidA', 1, false);
// Register a post-process waiter that NEVER releases (hung fetch).
const marker=_beginSessionSwitchLayoutPostProcess();
assert.ok(marker, 'pending marker registered');
assert.strictEqual(_sessionSwitchLayoutPostProcessPending, 1);
// Advance past the quiet window: pending>0 so the quiet-check must NOT settle.
advance(1000);
assert.strictEqual(_classes.has('session-switch-layout-stabilizing'), true, 'still armed while pending>0');
// Advance past the watchdog cap: it must force-retire despite the stuck waiter.
advance(15000);
assert.strictEqual(_classes.has('session-switch-layout-stabilizing'), false, 'watchdog force-retired the stuck stabilization');
assert.strictEqual(_sessionSwitchLayoutStabilizationSid, '', 'state cleared');
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_session_switch_has_no_fixed_load_expiry_and_postprocess_is_event_driven():
    begin = _function_body(UI_JS, "_beginSessionSwitchLayoutStabilization")
    settle = _function_body(UI_JS, "_settleSessionSwitchLayoutStabilization")
    quiet = _function_body(UI_JS, "_scheduleSessionSwitchLayoutQuietCheck")
    post = _function_body(UI_JS, "_postProcessWithAnchorSuppression")
    assert "15000" not in begin
    assert "ResizeObserver" in settle
    assert "_sessionSwitchLayoutPostProcessPending>0" in quiet
    assert "_beginSessionSwitchLayoutPostProcess()" in post
    assert "_endSessionSwitchLayoutPostProcess(sessionSwitchPostProcess)" in post


def test_live_anchor_delayed_restore_is_generation_and_session_guarded():
    guard = _function_body(UI_JS, "_liveAnchorScrollRebuildGuardCurrent")
    assert "_liveAnchorScrollRebuildGeneration" in guard
    assert "S.session.session_id" in guard
    for name in ("renderLiveAnchorActivityScene", "_renderLiveAnchorActivitySceneTransparent"):
        body = _function_body(UI_JS, name)
        guarded_identity = "const scrollRebuildIdentity=(typeof _nextLiveAnchorScrollRebuildGuard==='function')?_nextLiveAnchorScrollRebuildGuard():null;"
        assert guarded_identity in body
        assert "_liveAnchorScrollRebuildGuardCurrent(scrollRebuildIdentity)" in body


def test_settle_skips_observer_when_switching_into_streaming_session():
    """Switching INTO an actively-streaming session must not arm the
    ResizeObserver: the live turn grows continuously, so the observer would keep
    firing and the quiet-check would never reach zero-pending until the stream
    pauses, leaving content-visibility forced on every user row for the whole
    stream (temporarily undoing #5637). The streaming call site passes the flag;
    the idle branch does not (observer still arms for a static transcript).
    """
    settle = _function_body(UI_JS, "_settleSessionSwitchLayoutStabilization")
    assert "function _settleSessionSwitchLayoutStabilization(sid,loadGeneration,streaming)" in settle
    # The observer arm is gated on NOT streaming. Mutation guard: dropping the
    # !streaming term would re-arm the observer during streaming (the bug).
    assert "if(!streaming&&typeof ResizeObserver==='function')" in settle
    # The streaming loadSession branch opts out of the observer; the idle branch
    # keeps the default (no third arg).
    assert "window._settleSessionSwitchLayoutStabilization(sid, _loadGeneration, true);" in SESSIONS_JS
    assert "window._settleSessionSwitchLayoutStabilization(sid, _loadGeneration);" in SESSIONS_JS
    # A single quiet-check is still scheduled so stabilization settles (waiting on
    # any pending async post-processing) even without the observer.
    assert "_scheduleSessionSwitchLayoutQuietCheck(token,sid,loadGeneration);" in settle


def test_touch_css_forces_real_user_row_layout_only_during_switch():
    selector = ".messages.session-switch-layout-stabilizing .msg-row[data-role=\"user\"] { content-visibility: visible; }"
    assert selector in STYLE_CSS
    media = STYLE_CSS[STYLE_CSS.index("@media (pointer: coarse)"):]
    assert media.index(".msg-row[data-role=\"user\"] { content-visibility: auto;") < media.index(selector)


def test_current_load_can_force_retire_superseded_stabilization():
    """A superseding current load must be able to retire stabilization owned by
    the load it replaced (a forced reload that skipped _begin because
    currentSid===sid), while a stale continuation must stay gated by generation.
    The end helper takes a `force` flag, and every sessions.js cleanup passes
    `_isCurrentLoad()` so only the authoritative current load forces the retire.
    """
    end = _function_body(UI_JS, "_endSessionSwitchLayoutStabilization")
    # Signature carries the force flag and the generation gate is bypassed only
    # when force is truthy.
    assert "function _endSessionSwitchLayoutStabilization(loadGeneration,token,force)" in end
    assert "if(!force&&loadGeneration!==undefined&&loadGeneration!==null&&loadGeneration!==_sessionSwitchLayoutLoadGeneration) return;" in end
    # Mutation guard: removing the !force bypass would let a stale continuation
    # clear newer state (the very bug the finding describes).
    assert "!force&&" in end
    # Every sessions.js cleanup passes its ownership decision as the force arg.
    calls = SESSIONS_JS.count(
        "window._endSessionSwitchLayoutStabilization(_loadGeneration, undefined, _isCurrentLoad());"
    )
    assert calls == 6, f"expected 6 ownership-aware cleanups, found {calls}"
    assert "window._endSessionSwitchLayoutStabilization(_loadGeneration);" not in SESSIONS_JS


def test_async_pdf_and_yaml_processors_hold_pending_marker_until_completion():
    """PDF rendering and the deferred js-yaml tree-view build are asynchronous
    past postProcessRenderedMessages() returning. Both must register a
    session-switch pending marker and release it only on real completion, so the
    quiet timer cannot expire before their final DOM insertion.
    """
    pdf = _function_body(UI_JS, "loadPdfInline")
    assert "_beginSessionSwitchLayoutPostProcess()" in pdf
    assert "releaseMarker" in pdf
    # Marker is released, not just on launch: the render-complete branch (i>n),
    # the too-large branch, the no-pdf branch, the error catch, and the pdfjs
    # load timeout all release it.
    assert pdf.count("releaseMarker()") >= 4
    # Idempotent release guard so double-release can't corrupt the pending count.
    assert "_pdfMkDone" in pdf

    tree = _function_body(UI_JS, "initTreeViews")
    assert "_beginSessionSwitchLayoutPostProcess()" in tree
    assert "_loadJsyamlThen(" in tree
    assert "_endSessionSwitchLayoutPostProcess(yamlMarker)" in tree
    # The loader must invoke the settle callback on BOTH success and failure.
    loader = _function_body(UI_JS, "_loadJsyamlThen")
    assert "s.onerror=()=>{ _jsyamlLoading=false; if(settle) settle(); }" in loader
    assert "s.onload=()=>{ _jsyamlLoading=false; try{ cb(); } finally{ if(settle) settle(); } }" in loader


def test_multi_pdf_shared_loader_failure_releases_every_waiter():
    """pdfjs is loaded once and shared. When two PDFs are pending and the shared
    loader fails/stalls, EVERY waiter must release its own marker — not just the
    loader owner — or a blocked CDN strands the other waiters' markers and
    stabilization never settles. The fix gives each waiting el its own bounded
    timeout (outside the `!_pdfjsLoading` owner block) that releases its own
    marker, plus an onerror reset so a failed load doesn't wedge _pdfjsLoading.
    """
    pdf = _function_body(UI_JS, "loadPdfInline")
    compact = re.sub(r"\s+", "", pdf)
    # The per-el timeout must live in the shared else-branch, NOT nested inside
    # the `if(!_pdfjsLoading)` owner-only block. Assert the timeout guard + its
    # release are present and reference the per-el done flag (so it runs for
    # every waiter, and no-ops if the el already settled).
    assert "if(_pdfjsReady||_pdfMkDone)return;" in compact, \
        "each waiting PDF must have its own bounded cleanup timeout"
    # The owner block resets _pdfjsLoading on script error so a failed shared
    # load can be retried and doesn't permanently block future PDFs.
    assert "s.onerror=()=>{_pdfjsLoading=false;};" in compact
    # The pdfjs-ready listener is registered for every waiter (outside the owner
    # block), so a late success still renders all pending PDFs.
    owner_block = pdf[pdf.index("if(!_pdfjsLoading){"):pdf.index("window.addEventListener('pdfjs-ready'")]
    assert "setTimeout(" not in owner_block, \
        "the bounded cleanup timeout must be per-el (outside the loader-owner block)"
