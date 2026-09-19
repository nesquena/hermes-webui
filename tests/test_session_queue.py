import io
import json
import pathlib
import subprocess
import sys
import time
import types
from urllib.parse import urlparse

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


def test_queue_steer_capability_route_is_read_only_and_cache_safe():
    handler = _FakeHandler()

    handled = routes.handle_get(
        handler, urlparse("http://example.test/api/session/queue/steer-capability")
    )

    assert handled is None
    assert handler.status == 200
    assert handler.headers["Cache-Control"] == "no-store"
    assert handler.json_body() == {
        "ok": True,
        "queue_item_steer": True,
        "protocol": 1,
    }


def test_queue_steer_capability_fails_closed_against_old_backend():
    _run_queue_sync_node_script(
        r"""
fetch = async () => ({ok: false, status: 404, json: async () => ({error: 'not found'})});
if(await _ensureQueueSteerCapability() !== false || _queueSteerCapability !== false){
  throw new Error('old backend must leave queued steer disabled');
}
_queueSteerCapability = null;
fetch = async () => ({ok: true, status: 200, json: async () => ({queue_item_steer: true, protocol: 1})});
if(await _ensureQueueSteerCapability() !== true || _queueSteerCapability !== true){
  throw new Error('new backend capability handshake was not accepted');
}
"""
    )


def test_enqueue_persists_and_lists_session_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)

    item = session_queue.enqueue(
        "sid-1",
        {"text": "next please", "model": "m1", "model_provider": "p1", "profile": "default"},
    )

    assert item["id"]
    assert item["text"] == "next please"
    assert session_queue.list_queue("sid-1") == [item]


def test_enqueue_requires_text_even_with_attachments(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)

    try:
        session_queue.enqueue("sid-empty", {"attachments": [{"name": "file.txt"}]})
    except ValueError as exc:
        assert str(exc) == "text is required"
    else:  # pragma: no cover - defensive clarity for the regression
        raise AssertionError("attachments-only backend queue item should be rejected")


def test_enqueue_coerces_provider_and_caps_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(session_queue, "_MAX_QUEUE_ITEMS", 3)

    for idx in range(5):
        session_queue.enqueue(
            "sid-cap",
            {"text": f"item {idx}", "model_provider": {"provider": idx}},
        )

    queued = session_queue.list_queue("sid-cap")
    assert [item["text"] for item in queued] == ["item 2", "item 3", "item 4"]
    assert queued[-1]["model_provider"] == "{'provider': 4}"


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


def test_frontend_sync_hydrates_persisted_queue_before_reconcile():
    _run_queue_sync_node_script(
        r"""
const sid = 'sid-hydrate';
sessionStorage.setItem('hermes-queue-'+sid, JSON.stringify([
  {text: ' persisted pending ', _server_pending: true, _client_queue_id: 'persisted-1'},
]));
fetch = async () => ({ok: true, json: async () => ({items: [
  {id: 'srv-hydrated', text: 'persisted pending', attachments: [], created_at: 1700000000},
]})});
syncBackendSessionQueue(sid);
await new Promise(resolve => setTimeout(resolve, 0));
const q = SESSION_QUEUES[sid];
if(!q || q.length !== 1 || q[0]._server_queue_id !== 'srv-hydrated' || q[0]._server_pending){
  throw new Error('persisted pending entry was not hydrated and reconciled: '+JSON.stringify(q));
}
"""
    )


def test_frontend_ack_deletes_backend_item_when_local_chip_was_removed():
    _run_queue_sync_node_script(
        r"""
const sid = 'sid-ghost';
const entry = {text: 'ghost followup', _client_queue_id: 'local-ghost'};
const calls = [];
fetch = async (url, opts) => {
  calls.push({url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null});
  if(String(url).includes('/delete')) return {ok: true, json: async () => ({ok: true})};
  return {ok: true, json: async () => ({item: {id: 'srv-ghost'}})};
};
_backendAcknowledgeQueuedMessage(sid, entry);
await new Promise(resolve => setTimeout(resolve, 0));
await new Promise(resolve => setTimeout(resolve, 0));
if(calls.length !== 2 || !String(calls[1].url).includes('api/session/queue/delete')){
  throw new Error('expected cleanup delete after missing local chip: '+JSON.stringify(calls));
}
if(calls[1].body.id !== 'srv-ghost' || calls[1].body.session_id !== sid){
  throw new Error('delete payload mismatch: '+JSON.stringify(calls[1]));
}
"""
    )


def test_frontend_queue_upgrade_to_steer_removes_only_accepted_entry():
    _run_queue_sync_node_script(
        r"""
const sid = 'sid-steer';
SESSION_QUEUES[sid] = [
  {text: 'keep queued', _queued_at: 1, _server_owned: true, _server_queue_id: 'srv-keep'},
  {text: 'steer this', _queued_at: 2, _server_owned: true, _server_queue_id: 'srv-steer'},
];
S.session = {session_id: sid, active_stream_id: 'stream-1'};
S.activeStreamId = 'stream-1';
S.busy = true;
const calls = [];
fetch = async (url, opts) => {
  calls.push({url: String(url), body: JSON.parse(opts.body)});
  return {ok: true, json: async () => ({accepted: true, fallback: null, stream_id: 'stream-1'})};
};
const accepted = await _steerQueuedEntry(sid, 2);
if(!accepted) throw new Error('expected queued steer to be accepted');
if(calls.length !== 1 || !calls[0].url.includes('api/chat/steer')){
  throw new Error('wrong steer request: '+JSON.stringify(calls));
}
if(calls[0].body.queue_item_id !== 'srv-steer' || calls[0].body.text !== 'steer this'){
  throw new Error('steer payload did not identify the selected queue item: '+JSON.stringify(calls[0].body));
}
const q = SESSION_QUEUES[sid];
if(q.length !== 1 || q[0]._server_queue_id !== 'srv-keep'){
  throw new Error('accepted steer removed the wrong queue entry: '+JSON.stringify(q));
}
"""
    )


def test_frontend_queue_upgrade_to_steer_keeps_entry_on_rejection():
    _run_queue_sync_node_script(
        r"""
const sid = 'sid-steer-reject';
SESSION_QUEUES[sid] = [
  {text: 'still queued', _queued_at: 7, _server_owned: true, _server_queue_id: 'srv-reject'},
];
S.session = {session_id: sid, active_stream_id: 'stream-2'};
S.activeStreamId = 'stream-2';
S.busy = true;
fetch = async () => ({ok: true, json: async () => ({accepted: false, fallback: 'stream_dead'})});
const accepted = await _steerQueuedEntry(sid, 7);
if(accepted) throw new Error('rejected steer must return false');
const q = SESSION_QUEUES[sid];
if(q.length !== 1 || q[0]._server_queue_id !== 'srv-reject'){
  throw new Error('rejected steer lost the queued entry: '+JSON.stringify(q));
}
"""
    )


def test_queue_claim_by_id_preserves_other_items_and_can_be_restored(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    first = session_queue.enqueue("sid-claim", {"text": "first"})
    second = session_queue.enqueue("sid-claim", {"text": "second"})

    claimed = session_queue.claim_item("sid-claim", second["id"])

    assert claimed is not None
    assert claimed["item"]["id"] == second["id"]
    assert claimed["index"] == 1
    assert [item["id"] for item in session_queue.list_queue("sid-claim")] == [first["id"]]

    session_queue.restore_claim("sid-claim", claimed)
    assert [item["id"] for item in session_queue.list_queue("sid-claim")] == [
        first["id"],
        second["id"],
    ]


def test_queue_claim_by_id_is_atomic_under_concurrency(monkeypatch, tmp_path):
    import threading

    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    item = session_queue.enqueue("sid-concurrent-claim", {"text": "claim once"})
    barrier = threading.Barrier(3)
    claims = []

    def claim_selected():
        barrier.wait()
        claims.append(session_queue.claim_item("sid-concurrent-claim", item["id"]))

    workers = [threading.Thread(target=claim_selected) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert sum(claim is not None for claim in claims) == 1
    assert session_queue.list_queue("sid-concurrent-claim") == []


def test_existing_session_load_syncs_backend_queue():
    src = (pathlib.Path(__file__).parent.parent / "static" / "sessions.js").read_text(
        encoding="utf-8"
    )
    load_start = src.index("async function loadSession(")
    load_body = src[load_start : src.index("// ── Handoff hint logic", load_start)]
    assign_pos = load_body.index("S.session=data.session")
    stream_pos = load_body.index("startSessionStream(S.session.session_id)")
    sync_pos = load_body.index("syncBackendSessionQueue(S.session.session_id)")
    active_stream_pos = load_body.index("let activeStreamId=S.session.active_stream_id")
    assert assign_pos < active_stream_pos < stream_pos < sync_pos


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


def test_permanent_start_errors_stop_churning_after_retry_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(session_queue, "_MAX_START_RETRIES", 2)

    item = session_queue.enqueue("sid-bad", {"text": "bad model"})
    calls = []

    def fake_start_session_turn(session_id, message, **kwargs):
        calls.append((session_id, message, kwargs))
        return {"error": "invalid model", "_status": 400}

    _install_fake_routes(monkeypatch, fake_start_session_turn)

    assert session_queue.drain_for_session("sid-bad") == 1
    assert _wait_until(lambda: session_queue.list_queue("sid-bad") and len(calls) == 1)
    assert session_queue.drain_for_session("sid-bad") == 1
    assert _wait_until(lambda: session_queue.list_queue("sid-bad")[0].get("blocked") is True)
    queued = session_queue.list_queue("sid-bad")
    assert queued[0]["id"] == item["id"]
    assert queued[0]["blocked"] is True
    assert queued[0]["error"] == "invalid model"
    assert session_queue.drain_for_session("sid-bad") == 0
    assert len(calls) == 2


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
