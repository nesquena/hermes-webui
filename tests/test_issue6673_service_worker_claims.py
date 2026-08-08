"""Service-worker-owned notification identity claims for #6673."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _run_node(script: str) -> dict:
    if NODE is None:
        pytest.skip("node executable is required for service-worker claim checks")
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".js",
        encoding="utf-8",
        delete=False,
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, (
        f"service-worker claim driver failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_worker_claims_identity_once_and_keeps_failed_claim_owned():
    script = f"""
const vm = require('vm');
const listeners = {{}};
const databases = new Map();
const showAttempts = [];
let rejectNextShow = false;
let failOpen = false;
let failTransaction = false;

class Port {{
  constructor() {{ this.peer = null; this.onmessage = null; }}
  postMessage(data) {{
    const peer = this.peer;
    queueMicrotask(() => {{
      if (peer && typeof peer.onmessage === 'function') peer.onmessage({{data}});
    }});
  }}
  close() {{}}
  start() {{}}
}}
class MessageChannel {{
  constructor() {{
    this.port1 = new Port();
    this.port2 = new Port();
    this.port1.peer = this.port2;
    this.port2.peer = this.port1;
  }}
}}

function makeDatabase() {{
  const records = new Map();
  return {{
    objectStoreNames: {{contains: () => records.has('__store__')}},
    createObjectStore() {{ records.set('__store__', true); return {{}}; }},
    transaction() {{
      const tx = {{oncomplete: null, onerror: null, onabort: null, error: null}};
      if (failTransaction) {{
        tx.objectStore = () => ({{
          add() {{
            const request = {{onsuccess: null, onerror: null, error: null}};
            queueMicrotask(() => {{
              tx.error = {{name: 'QuotaExceededError'}};
              tx.onerror?.({{target: tx, preventDefault: () => {{}}}});
              tx.onabort?.({{target: tx}});
            }});
            return request;
          }},
        }});
        return tx;
      }}
          tx.objectStore = () => ({{
                add(value) {{
                  const request = {{onsuccess: null, onerror: null, error: null}};
              queueMicrotask(() => {{
                const key = JSON.stringify([value.streamId, value.lastEventId]);
                if (records.has(key)) {{
                  request.error = {{name: 'ConstraintError'}};
                  let prevented = false;
                  request.onerror?.({{target: request, preventDefault: () => {{prevented = true;}}}});
                  tx.error = request.error;
                  tx.onerror?.({{target: tx, preventDefault: () => {{}}}});
                  if (prevented) queueMicrotask(() => tx.oncomplete?.({{}}));
                  else tx.onabort?.({{}});
                  return;
                }}
                records.set(key, value);
                request.onsuccess?.({{target: request}});
                queueMicrotask(() => tx.oncomplete?.({{}}));
              }});
              return request;
                }},
              }});
          return tx;
    }},
    close() {{}},
  }};
}}

const indexedDB = {{
    open(name) {{
        const request = {{onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null, error: null}};
    let db = databases.get(name);
        const isNew = !db;
        if (!db) {{ db = makeDatabase(); databases.set(name, db); }}
    request.result = db;
    queueMicrotask(() => {{
      if (failOpen) {{
        request.error = {{name: 'UnknownError'}};
        request.onerror?.({{target: request}});
        return;
      }}
      if (isNew) request.onupgradeneeded?.({{target: {{result: db}}}});
      request.onsuccess?.({{target: {{result: db}}}});
    }});
    return request;
  }},
}};

const self = {{
  location: {{origin: 'https://webui.test'}},
  registration: {{
    scope: 'https://webui.test/',
    showNotification(title, options) {{
      showAttempts.push({{title, options}});
      if (rejectNextShow) {{ rejectNextShow = false; return Promise.reject(new Error('native reject')); }}
      return Promise.resolve();
    }},
  }},
  clients: {{matchAll: () => Promise.resolve([]), openWindow: () => Promise.resolve()}},
  addEventListener(name, handler) {{ listeners[name] = handler; }},
  skipWaiting() {{}},
}};
const caches = {{keys: () => Promise.resolve([]), open: () => Promise.resolve({{addAll: () => Promise.resolve()}})}};
const workerSource = {json.dumps(SW_JS)};
const workerGlobals = {{
  self, caches, indexedDB, URL, Set, Promise, Map, queueMicrotask, MessageChannel,
  console,
}};
function loadWorker() {{
  vm.runInNewContext(workerSource, {{...workerGlobals}});
}}
loadWorker();

function send(data, sourceId = 'client-a', sourceUrl = 'https://webui.test/') {{
  return new Promise(resolve => {{
    const timeout = setTimeout(() => resolve({{status: 'timeout'}}), 1000);
    const channel = new MessageChannel();
    const replies = [];
    channel.port1.onmessage = event => {{ replies.push(event.data); }};
    const waits = [];
    const event = {{
      data,
      ports: [channel.port2],
      source: {{id: sourceId, url: sourceUrl}},
      waitUntil: promise => waits.push(Promise.resolve(promise)),
    }};
    listeners.message(event);
    Promise.all(waits).then(() => queueMicrotask(() => {{ clearTimeout(timeout); resolve(replies[0]); }}));
  }});
}}

const options = {{
  body: 'The task finished.',
  tag: 'hermes-session-6673',
  renotify: true,
  icon: 'static/favicon-192.png',
  badge: 'static/favicon-32.png',
  data: {{url: 'https://webui.test/?session=session-6673'}},
}};
const base = {{
  type: 'hermes.notification.claim',
  title: 'Response complete',
  options,
  identity: {{streamId: 'stream-6673', lastEventId: 'stream-6673:1'}},
}};
(async () => {{
  const same = await Promise.all([send(base, 'client-a'), send(base, 'client-b')]);
  const next = await send({{...base, identity: {{...base.identity, lastEventId: 'stream-6673:2'}}}});
  rejectNextShow = true;
  const rejected = await send({{...base, identity: {{...base.identity, lastEventId: 'stream-6673:3'}}}});
  const rejectedDuplicate = await send({{...base, identity: {{...base.identity, lastEventId: 'stream-6673:3'}}}}, 'client-b');
  const invalid = await send({{...base, identity: {{streamId: 'stream-6673', lastEventId: ''}}}});
  const malformed = await send({{...base, identity: null}});
  const nonString = await send({{...base, identity: {{streamId: 42, lastEventId: 'stream-6673:4'}}}});
  const oversizedStream = await send({{...base, identity: {{streamId: 'x'.repeat(513), lastEventId: 'stream-6673:5'}}}});
  const oversizedEvent = await send({{...base, identity: {{streamId: 'stream-6673', lastEventId: 'x'.repeat(513)}}}});
  loadWorker();
  const replayAfterWorkerRestart = await send(base, 'client-b');
  const crossOriginUrl = await send({{...base, identity: {{...base.identity, lastEventId: 'stream-6673:4'}}, options: {{...options, data: {{url: 'https://evil.test/'}}}}}});
  self.registration.scope = 'https://webui.test/app/';
  const outOfScopeUrl = await send({{...base, identity: {{...base.identity, lastEventId: 'stream-6673:5'}}, options: {{...options, data: {{url: 'https://webui.test/outside'}}}}}}, 'client-a', 'https://webui.test/app/');
  self.registration.scope = 'https://webui.test/';
  failOpen = true;
  const openFailure = await send({{...base, identity: {{...base.identity, lastEventId: 'stream-6673:6'}}}});
  failOpen = false;
  failTransaction = true;
  const transactionFailure = await send({{...base, identity: {{...base.identity, lastEventId: 'stream-6673:7'}}}});
  failTransaction = false;
  console.log(JSON.stringify({{
    same,
    next,
    rejected,
    rejectedDuplicate,
    invalid,
    malformed,
    nonString,
    oversizedStream,
    oversizedEvent,
    replayAfterWorkerRestart,
    crossOriginUrl,
    outOfScopeUrl,
    openFailure,
    transactionFailure,
    showAttempts: showAttempts.length,
        tags: showAttempts.map(item => item.options.tag),
  }}));
}})().catch(error => {{
  console.error(error.stack || error);
  process.exitCode = 1;
}});
    """
    result = _run_node(script)

    assert sorted(item["status"] for item in result["same"]) == ["duplicate", "shown"], result
    assert result["next"]["status"] == "shown"
    assert result["rejected"]["status"] == "fallback-owner"
    assert result["rejectedDuplicate"]["status"] == "duplicate"
    assert result["invalid"]["status"] == "invalid"
    assert [
        result["malformed"]["status"],
        result["nonString"]["status"],
        result["oversizedStream"]["status"],
        result["oversizedEvent"]["status"],
    ] == ["invalid"] * 4
    assert result["replayAfterWorkerRestart"]["status"] == "duplicate"
    assert result["crossOriginUrl"]["status"] == "invalid"
    assert result["outOfScopeUrl"]["status"] == "invalid"
    assert result["openFailure"]["status"] == "ambiguous"
    assert result["transactionFailure"]["status"] == "ambiguous"
    assert result["showAttempts"] == 3
    assert result["tags"] == ["hermes-session-6673"] * 3
