"""Kanban read-only bridge tests.

The first upstream WebUI Kanban integration is intentionally read-only: it
surfaces Hermes Agent Kanban data under /api/kanban/* while keeping the Agent
kanban database as the only source of truth.

CI for hermes-webui does not install hermes-agent, so these tests inject a tiny
fake ``hermes_cli.kanban_db`` module and verify the bridge contract without
requiring the external package.
"""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace


@dataclass
class FakeTask:
    id: str
    title: str
    status: str = "ready"
    assignee: str | None = None
    tenant: str | None = None
    priority: int = 0


@dataclass
class FakeEvent:
    id: int
    task_id: str
    run_id: str | None
    kind: str
    payload: dict | None
    created_at: int


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class FakeConn:
    def __init__(self, tasks, events):
        self.tasks = tasks
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        if "MAX(id)" in sql:
            latest = max((event.id for event in self.events), default=0)
            return SimpleNamespace(fetchone=lambda: FakeRow(latest=latest))
        if "FROM task_links" in sql:
            return SimpleNamespace(fetchall=lambda: [])
        if "FROM task_comments" in sql:
            return SimpleNamespace(fetchall=lambda: [])
        if "FROM task_events WHERE id >" in sql:
            since, limit = params
            rows = [
                FakeRow(
                    id=e.id,
                    task_id=e.task_id,
                    run_id=e.run_id,
                    kind=e.kind,
                    payload='{"status":"ready"}' if e.payload else None,
                    created_at=e.created_at,
                )
                for e in self.events
                if e.id > since
            ][:limit]
            return SimpleNamespace(fetchall=lambda: rows)
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeKanbanDB:
    def __init__(self):
        self.tasks = [
            FakeTask("t_1", "Read-only board target", "ready", "webui-test"),
            FakeTask("t_2", "Blocked target", "blocked", "other"),
        ]
        self.events = [FakeEvent(7, "t_1", None, "created", {"status": "ready"}, 123)]

    def init_db(self):
        return None

    def connect(self):
        return FakeConn(self.tasks, self.events)

    def list_tasks(self, conn, tenant=None, assignee=None, include_archived=False):
        tasks = list(conn.tasks)
        if tenant:
            tasks = [task for task in tasks if task.tenant == tenant]
        if assignee:
            tasks = [task for task in tasks if task.assignee == assignee]
        if not include_archived:
            tasks = [task for task in tasks if task.status != "archived"]
        return tasks

    def get_task(self, conn, task_id):
        return next((task for task in conn.tasks if task.id == task_id), None)

    def task_age(self, task):
        return 42

    def list_comments(self, conn, task_id):
        return []

    def list_events(self, conn, task_id):
        return [event for event in self.events if event.task_id == task_id]

    def list_runs(self, conn, task_id):
        return []

    def parent_ids(self, conn, task_id):
        return []

    def child_ids(self, conn, task_id):
        return []


def _load_bridge(monkeypatch):
    fake_kanban = FakeKanbanDB()
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.kanban_db = fake_kanban
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kanban)
    import api.kanban_bridge as bridge

    return importlib.reload(bridge)


def _parsed(path="/api/kanban/board", query=""):
    return SimpleNamespace(path=path, query=query)


def test_kanban_board_payload_exposes_read_only_board(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    data = bridge._board_payload(_parsed())

    assert "columns" in data
    assert "latest_event_id" in data
    assert data["read_only"] is True
    names = [column["name"] for column in data["columns"]]
    for expected in ("triage", "todo", "ready", "running", "blocked", "done"):
        assert expected in names
    all_tasks = [task for column in data["columns"] for task in column["tasks"]]
    assert any(task["id"] == "t_1" and task["title"] == "Read-only board target" for task in all_tasks)


def test_kanban_task_detail_payload_exposes_comments_events_links_and_runs(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    data = bridge._task_detail_payload("t_1")

    assert data["task"]["id"] == "t_1"
    assert data["task"]["title"] == "Read-only board target"
    assert set(data) >= {"task", "comments", "events", "links", "runs", "read_only"}
    assert data["read_only"] is True
    assert isinstance(data["comments"], list)
    assert isinstance(data["events"], list)
    assert isinstance(data["links"], dict)
    assert isinstance(data["runs"], list)


def test_kanban_board_since_returns_lightweight_unchanged_payload(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    unchanged = bridge._board_payload(_parsed(query="since=7"))

    assert unchanged == {"changed": False, "latest_event_id": 7, "read_only": True}


def test_kanban_events_payload_matches_polling_shape(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    events = bridge._events_payload(_parsed(path="/api/kanban/events", query="since=0"))

    assert events["cursor"] == 7
    assert events["latest_event_id"] == 7
    assert events["read_only"] is True
    assert events["events"][0]["task_id"] == "t_1"
    assert {"id", "task_id", "run_id", "kind", "payload", "created_at"} <= set(events["events"][0])


def test_routes_dispatches_api_kanban_get_to_bridge():
    src = open("api/routes.py", encoding="utf-8").read()
    assert 'parsed.path.startswith("/api/kanban/")' in src
    assert "handle_kanban_get(handler, parsed)" in src
