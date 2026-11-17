"""Behavioral tests for cost-aware live-stream render pacing."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for the render pacing probe")

PRELUDE = r"""
let now=0, renderCostMs=0;
const timers=[], rafs=[];
const performance={now:()=>now};
function setTimeout(fn,delay){timers.push({fn,delay});return timers.length;}
function requestAnimationFrame(fn){rafs.push(fn);return rafs.length;}
let _renderPending=false, _streamFinalized=false, _pendingRafHandle=null;
let assistantText='', liveReasoningText='', segmentStart=0;
let _cachedParsed=null, _cachedParsedText='', _cachedParsedReasoning='';
const S={activeStreamId:'stream'}, streamId='stream', assistantBody=null, assistantRow=null;
const window={};
function _isActiveSession(){return true;}
function _flushPendingReasoningRender(){}
function _parseStreamState(){now+=renderCostMs;return {displayText:''};}
function _renderLiveThinking(){}
function _shouldUseLiveProseFade(){return false;}
function _upsertAnchorProcessProse(){}
function scrollIfPinned(){}
function _throttledSnapshotLiveTurn(){}
"""

HARNESS = r"""
const vm=require('node:vm');
const context={costs:JSON.parse(process.argv[2]),finalized:process.argv[3]==='finalized'};
vm.createContext(context);
vm.runInContext(process.argv[1],context,{filename:'messages-schedule-render.js'});
const result=vm.runInContext(`
if(finalized){
  _streamFinalized=true;
  _scheduleRender();
  ({finalized:true,scheduled:timers.length+rafs.length});
}else{
  for(const cost of costs){
    renderCostMs=cost;
    _scheduleRender();
    if(timers.length){
      const timer=timers.shift();
      now+=timer.delay;
      timer.fn();
    }
    const raf=rafs.shift();
    if(!raf) throw new Error('render was not scheduled');
    raf();
  }
  _scheduleRender();
  const delay=timers.length?timers[0].delay:0;
  ({costMs:_lastRenderCostMs,intervalMs:now-_lastRenderMs+delay,scheduled:timers.length+rafs.length});
}
`,context);
console.log(JSON.stringify(result));
"""


def _extract_declaration(src: str, name: str, *, required: bool = True) -> str:
    match = re.search(
        rf"^[ \t]*(?:const|let|var)\s+{re.escape(name)}\s*=\s*[^;\n]+;",
        src,
        re.MULTILINE,
    )
    if required:
        assert match, f"production declaration {name} not found"
    return match.group(0).strip() if match else ""


def _extract_function(src: str, name: str, *, required: bool = True) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\)\{{", src)
    if not match:
        if required:
            raise AssertionError(f"production function {name} not found")
        return ""
    depth = 0
    for index in range(match.end() - 1, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[match.start() : index + 1]
    raise AssertionError(f"unbalanced braces in production function {name}")


def _render_probe(costs: list[float], finalized: bool = False) -> dict:
    src = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    production = [
        _extract_declaration(src, name)
        for name in ("_lastRenderMs", "_lastRenderCostMs", "LIVE_RENDER_BUSY_FACTOR")
    ]
    # If the sibling typing-deferral feature is composed into this checkout,
    # execute its real helper and state; otherwise inject no substitute helper.
    production.extend(
        _extract_declaration(src, name, required=False)
        for name in (
            "COMPOSER_TYPING_RENDER_DEFER_MS",
            "COMPOSER_TYPING_RENDER_MAX_DEFER_MS",
            "_composerTypingUntil",
            "_composerTypingDeferStarted",
        )
    )
    production.append(_extract_function(src, "_deferLiveRenderForTyping", required=False))
    production.append(_extract_function(src, "_scheduleRender"))
    result = subprocess.run(
        [NODE or "node", "-e", HARNESS, PRELUDE + "\n".join(production), json.dumps(costs),
         "finalized" if finalized else "live"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cheap_render_keeps_66ms_interval():
    assert _render_probe([10])["intervalMs"] == pytest.approx(66)


def test_100ms_render_paces_next_start_at_400ms():
    assert _render_probe([100])["intervalMs"] == pytest.approx(400)


def test_cost_spike_is_smoothed_by_ema():
    result = _render_probe([100, 100, 300])
    assert result["costMs"] == pytest.approx(160)
    assert result["intervalMs"] == pytest.approx(640)


def test_render_interval_clamps_at_1000ms():
    assert _render_probe([400])["intervalMs"] == pytest.approx(1000)


def test_interval_recovers_after_renders_become_cheap():
    assert _render_probe([100] + [10] * 10)["intervalMs"] == pytest.approx(66)


def test_finalized_stream_still_never_schedules():
    result = _render_probe([], finalized=True)
    assert result["finalized"] is True
    assert result["scheduled"] == 0
