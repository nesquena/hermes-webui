"""Kanban routing must belong to the creating turn, not process-global env."""
import contextvars
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest


@pytest.fixture
def test_server():
    """These in-process regressions need no HTTP server."""
    yield


def test_concurrent_kanban_targets_ignore_other_turn_environment(monkeypatch, tmp_path):
    from api import streaming
    sc = pytest.importorskip("gateway.session_context")
    kt = pytest.importorskip("tools.kanban_tools")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "wrong-chat")
    monkeypatch.setenv("HERMES_SESSION_ID", "wrong-origin")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "foreign-topic")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "wrong-profile")
    from contextlib import contextmanager
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    import json
    from tools.registry import registry
    original_entry = registry.get_entry("kanban_create")
    original_handler = original_entry.handler
    monkeypatch.setattr(original_entry, "handler", original_handler)
    streaming._install_streaming_kanban_origin_wrapper()
    create = original_entry.handler
    @contextmanager
    def isolated_board(board, **kwargs):
        conn = kbc.connect(db_path=tmp_path / (board + ".db"))
        try:
            yield kb, conn
        finally:
            conn.close()
    monkeypatch.setattr(kt, "_board", isolated_board)
    monkeypatch.setattr(kt, "load_config", lambda: {})
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    barrier = threading.Barrier(2)

    def create_and_read(sid):
        result = json.loads(create({"title": "identity regression", "assignee": "scout",
                                    "initial_status": "blocked", "board": sid}))
        assert result.get("ok"), result
        with isolated_board(sid) as (_, conn):
            row = conn.execute("SELECT session_id FROM tasks WHERE id=?",
                               (result["task_id"],)).fetchone()
            sub = conn.execute("SELECT chat_id,notifier_profile FROM kanban_notify_subs WHERE task_id=?",
                               (result["task_id"],)).fetchone()
        return kt._resolve_notify_target(), row[0], tuple(sub)

    def turn(sid, profile):
        tokens = streaming._set_turn_session_identity(sid, profile=profile)
        try:
            barrier.wait(timeout=5)
            # Same context-copy boundary used by concurrent agent tools.
            with ThreadPoolExecutor(max_workers=1) as tools:
                target = tools.submit(contextvars.copy_context().run,
                                      create_and_read, sid).result(timeout=10)
            return target
        finally:
            streaming._reset_turn_session_identity(tokens)
            assert sc._SESSION_CHAT_ID.get() is sc._UNSET

    with ThreadPoolExecutor(max_workers=2) as turns:
        a = turns.submit(turn, "reasoning-chat", "default")
        b = turns.submit(turn, "nanit-chat", "home")
        for result, sid, profile in [(a, "reasoning-chat", "default"),
                                     (b, "nanit-chat", "home")]:
            target, origin, subscription = result.result(timeout=15)
            assert origin == sid
            assert subscription == (sid, profile)
            assert target["chat_id"] == sid
            assert target["platform"] == "webui"
            assert target["notifier_profile"] == profile
            assert target["thread_id"] is None


def test_kanban_origin_adapter_is_scoped_and_restores_after_exception(monkeypatch):
    from api import streaming
    registry = pytest.importorskip("tools.registry").registry
    pytest.importorskip("tools.kanban_tools")
    entry = registry.get_entry("kanban_create")
    calls = []
    def capture(args, **kwargs):
        calls.append(dict(args))
        return args.get("session_id")
    monkeypatch.setattr(entry, "handler", capture)
    streaming._install_streaming_kanban_origin_wrapper()
    wrapped = registry.get_entry("kanban_create").handler
    streaming._install_streaming_kanban_origin_wrapper()
    assert registry.get_entry("kanban_create").handler is wrapped
    sc = pytest.importorskip("gateway.session_context")
    routing_before = {name: var.get() for name, var in sc._VAR_MAP.items()}
    try:
        args = {"title": "test", "session_id": "untrusted-origin"}
        assert wrapped(args) == "untrusted-origin"  # non-WebUI remains untouched
        outer = streaming._set_turn_session_identity("outer")
        try:
            assert wrapped(args) == "outer"
            inner = streaming._set_turn_session_identity("inner")
            try:
                assert wrapped(args) == "inner"
                raise ValueError("turn failed")
            except ValueError:
                pass
            finally:
                streaming._reset_turn_session_identity(inner)
            assert wrapped(args) == "outer"
        finally:
            streaming._reset_turn_session_identity(outer)
        assert wrapped(args) == "untrusted-origin"
        assert args["session_id"] == "untrusted-origin"
        assert {name: var.get() for name, var in sc._VAR_MAP.items()} == routing_before
    finally:
        registry.get_entry("kanban_create").handler = capture
