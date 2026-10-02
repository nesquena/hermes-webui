"""Exercise real lazy loaders across a session switch, including fallbacks."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


UI = (Path(__file__).resolve().parents[1] / "static/ui.js").read_text(encoding="utf-8")
SOURCE = (
    UI[UI.index("const CSV_MAX_SIZE="):UI.index("let _excalidrawScriptLoaded=")]
    + UI[UI.index("let _pdfjsReady="):UI.index("function renderMermaidBlocks(")]
)
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("session_id", ["session-a", ""])
@pytest.mark.parametrize("kind,mode", [
    ("Csv", "ok"), ("Csv", "error"), ("Csv", "invalid"), ("Csv", "large"),
    ("Excalidraw", "ok"),
    ("Html", "ok"), ("Html", "error"), ("Html", "large"),
    ("Pdf", "ok"), ("Pdf", "error"), ("Pdf", "large"),
    ("Pdf", "timeout"), ("Pdf", "delayed"),
])
def test_lazy_action_links_retain_request_session_and_snapshot(kind, mode, session_id):
    driver = r"""
const vm = require('vm');
const [source, kind, mode, sid] = process.argv.slice(1);
const requests = [], rendered = [], timers = [], listeners = {};
let resolveFetch;
const snap = 'a'.repeat(64);
const path = '/outside/a file.' + kind.toLowerCase();
function node() {
  return {
    dataset: {path, snap}, parentNode: {},
    setAttribute() {}, appendChild() {}, getContext() { return {}; },
    querySelector() { return node(); },
    replaceWith() {},
    set outerHTML(v) { rendered.push(v); },
    set innerHTML(v) { rendered.push(v); },
  };
}
const el = node();
const root = {querySelectorAll: () => [el]};
const context = {
  S: {session: {session_id: sid}},
  document: {createElement: node, head: {appendChild() {}}},
  window: {
    _pdfjsLib: {getDocument: () => ({promise: Promise.resolve({numPages: 1,
      getPage: async () => ({getViewport: () => ({width: 1, height: 1}),
        render: () => ({promise: Promise.resolve()})})})})},
    addEventListener: (name, fn) => { listeners[name] = fn; },
  },
  root,
  Blob: class {},
  URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}},
  requestAnimationFrame() {},
  setTimeout: fn => { timers.push(fn); },
  esc: v => String(v).replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
  t: v => v,
  fetch: url => {
    requests.push(url);
    return new Promise(resolve => { resolveFetch = resolve; });
  },
};
vm.createContext(context);
vm.runInContext(source, context);
if (mode !== 'timeout' && mode !== 'delayed') vm.runInContext('_pdfjsReady=true;', context);
vm.runInContext('load' + kind + 'Inline(root);', context);
context.S.session = {session_id: 'session-b'};
if (mode === 'delayed') listeners['pdfjs-ready']();
if (mode === 'timeout') timers.forEach(fn => fn());
else {
  let text = kind === 'Csv' ? 'name,value\nalpha,1' :
    kind === 'Excalidraw' ? '{"type":"excalidraw","elements":[]}' : '<p>hello</p>';
  if (mode === 'invalid') text = '';
  if (mode === 'large') text = 'x'.repeat(512 * 1024);
  resolveFetch({ok: mode !== 'error', status: mode === 'error' ? 500 : 200,
    text: async () => text,
    arrayBuffer: async () => ({byteLength: mode === 'large' ? 5 * 1024 * 1024 : 1}),
  });
}
(async () => {
  for (let i=0; i<5; i++) await new Promise(resolve => setImmediate(resolve));
  const links = rendered.flatMap(html => [...html.matchAll(/href="([^"]+)"/g)]
    .map(m => m[1].replaceAll('&amp;', '&')));
  process.stdout.write(JSON.stringify({requests, links, path, snap}));
})().catch(error => { console.error(error); process.exitCode=1; });
"""
    run = subprocess.run(
        [NODE, "-e", driver, SOURCE, kind, mode, session_id],
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["links"], "loader must expose an action/fallback link"
    from urllib.parse import parse_qs, urlparse

    for url in result["requests"] + result["links"]:
        query = parse_qs(urlparse(url).query)
        assert query.get("session_id", [""]) == [session_id], (kind, mode, url)
        assert query["path"] == [result["path"]]
        assert query["snap"] == [result["snap"]]
    for url in result["links"]:
        query = parse_qs(urlparse(url).query)
        expected_action = "inline" if kind == "Html" and mode != "error" else "download"
        assert query[expected_action] == ["1"]
