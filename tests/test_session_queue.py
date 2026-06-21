import io
import json
import pathlib
import subprocess
import sys
import time
import types

from api import config
from api import routes
from api import session_queue


def _wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _install_fake_routes(monkeypatch, start_session_turn):
    fake_routes = types.SimpleNamespace(start_session_turn=start_session_turn)
    monkeypatch.setitem(sys.modules, "api.routes", fake_routes)


def _is_empty_queue_dir(path):
    qdir = path / "_session_queue"
    return not qdir.exists() or not any(qdir.iterdir())


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _run_queue_sync_node_script(script_body):
    ui_src = (pathlib.Path(__file__).parent.parent / "static" / "ui.js").read_text(
        encoding="utf-8"
    )
    start = ui_src.index("function _getSessionQueue")
    end = ui_src.index("function _compressionSessionLock", start)
    queue_src = ui_src[start:end]
    script = f"""
const vm = require('vm');
const storage = {{}};
const store = {{
  getItem: (key) => Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null,
  setItem: (key, value) => {{ storage[key] = String(value); }},
  removeItem: (key) => {{ delete storage[key]; }},
}};
const ctx = {{
  SESSION_QUEUES: {{}},
  S: {{activeProfile: 'default'}},
  _queueRenderKeys: {{}},
  sessionStorage: store,
  localStorage: store,
  document: {{baseURI: 'http://example.test/session/sid/'}},
  location: {{href: 'http://example.test/session/sid/', pathname: '/session/sid/', search: ''}},
  fetch: null,
  updateQueueBadge: () => {{}},
  File: function File(){{}},
  URL,
  setTimeout,
  clearTimeout,
}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext({json.dumps(queue_src)}, ctx, {{filename: 'ui-queue.js'}});
(async () => {{
  await vm.runInContext(`(async () => {{
{script_body}
  }})()`, ctx, {{filename: 'ui-queue-test.js'}});
}})().catch(err => {{
  console.error(err && err.stack || err);
  process.exit(1);
}});
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_enqueue_persists_and_lists_session_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)

    item = session_queue.enqueue(
        "sid-1",
        {"text": "next please", "model": "m1", "model_provider": "p1", "profile": "default"},
    )

    assert item["id"]
    assert item["text"] == "next please"
    assert session_queue.list_queue("sid-1") == [item]


def test_enqueue_handler_attempts_idle_drain(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_session", lambda sid: {"id": sid})
    drained = []

    def fake_drain_for_session(sid):
        drained.append(sid)
        return 1

    monkeypatch.setattr(session_queue, "drain_for_session", fake_drain_for_session)

    handler = _FakeHandler()
    routes._handle_session_queue_enqueue(
        handler,
        {"session_id": "sid-idle", "text": "queued while idle", "profile": "default"},
    )

    assert handler.status == 200
    assert drained == ["sid-idle"]
    body = handler.json_body()
    assert body["ok"] is True
    assert body["item"]["text"] == "queued while idle"


def test_frontend_sync_recovers_stale_pending_and_matches_trimmed_text():
    _run_queue_sync_node_script(
        r"""
const sid = 'sid-sync';
SESSION_QUEUES[sid] = [
  {text: '  hello from pending  ', _server_pending: true, _client_queue_id: 'local-1'},
  {text: 'orphan after tab close', _server_pending: true, _client_queue_id: 'local-2'},
];
fetch = async () => ({ok: true, json: async () => ({items: [
  {id: 'srv-1', text: 'hello from pending', attachments: [], model: 'm1', model_provider: 'p1', profile: 'default', created_at: 1700000000},
]})});
syncBackendSessionQueue(sid);
await new Promise(resolve => setTimeout(resolve, 0));
const q = SESSION_QUEUES[sid];
if(q.length !== 2) throw new Error('expected no duplicate server chip, got '+q.length);
if(q[0]._server_queue_id !== 'srv-1' || !q[0]._server_owned || q[0]._server_pending){
  throw new Error('trimmed pending entry was not promoted: '+JSON.stringify(q[0]));
}
if(q[1]._server_pending || q[1]._server_owned || q[1]._server_queue_id){
  throw new Error('orphan pending entry was not reset: '+JSON.stringify(q[1]));
}
const shifted = shiftQueuedSessionMessage(sid);
if(!shifted || shifted.text !== 'orphan after tab close'){
  throw new Error('reset orphan should be browser-drainable: '+JSON.stringify(shifted));
}
"""
    )


def test_drain_for_session_starts_one_backend_owned_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})

    item = session_queue.enqueue(
        "sid-drain",
        {"text": "queued followup", "model": "m-drain", "model_provider": "p-drain"},
    )
    calls = []

    def fake_start_session_turn(session_id, message, **kwargs):
        calls.append((session_id, message, kwargs))
        return {"stream_id": "stream-1", "_status": 200}

    _install_fake_routes(monkeypatch, fake_start_session_turn)

    assert session_queue.drain_for_session("sid-drain") == 1
    assert _wait_until(lambda: calls)
    assert calls == [
        (
            "sid-drain",
            "queued followup",
            {
                "source": "queued_followup",
                "attachments": [],
                "requested_model": "m-drain",
                "requested_provider": "p-drain",
                "queue_item_id": item["id"],
            },
        )
    ]
    assert session_queue.list_queue("sid-drain") == []
    assert _is_empty_queue_dir(tmp_path)


def test_drain_requeues_item_when_start_races_active_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})

    item = session_queue.enqueue("sid-race", {"text": "still needed"})

    def fake_start_session_turn(session_id, message, **kwargs):
        return {"error": "session already has an active stream", "_status": 409}

    _install_fake_routes(monkeypatch, fake_start_session_turn)

    assert session_queue.drain_for_session("sid-race") == 1
    assert _wait_until(lambda: session_queue.list_queue("sid-race"))
    queued = session_queue.list_queue("sid-race")
    assert len(queued) == 1
    assert queued[0]["id"] == item["id"]
    assert queued[0]["text"] == "still needed"


def test_drain_does_not_claim_while_session_has_active_run(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(
        config,
        "ACTIVE_RUNS",
        {"stream-active": {"session_id": "sid-active"}},
    )

    item = session_queue.enqueue("sid-active", {"text": "later"})

    assert session_queue.drain_for_session("sid-active") == 0
    assert session_queue.list_queue("sid-active")[0]["id"] == item["id"]
