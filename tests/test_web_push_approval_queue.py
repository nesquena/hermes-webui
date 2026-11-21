"""Approval head push scheduling shares the bounded delivery workers."""
import queue
import threading
import time
from types import SimpleNamespace

from api import route_approvals, web_push
from tests.test_web_push import OWNER_A, _enable


class _ThreadProbe:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass


def _paused_pool(monkeypatch):
    scheduled = []

    def thread(**kwargs):
        scheduled.append(kwargs)
        return _ThreadProbe(**kwargs)

    # Intercept only the approval module's old per-head thread creation.
    monkeypatch.setattr(route_approvals, "threading", SimpleNamespace(Thread=thread))
    monkeypatch.setattr(web_push, "_ensure_workers", lambda: None)
    monkeypatch.setattr(web_push, "_QUEUE", queue.Queue(maxsize=web_push._MAX_PENDING))
    monkeypatch.setattr(web_push, "_STOP", threading.Event())
    monkeypatch.setattr(web_push, "_SEEN", {})
    return scheduled


def test_identical_heads_coalesce_before_any_background_scheduling(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    scheduled = _paused_pool(monkeypatch)
    for _ in range(100):
        route_approvals._schedule_web_push_head("s1", {"approval_id": "same", "description": "fixture"})
    assert len(scheduled) <= web_push._MAX_WORKERS
    assert web_push._QUEUE.qsize() == 1


def test_distinct_heads_drop_without_blocking_when_shared_queue_is_full(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    scheduled = _paused_pool(monkeypatch)
    started = time.monotonic()
    for i in range(100):
        route_approvals._schedule_web_push_head("s1", {"approval_id": str(i)})
    assert time.monotonic() - started < 1
    assert len(scheduled) <= web_push._MAX_WORKERS
    assert web_push._QUEUE.qsize() == web_push._MAX_PENDING


def test_head_preparation_and_delivery_run_on_existing_pool(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    monkeypatch.setattr(web_push, "_QUEUE", queue.Queue(maxsize=web_push._MAX_PENDING))
    monkeypatch.setattr(web_push, "_STOP", threading.Event())
    monkeypatch.setattr(web_push, "_WORKERS", [])
    monkeypatch.setattr(web_push, "_ATEXIT", [True])
    monkeypatch.setattr(web_push, "_SEEN", {})
    prepared, sent = [], []
    received = threading.Event()

    def targets(sid):
        prepared.append(threading.current_thread())
        return [OWNER_A]

    def send(payload):
        sent.append(payload)
        received.set()

    monkeypatch.setattr(web_push, "_targets_for_session", targets)
    monkeypatch.setattr(web_push, "_send_to_all", send)
    try:
        with route_approvals._lock:
            for _ in range(100):
                route_approvals._schedule_web_push_head("s1", {"approval_id": "same", "description": "fixture"})
        assert received.wait(3)
        assert len(sent) == 1
        assert len(web_push._WORKERS) == web_push._MAX_WORKERS
        assert prepared and all(t in web_push._WORKERS for t in prepared)
        assert sent[0]["options"]["body"] == "fixture"
    finally:
        web_push.shutdown(wait=2)
