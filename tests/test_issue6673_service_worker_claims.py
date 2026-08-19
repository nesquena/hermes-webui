"""Service-worker-owned displayed-record presentation for #6673."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _run_node(script: str) -> dict:
    if NODE is None:
        raise AssertionError("node executable is required for service-worker presentation checks")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run([NODE, str(script_path)], cwd=ROOT, text=True,
                                capture_output=True, timeout=10, check=False)
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, f"worker driver failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout)


def test_worker_suppresses_only_currently_displayed_exact_event_and_retries_failures():
    script = f"""
const vm = require('vm');
const listeners = {{}};
const displayed = [];
const attempts = [];
let rejectNext = false;
class Port {{ constructor() {{ this.peer = null; this.onmessage = null; }} postMessage(data) {{ queueMicrotask(() => this.peer?.onmessage?.({{data}})); }} close() {{}} start() {{}} }}
class MessageChannel {{ constructor() {{ this.port1 = new Port(); this.port2 = new Port(); this.port1.peer = this.port2; this.port2.peer = this.port1; }} }}
const self = {{
  location: {{origin: 'https://webui.test'}},
  registration: {{
    scope: 'https://webui.test/',
    getNotifications: async () => displayed.filter(item => item.options.tag === 'hermes-session-6673'),
    showNotification: async (title, options) => {{
      attempts.push({{title, options}});
      if (rejectNext) {{ rejectNext = false; throw new Error('native reject'); }}
      displayed.push({{data: options.data, options}});
    }},
  }},
  clients: {{matchAll: () => Promise.resolve([]), openWindow: () => Promise.resolve()}},
  addEventListener(name, handler) {{ listeners[name] = handler; }},
  skipWaiting() {{}},
}};
const caches = {{keys: () => Promise.resolve([]), open: () => Promise.resolve({{addAll: () => Promise.resolve()}})}};
vm.runInNewContext({json.dumps(SW_JS)}, {{self, caches, URL, Set, Map, Promise, queueMicrotask, MessageChannel, console}});
function send(eventId) {{ return new Promise(resolve => {{ const c = new MessageChannel(); c.port1.onmessage = e => resolve(e.data); listeners.message({{data: {{type:'hermes.notification.present', protocolVersion:1, eventId, title:'Done', options: {{body:'body', tag:'hermes-session-6673', renotify:true, icon:'static/favicon-192.png', badge:'static/favicon-32.png', data: {{url:'https://webui.test/?session=s'}}}}}}, ports:[c.port2], source:{{id:'client',url:'https://webui.test/'}}, waitUntil:p=>p}}); }}); }}
(async () => {{
  const same = await Promise.all([send('stream-6673:1'), send('stream-6673:1')]);
  const distinct = await send('stream-6673:2');
  rejectNext = true;
  const rejected = await send('stream-6673:3');
  const retry = await send('stream-6673:3');
  const opaque = await send('opaque-event-4');
  console.log(JSON.stringify({{same, distinct, rejected, retry, opaque, attempts: attempts.length, ids: displayed.map(item => item.data.eventId)}}));
}})().catch(error => {{ console.error(error.stack || error); process.exitCode = 1; }});
"""
    result = _run_node(script)
    assert sorted(item["status"] for item in result["same"]) == ["duplicate", "shown"]
    assert result["distinct"]["status"] == "shown"
    assert result["rejected"]["status"] == "unavailable"
    assert result["retry"]["status"] == "shown"
    assert result["opaque"]["status"] == "shown"
    assert result["attempts"] == 5
    assert result["ids"] == ["stream-6673:1", "stream-6673:2", "stream-6673:3", "opaque-event-4"]


def test_worker_has_no_notification_claim_database():
    assert "indexedDB.open(" not in SW_JS
    assert "hermes.notification.present" in SW_JS
    assert "getNotifications({tag})" in SW_JS
