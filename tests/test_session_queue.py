import sys
import time
import types

from api import config
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


def test_enqueue_persists_and_lists_session_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)

    item = session_queue.enqueue(
        "sid-1",
        {"text": "next please", "model": "m1", "model_provider": "p1", "profile": "default"},
    )

    assert item["id"]
    assert item["text"] == "next please"
    assert session_queue.list_queue("sid-1") == [item]


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
