"""Regression tests for WebUI Kanban wakeup delivery."""

from __future__ import annotations

import sys
import threading
import types
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

import pytest


@dataclass
class FakeTask:
    id: str
    title: str
    status: str = "ready"
    assignee: str | None = None


@dataclass
class FakeEvent:
    id: int
    task_id: str
    kind: str
    payload: dict | None = None


class FakeConn:
    def __init__(self, board):
        self.board = board

    def close(self):
        return None


class FakeKanbanDB:
    DEFAULT_BOARD = "default"

    def __init__(self):
        self.data = {
            "default": {"tasks": {}, "events": [], "subs": []},
        }
        self.fail_claim_for: set[str] = set()
        self.fail_remove_count = 0
        self.removed: list[tuple[str, str, str]] = []

    def add_board(self, slug):
        self.data[slug] = {"tasks": {}, "events": [], "subs": []}

    def list_boards(self, *, include_archived=False):
        return [{"slug": slug} for slug in self.data]

    def read_board_metadata(self, slug):
        return {"slug": slug}

    def kanban_db_path(self, slug):
        from pathlib import Path

        return Path(f"/tmp/{slug}.db")

    def connect(self, *, board=None):
        slug = board or self.DEFAULT_BOARD
        if slug not in self.data:
            raise RuntimeError(f"unknown board {slug}")
        return FakeConn(slug)

    def list_notify_subs(self, conn):
        return list(self.data[conn.board]["subs"])

    def claim_unseen_events_for_sub(
        self,
        conn,
        *,
        task_id,
        platform,
        chat_id,
        thread_id=None,
        kinds=None,
    ):
        if task_id in self.fail_claim_for:
            raise RuntimeError("broken subscription")
        sub = next(
            (
                item
                for item in self.data[conn.board]["subs"]
                if item["task_id"] == task_id
                and item["platform"] == platform
                and item["chat_id"] == chat_id
                and (item.get("thread_id") or "") == (thread_id or "")
            ),
            None,
        )
        if sub is None:
            return 0, 0, []
        old_cursor = int(sub.get("last_event_id") or 0)
        events = [
            event
            for event in self.data[conn.board]["events"]
            if event.task_id == task_id
            and event.id > old_cursor
            and (not kinds or event.kind in kinds)
        ]
        if not events:
            return old_cursor, old_cursor, []
        new_cursor = max(event.id for event in events)
        sub["last_event_id"] = new_cursor
        return old_cursor, new_cursor, events

    def unseen_events_for_sub(
        self,
        conn,
        *,
        task_id,
        platform,
        chat_id,
        thread_id=None,
        kinds=None,
    ):
        old_cursor, new_cursor, events = self.claim_unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            kinds=kinds,
        )
        sub = next(
            item
            for item in self.data[conn.board]["subs"]
            if item["task_id"] == task_id
            and item["platform"] == platform
            and item["chat_id"] == chat_id
        )
        sub["last_event_id"] = old_cursor
        return new_cursor, events

    def advance_notify_cursor(
        self,
        conn,
        *,
        task_id,
        platform,
        chat_id,
        thread_id=None,
        new_cursor,
    ):
        sub = next(
            item
            for item in self.data[conn.board]["subs"]
            if item["task_id"] == task_id
            and item["platform"] == platform
            and item["chat_id"] == chat_id
        )
        sub["last_event_id"] = new_cursor

    def rewind_notify_cursor(
        self,
        conn,
        *,
        task_id,
        platform,
        chat_id,
        thread_id=None,
        claimed_cursor,
        old_cursor,
    ):
        sub = next(
            (
                item
                for item in self.data[conn.board]["subs"]
                if item["task_id"] == task_id
                and item["platform"] == platform
                and item["chat_id"] == chat_id
                and (item.get("thread_id") or "") == (thread_id or "")
            ),
            None,
        )
        if sub is None or sub.get("last_event_id") != claimed_cursor:
            return False
        sub["last_event_id"] = old_cursor
        return True

    def get_task(self, conn, task_id):
        return self.data[conn.board]["tasks"].get(task_id)

    def remove_notify_sub(
        self,
        conn,
        *,
        task_id,
        platform,
        chat_id,
        thread_id=None,
    ):
        if self.fail_remove_count:
            self.fail_remove_count -= 1
            raise OSError("database busy")
        subs = self.data[conn.board]["subs"]
        before = len(subs)
        subs[:] = [
            item
            for item in subs
            if not (
                item["task_id"] == task_id
                and item["platform"] == platform
                and item["chat_id"] == chat_id
                and (item.get("thread_id") or "") == (thread_id or "")
            )
        ]
        self.removed.append((conn.board, task_id, chat_id))
        return len(subs) != before


def make_sub(task_id="t_1", *, platform="webui", chat_id="session-1", cursor=0):
    return {
        "task_id": task_id,
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": "",
        "last_event_id": cursor,
    }


@pytest.fixture
def fake_kanban(monkeypatch):
    fake = FakeKanbanDB()
    package = types.ModuleType("hermes_cli")
    package.kanban_db = fake
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake)
    return fake


@pytest.fixture
def notifier(fake_kanban):
    from api import kanban_notifier

    kanban_notifier.stop_notifier_thread(timeout=0.2)
    yield kanban_notifier
    kanban_notifier.stop_notifier_thread(timeout=0.2)


def test_collects_webui_wake_events_across_boards(fake_kanban, notifier):
    fake_kanban.add_board("project-b")
    fake_kanban.data["default"]["tasks"]["t_1"] = FakeTask(
        "t_1", "Blocked work", "blocked", "kanban"
    )
    fake_kanban.data["default"]["events"].append(
        FakeEvent(1, "t_1", "blocked", {"reason": "needs input"})
    )
    fake_kanban.data["default"]["subs"].append(make_sub("t_1", chat_id="session-a"))
    fake_kanban.data["project-b"]["tasks"]["t_2"] = FakeTask(
        "t_2", "Finished work", "done", "reviewer"
    )
    fake_kanban.data["project-b"]["events"].append(
        FakeEvent(7, "t_2", "completed", {"summary": "ready"})
    )
    fake_kanban.data["project-b"]["subs"].append(make_sub("t_2", chat_id="session-b"))

    deliveries = notifier.collect_once()

    assert [(item.board, item.session_id) for item in deliveries] == [
        ("default", "session-a"),
        ("project-b", "session-b"),
    ]
    assert fake_kanban.data["default"]["subs"][0]["last_event_id"] == 0
    assert fake_kanban.data["project-b"]["subs"][0]["last_event_id"] == 0


def test_bad_subscription_does_not_block_other_subscriptions(fake_kanban, notifier):
    fake_kanban.data["default"]["events"].extend(
        [
            FakeEvent(1, "bad", "blocked"),
            FakeEvent(2, "good", "blocked"),
        ]
    )
    fake_kanban.data["default"]["subs"].extend(
        [make_sub("bad", chat_id="session-bad"), make_sub("good", chat_id="session-good")]
    )
    fake_kanban.fail_claim_for.add("bad")

    deliveries = notifier.collect_once()

    assert [item.task_id for item in deliveries] == ["good"]
    assert fake_kanban.data["default"]["subs"][0]["last_event_id"] == 0


def test_non_webui_and_empty_session_subscriptions_are_not_claimed(fake_kanban, notifier):
    fake_kanban.data["default"]["events"].append(FakeEvent(1, "t_1", "blocked"))
    telegram = make_sub("t_1", platform="telegram", chat_id="42")
    empty = make_sub("t_1", chat_id="")
    fake_kanban.data["default"]["subs"].extend([telegram, empty])

    assert notifier.collect_once() == []
    assert telegram["last_event_id"] == 0
    assert empty["last_event_id"] == 0


def test_failed_turn_start_rewinds_claim(fake_kanban, notifier):
    task = FakeTask("t_1", "Blocked", "blocked", "kanban")
    sub = make_sub("t_1")
    fake_kanban.data["default"]["tasks"][task.id] = task
    fake_kanban.data["default"]["events"].append(FakeEvent(4, task.id, "blocked"))
    fake_kanban.data["default"]["subs"].append(sub)
    delivery = notifier.collect_once()[0]

    with patch("api.background_process._session_has_active_turn", return_value=False), patch(
        "api.routes.start_session_turn", return_value={"_status": 500, "error": "failed"}
    ):
        assert notifier.deliver_one(delivery) is False

    assert sub["last_event_id"] == 0


def test_accepted_turn_with_failed_cursor_advance_is_retried(
    fake_kanban, notifier, monkeypatch
):
    task = FakeTask("t_1", "Done", "done", "kanban")
    sub = make_sub("t_1")
    fake_kanban.data["default"]["tasks"][task.id] = task
    fake_kanban.data["default"]["events"].append(FakeEvent(4, task.id, "completed"))
    fake_kanban.data["default"]["subs"].append(sub)
    delivery = notifier.collect_once()[0]

    def fail_advance(*_args, **_kwargs):
        raise OSError("disk busy")

    monkeypatch.setattr(fake_kanban, "advance_notify_cursor", fail_advance)
    with patch("api.background_process._session_has_active_turn", return_value=False), patch(
        "api.routes.start_session_turn",
        return_value={"_status": 200, "stream_id": "stream-1"},
    ):
        assert notifier.deliver_one(delivery) is False

    assert sub["last_event_id"] == 0
    assert fake_kanban.data["default"]["subs"] == [sub]


def test_retry_after_cursor_failure_reuses_durable_delivery_key(
    fake_kanban, notifier, monkeypatch
):
    task = FakeTask("t_1", "Done", "done", "kanban")
    sub = make_sub("t_1")
    fake_kanban.data["default"]["tasks"][task.id] = task
    fake_kanban.data["default"]["events"].append(FakeEvent(4, task.id, "completed"))
    fake_kanban.data["default"]["subs"].append(sub)
    real_advance = fake_kanban.advance_notify_cursor
    advances = 0
    started_keys = []
    started_effects = []

    def flaky_advance(*args, **kwargs):
        nonlocal advances
        advances += 1
        if advances == 1:
            raise OSError("disk busy")
        return real_advance(*args, **kwargs)

    def idempotent_start(_session_id, _prompt, *, source, idempotency_key):
        assert source == "process_wakeup"
        started_keys.append(idempotency_key)
        if idempotency_key not in started_effects:
            started_effects.append(idempotency_key)
        return {"_status": 200, "stream_id": "stream-1"}

    monkeypatch.setattr(fake_kanban, "advance_notify_cursor", flaky_advance)
    with patch("api.background_process._session_has_active_turn", return_value=False), patch(
        "api.routes.start_session_turn", side_effect=idempotent_start
    ):
        assert notifier.deliver_one(notifier.collect_once()[0]) is False
        assert notifier.deliver_one(notifier.collect_once()[0]) is True

    assert started_keys == ["kanban:default:t_1:4", "kanban:default:t_1:4"]
    assert started_effects == ["kanban:default:t_1:4"]
    assert sub["last_event_id"] == 4


def test_active_session_leaves_cursor_for_durable_retry(fake_kanban, notifier):
    sub = make_sub("t_1")
    fake_kanban.data["default"]["events"].append(FakeEvent(3, "t_1", "blocked"))
    fake_kanban.data["default"]["subs"].append(sub)
    delivery = notifier.collect_once()[0]

    with patch("api.background_process._session_has_active_turn", return_value=True):
        assert notifier.deliver_one(delivery) is False
    assert sub["last_event_id"] == 0

def test_later_events_for_same_task_use_distinct_wakeup_cursors(fake_kanban, notifier):
    sub = make_sub("t_1")
    fake_kanban.data["default"]["subs"].append(sub)
    fake_kanban.data["default"]["events"].append(FakeEvent(3, "t_1", "blocked"))
    first = notifier.collect_once()[0]
    fake_kanban.data["default"]["events"].append(FakeEvent(9, "t_1", "crashed"))

    prompts = []

    def start(_session_id, prompt, *, source, idempotency_key):
        prompts.append((prompt, source, idempotency_key))
        return {"_status": 200, "stream_id": f"stream-{len(prompts)}"}

    with patch("api.background_process._session_has_active_turn", return_value=False), patch(
        "api.routes.start_session_turn", side_effect=start
    ):
        assert notifier.deliver_one(first) is True
        second = notifier.collect_once()[0]
        assert notifier.deliver_one(second) is True

    assert [("Event cursor: 3" in prompt, source, key) for prompt, source, key in prompts] == [
        (True, "process_wakeup", "kanban:default:t_1:3"),
        (False, "process_wakeup", "kanban:default:t_1:9"),
    ]
    assert "Event cursor: 9" in prompts[1][0]


def test_terminal_subscription_is_removed_only_after_acceptance(fake_kanban, notifier):
    task = FakeTask("t_1", "Done", "done", "kanban")
    sub = make_sub("t_1")
    fake_kanban.data["default"]["tasks"][task.id] = task
    fake_kanban.data["default"]["events"].append(FakeEvent(2, task.id, "completed"))
    fake_kanban.data["default"]["subs"].append(sub)
    delivery = notifier.collect_once()[0]

    with patch("api.background_process._session_has_active_turn", return_value=False), patch(
        "api.routes.start_session_turn", return_value={"_status": 200, "stream_id": "stream-1"}
    ):
        assert notifier.deliver_one(delivery) is True

    assert fake_kanban.data["default"]["subs"] == []
    assert fake_kanban.removed == [("default", "t_1", "session-1")]


def test_terminal_subscription_cleanup_is_retried_after_transient_failure(
    fake_kanban, notifier
):
    task = FakeTask("t_1", "Done", "done", "kanban")
    sub = make_sub("t_1")
    fake_kanban.data["default"]["tasks"][task.id] = task
    fake_kanban.data["default"]["events"].append(FakeEvent(2, task.id, "completed"))
    fake_kanban.data["default"]["subs"].append(sub)
    fake_kanban.fail_remove_count = 1

    with patch("api.background_process._session_has_active_turn", return_value=False), patch(
        "api.routes.start_session_turn", return_value={"_status": 200, "stream_id": "stream-1"}
    ):
        assert notifier.deliver_one(notifier.collect_once()[0]) is True

    assert sub["last_event_id"] == 2
    assert fake_kanban.data["default"]["subs"] == [sub]
    cleanup = notifier.collect_once()
    assert len(cleanup) == 1
    assert cleanup[0].events == ()
    assert notifier.deliver_one(cleanup[0]) is True
    assert fake_kanban.data["default"]["subs"] == []


def test_parallel_starts_create_only_one_notifier_thread(notifier, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def loop():
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(notifier, "_notifier_enabled", lambda: True)
    monkeypatch.setattr(notifier, "_notifier_loop", loop)
    results = []

    def start():
        results.append(notifier.start_notifier_thread())

    callers = [threading.Thread(target=start) for _ in range(12)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=2)

    assert entered.wait(timeout=1)
    assert results.count(True) == 1
    assert results.count(False) == 11
    release.set()


def test_session_channel_runtime_owns_notifier_lifecycle(notifier, monkeypatch):
    from api import background_process

    calls = []

    class RunningThread:
        def is_alive(self):
            return True

        def join(self, *, timeout):
            calls.append(("join", timeout))

    monkeypatch.setattr(background_process, "_REAPER_THREAD", RunningThread())
    monkeypatch.setattr(notifier, "start_notifier_thread", lambda: calls.append("start"))
    monkeypatch.setattr(
        notifier, "stop_notifier_thread", lambda *, timeout: calls.append(("stop", timeout))
    )

    assert background_process.start_session_channel_reaper() is False
    background_process.stop_session_channel_reaper(timeout=0.25)

    assert calls == ["start", ("join", 0.25), ("stop", 0.25)]


def test_poll_holds_process_lock_across_read_and_delivery(notifier, monkeypatch):
    order = []

    @contextmanager
    def process_lock():
        order.append("lock-enter")
        yield
        order.append("lock-exit")

    monkeypatch.setattr(notifier, "_notifier_process_lock", process_lock)
    monkeypatch.setattr(
        notifier, "collect_once", lambda: order.append("collect") or [object()]
    )
    monkeypatch.setattr(
        notifier, "deliver_one", lambda _delivery: order.append("deliver") or True
    )

    assert notifier.poll_and_deliver_once() == 1
    assert order == ["lock-enter", "collect", "deliver", "lock-exit"]


def test_process_lock_path_is_anchored_to_shared_kanban_db(
    fake_kanban, notifier, monkeypatch, tmp_path
):
    shared_db = tmp_path / "shared-kanban" / "kanban.db"
    monkeypatch.setattr(fake_kanban, "kanban_db_path", lambda _board: shared_db)

    from api import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui-a")
    first = notifier._notifier_lock_path()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui-b")
    second = notifier._notifier_lock_path()

    assert first == second
    assert first.parent == shared_db.parent.resolve()
    assert first.name == ".kanban-notifier.lock"


def test_notifier_is_opt_in(notifier, monkeypatch):
    monkeypatch.setattr(notifier, "_notifier_enabled", lambda: False)

    assert notifier.start_notifier_thread() is False


def test_notifier_can_be_enabled_declaratively_by_environment(notifier, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_KANBAN_NOTIFIER_ENABLED", "true")

    assert notifier._notifier_enabled() is True


def test_wakeup_prompt_exposes_kanban_display_metadata(notifier):
    from api.process_event_utils import wakeup_display_meta

    delivery = notifier.Delivery(
        board="project-a",
        sub=make_sub("t_42"),
        task=FakeTask("t_42", "Needs review", "blocked", "reviewer"),
        events=(FakeEvent(8, "t_42", "blocked", {"reason": "decision needed"}),),
        new_cursor=8,
    )

    meta = wakeup_display_meta(notifier.format_wakeup_prompt(delivery))

    assert meta == {
        "type": "kanban",
        "task_id": "t_42",
        "title": "Needs review",
        "assignee": "@reviewer",
        "board": "project-a",
        "event_cursor": 8,
    }


def test_process_wakeup_renderer_has_a_kanban_variant():
    from pathlib import Path

    ui = (Path(__file__).resolve().parents[1] / "static" / "ui.js").read_text(
        encoding="utf-8"
    )

    assert "const isKanban=info.type==='kanban';" in ui
    assert "isKanban?'Kanban':t('process_wakeup_label')" in ui
