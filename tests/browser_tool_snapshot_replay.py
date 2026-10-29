"""Synthetic Chromium frozen-tab SSE replay at the live HTML snapshot seam.

Usage: python tests/browser_tool_snapshot_replay.py --output-dir <private-dir>
Optional --baseline <git-ref> compares the same harness against that revision.
No Agent/backend/user data is used. Scene painting and persistence are stubbed;
real EventSource delivery, lifecycle freeze/resume and outerHTML are measured.
"""

import argparse
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from test_tool_snapshot_coalescing import ROOT, _harness


def browser_script(messages):
    script = _harness(messages)
    start = script.index("const turn={")
    end = script.index("let observed=[];", start)
    script = script[:start] + """
const turn=document.getElementById('liveAssistantTurn');
const nativeHTML=Object.getOwnPropertyDescriptor(Element.prototype,'outerHTML');
let serializedBytes=0, snapshotMs=0;
Object.defineProperty(turn,'outerHTML',{get(){
  const start=performance.now();
  const value=nativeHTML.get.call(this);
  snapshotMs+=performance.now()-start;snapshots++;serializedBytes+=value.length;
  return value;
}});
function $(id){return document.getElementById(id);}
""" + script[end:]
    script = script.replace(
        "observed.push([d.tid,phase]);return {...d};",
        "observed.push([d.tid,phase]);document.getElementById('progress').textContent=observed.length;return {...d};",
    )
    start = script.index("const source={listeners:")
    end = script.index("function snapshotLiveTurnHtmlForSession", start)
    script = script[:start] + "const source=new EventSource('/events');\n" + script[end:]
    script += """
let firstEventAt=null, lastEventAt=null;
for(const name of ['tool','tool_complete']) source.addEventListener(name,()=>{
  if(firstEventAt===null) firstEventAt=performance.now();
  lastEventAt=performance.now();
});
window.replayResult=()=>({events:observed.length,snapshots,serializedBytes,snapshotMs,
  drainMs:lastEventAt-firstEventAt,saved:!!INFLIGHT[activeSid].liveTurnHtml,
  latest:!!INFLIGHT[activeSid].liveTurnHtml?.includes('id="progress">1000<'),
  nodes:turn.querySelectorAll('*').length});
"""
    return script


def run_lane(browser, messages, width, output, name):
    ready, release, sent, close = (threading.Event() for _ in range(4))
    body = (
        '<!doctype html><meta charset="utf-8"><title>Synthetic snapshot replay</title>'
        '<style>body{font:14px sans-serif;background:#111;color:#eee;margin:20px}'
        '.tool{padding:8px;border-bottom:1px solid #444}pre{white-space:pre-wrap}</style>'
        '<h2>Synthetic tool replay — snapshot-only probe</h2>'
        '<div id="liveAssistantTurn" data-session-id="session-a">'
        '<p>Delivered events: <span id="progress">0</span></p>'
        + ''.join(f'<div class="tool">Tool {i}<pre>{"x" * 2048}</pre></div>' for i in range(1200))
        + '</div>'
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            if self.path == '/events':
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                ready.set()
                if not release.wait(15):
                    return
                try:
                    for i in range(500):
                        for event in ('tool', 'tool_complete'):
                            payload = json.dumps({'name': 'terminal', 'tid': f'tool-{i}'})
                            self.wfile.write(f'event: {event}\ndata: {payload}\n\n'.encode())
                    self.wfile.flush()
                    sent.set()
                    close.wait(30)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(body.encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = browser.new_page(viewport={'width': width, 'height': 800})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(f'http://127.0.0.1:{server.server_port}/')
        page.add_script_tag(content=browser_script(messages))
        assert ready.wait(5), 'EventSource did not connect'
        cdp = page.context.new_cdp_session(page)
        cdp.send('Page.setWebLifecycleState', {'state': 'frozen'})
        release.set()
        assert sent.wait(5), 'server did not send frozen-tab backlog'
        cdp.send('Page.setWebLifecycleState', {'state': 'active'})
        page.wait_for_function('window.replayResult().events===1000 && window.replayResult().latest', timeout=30000)
        result = page.evaluate('window.replayResult()')
        assert not errors, errors
        assert result['latest'], result
        assert result['nodes'] == 2402, result
        page.screenshot(path=str(output / f'{name}-{width}.png'))
        return result
    finally:
        close.set()
        page.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    sources = {'candidate': (ROOT / 'static/messages.js').read_text()}
    if args.baseline:
        sources['baseline'] = subprocess.run(
            ['git', 'show', f'{args.baseline}:static/messages.js'], cwd=ROOT,
            text=True, capture_output=True, check=True,
        ).stdout
    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium")
        try:
            results['browser'] = browser.version
            for name, source in sources.items():
                for width in (1280, 390):
                    result = run_lane(browser, source, width, args.output_dir, name)
                    results[f'{name}-{width}'] = result
                    print(name, width, json.dumps(result), flush=True)
                    if name == 'candidate':
                        assert result['snapshots'] <= 3, result
                    else:
                        assert result['snapshots'] == 1000, result
        finally:
            browser.close()
    (args.output_dir / 'metrics.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
