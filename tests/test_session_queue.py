import io
import json
import pathlib
import subprocess
import sys
import threading
import time
import types

import pytest

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
    return not qdir.exists() or not any(qdir.glob("*.json"))


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
        {
            "text": "next please",
            "model": "m1",
            "model_provider": "p1",
            "profile": "default",
            "client_queue_id": "client-next",
        },
    )

    assert item["id"]
    assert item["text"] == "next please"
    assert item["client_queue_id"] == "client-next"
    assert item["state"] == "queued"
    assert session_queue.list_queue("sid-1") == [item]


def test_enqueue_is_idempotent_by_client_queue_id(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    payload = {"text": "only once", "client_queue_id": "stable-client-id"}

    first = session_queue.enqueue("sid-idempotent", payload)
    second = session_queue.enqueue("sid-idempotent", payload)

    assert second == first
    assert session_queue.list_queue("sid-idempotent") == [first]


def test_completed_item_keeps_bounded_durable_idempotency_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    payload = {"text": "run exactly once", "client_queue_id": "client-completed"}
    first = session_queue.enqueue("sid-receipt", payload)
    session_queue.claim_next("sid-receipt")
    monkeypatch.setattr(config, "ACTIVE_RUNS", {"stream-receipt": {"session_id": "sid-receipt"}})
    session_queue._finish_attempt("sid-receipt", first["id"], {"stream_id": "stream-receipt"})
    assert session_queue.complete_started("sid-receipt", "stream-receipt") is True

    replay = session_queue.enqueue("sid-receipt", payload)

    assert replay["id"] == first["id"]
    assert replay["state"] == "completed"
    assert session_queue.list_queue("sid-receipt") == []


def test_queue_rejects_unsafe_or_mismatched_session_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    with pytest.raises(ValueError, match="session_id"):
        session_queue.enqueue("a/b", {"text": "one", "client_queue_id": "client-one"})

    session_queue.enqueue("safe-one", {"text": "owned", "client_queue_id": "client-owned"})
    path = session_queue._queue_path("safe-one")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[0]["session_id"] = "safe-two"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Exception, match="owner|session"):
        session_queue.list_queue("safe-one")


def test_enqueue_requires_text_even_with_attachments(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)

    try:
        session_queue.enqueue("sid-empty", {"attachments": [{"name": "file.txt"}]})
    except ValueError as exc:
        assert str(exc) == "text is required"
    else:  # pragma: no cover - defensive clarity for the regression
        raise AssertionError("attachments-only backend queue item should be rejected")


def test_enqueue_rejects_capacity_without_evicting_acknowledged_items(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(session_queue, "_MAX_QUEUE_ITEMS", 3)

    for idx in range(3):
        session_queue.enqueue(
            "sid-cap",
            {
                "text": f"item {idx}",
                "model_provider": {"provider": idx},
                "client_queue_id": f"client-{idx}",
            },
        )

    with pytest.raises(Exception, match="capacity|full"):
        session_queue.enqueue(
            "sid-cap",
            {"text": "rejected item", "client_queue_id": "client-rejected"},
        )

    queued = session_queue.list_queue("sid-cap")
    assert [item["text"] for item in queued] == ["item 0", "item 1", "item 2"]
    assert queued[-1]["model_provider"] == "{'provider': 2}"


def test_corrupt_queue_fails_closed_and_is_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    queue_path = session_queue._queue_path("sid-corrupt")
    queue_path.write_text('{"owed": "turn"', encoding="utf-8")

    with pytest.raises(Exception, match="corrupt|read"):
        session_queue.enqueue(
            "sid-corrupt",
            {"text": "new item", "client_queue_id": "client-new"},
        )

    assert queue_path.read_text(encoding="utf-8") == '{"owed": "turn"'


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
        {
            "session_id": "sid-idle",
            "text": "queued while idle",
            "profile": "default",
            "client_queue_id": "client-idle",
        },
    )

    assert handler.status == 200
    assert drained == ["sid-idle"]
    body = handler.json_body()
    assert body["ok"] is True
    assert body["item"]["text"] == "queued while idle"


def test_frontend_sync_preserves_uncertain_ack_and_matches_legacy_server_item():
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
if(!q[1]._server_pending || q[1]._server_owned || q[1]._server_queue_id){
  throw new Error('uncertain acknowledgement was not preserved: '+JSON.stringify(q[1]));
}
const shifted = shiftQueuedSessionMessage(sid);
if(shifted){
  throw new Error('uncertain acknowledgement must not become browser-drainable: '+JSON.stringify(shifted));
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


def test_frontend_sync_is_awaitable_and_reconciles_by_stable_client_id():
    _run_queue_sync_node_script(
        r"""
const sid = 'sid-stable-client';
SESSION_QUEUES[sid] = [
  {text: 'local text changed before lost ACK', _server_pending: true, _client_queue_id: 'stable-1'},
];
fetch = async () => {
  await new Promise(resolve => setTimeout(resolve, 10));
  return {ok: true, json: async () => ({items: [
    {id: 'srv-stable', client_queue_id: 'stable-1', state: 'queued', text: 'server authoritative text', attachments: [], created_at: 1700000000},
  ]})};
};
await syncBackendSessionQueue(sid);
const q = SESSION_QUEUES[sid];
if(q.length !== 1) throw new Error('stable client id created duplicate: '+JSON.stringify(q));
if(q[0]._server_queue_id !== 'srv-stable' || q[0].text !== 'server authoritative text'){
  throw new Error('stable client id was not authoritatively reconciled: '+JSON.stringify(q));
}
"""
    )


def test_frontend_early_start_event_removes_by_client_id_before_ack_lands():
    _run_queue_sync_node_script(
        r"""
const sid = 'sid-early-start';
SESSION_QUEUES[sid] = [
  {text: 'already starting', _server_pending: true, _client_queue_id: 'stable-early'},
];
const removed = _removeServerQueuedEntry(sid, 'srv-not-stamped-yet', 'stable-early');
if(!removed) throw new Error('early start event did not remove pending client owner');
if(_getSessionQueue(sid, false).length !== 0) throw new Error('early start chip remained');
"""
    )


def test_frontend_ignores_stale_overlapping_queue_sync_response():
    _run_queue_sync_node_script(
        r"""
const sid='sid-sync-race';
SESSION_QUEUES[sid]=[{text:'queued',_server_pending:true,_client_queue_id:'client-race'}];
let resolveFirst;
const first=new Promise(resolve=>{resolveFirst=resolve;});
let call=0;
fetch=async()=>{
  call++;
  if(call===1){await first;return {ok:true,json:async()=>({items:[]})};}
  return {ok:true,json:async()=>({items:[
    {id:'server-race',client_queue_id:'client-race',state:'queued',text:'queued',attachments:[],created_at:1700000000},
  ]})};
};
const oldSync=syncBackendSessionQueue(sid);
await syncBackendSessionQueue(sid);
resolveFirst();
await oldSync;
const q=_getSessionQueue(sid,false);
if(q.length!==1||q[0]._server_queue_id!=='server-race'){
  throw new Error('stale GET erased newer authority: '+JSON.stringify(q));
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
        {
            "text": "queued followup",
            "model": "m-drain",
            "model_provider": "p-drain",
            "client_queue_id": "client-drain",
        },
    )
    calls = []

    def fake_start_session_turn(session_id, message, **kwargs):
        calls.append((session_id, message, kwargs))
        config.ACTIVE_RUNS["stream-1"] = {"session_id": session_id}
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
                "queue_client_id": "client-drain",
            },
        )
    ]
    queued = session_queue.list_queue("sid-drain")
    assert len(queued) == 1
    assert queued[0]["id"] == item["id"]
    assert queued[0]["state"] == "started"
    assert queued[0]["stream_id"] == "stream-1"
    assert session_queue.complete_started("sid-drain", "stream-1") is True
    assert session_queue.list_queue("sid-drain") == []
    assert _is_empty_queue_dir(tmp_path)


def test_turn_that_finishes_before_started_transition_does_not_leave_tombstone(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    session_queue.enqueue(
        "sid-fast", {"text": "fast", "client_queue_id": "client-fast"}
    )

    def fake_start_session_turn(session_id, message, **kwargs):
        return {"stream_id": "stream-fast", "session_id": session_id, "_status": 200}

    _install_fake_routes(monkeypatch, fake_start_session_turn)
    assert session_queue.drain_for_session("sid-fast") == 1
    assert _wait_until(lambda: not session_queue.list_queue("sid-fast"))


def test_thread_start_failure_restores_starting_item(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    item = session_queue.enqueue(
        "sid-thread-fail",
        {"text": "must survive", "client_queue_id": "client-thread-fail"},
    )
    monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

    assert session_queue.drain_for_session("sid-thread-fail") == 0
    queued = session_queue.list_queue("sid-thread-fail")
    assert [(entry["id"], entry["state"]) for entry in queued] == [(item["id"], "queued")]


def test_concurrent_drains_serialize_one_fifo_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    first = session_queue.enqueue(
        "sid-fifo", {"text": "first", "client_queue_id": "client-first"}
    )
    session_queue.enqueue(
        "sid-fifo", {"text": "second", "client_queue_id": "client-second"}
    )
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_start_session_turn(session_id, message, **kwargs):
        calls.append((message, kwargs["queue_item_id"]))
        entered.set()
        assert release.wait(2)
        config.ACTIVE_RUNS["stream-fifo"] = {"session_id": session_id}
        return {"stream_id": "stream-fifo", "_status": 200}

    _install_fake_routes(monkeypatch, fake_start_session_turn)

    assert session_queue.drain_for_session("sid-fifo") == 1
    assert entered.wait(1)
    assert session_queue.drain_for_session("sid-fifo") == 0
    release.set()
    assert _wait_until(
        lambda: session_queue.list_queue("sid-fifo")[0].get("state") == "started"
    )
    assert calls == [("first", first["id"])]
    assert [entry["text"] for entry in session_queue.list_queue("sid-fifo")] == [
        "first",
        "second",
    ]


def test_starting_items_reject_edit_delete_and_reorder(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    first = session_queue.enqueue(
        "sid-authoritative", {"text": "first", "client_queue_id": "client-first"}
    )
    second = session_queue.enqueue(
        "sid-authoritative", {"text": "second", "client_queue_id": "client-second"}
    )
    claimed = session_queue.claim_next("sid-authoritative")
    assert claimed and claimed["id"] == first["id"]

    with pytest.raises(Exception, match="started|starting"):
        session_queue.update_item("sid-authoritative", first["id"], {"text": "wrong"})
    with pytest.raises(Exception, match="started|starting"):
        session_queue.delete_item("sid-authoritative", first["id"])
    with pytest.raises(Exception, match="started|starting"):
        session_queue.reorder_items("sid-authoritative", [second["id"], first["id"]])

    assert [item["text"] for item in session_queue.list_queue("sid-authoritative")] == [
        "first",
        "second",
    ]


def test_backend_reorder_combine_and_clear_are_atomic(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    first = session_queue.enqueue(
        "sid-mutate", {"text": "first", "client_queue_id": "client-first"}
    )
    second = session_queue.enqueue(
        "sid-mutate", {"text": "second", "client_queue_id": "client-second"}
    )

    reordered = session_queue.reorder_items("sid-mutate", [second["id"], first["id"]])
    assert [item["id"] for item in reordered] == [second["id"], first["id"]]
    combined = session_queue.combine_items("sid-mutate", [second["id"], first["id"]])
    assert len(combined) == 1
    assert combined[0]["id"] == second["id"]
    assert combined[0]["text"] == "second\n\nfirst"
    assert session_queue.clear_queue("sid-mutate") == 1
    assert session_queue.list_queue("sid-mutate") == []


def test_startup_recovery_requeues_unsubmitted_starting_item(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    item = session_queue.enqueue(
        "sid-recover", {"text": "survive restart", "client_queue_id": "client-recover"}
    )
    assert session_queue.claim_next("sid-recover")["state"] == "starting"
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid: types.SimpleNamespace(
            active_stream_id="stale-stream",
            pending_user_message="survive restart",
            pending_queue_item_id=item["id"],
        ),
    )

    result = session_queue.recover_all_queues(schedule=False)

    assert result["requeued"] == 1
    queued = session_queue.list_queue("sid-recover")
    assert queued[0]["id"] == item["id"]
    assert queued[0]["state"] == "queued"


def test_startup_recovery_keeps_correlated_started_owner_and_schedules_fifo(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    item = session_queue.enqueue(
        "sid-correlated", {"text": "already submitted", "client_queue_id": "client-correlated"}
    )
    session_queue.claim_next("sid-correlated")
    monkeypatch.setattr(
        config,
        "ACTIVE_RUNS",
        {"stream-correlated": {"session_id": "sid-correlated"}},
    )
    session_queue._finish_attempt("sid-correlated", item["id"], {"stream_id": "stream-correlated"})
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid: types.SimpleNamespace(
            active_stream_id="stream-correlated",
            pending_user_message="already submitted",
            pending_queue_item_id=item["id"],
        ),
    )
    scheduled = []
    monkeypatch.setattr(session_queue, "drain_for_session", lambda sid: scheduled.append(sid) or 0)

    result = session_queue.recover_all_queues(schedule=True)

    assert result["started"] == 1
    assert session_queue.list_queue("sid-correlated")[0]["state"] == "started"
    assert scheduled == []


def test_drain_requeues_item_when_start_races_active_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})

    item = session_queue.enqueue(
        "sid-race", {"text": "still needed", "client_queue_id": "client-race"}
    )

    def fake_start_session_turn(session_id, message, **kwargs):
        return {"error": "session already has an active stream", "_status": 409}

    _install_fake_routes(monkeypatch, fake_start_session_turn)

    assert session_queue.drain_for_session("sid-race") == 1
    assert _wait_until(lambda: session_queue.list_queue("sid-race"))
    queued = session_queue.list_queue("sid-race")
    assert len(queued) == 1
    assert queued[0]["id"] == item["id"]
    assert queued[0]["text"] == "still needed"


@pytest.mark.parametrize("status", [400, 500])
def test_start_errors_stop_churning_after_retry_limit(monkeypatch, tmp_path, status):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(session_queue, "_MAX_START_RETRIES", 2)

    item = session_queue.enqueue(
        "sid-bad", {"text": "bad model", "client_queue_id": "client-bad"}
    )
    calls = []

    def fake_start_session_turn(session_id, message, **kwargs):
        calls.append((session_id, message, kwargs))
        return {"error": "start failed", "_status": status}

    _install_fake_routes(monkeypatch, fake_start_session_turn)

    assert session_queue.drain_for_session("sid-bad") == 1
    assert _wait_until(
        lambda: session_queue.list_queue("sid-bad")
        and session_queue.list_queue("sid-bad")[0].get("state") == "queued"
        and len(calls) == 1
        and "sid-bad" not in session_queue._DRAINING_SESSIONS
    )
    assert session_queue.drain_for_session("sid-bad") == 1
    assert _wait_until(lambda: session_queue.list_queue("sid-bad")[0].get("blocked") is True)
    queued = session_queue.list_queue("sid-bad")
    assert queued[0]["id"] == item["id"]
    assert queued[0]["blocked"] is True
    assert queued[0]["error"] == "start failed"
    assert session_queue.drain_for_session("sid-bad") == 0
    assert len(calls) == 2


def test_raised_start_error_retries_automatically_then_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(session_queue, "_MAX_START_RETRIES", 2)
    monkeypatch.setattr(session_queue, "_RETRY_DELAY_SECONDS", 0.01)
    session_queue.enqueue(
        "sid-raised", {"text": "keep retrying", "client_queue_id": "client-raised"}
    )
    calls = []

    def fake_start_session_turn(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("worker unavailable")

    _install_fake_routes(monkeypatch, fake_start_session_turn)
    assert session_queue.drain_for_session("sid-raised") == 1
    assert _wait_until(
        lambda: session_queue.list_queue("sid-raised")
        and session_queue.list_queue("sid-raised")[0].get("state") == "blocked"
    )
    assert len(calls) == 2
    assert session_queue.list_queue("sid-raised")[0]["retry_count"] == 2


def test_drain_does_not_claim_while_session_has_active_run(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(
        config,
        "ACTIVE_RUNS",
        {"stream-active": {"session_id": "sid-active"}},
    )

    item = session_queue.enqueue(
        "sid-active", {"text": "later", "client_queue_id": "client-active"}
    )

    assert session_queue.drain_for_session("sid-active") == 0
    assert session_queue.list_queue("sid-active")[0]["id"] == item["id"]
