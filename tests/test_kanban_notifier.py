"""Tests for the WebUI kanban agent wakeup notifier.

These tests inject a fake ``hermes_cli.kanban_db`` module (same pattern as
test_kanban_bridge.py) and verify that:

1. ``_poll_once`` collects terminal events for ``platform='webui'`` subs
2. ``_format_wakeup_prompt`` produces the expected message
3. ``_deliver`` calls ``_start_server_side_wakeup_turn`` for idle sessions
4. ``_deliver`` defers (records deferred wakeup) when the session is active
5. Subscriptions for terminal (done/archived) tasks are cleaned up
6. Non-webui subscriptions (telegram, discord) are skipped
7. Non-terminal event kinds (created, assigned) are not delivered
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@dataclass
class FakeTask:
    id: str
    title: str
    status: str = "ready"
    assignee: str | None = None
    tenant: str | None = None
    priority: int = 0
    body: str | None = None


@dataclass
class FakeEvent:
    id: int
    task_id: str
    run_id: str | None
    kind: str
    payload: dict | None
    created_at: int


class FakeConn:
    def __init__(self, tasks, events, subs):
        self.tasks = tasks
        self.events = events
        self.subs = subs

    def close(self):
        pass

    def execute(self, sql, params=()):
        # SELECT last_event_id FROM kanban_notify_subs
        if "SELECT last_event_id FROM kanban_notify_subs" in sql:
            # params: (task_id, platform, chat_id, thread_id)
            task_id = params[0]
            for s in self.subs:
                if s["task_id"] == task_id and s["platform"] == params[1]:
                    row = SimpleNamespace(fetchone=lambda s=s: SimpleNamespace(
                        __getitem__=lambda _, k="last_event_id": s["last_event_id"],
                        __iter__=lambda s=s: iter([s["last_event_id"]]),
                    ))
                    # Return a dict-like row
                    class _Row:
                        def __init__(self, val):
                            self._val = val
                        def __getitem__(self, k):
                            return self._val
                    return SimpleNamespace(fetchone=lambda: _Row(s["last_event_id"]))
            return SimpleNamespace(fetchone=lambda: None)

        # SELECT * FROM task_events WHERE task_id = ? AND id > ?
        if "SELECT * FROM task_events WHERE task_id = ? AND id > ?" in sql:
            task_id = params[0]
            cursor = params[1]
            kinds = params[2:] if len(params) > 2 else None

            matching = [
                e for e in self.events
                if e.task_id == task_id and e.id > cursor
            ]
            if kinds:
                matching = [e for e in matching if e.kind in kinds]

            class _EventRow:
                def __init__(self, e):
                    self.id = e.id
                    self.task_id = e.task_id
                    self.run_id = e.run_id
                    self.kind = e.kind
                    self.payload = '{"status":"test"}' if e.payload else None
                    self.created_at = e.created_at

                def __getitem__(self, k):
                    return getattr(self, k)

                def keys(self):
                    return ["id", "task_id", "run_id", "kind", "payload", "created_at"]

            return SimpleNamespace(fetchall=lambda: [_EventRow(e) for e in matching])

        # UPDATE kanban_notify_subs SET last_event_id
        if "UPDATE kanban_notify_subs SET last_event_id" in sql:
            new_cursor = params[0]
            task_id = params[1]
            for s in self.subs:
                if s["task_id"] == task_id and s["platform"] == params[2]:
                    s["last_event_id"] = new_cursor
            return SimpleNamespace(rowcount=1)

        # DELETE FROM kanban_notify_subs
        if "DELETE FROM kanban_notify_subs" in sql:
            task_id = params[0]
            before = len(self.subs)
            self.subs = [
                s for s in self.subs
                if not (s["task_id"] == task_id and s["platform"] == params[1])
            ]
            return SimpleNamespace(rowcount=before - len(self.subs))

        raise AssertionError(f"unexpected SQL: {sql} params={params}")


class FakeKanbanDB:
    DEFAULT_BOARD = "default"

    def __init__(self):
        self.tasks = []
        self.events = []
        self.subs = []
        self._next_event_id = 1

    def connect(self, *, board=None):
        return FakeConn(self.tasks, self.events, self.subs)

    def list_boards(self, *, include_archived=True):
        return [{"slug": "default", "name": "Default", "archived": False}]

    def get_task(self, conn, task_id):
        return next((t for t in self.tasks if t.id == task_id), None)

    def list_notify_subs(self, conn, task_id=None):
        if task_id:
            return [s for s in conn.subs if s["task_id"] == task_id]
        return list(conn.subs)

    def claim_unseen_events_for_sub(self, conn, *, task_id, platform, chat_id,
                                    thread_id=None, kinds=None):
        # Find the sub
        sub = None
        for s in conn.subs:
            if (s["task_id"] == task_id and s["platform"] == platform
                    and s["chat_id"] == chat_id):
                sub = s
                break
        if not sub:
            return 0, 0, []

        old_cursor = sub["last_event_id"]

        # Find events
        matching = [
            e for e in conn.events
            if e.task_id == task_id and e.id > old_cursor
        ]
        if kinds:
            matching = [e for e in matching if e.kind in kinds]

        if not matching:
            return old_cursor, old_cursor, []

        new_cursor = max(e.id for e in matching)
        sub["last_event_id"] = new_cursor

        # Return as Event-like objects
        from collections import namedtuple
        Event = namedtuple("Event", ["id", "task_id", "kind", "payload", "created_at", "run_id"])
        events = [
            Event(id=e.id, task_id=e.task_id, kind=e.kind,
                  payload=e.payload, created_at=e.created_at, run_id=e.run_id)
            for e in matching
        ]
        return old_cursor, new_cursor, events

    def remove_notify_sub(self, conn, *, task_id, platform, chat_id, thread_id=None):
        before = len(conn.subs)
        conn.subs[:] = [
            s for s in conn.subs
            if not (s["task_id"] == task_id and s["platform"] == platform
                    and s["chat_id"] == chat_id)
        ]
        return before != len(conn.subs)

    def rewind_notify_cursor(self, conn, *, task_id, platform, chat_id,
                             thread_id=None, claimed_cursor=None, old_cursor=None):
        for s in conn.subs:
            if (s["task_id"] == task_id and s["platform"] == platform
                    and s["chat_id"] == chat_id):
                if s["last_event_id"] == claimed_cursor:
                    s["last_event_id"] = old_cursor
                    return True
        return False

    @staticmethod
    def _normalize_board_slug(slug):
        if slug is None:
            return None
        s = str(slug).strip().lower().replace(" ", "-")
        if not s:
            return None
        return s


@pytest.fixture
def fake_kanban(monkeypatch):
    """Install a fake hermes_cli.kanban_db module."""
    fake = FakeKanbanDB()
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.kanban_db = fake
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake)
    return fake


@pytest.fixture
def clean_notifier():
    """Reset the notifier module's internal state."""
    from api import kanban_notifier
    kanban_notifier._DELIVERED_EVENTS.clear()
    yield kanban_notifier
    kanban_notifier._DELIVERED_EVENTS.clear()


def _make_sub(task_id="t_1", platform="webui", chat_id="sess-123",
              last_event_id=0, thread_id="", user_id=None, notifier_profile=""):
    return {
        "task_id": task_id,
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "notifier_profile": notifier_profile,
        "created_at": 0,
        "last_event_id": last_event_id,
    }


class TestFormatWakeupPrompt:
    def test_completed(self, clean_notifier):
        prompt = clean_notifier._format_wakeup_prompt(
            "t_1", "My Task", "worker", "default", {"completed"}
        )
        assert "t_1" in prompt
        assert "completed" in prompt
        assert "My Task" in prompt
        assert "@worker" in prompt
        assert "default" in prompt

    def test_blocked(self, clean_notifier):
        prompt = clean_notifier._format_wakeup_prompt(
            "t_2", "Blocked Task", "dev", "ops-board", {"blocked"}
        )
        assert "blocked" in prompt
        assert "needs attention" in prompt
        assert "Blocked Task" in prompt

    def test_multiple_kinds(self, clean_notifier):
        prompt = clean_notifier._format_wakeup_prompt(
            "t_3", "Triple Fail", "agent", "default",
            {"crashed", "timed_out", "gave_up"}
        )
        assert "crashed" in prompt
        assert "timed out" in prompt
        assert "gave up" in prompt


class TestPollOnce:
    def test_collects_webui_terminal_events(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Test Task", "blocked", "worker")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {"reason": "needs input"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()

        assert len(deliveries) == 1
        assert deliveries[0]["sub"]["chat_id"] == "sess-1"
        assert len(deliveries[0]["events"]) == 1
        assert deliveries[0]["events"][0].kind == "blocked"
        assert deliveries[0]["board"] == "default"

    def test_skips_non_webui_subs(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Test", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {}, 100),
        ]
        fake_kanban.subs = [
            _make_sub(task_id="t_1", platform="telegram", chat_id="123"),
        ]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 0

    def test_skips_non_terminal_events(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Test", "ready", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "created", {"status": "ready"}, 100),
            FakeEvent(2, "t_1", None, "assigned", {"assignee": "w"}, 101),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 0

    def test_advances_cursor(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Test", "done", "w")]
        fake_kanban.events = [
            FakeEvent(5, "t_1", None, "completed", {"summary": "done"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1

        # Second poll should find nothing — cursor advanced
        deliveries2 = clean_notifier._poll_once()
        assert len(deliveries2) == 0

    def test_handles_empty_subs(self, fake_kanban, clean_notifier):
        fake_kanban.subs = []
        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 0

    def test_handles_no_boards(self, fake_kanban, clean_notifier, monkeypatch):
        monkeypatch.setattr(fake_kanban, "list_boards", lambda **kw: [])
        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 0

    def test_handles_db_connection_error(self, fake_kanban, clean_notifier, monkeypatch):
        def bad_connect(*, board=None):
            raise Exception("DB locked")
        monkeypatch.setattr(fake_kanban, "connect", bad_connect)
        # Should not raise
        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 0


class TestDeliver:
    def test_starts_wakeup_for_idle_session(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Test Task", "blocked", "worker")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {"reason": "x"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()

        mock_resp = {"_status": 200, "stream_id": "s1"}
        with patch("api.background_process._session_has_active_turn", return_value=False) as mock_active, \
             patch("api.routes.start_session_turn", return_value=mock_resp) as mock_turn, \
             patch("api.background_process.record_deferred_wakeup") as mock_defer:
            clean_notifier._deliver(deliveries)

            mock_active.assert_called_once_with("sess-1")
            mock_turn.assert_called_once()
            mock_defer.assert_not_called()
            # Check the prompt
            args = mock_turn.call_args
            assert "sess-1" == args[0][0]
            prompt = args[0][1]
            assert "t_1" in prompt
            assert "blocked" in prompt

    def test_defers_when_session_active(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Test", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()

        with patch("api.background_process._session_has_active_turn", return_value=True), \
             patch("api.routes.start_session_turn") as mock_turn, \
             patch("api.background_process.record_deferred_wakeup") as mock_defer:
            clean_notifier._deliver(deliveries)

            mock_turn.assert_not_called()
            mock_defer.assert_called_once()
            # Check the deferred prompt
            args = mock_defer.call_args
            assert args[0][0] == "sess-1" or args.kwargs.get("session_id") == "sess-1"

    def test_defers_on_409_race(self, fake_kanban, clean_notifier):
        """When start_session_turn returns 409 (active turn race),
        the wakeup should be deferred, not treated as failure."""
        fake_kanban.tasks = [FakeTask("t_1", "Test", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()

        mock_resp = {"_status": 409, "error": "turn active"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp), \
             patch("api.background_process.record_deferred_wakeup") as mock_defer:
            clean_notifier._deliver(deliveries)
            mock_defer.assert_called_once()

    def test_rewinds_on_400_failure(self, fake_kanban, clean_notifier):
        """When start_session_turn returns >=400 (not 409), the cursor
        should be rewound for retry."""
        fake_kanban.tasks = [FakeTask("t_1", "Test", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert fake_kanban.subs[0]["last_event_id"] == 1

        mock_resp = {"_status": 404, "error": "session not found"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp), \
             patch("api.background_process.record_deferred_wakeup"):
            clean_notifier._deliver(deliveries)

        # Cursor should be rewound
        assert fake_kanban.subs[0]["last_event_id"] == 0

    def test_cleans_up_terminal_subscription(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Done Task", "done", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "completed", {"summary": "all done"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1

        mock_resp = {"_status": 200, "stream_id": "s1"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp), \
             patch("api.background_process.record_deferred_wakeup"):
            clean_notifier._deliver(deliveries)

        # Subscription should be removed
        assert len(fake_kanban.subs) == 0

    def test_does_not_cleanup_non_terminal(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Blocked Task", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {"reason": "x"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()

        mock_resp = {"_status": 200, "stream_id": "s1"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp), \
             patch("api.background_process.record_deferred_wakeup"):
            clean_notifier._deliver(deliveries)

        # Subscription should still exist (task is "blocked", not "done")
        assert len(fake_kanban.subs) == 1

    def test_handles_missing_session_id(self, fake_kanban, clean_notifier):
        fake_kanban.tasks = [FakeTask("t_1", "Test", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="")]

        deliveries = clean_notifier._poll_once()
        if deliveries:
            with patch("api.routes.start_session_turn") as mock_turn:
                clean_notifier._deliver(deliveries)
                mock_turn.assert_not_called()

    def test_rewinds_cursor_on_delivery_exception(self, fake_kanban, clean_notifier):
        """When start_session_turn raises an exception, the DB cursor is
        rewound so the event is retried on the next tick."""
        fake_kanban.tasks = [FakeTask("t_1", "Test", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {"reason": "x"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert fake_kanban.subs[0]["last_event_id"] == 1

        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", side_effect=Exception("RPC failed")), \
             patch("api.background_process.record_deferred_wakeup"):
            clean_notifier._deliver(deliveries)

        assert fake_kanban.subs[0]["last_event_id"] == 0

    def test_retry_succeeds_after_rewind(self, fake_kanban, clean_notifier):
        """After a cursor rewind, the next _poll_once should re-claim the
        same events and successfully deliver them."""
        fake_kanban.tasks = [FakeTask("t_1", "Test", "blocked", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {"reason": "x"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1

        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", side_effect=Exception("transient")), \
             patch("api.background_process.record_deferred_wakeup"):
            clean_notifier._deliver(deliveries)

        deliveries2 = clean_notifier._poll_once()
        assert len(deliveries2) == 1
        assert len(deliveries2[0]["events"]) == 1

        mock_resp = {"_status": 200, "stream_id": "s1"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp) as mock_turn:
            clean_notifier._deliver(deliveries2)
            mock_turn.assert_called_once()

    def test_does_not_unsub_on_delivery_failure_for_terminal_task(
        self, fake_kanban, clean_notifier
    ):
        """When delivery fails for a done/archived task, the subscription
        must NOT be removed — the cursor was rewound and the retry needs
        the sub to still exist."""
        fake_kanban.tasks = [FakeTask("t_1", "Done Task", "done", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "completed", {"summary": "ok"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1

        mock_resp = {"_status": 500, "error": "internal"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp), \
             patch("api.background_process.record_deferred_wakeup"):
            clean_notifier._deliver(deliveries)

        assert len(fake_kanban.subs) == 1
        assert fake_kanban.subs[0]["last_event_id"] == 0

    def test_dedup_key_cleared_on_terminal_unsub(self, fake_kanban, clean_notifier):
        """When a terminal task's subscription is removed, the in-memory
        dedup key should also be removed to prevent unbounded growth."""
        fake_kanban.tasks = [FakeTask("t_1", "Done", "done", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "completed", {}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()

        mock_resp = {"_status": 200, "stream_id": "s1"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp), \
             patch("api.background_process.record_deferred_wakeup"):
            clean_notifier._deliver(deliveries)

        key = clean_notifier._dedup_key("default", "t_1", "sess-1")
        assert key not in clean_notifier._DELIVERED_EVENTS

    def test_none_assignee_renders_empty(self, fake_kanban, clean_notifier):
        """None assignee should render as empty string, not @None."""
        prompt = clean_notifier._format_wakeup_prompt(
            "t_1", "Test", None, "default", {"blocked"}
        )
        assert "@None" not in prompt

    def test_deliver_when_task_is_none(self, fake_kanban, clean_notifier):
        """When get_task returns None (task deleted), _deliver should still
        work without AttributeError — title falls back to task_id, assignee
        is empty, and the prompt is delivered."""
        fake_kanban.tasks = []  # No tasks — get_task returns None
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "blocked", {"reason": "x"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1
        assert deliveries[0]["task"] is None

        mock_resp = {"_status": 200, "stream_id": "s1"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp) as mock_turn:
            clean_notifier._deliver(deliveries)
            mock_turn.assert_called_once()
            prompt = mock_turn.call_args[0][1]
            assert "t_1" in prompt
            assert "@None" not in prompt

    def test_silent_terminal_event_cleans_up_subscription(
        self, fake_kanban, clean_notifier
    ):
        """A task that reaches done via a non-wake event (e.g. 'status')
        should NOT trigger a wakeup prompt, but SHOULD clean up the
        subscription and dedup entry — no stale-sub leak."""
        fake_kanban.tasks = [FakeTask("t_1", "Done Task", "done", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "status", {"to": "done"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1

        with patch("api.routes.start_session_turn") as mock_turn:
            clean_notifier._deliver(deliveries)
            # No prompt should be sent for a silent terminal event
            mock_turn.assert_not_called()

        # Subscription should be removed (cleanup ran)
        assert len(fake_kanban.subs) == 0
        # Dedup key should be cleaned up too
        key = clean_notifier._dedup_key("default", "t_1", "sess-1")
        assert key not in clean_notifier._DELIVERED_EVENTS

    def test_silent_terminal_event_no_cleanup_for_non_terminal(
        self, fake_kanban, clean_notifier
    ):
        """A silent event (e.g. 'status') for a non-terminal task should
        neither prompt nor clean up — just skip."""
        fake_kanban.tasks = [FakeTask("t_1", "Running Task", "running", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "status", {"to": "running"}, 100),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1

        with patch("api.routes.start_session_turn") as mock_turn:
            clean_notifier._deliver(deliveries)
            mock_turn.assert_not_called()

        # Subscription should still exist (task is not terminal)
        assert len(fake_kanban.subs) == 1

    def test_mixed_wake_and_silent_events(self, fake_kanban, clean_notifier):
        """When a delivery contains both wake and silent kinds, only the
        wake kinds should appear in the prompt, and cleanup should still
        run if the task is terminal."""
        fake_kanban.tasks = [FakeTask("t_1", "Done Task", "done", "w")]
        fake_kanban.events = [
            FakeEvent(1, "t_1", None, "completed", {"summary": "ok"}, 100),
            FakeEvent(2, "t_1", None, "status", {"to": "done"}, 101),
        ]
        fake_kanban.subs = [_make_sub(task_id="t_1", chat_id="sess-1")]

        deliveries = clean_notifier._poll_once()
        assert len(deliveries) == 1
        assert len(deliveries[0]["events"]) == 2

        mock_resp = {"_status": 200, "stream_id": "s1"}
        with patch("api.background_process._session_has_active_turn", return_value=False), \
             patch("api.routes.start_session_turn", return_value=mock_resp) as mock_turn:
            clean_notifier._deliver(deliveries)
            mock_turn.assert_called_once()
            prompt = mock_turn.call_args[0][1]
            # Only "completed" should be in the prompt, not "status"
            assert "completed" in prompt
            assert "status changed" not in prompt

        # Cleanup should run (task is done)
        assert len(fake_kanban.subs) == 0


class TestThreadLifecycle:
    def test_start_and_stop(self, clean_notifier):
        assert clean_notifier.start_notifier_thread() is True
        assert clean_notifier.start_notifier_thread() is False  # already running
        clean_notifier.stop_notifier_thread(timeout=1)
        # After stop, can start again
        assert clean_notifier.start_notifier_thread() is True
        clean_notifier.stop_notifier_thread(timeout=1)
