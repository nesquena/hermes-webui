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
_beginSessionSwitchLayoutStabilization('incoming');
assert(classes.has('session-switch-layout-stabilizing'));
_settleSessionSwitchLayoutStabilization('incoming');
timers.at(-1)();
while(raf.length) raf.shift()();
assert(!classes.has('session-switch-layout-stabilizing'));
assert.strictEqual(bottomCalls,1);
_beginSessionSwitchLayoutStabilization('incoming-2');
_messageUserUnpinned=true;
_settleSessionSwitchLayoutStabilization('incoming-2');
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
    begin = "window._beginSessionSwitchLayoutStabilization(sid);"
    reset = "window._resetScrollDirectionTracker();"
    settle = "window._settleSessionSwitchLayoutStabilization(sid);"
    assert begin in load
    assert load.index(begin) < load.index(reset)
    assert load.count(settle) == 2
    for marker in (
        "if(activeStreamId){",
        "}else{\n      S.busy=false;",
    ):
        branch = load[load.index(marker):]
        assert branch.index("renderMessages(") < branch.index(settle)


def test_live_anchor_scroll_guard_restores_after_release_layout_frame():
    for name in ("renderLiveAnchorActivityScene", "_renderLiveAnchorActivitySceneTransparent"):
        body = _function_body(UI_JS, name)
        compact = re.sub(r"\s+", "", body)
        release = "scrollRebuildGuard.release();"
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


def test_touch_css_forces_real_user_row_layout_only_during_switch():
    selector = ".messages.session-switch-layout-stabilizing .msg-row[data-role=\"user\"] { content-visibility: visible; }"
    assert selector in STYLE_CSS
    media = STYLE_CSS[STYLE_CSS.index("@media (pointer: coarse)"):]
    assert media.index(".msg-row[data-role=\"user\"] { content-visibility: auto;") < media.index(selector)
