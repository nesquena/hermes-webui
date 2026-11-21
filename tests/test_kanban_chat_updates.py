from collections import OrderedDict
"""Origin WebUI + separate-process Kanban events, without gateway/SSE mocks."""
import importlib
import json
import sqlite3
import types

import pytest

from api import config as cfg
from api.models import Session


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    module = importlib.import_module("api.kanban_chat_updates")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(cfg, "SESSION_DIR", state / "sessions")
    cfg.SESSION_DIR.mkdir()
    monkeypatch.setattr(cfg, "SESSION_INDEX_FILE", cfg.SESSION_DIR / "_index.json")
    monkeypatch.setattr(cfg, "SESSIONS", OrderedDict())
    from api import models
    monkeypatch.setattr(models, "SESSION_DIR", cfg.SESSION_DIR)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", cfg.SESSION_INDEX_FILE)
    monkeypatch.setattr(models, "SESSIONS", cfg.SESSIONS)
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    db = SessionDB(tmp_path / "state.db")
    for sid, profile in (("origin", "owner"), ("other", "owner"), ("foreign", "elsewhere")):
        db.create_session(sid, source="webui", profile_name=profile)
        session = Session(session_id=sid, profile=profile,
                          messages=[{"role": "user", "content": "do a controlled test", "timestamp": 1}])
        session.save()
    board = tmp_path / "kanban.db"
    with sqlite3.connect(board) as conn:
        conn.executescript("""
            CREATE TABLE tasks(id TEXT PRIMARY KEY, session_id TEXT, assignee TEXT, title TEXT);
            CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
                                     payload TEXT, created_at INTEGER);
            CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT);
            INSERT INTO tasks VALUES('task', 'origin', 'worker', 'private client title');
            INSERT INTO tasks VALUES('other-task', 'other', 'worker', 'another');
            INSERT INTO tasks VALUES('foreign-task', 'foreign', 'worker', 'private');
        """)
    consumer = module.KanbanChatUpdates(state, tmp_path, "owner", "ar-EG",
                                      boards=lambda: [("default", board)])
    consumer.tick()  # activation watermark: old work is never replayed
    def event(kind, payload=None, task="task"):
        with sqlite3.connect(board) as conn:
            cur = conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,1)",
                               (task, kind, json.dumps(payload or {})))
            return cur.lastrowid
    yield types.SimpleNamespace(module=module, consumer=consumer, db=db, board=board,
                                event=event, state=state, home=tmp_path)
    db.close()


def messages(sid):
    return Session.load(sid).messages


def test_origin_delivery_is_arabic_durable_and_once(delivery):
    delivery.event("completed", {"summary": "SECRET customer info", "result_len": 123})
    delivery.consumer.tick()
    delivery.consumer.tick()
    m = messages("origin")
    assert len(m) == 2
    assert "العامل سلّم" in m[-1]["content"]
    assert "SECRET" not in m[-1]["content"]
    assert "private client" not in m[-1]["content"]
    assert len(messages("other")) == len(messages("foreign")) == 1
    rows = delivery.db._read_all("SELECT * FROM messages WHERE display_kind='kanban_update'")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "origin"
    assert json.loads(rows[0]["display_metadata"])["event_key"] == m[-1]["kanban_event_key"]


def test_restart_backfill_and_ack_crash_dedupe(delivery, monkeypatch):
    delivery.event("blocked", {"kind": "needs_input", "reason": "Bearer secret customer"})
    original = delivery.consumer._save
    failed = False
    def fail_ack(value):
        nonlocal failed
        if not value["pending"] and not failed:
            failed = True
            raise OSError("simulate crash after transcript commit")
        original(value)
    monkeypatch.setattr(delivery.consumer, "_save", fail_ack)
    with pytest.raises(OSError):
        delivery.consumer.tick()
    restarted = delivery.module.KanbanChatUpdates(delivery.state, delivery.home, "owner", "ar-EG",
                                                 boards=lambda: [("default", delivery.board)])
    restarted.tick()
    assert len(messages("origin")) == 2
    assert len(delivery.db._read_all("SELECT * FROM messages WHERE display_kind='kanban_update'")) == 1


def test_busy_origin_does_not_block_other_session(delivery, monkeypatch):
    from api import background_process as bp
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: sid == "origin")
    delivery.event("completed")
    delivery.event("completed", task="other-task")
    delivery.consumer.tick()
    assert len(messages("origin")) == 1
    assert len(messages("other")) == 2
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: False)
    delivery.consumer.tick()
    assert len(messages("origin")) == 2


def test_profile_and_routing_fail_closed(delivery):
    delivery.event("completed", task="foreign-task")
    with sqlite3.connect(delivery.board) as conn:
        conn.execute("UPDATE tasks SET session_id=NULL WHERE id='other-task'")
    delivery.event("completed", task="other-task")
    delivery.consumer.tick()
    assert len(messages("foreign")) == len(messages("other")) == 1


def test_lease_contention_defers_without_second_writer(delivery):
    holder = "test-live-worker"
    assert delivery.db.try_acquire_session_turn_lease("origin", holder)
    delivery.event("completed")
    delivery.consumer.tick()
    assert len(messages("origin")) == 1
    delivery.db.release_session_turn_lease("origin", holder)
    delivery.consumer.tick()
    assert len(messages("origin")) == 2


def test_heartbeat_raw_comment_and_identical_checkpoint_are_quiet(delivery):
    delivery.event("heartbeat")
    delivery.event("commented", {"comment_id": 1})
    delivery.event("continuation_required", {"checkpoint": {"changed_files": ["private/a", "private/b"]}})
    delivery.consumer.tick()
    assert len(messages("origin")) == 2
    assert "2 ملفات" in messages("origin")[-1]["content"]
    delivery.event("continuation_required", {"checkpoint": {"changed_files": ["private/a", "private/b"]}})
    delivery.consumer.tick()
    assert len(messages("origin")) == 2


def test_explicit_worker_milestone_uses_only_safe_structured_content(delivery):
    with sqlite3.connect(delivery.board) as conn:
        conn.execute("INSERT INTO task_comments VALUES(1,'task','worker',?)",
                     (json.dumps({"user_update": {"stage": "checks_passed", "count": 3},
                                  "log": "TOKEN=never-render-this"}),))
    delivery.event("commented", {"comment_id": 1})
    delivery.consumer.tick()
    assert "3 اختبارات" in messages("origin")[-1]["content"]
    assert "TOKEN" not in messages("origin")[-1]["content"]


def test_sidecar_failure_recovers_native_row_once(delivery, monkeypatch):
    delivery.event("timed_out")
    original = Session.save
    def fail(self, *args, **kwargs):
        raise OSError("sidecar unavailable")
    monkeypatch.setattr(Session, "save", fail)
    delivery.consumer.tick()
    assert len(delivery.db._read_all("SELECT * FROM messages WHERE display_kind='kanban_update'")) == 1
    monkeypatch.setattr(Session, "save", original)
    delivery.consumer.tick()
    assert len(messages("origin")) == 2
    assert len(delivery.db._read_all("SELECT * FROM messages WHERE display_kind='kanban_update'")) == 1


def test_pre_activation_events_not_replayed(delivery):
    with sqlite3.connect(delivery.board) as conn:
        conn.execute("INSERT INTO task_events VALUES(0,'task','completed','{}',1)")
    delivery.consumer.tick()
    assert len(messages("origin")) == 1


def test_retargeted_pending_event_cannot_leak(delivery, monkeypatch):
    from api import background_process as bp
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: True)
    delivery.event("completed")
    delivery.consumer.tick()
    with sqlite3.connect(delivery.board) as conn:
        conn.execute("UPDATE tasks SET session_id='other' WHERE id='task'")
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: False)
    delivery.consumer.tick()
    assert len(messages("origin")) == len(messages("other")) == 1

def test_channel_receipt_and_reconnect_backfill(delivery):
    from api import background_process as bp
    ch, queue = bp.subscribe_to_session_channel("origin")
    other, other_queue = bp.subscribe_to_session_channel("other")
    try:
        known = bp.persisted_message_count_for_session("origin")
        delivery.event("completed")
        delivery.consumer.tick()
        event, payload = queue.get_nowait()
        assert event == "session-updated" and payload["session_id"] == "origin"
        assert payload["message_count"] == 2
        assert other_queue.empty()
        # Reconnect needs only the persisted count, not an ephemeral channel.
        assert bp.should_emit_session_updated(known, bp.persisted_message_count_for_session("origin"))
        assert not bp.should_emit_session_updated(2, bp.persisted_message_count_for_session("origin"))
    finally:
        ch.unsubscribe(queue)
        other.unsubscribe(other_queue)


def test_foreign_profile_cannot_subscribe_before_queue_registration(monkeypatch):
    from api import routes
    called = []
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda h, sid: False)
    from api import background_process as bp
    monkeypatch.setattr(bp, "subscribe_to_session_channel", lambda *a, **kw: called.append(a))
    from urllib.parse import urlparse
    assert routes._handle_session_sse_stream(object(), urlparse("/api/session/stream?session_id=foreign")) is None
    assert not called

def test_native_compression_continuation_keeps_origin_receipt(delivery):
    delivery.db.end_session("origin", "compression")
    delivery.db.create_session("originchild", source="webui", profile_name="owner",
                               parent_session_id="origin")
    Session(session_id="originchild", profile="owner",
            messages=[{"role": "user", "content": "continued request", "timestamp": 2}]).save()
    delivery.event("completed")
    delivery.consumer.tick()
    assert len(messages("origin")) == 1
    assert len(messages("originchild")) == 2
    assert len(messages("other")) == 1


@pytest.mark.parametrize("kind,word", [("crashed","غير متوقع"),("gave_up","الفشل"),
                                     ("timed_out","وقت التنفيذ"),("review_requested","المراجعة")])
def test_terminal_outcomes_are_honest_and_no_raw_diagnostics(delivery, kind, word):
    delivery.event(kind, {"reason":"customer-name TOKEN=secret /private/path"})
    delivery.consumer.tick()
    text = messages("origin")[-1]["content"]
    assert word in text
    assert "secret" not in text and "customer" not in text


def test_milestone_sidecar_failure_keeps_delivery_pending(delivery, monkeypatch):
    delivery.event("continuation_required", {"checkpoint":{"changed_files":["private/a"]}})
    original = Session.save
    monkeypatch.setattr(Session, "save", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    delivery.consumer.tick()
    assert json.loads(delivery.consumer.path.read_text())["pending"]
    delivery.consumer.tick()
    assert json.loads(delivery.consumer.path.read_text())["pending"]
    monkeypatch.setattr(Session, "save", original)
    delivery.consumer.tick()
    assert len(messages("origin")) == 2
    assert not json.loads(delivery.consumer.path.read_text())["pending"]


@pytest.mark.parametrize("payload", ["[]", "null", "{bad json"])
def test_malformed_event_does_not_poison_next_receipt(delivery, payload):
    with sqlite3.connect(delivery.board) as conn:
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('task','blocked',?,1)", (payload,))
    delivery.event("completed")
    delivery.consumer.tick()
    assert len(messages("origin")) == 2
    assert "سلّم" in messages("origin")[-1]["content"]
