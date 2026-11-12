#!/usr/bin/env python3
"""Benchmark real live-turn SSE rendering with deterministic fixture events.

Run from a checkout with Playwright and Google Chrome installed:
  .venv/bin/python scripts/bench/live_render_scaling.py --runs 2

Starts an agent-free WebUI and a separate headless Chrome on ephemeral ports,
with disposable HOME/state/browser profiles. No provider requests or production
state are used. Timings are JS work, not end-to-end provider latency or painted
frames: headless mode does not certify screen paint/compositor cost.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
INIT = r"""
if ('serviceWorker' in navigator) navigator.serviceWorker.register =
  () => Promise.reject(new Error('disabled by render benchmark'));
window.fixtureSources = [];
window.__longtasks = [];
window.__longtaskStart = performance.now();
new PerformanceObserver(list => {
  for (const e of list.getEntries()) window.__longtasks.push(
    {start: e.startTime, duration: e.duration});
}).observe({type: 'longtask', buffered: true});
class FixtureEventSource {
  static OPEN=1; static CONNECTING=0; static CLOSED=2;
  constructor(url) { this.url=String(url); this.readyState=1;
    this.listeners={}; window.fixtureSources.push(this); }
  addEventListener(name,fn) {(this.listeners[name]||=[]).push(fn);}
  removeEventListener() {}
  close() {this.readyState=2;}
  emit(name,data,id) {for (const fn of this.listeners[name]||[])
    fn({data:JSON.stringify(data),lastEventId:String(id||'')});}
}
window.EventSource=FixtureEventSource;
"""
RUN = r"""async ({events, checkpoints}) => {
  const sid='render-bench-fixture', stream='render-bench-stream';
  window._chatActivityDisplayMode=chatActivityMode(); // retain boot default
  await loadSession(sid);
  const deadline=performance.now()+10000;
  while (!fixtureSources.some(s => s.url.includes('api/chat/stream?'))) {
    if (performance.now()>deadline) throw new Error('no live fixture stream');
    await new Promise(r=>setTimeout(r,10));
  }
  const source=fixtureSources.findLast(s =>
    s.url.includes('api/chat/stream?') && s.readyState===1);
  if (!source) throw new Error('no open fixture source');
  const samples=[], rows=[];
  let current=null, depth=0;
  const originalRaf=window.requestAnimationFrame;
  window.requestAnimationFrame=function(callback) {
    return originalRaf.call(this,timestamp=>{
      const start=performance.now();
      try { return callback(timestamp); }
      finally { if (current!==null) samples[current].work.push(performance.now()-start); }
    });
  };
  const original=window._renderLiveAnchorActivitySceneForStream;
  if (typeof original!=='function') throw new Error('live renderer hook absent');
  window._renderLiveAnchorActivitySceneForStream=function(...args) {
    if (current===null) return original.apply(this,args);
    const start=performance.now(); depth++;
    try { return original.apply(this,args); }
    finally { depth--; if (depth===0)
      samples[current].renders.push(performance.now()-start); }
  };
  const median=values => {
    if (!values.length) return null;
    const sorted=[...values].sort((a,b)=>a-b), middle=sorted.length>>1;
    return sorted.length%2 ? sorted[middle] : (sorted[middle-1]+sorted[middle])/2;
  };
  const summarize=sample => {
    const byType={};
    for (const type of ['reasoning','tool','tool_complete','token']) {
      const times=samples.filter(s=>s.type===type).flatMap(s=>s.work);
      if (times.length) byType[type]=median(times);
    }
    return {events_so_far:sample.events,
      median_per_render_ms:median(sample.renders),
      render_count:sample.renders.length, median_by_event_type_ms:byType};
  };
  // Every cycle emits exactly four SSE events. Tool results are 2048 chars.
  const result='r'.repeat(2048);
  for (let i=1; i<=events; i++) {
    const phase=(i-1)%4, toolIndex=Math.floor((i-1)/4);
    let type, data;
    if (phase===0) {type='reasoning';data={text:'Checking fixture step '+toolIndex+'. '};}
    else if (phase===1) {type='tool';data={name:'terminal',tid:'bench-'+toolIndex,
      args:{command:'printf result-'+toolIndex},preview:'running '+toolIndex};}
    else if (phase===2) {type='tool_complete';data={name:'terminal',tid:'bench-'+toolIndex,
      preview:result,snippet:result,duration:0.01,is_error:false};}
    else {type='token';data={text:'Fixture progress '+toolIndex+'. '};}
    current=samples.length;
    samples.push({events:i,type,renders:[],work:[]});
    const start=performance.now();
    source.emit(type,data,stream+':'+i);
    samples[current].work.push(performance.now()-start);
    // The production renderer throttles token/reasoning work to ~15 fps. Let
    // its actual queued rAF run while attribution still points at this event.
    await new Promise(r=>setTimeout(r,120));
    samples[current].elapsed_ms=performance.now()-start;
    const sample=samples[current];
    // rAF work already contains nested scene work; timer/microtask scene work
    // is separate. Take the larger attributed total, never double-count both.
    sample.work=[Math.max(sample.work.reduce((a,b)=>a+b,0),
      sample.renders.reduce((a,b)=>a+b,0))];
    current=null;
    if (checkpoints.includes(i)) {
      const accumulated=samples.flatMap(s=>s.renders);
      const accumulatedWork=samples.flatMap(s=>s.work);
      const now=performance.now(), windowStart=Math.max(0,now-5000);
      let busyMs=0;
      for (const task of window.__longtasks) {
        const end=task.start+task.duration;
        busyMs+=Math.max(0,Math.min(end,now)-Math.max(task.start,windowStart));
      }
      const span=Math.max(1,Math.min(5000,now-window.__longtaskStart));
      rows.push({events_so_far:i,
        median_per_render_ms:median(accumulated), render_count:accumulated.length,
        median_event_work_ms:median(accumulatedWork),
        p95_per_render_ms:(()=>{const a=accumulated.sort((x,y)=>x-y);
          return a.length?a[Math.min(a.length-1,Math.floor(a.length*.95))]:null;})(),
        dom_nodes:document.querySelectorAll('*').length,
        main_thread_busy_pct_last_5s:Math.min(100,busyMs*100/span),
        cumulative_by_event_type_ms:Object.fromEntries(
          ['reasoning','tool','tool_complete','token'].map(type=>[type,
            median(samples.filter(s=>s.type===type).flatMap(s=>s.work))])),
        latest_interval:summarize({events:i,
          renders:samples.slice(Math.max(0,(checkpoints[checkpoints.indexOf(i)-1]||0)))
            .flatMap(s=>s.renders)}),
        mode:chatActivityMode()});
    }
  }
  window._renderLiveAnchorActivitySceneForStream=original;
  window.requestAnimationFrame=originalRaf;
  return {mode:chatActivityMode(),rows,samples};
}"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_url(url: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process exited {proc.returncode}: {url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(url)


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def fixture(route, session, sid, workspace):
    requested = route.request.url.split("session_id=", 1)[-1].split("&", 1)[0]
    data = session if requested == sid else {
        "session_id": "idle", "messages": [], "message_count": 0,
        "tool_calls": [], "workspace": workspace,
    }
    route.fulfill(json={"session": data})


def fixed_session(session, sid, workspace):
    return lambda route: fixture(route, session, sid, workspace)


def wait_boot(page) -> None:
    deadline = time.monotonic() + 30
    while not page.evaluate("typeof loadSession==='function' && S._bootReady===true"):
        if time.monotonic() > deadline:
            raise TimeoutError("WebUI boot")
        page.wait_for_timeout(50)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--checkpoints", default="50,100,200,400,700,1000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 1 or args.events < 1:
        parser.error("--runs and --events must be positive")
    checkpoints = [int(value) for value in args.checkpoints.split(",")]
    if checkpoints[-1] != args.events or checkpoints != sorted(set(checkpoints)):
        parser.error("checkpoints must be unique, ascending, and end at --events")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error("install Playwright in the selected Python environment")
    results: list[dict] = []
    server: subprocess.Popen | None = None
    chrome: subprocess.Popen | None = None
    with tempfile.TemporaryDirectory(prefix="hermes-render-bench-") as temp:
        root = Path(temp)
        for subdir in ("home", "agent", "logs"):
            (root / subdir).mkdir()
        log = (root / "logs/server.log").open("w")
        env = {key: os.environ.get(key, "") for key in ("PATH", "SYSTEMROOT", "TMPDIR")}
        env.pop("PYTHONPATH", None); env.pop("PYTHONSAFEPATH", None)
        env.update(HOME=temp, HERMES_HOME=temp, HERMES_BASE_HOME=temp,
            HERMES_WEBUI_STATE_DIR=str(root / "state"), HERMES_CONFIG_PATH=str(root / "config.yaml"),
            HERMES_WEBUI_HOST="127.0.0.1", HERMES_WEBUI_PORT=str(free_port()),
            HERMES_WEBUI_SKIP_ONBOARDING="1", HERMES_WEBUI_AGENT_DIR=str(root / "agent"),
            HERMES_WEBUI_DEFAULT_WORKSPACE=temp, NO_PROXY="127.0.0.1,localhost")
        base = f"http://127.0.0.1:{env['HERMES_WEBUI_PORT']}"
        server = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_url(base + "/health", server)
            debug_port = free_port()
            chrome = subprocess.Popen([CHROME, "--headless=new",
                f"--remote-debugging-port={debug_port}", f"--user-data-dir={root / 'chrome'}",
                "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
                "--disable-background-networking", "--window-size=1440,900", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            wait_url(f"http://127.0.0.1:{debug_port}/json/version", chrome)
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
                context = browser.contexts[0]
                page = context.new_page()
                page.add_init_script(INIT)
                cdp = context.new_cdp_session(page)
                cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                page.goto(base, wait_until="load")
                wait_boot(page)
                for run in range(1, args.runs + 1):
                    if run > 1:
                        page.close()
                        page = context.new_page()
                        page.add_init_script(INIT)
                        cdp = context.new_cdp_session(page)
                        cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                        page.goto(base, wait_until="load")
                        wait_boot(page)

                    snapshot = {"stream_id": "render-bench-stream", "last_seq": 0,
                        "last_event_id": "", "messages": [], "tool_calls": [],
                        "last_assistant_text": "", "last_reasoning_text": "",
                        "anchor_activity_scene": {"version": "activity_scene_v1",
                            "identity": {"session_id": "render-bench-fixture",
                                "stream_id": "render-bench-stream", "run_id": "render-bench-stream"},
                            "activity_rows": []}}
                    session = {"session_id": "render-bench-fixture", "title": "Render benchmark",
                        "workspace": temp, "messages": [], "message_count": 0, "tool_calls": [],
                        "active_stream_id": "render-bench-stream", "pending_user_message": "Fixture",
                        "pending_started_at": time.time(), "runtime_journal_snapshot": snapshot}
                    page.route("**/api/session?*", fixed_session(session,
                        "render-bench-fixture", temp))
                    page.route("**/api/chat/stream/status?*", lambda route:
                        route.fulfill(json={"active": True}))
                    details = page.evaluate(RUN, {"events": args.events, "checkpoints": checkpoints})
                    result = {"run": run, "mode": details["mode"], "rows": details["rows"]}
                    results.append(result if not args.output else {**result, "samples": details["samples"]})
                    print(f"\nRun {run}: activity_mode={result['mode']}")
                    print("events  render_ms  event_ms  p95_ms  DOM_nodes  longtask_busy_5s  renders")
                    for row in result["rows"]:
                        print(f"{row['events_so_far']:6}  {row['median_per_render_ms']:9.3f}  "
                            f"{row['median_event_work_ms']:8.3f}  {row['p95_per_render_ms']:6.3f}  {row['dom_nodes']:9}  "
                            f"{row['main_thread_busy_pct_last_5s']:15.2f}  {row['render_count']:7}")
                    print("  full event work by type (cumulative median ms): " + ", ".join(
                        f"{key}={value:.3f}" if value is not None else f"{key}=n/a"
                        for key, value in result["rows"][-1]["cumulative_by_event_type_ms"].items()))
                    if run == 1:
                        medians = [row["median_per_render_ms"] for row in result["rows"]]
                        statistics.median(medians)  # validate finite numeric result shape
                browser.close()
        finally:
            stop(server); stop(chrome)
            log.close()
    if args.output:
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nRaw samples and summaries: {args.output}")
    print("Headless timings measure main-thread JS/layout work, not painted-frame latency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
