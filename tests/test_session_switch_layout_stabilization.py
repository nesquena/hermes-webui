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
timers.at(-1)();
while(raf.length) raf.shift()();
assert(!classes.has('session-switch-layout-stabilizing'));
assert.strictEqual(bottomCalls,1);
_beginSessionSwitchLayoutStabilization('incoming-2',2);
_messageUserUnpinned=true;
_settleSessionSwitchLayoutStabilization('incoming-2',2);
timers.at(-1)();
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
    assert load.count(settle_streaming) == 1
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
        # The release() call must itself be gated on still owning the guard, so a
        # superseded older callback cannot restore its stale min-height and tear
        # down a newer rebuild's active guard.
        guarded_release = "if(scrollRebuildIdentity&&typeof_liveAnchorScrollRebuildGuardCurrent==='function'&&!_liveAnchorScrollRebuildGuardCurrent(scrollRebuildIdentity))return;scrollRebuildGuard.release();"
        assert guarded_release in compact, f"{name}: release() must be identity-gated"
        nested = "requestAnimationFrame(()=>{if(scrollRebuildIdentity&&typeof_liveAnchorScrollRebuildGuardCurrent==='function'&&!_liveAnchorScrollRebuildGuardCurrent(scrollRebuildIdentity))return;if(_messageUserUnpinned)_restoreMessageScrollSnapshotSameFrame(scrollSnapshot);});"
        assert release in compact
        assert nested in compact
        assert compact.index(release) < compact.index(nested)


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
    # The deferred js-yaml path holds a marker across the async lib load + the
    # re-run that inserts the tree DOM, releasing it from the settle callback so
    # a CDN failure (onerror, which never invokes cb) still clears the pending
    # count instead of wedging the quiet check forever.
    assert "_beginSessionSwitchLayoutPostProcess()" in tree
    assert "_loadJsyamlThen(" in tree
    assert "_endSessionSwitchLayoutPostProcess(yamlMarker)" in tree
    # The loader must invoke the settle callback on BOTH success and failure.
    loader = _function_body(UI_JS, "_loadJsyamlThen")
    assert "s.onerror=()=>{ _jsyamlLoading=false; if(settle) settle(); }" in loader
    assert "s.onload=()=>{ _jsyamlLoading=false; try{ cb(); } finally{ if(settle) settle(); } }" in loader
