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
import time
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
    body: str | None = None


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
        if "SELECT status, assignee, COUNT(*) AS n FROM tasks" in sql:
            rows = []
            grouped = {}
            for task in self.tasks:
                if task.status == "archived":
                    continue
                key = (task.status, task.assignee)
                grouped[key] = grouped.get(key, 0) + 1
            for (status, assignee), n in grouped.items():
                rows.append(FakeRow(status=status, assignee=assignee, n=n))
            return SimpleNamespace(fetchall=lambda: rows)
        if "SELECT DISTINCT assignee FROM tasks" in sql:
            rows = [FakeRow(assignee=a) for a in sorted({t.assignee for t in self.tasks if t.assignee})]
            return SimpleNamespace(fetchall=lambda: rows)
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
        if sql.startswith("UPDATE tasks SET "):
            fields = [part.strip().split(" = ")[0] for part in sql[len("UPDATE tasks SET "):].split(" WHERE id = ")[0].split(",")]
            *values, task_id = params
            task = next((task for task in self.tasks if task.id == task_id), None)
            if task:
                for field, value in zip(fields, values):
                    setattr(task, field, value)
            return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None)
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeKanbanDB:
    def __init__(self):
        self.tasks = [
            FakeTask("t_1", "Read-only board target", "ready", "webui-test", tenant="webui"),
            FakeTask("t_2", "Blocked target", "blocked", "other", tenant="ops"),
        ]
        self.events = [FakeEvent(7, "t_1", None, "created", {"status": "ready"}, 123)]
        self.comments = []
        self.links = []
        self.next_id = 3
        self.next_event_id = 8

    def init_db(self, *, board=None):
        # board param accepted but ignored — the fake stores everything
        # in a single in-memory list for test simplicity. Real kanban_db
        # uses the param to pick which sqlite file to open.
        return None

    def connect(self, *, board=None):
        return FakeConn(self.tasks, self.events)

    def list_tasks(self, conn, tenant=None, assignee=None, include_archived=False, **_kwargs):
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
        return [comment for comment in self.comments if comment.task_id == task_id]

    def list_events(self, conn, task_id):
        return [event for event in self.events if event.task_id == task_id]

    def list_runs(self, conn, task_id):
        return []

    def parent_ids(self, conn, task_id):
        return [parent for parent, child in self.links if child == task_id]

    def child_ids(self, conn, task_id):
        return [child for parent, child in self.links if parent == task_id]

    def _event(self, task_id, kind, payload=None):
        self.events.append(FakeEvent(self.next_event_id, task_id, None, kind, payload or {}, 456))
        self.next_event_id += 1

    def create_task(self, conn, **kwargs):
        task_id = f"t_{self.next_id}"
        self.next_id += 1
        status = "triage" if kwargs.get("triage") else "ready"
        task = FakeTask(
            task_id,
            kwargs["title"],
            status,
            kwargs.get("assignee"),
            kwargs.get("tenant"),
            int(kwargs.get("priority") or 0),
            kwargs.get("body"),
        )
        self.tasks.append(task)
        self._event(task_id, "created", {"status": status})
        return task_id

    def assign_task(self, conn, task_id, assignee):
        task = self.get_task(conn, task_id)
        if not task:
            return False
        task.assignee = assignee
        self._event(task_id, "assigned", {"assignee": assignee})
        return True

    def complete_task(self, conn, task_id, result=None, summary=None):
        task = self.get_task(conn, task_id)
        if not task:
            return False
        task.status = "done"
        self._event(task_id, "completed", {"result": result, "summary": summary})
        return True

    def block_task(self, conn, task_id, reason=None):
        task = self.get_task(conn, task_id)
        if not task:
            return False
        task.status = "blocked"
        self._event(task_id, "blocked", {"reason": reason})
        return True

    def archive_task(self, conn, task_id):
        task = self.get_task(conn, task_id)
        if not task:
            return False
        task.status = "archived"
        self._event(task_id, "archived", {})
        return True

    def unblock_task(self, conn, task_id):
        task = self.get_task(conn, task_id)
        if not task:
            return False
        task.status = "ready"
        self._event(task_id, "unblocked", {})
        return True

    def known_assignees(self, conn):
        return sorted({task.assignee for task in conn.tasks if task.assignee})

    def board_stats(self, conn):
        by_status = {}
        by_assignee = {}
        for task in conn.tasks:
            if task.status == "archived":
                continue
            by_status[task.status] = by_status.get(task.status, 0) + 1
            assignee = task.assignee or "unassigned"
            by_assignee[assignee] = by_assignee.get(assignee, 0) + 1
        return {"by_status": by_status, "by_assignee": by_assignee}

    def read_worker_log(self, task_id, tail_bytes=None):
        return f"worker log for {task_id}"

    def worker_log_path(self, task_id):
        from pathlib import Path
        return Path(f"/tmp/hermes-kanban/{task_id}.log")

    def dispatch_once(self, conn, dry_run=False, max_spawn=8):
        return {"dry_run": dry_run, "max_spawn": max_spawn, "spawned": []}

    def add_comment(self, conn, task_id, author, body):
        self.comments.append(SimpleNamespace(id=len(self.comments) + 1, task_id=task_id, author=author, body=body))
        self._event(task_id, "commented", {"author": author})
        return len(self.comments)

    def link_tasks(self, conn, parent_id, child_id):
        if (parent_id, child_id) not in self.links:
            self.links.append((parent_id, child_id))
        self._event(child_id, "linked", {"parent_id": parent_id, "child_id": child_id})
        return True

    def unlink_tasks(self, conn, parent_id, child_id):
        before = len(self.links)
        self.links = [link for link in self.links if link != (parent_id, child_id)]
        return len(self.links) != before

    # ------------------------------------------------------------------
    # Multi-board fakes — these are no-ops on tasks because the fake
    # stores everything in a single in-memory list. They give the bridge
    # enough surface to call the library API and round-trip without
    # touching real disk. Tests that exercise actual board isolation use
    # a FakeKanbanDB instance per board (or just inspect side effects on
    # `self.boards`).
    # ------------------------------------------------------------------
    DEFAULT_BOARD = "default"

    @staticmethod
    def _normalize_board_slug(slug):
        if slug is None:
            return None
        s = str(slug).strip().lower().replace(" ", "-")
        # Reject anything that would be a path-traversal vector or
        # contains characters outside the allowed alnum/dash/underscore set.
        if not s:
            return None
        if any(c in s for c in ("/", "\\", "..")):
            raise ValueError(f"invalid board slug: {slug!r}")
        return s

    def board_exists(self, slug):
        return slug == "default" or slug in getattr(self, "boards", {})

    def list_boards(self, *, include_archived=True):
        boards = getattr(self, "boards", None)
        if boards is None:
            self.boards = {"default": {"slug": "default", "name": "Default board", "archived": False}}
            boards = self.boards
        out = []
        for slug, meta in boards.items():
            if not include_archived and meta.get("archived"):
                continue
            out.append(dict(meta))
        return out

    def create_board(self, slug, *, name=None, description=None, icon=None, color=None):
        boards = getattr(self, "boards", None)
        if boards is None:
            self.boards = {"default": {"slug": "default", "name": "Default board", "archived": False}}
            boards = self.boards
        normed = self._normalize_board_slug(slug)
        if not normed:
            raise ValueError("slug is required")
        if normed in boards:
            return dict(boards[normed])
        meta = {
            "slug": normed,
            "name": name or normed,
            "description": description or "",
            "icon": icon or "",
            "color": color or "",
            "archived": False,
        }
        boards[normed] = meta
        return dict(meta)

    def write_board_metadata(self, slug, *, name=None, description=None, icon=None, color=None, archived=None):
        boards = getattr(self, "boards", None) or {}
        if slug not in boards:
            raise LookupError(f"board {slug!r} does not exist")
        meta = dict(boards[slug])
        if name is not None: meta["name"] = name
        if description is not None: meta["description"] = description
        if icon is not None: meta["icon"] = icon
        if color is not None: meta["color"] = color
        if archived is not None: meta["archived"] = bool(archived)
        boards[slug] = meta
        return dict(meta)

    def remove_board(self, slug, *, archive=True):
        boards = getattr(self, "boards", None) or {}
        if slug not in boards:
            raise LookupError(f"board {slug!r} does not exist")
        if archive:
            boards[slug]["archived"] = True
            return dict(boards[slug])
        return boards.pop(slug)

    def get_current_board(self):
        return getattr(self, "_current_board", "default")

    def set_current_board(self, slug):
        normed = self._normalize_board_slug(slug)
        if not normed:
            raise ValueError("slug is required")
        self._current_board = normed
        return None

    def clear_current_board(self):
        if hasattr(self, "_current_board"):
            del self._current_board

    def read_board_metadata(self, slug):
        boards = getattr(self, "boards", None) or {}
        return dict(boards.get(slug, {"slug": slug, "name": slug, "archived": False}))


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
    # The bridge has been writable since #1649; this PR makes the read_only
    # flag honest (was hardcoded True even when fully writable).
    assert data["read_only"] is False
    names = [column["name"] for column in data["columns"]]
    for expected in ("triage", "todo", "ready", "running", "blocked", "done"):
        assert expected in names
    all_tasks = [task for column in data["columns"] for task in column["tasks"]]
    assert any(task["id"] == "t_1" and task["title"] == "Read-only board target" for task in all_tasks)


def test_board_pointer_drift_falls_back_to_default(monkeypatch):
    bridge = _load_bridge(monkeypatch)
    fake_kanban = sys.modules["hermes_cli.kanban_db"]
    fake_kanban.boards = {
        "default": {"slug": "default", "name": "Default board", "archived": False},
        "active": {"slug": "active", "name": "Active board", "archived": False},
    }
    fake_kanban.set_current_board("ghost")

    data = bridge._list_boards_payload(_parsed(path="/api/kanban/boards"))

    assert data["current"] == "default"
    assert fake_kanban.get_current_board() == "default"
    assert any(board["slug"] == "default" and board["is_current"] for board in data["boards"])


def test_kanban_task_detail_payload_exposes_comments_events_links_and_runs(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    data = bridge._task_detail_payload("t_1")

    assert data["task"]["id"] == "t_1"
    assert data["task"]["title"] == "Read-only board target"
    assert set(data) >= {"task", "comments", "events", "links", "runs", "read_only"}
    assert data["read_only"] is False
    assert isinstance(data["comments"], list)
    assert isinstance(data["events"], list)
    assert isinstance(data["links"], dict)
    assert isinstance(data["runs"], list)



def test_kanban_create_task_payload_writes_to_agent_kanban(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    data = bridge._create_task_payload({
        "title": "Write API target",
        "body": "Created from WebUI",
        "assignee": "webui-test",
        "tenant": "webui",
        "priority": 2,
    })

    assert data["read_only"] is False
    assert data["task"]["title"] == "Write API target"
    assert data["task"]["assignee"] == "webui-test"
    assert data["task"]["tenant"] == "webui"
    assert data["task"]["priority"] == 2


def test_kanban_patch_task_payload_updates_status_title_and_comment(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    created = bridge._create_task_payload({"title": "Patch target"})
    task_id = created["task"]["id"]
    patched = bridge._patch_task_payload(task_id, {"title": "Patched target", "status": "done"})
    comment = bridge._comment_payload(task_id, {"author": "webui", "body": "Looks done"})
    detail = bridge._task_detail_payload(task_id)

    assert patched["read_only"] is False
    assert patched["task"]["title"] == "Patched target"
    assert patched["task"]["status"] == "done"
    assert comment == {"ok": True, "comment_id": 1, "read_only": False}
    assert detail["comments"][0]["body"] == "Looks done"


def test_kanban_link_payload_adds_parent_child_relationship(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    parent = bridge._create_task_payload({"title": "Parent"})["task"]["id"]
    child = bridge._create_task_payload({"title": "Child"})["task"]["id"]
    linked = bridge._link_tasks_payload({"parent_id": parent, "child_id": child})
    detail = bridge._task_detail_payload(child)

    assert linked == {"ok": True, "parent_id": parent, "child_id": child, "read_only": False}
    assert detail["links"]["parents"] == [parent]

def test_kanban_board_since_returns_lightweight_unchanged_payload(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    unchanged = bridge._board_payload(_parsed(query="since=7"))

    assert unchanged == {"changed": False, "latest_event_id": 7, "read_only": False}


def test_kanban_events_payload_matches_polling_shape(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    events = bridge._events_payload(_parsed(path="/api/kanban/events", query="since=0"))

    assert events["cursor"] == 7
    assert events["latest_event_id"] == 7
    assert events["read_only"] is False
    assert events["events"][0]["task_id"] == "t_1"
    assert {"id", "task_id", "run_id", "kind", "payload", "created_at"} <= set(events["events"][0])


def test_routes_dispatches_api_kanban_get_to_bridge():
    src = open("api/routes.py", encoding="utf-8").read()
    assert 'parsed.path.startswith("/api/kanban/")' in src
    assert "handle_kanban_get(handler, parsed)" in src


def test_routes_dispatches_api_kanban_post_to_bridge():
    src = open("api/routes.py", encoding="utf-8").read()
    assert 'parsed.path.startswith("/api/kanban/")' in src
    assert "handle_kanban_post(handler, parsed, body)" in src



def test_kanban_dashboard_core_api_exposes_stats_assignees_config_and_logs(monkeypatch):
    bridge = _load_bridge(monkeypatch)

    stats = bridge._stats_payload()
    assignees = bridge._assignees_payload()
    config = bridge._config_payload()
    log = bridge._task_log_payload(_parsed(path="/api/kanban/tasks/t_1/log", query="tail=64"), "t_1")

    assert stats["by_status"]["ready"] == 1
    assert "webui-test" in assignees["assignees"]
    assert config["columns"]
    assert {"default_tenant", "lane_by_profile", "include_archived_by_default", "render_markdown", "assignees"} <= set(config)
    assert log["task_id"] == "t_1"
    assert log["content"] == "worker log for t_1"


def test_kanban_only_mine_bulk_dispatch_and_block_unblock(monkeypatch):
    bridge = _load_bridge(monkeypatch)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "webui-test", raising=False)

    mine = bridge._board_payload(_parsed(query="only_mine=1"))
    visible_ids = [task["id"] for col in mine["columns"] for task in col["tasks"]]
    bulk = bridge._bulk_tasks_payload({"ids": ["t_1", "t_2"], "status": "done", "priority": 3})
    blocked = bridge._task_action_payload("t_1", {"reason": "waiting"}, "block")
    unblocked = bridge._task_action_payload("t_1", {}, "unblock")
    dispatch = bridge._dispatch_payload(_parsed(path="/api/kanban/dispatch", query="dry_run=1&max=2"))

    assert visible_ids == ["t_1"]
    assert [row["ok"] for row in bulk["results"]] == [True, True]
    assert blocked["task"]["status"] == "blocked"
    assert unblocked["task"]["status"] == "ready"
    assert dispatch["dry_run"] is True
    assert dispatch["max_spawn"] == 2



def test_routes_dispatches_canonical_kanban_patch_and_delete_verbs():
    src = open("api/routes.py", encoding="utf-8").read()
    server = open("server.py", encoding="utf-8").read()
    assert "def do_PATCH" in server
    assert "def do_DELETE" in server
    assert "self._handle_write(handle_patch)" in server
    assert "self._handle_write(handle_delete)" in server
    assert 'parsed.path.startswith("/api/kanban/")' in src
    assert "handle_kanban_patch(handler, parsed, body)" in src
    assert "handle_kanban_delete(handler, parsed, body)" in src


def test_patch_status_running_is_rejected_to_protect_dispatcher_contract(monkeypatch):
    """Bridge must NOT allow status='running' via PATCH.

    The 'running' state is owned by the kanban dispatcher / claim_task path
    (sets claim_lock + claim_expires + started_at + worker_pid). A raw status
    flip would leave the task in a phantom-claimed state the dispatcher would
    treat as orphaned. Mirrors the agent dashboard plugin's contract at
    plugins/kanban/dashboard/plugin_api.py update_task — both surfaces must
    reject this transition.
    """
    bridge = _load_bridge(monkeypatch)
    bridge._OAUTH_FLOWS = getattr(bridge, '_OAUTH_FLOWS', {})  # no-op safe
    # The fake board includes t_1 (ready) — try to PATCH it to 'running'
    try:
        bridge._patch_task_payload("t_1", {"status": "running"})
    except ValueError as exc:
        assert "running" in str(exc).lower()
        return
    raise AssertionError("PATCH status='running' must raise ValueError")


def test_patch_status_done_to_running_is_rejected(monkeypatch):
    """A completed task must not be resurrected to 'running' via PATCH."""
    bridge = _load_bridge(monkeypatch)
    # The fake board includes t_2 (blocked); we'll PATCH any task to 'running'
    try:
        bridge._patch_task_payload("t_2", {"status": "running"})
    except ValueError as exc:
        assert "running" in str(exc).lower()
        return
    raise AssertionError("PATCH status='running' must raise ValueError")


def test_patch_status_blocked_to_ready_routes_through_unblock_task(monkeypatch):
    """blocked → ready transition must call kb.unblock_task (not raw UPDATE).

    kb.unblock_task is the structured verb that fires the 'unblocked' event
    and clears any block-related state. Going through raw UPDATE would skip
    that event firing, so live event polling and worker dispatchers wouldn't
    see the transition.
    """
    bridge = _load_bridge(monkeypatch)
    # Hook into the shared FakeKanbanDB instance
    kb = bridge._kb()
    kb.unblock_calls = []
    original_unblock = kb.unblock_task

    def fake_unblock(conn, task_id):
        kb.unblock_calls.append(task_id)
        return original_unblock(conn, task_id)

    monkeypatch.setattr(kb, "unblock_task", fake_unblock, raising=False)
    # t_2 is blocked in the fake fixture
    bridge._patch_task_payload("t_2", {"status": "ready"})
    assert kb.unblock_calls == ["t_2"], (
        f"blocked → ready transition must call kb.unblock_task; saw: {kb.unblock_calls}"
    )


def test_handle_kanban_get_returns_503_when_hermes_cli_missing(monkeypatch):
    """If hermes_cli is unavailable (webui-only deploy), the bridge must
    return a clean 503 with a `kanban unavailable` body — NOT a 500/exception
    that bubbles up to the user. The frontend's existing try/catch surfaces
    the toast cleanly only when the bridge gives a structured error.
    """
    bridge = _load_bridge(monkeypatch)
    # Force _kb() to raise ImportError as if hermes_cli was uninstalled
    monkeypatch.setattr(
        bridge, "_kb",
        lambda: (_ for _ in ()).throw(ImportError("No module named 'hermes_cli'")),
    )

    captured = {}

    class FakeHandler:
        def __init__(self):
            self.headers = {}
            self.body = None
            self.status = None

    h = FakeHandler()

    def fake_bad(handler, msg, status=400):
        captured["msg"] = msg
        captured["status"] = status
        return True

    monkeypatch.setattr(bridge, "bad", fake_bad)
    parsed = _parsed(path="/api/kanban/board")
    result = bridge.handle_kanban_get(h, parsed)
    assert result is True
    assert captured["status"] == 503
    assert "kanban unavailable" in captured["msg"]


def test_handle_kanban_post_returns_503_when_hermes_cli_missing(monkeypatch):
    """Same fallback contract for POST verb."""
    bridge = _load_bridge(monkeypatch)
    monkeypatch.setattr(
        bridge, "_kb",
        lambda: (_ for _ in ()).throw(ImportError("hermes_cli missing")),
    )
    captured = {}

    def fake_bad(handler, msg, status=400):
        captured["msg"] = msg
        captured["status"] = status
        return True

    monkeypatch.setattr(bridge, "bad", fake_bad)

    class FakeHandler:
        pass

    parsed = _parsed(path="/api/kanban/tasks")
    result = bridge.handle_kanban_post(FakeHandler(), parsed, {"title": "x"})
    assert result is True
    assert captured["status"] == 503


def test_handle_kanban_patch_returns_503_when_hermes_cli_missing(monkeypatch):
    """Same fallback contract for PATCH verb."""
    bridge = _load_bridge(monkeypatch)
    monkeypatch.setattr(
        bridge, "_kb",
        lambda: (_ for _ in ()).throw(ImportError("hermes_cli missing")),
    )
    captured = {}

    def fake_bad(handler, msg, status=400):
        captured["msg"] = msg
        captured["status"] = status
        return True

    monkeypatch.setattr(bridge, "bad", fake_bad)

    class FakeHandler:
        pass

    parsed = _parsed(path="/api/kanban/tasks/t_1")
    result = bridge.handle_kanban_patch(FakeHandler(), parsed, {"title": "x"})
    assert result is True
    assert captured["status"] == 503


# ── Multi-board management tests ────────────────────────────────────────────
#
# These exercise the /api/kanban/boards surface added by #1662. They mirror
# the agent dashboard plugin's /boards contract so a downstream client
# (CLI, gateway slash command, dashboard) and the WebUI can share the
# same active-board pointer.


def test_list_boards_includes_default_when_only_default_exists(monkeypatch):
    """A fresh deploy with no extra boards must still surface the default
    board in /boards so the UI can render the switcher consistently."""
    bridge = _load_bridge(monkeypatch)
    payload = bridge._list_boards_payload(_parsed())
    assert payload["current"] == "default"
    assert payload["read_only"] is False
    slugs = [b["slug"] for b in payload["boards"]]
    assert "default" in slugs


def test_board_counts_returns_empty_for_nonexistent_board(monkeypatch):
    """_board_counts_for_slug returns {} early for boards whose sqlite
    file has not been materialized yet (board_exists returns False),
    avoiding an unnecessary connect() call on the hot board-list path."""
    fake_kanban = FakeKanbanDB()
    connect_calls = []
    orig_connect = fake_kanban.connect
    def tracking_connect(*, board=None):
        connect_calls.append(("connect", board))
        return orig_connect(board=board)
    fake_kanban.connect = tracking_connect

    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.kanban_db = fake_kanban
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kanban)
    import api.kanban_bridge as bridge
    bridge = importlib.reload(bridge)

    counts = bridge._board_counts_for_slug("no-such-board")
    assert counts == {}
    # connect must NOT have been called — early-out via board_exists
    assert connect_calls == []


def test_board_counts_returns_real_counts_for_populated_board(monkeypatch):
    """When a board has tasks, _board_counts_for_slug must return actual
    per-status counts. The FakeConn needs to handle the board-counts SQL
    pattern (which differs from the dashboard stats SQL)."""
    fake_kanban = FakeKanbanDB()
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.kanban_db = fake_kanban
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kanban)
    import api.kanban_bridge as bridge
    bridge = importlib.reload(bridge)

    # Patch FakeConn.execute to handle the board-counts SQL:
    #   SELECT status, COUNT(*) AS n FROM tasks WHERE status != 'archived' GROUP BY status
    orig_execute = FakeConn.execute
    def patched_execute(self, sql, params=()):
        if "SELECT status, COUNT(*) AS n FROM tasks" in sql and "GROUP BY status" in sql:
            rows = []
            grouped = {}
            for task in self.tasks:
                if task.status == "archived":
                    continue
                grouped[task.status] = grouped.get(task.status, 0) + 1
            for status, n in grouped.items():
                rows.append(FakeRow(status=status, n=n))
            return SimpleNamespace(fetchall=lambda: rows)
        return orig_execute(self, sql, params)
    FakeConn.execute = patched_execute

    try:
        counts = bridge._board_counts_for_slug("default")
        # Default fake has t_1=ready, t_2=blocked
        assert counts.get("ready") == 1
        assert counts.get("blocked") == 1
    finally:
        FakeConn.execute = orig_execute


def test_create_board_payload_creates_and_optionally_switches(monkeypatch):
    """POST /boards must create a board and, when ``switch=true``, also set
    it as the active board so subsequent requests resolve to it."""
    bridge = _load_bridge(monkeypatch)
    payload = bridge._create_board_payload({
        "slug": "experiments",
        "name": "Experiments",
        "description": "Research backlog",
        "icon": "🧪",
        "color": "#7aa2ff",
        "switch": True,
    })
    assert payload["board"]["slug"] == "experiments"
    assert payload["board"]["name"] == "Experiments"
    assert payload["current"] == "experiments"  # switch=true honoured


def test_create_board_payload_rejects_empty_slug(monkeypatch):
    """Empty/missing slug must surface a 400-shape ValueError, not a 500."""
    bridge = _load_bridge(monkeypatch)
    try:
        bridge._create_board_payload({"slug": "", "name": "x"})
    except ValueError as exc:
        assert "slug" in str(exc).lower()
        return
    raise AssertionError("empty slug must raise ValueError")


def test_update_board_payload_renames_metadata_only(monkeypatch):
    """PATCH /boards/<slug> updates display metadata. The slug itself is
    immutable — renaming the slug would mean moving the on-disk directory
    and re-pointing every saved active-board pointer."""
    bridge = _load_bridge(monkeypatch)
    bridge._create_board_payload({"slug": "experiments", "name": "Experiments"})
    res = bridge._update_board_payload("experiments", {
        "name": "R&D Experiments",
        "description": "All ongoing research",
        "icon": "🔬",
    })
    assert res["board"]["name"] == "R&D Experiments"
    assert res["board"]["description"] == "All ongoing research"
    assert res["board"]["icon"] == "🔬"
    assert res["board"]["slug"] == "experiments"  # slug unchanged


def test_update_board_payload_rejects_unknown_slug(monkeypatch):
    """Renaming a board that doesn't exist is a 404, not a silent no-op."""
    bridge = _load_bridge(monkeypatch)
    try:
        bridge._update_board_payload("does-not-exist", {"name": "x"})
    except LookupError as exc:
        assert "does not exist" in str(exc)
        return
    raise AssertionError("unknown slug must raise LookupError")


def test_delete_board_payload_archives_by_default(monkeypatch):
    """DELETE without ?delete=1 archives, preserving on-disk data so the
    board is recoverable from kanban/boards/_archived/."""
    bridge = _load_bridge(monkeypatch)
    bridge._create_board_payload({"slug": "experiments", "name": "Experiments"})
    res = bridge._delete_board_payload("experiments", _parsed())
    # Result either has a result dict with `archived` action OR explicit archive flag
    # The test fake's remove_board sets archived=True; library's returns action='archived'
    assert "result" in res
    assert res["current"] == "default"  # falls back to default after delete


def test_delete_board_payload_refuses_to_delete_default(monkeypatch):
    """The default board cannot be removed — that would leave the system
    without a fallback active board on the next CLI / dashboard call."""
    bridge = _load_bridge(monkeypatch)
    try:
        bridge._delete_board_payload("default", _parsed())
    except ValueError as exc:
        assert "default" in str(exc).lower()
        return
    raise AssertionError("deleting default must raise ValueError")


def test_switch_board_payload_updates_active_pointer(monkeypatch):
    """POST /boards/<slug>/switch sets the active-board pointer that's
    shared by CLI, dashboard, and WebUI."""
    bridge = _load_bridge(monkeypatch)
    bridge._create_board_payload({"slug": "experiments", "name": "Experiments"})
    res = bridge._switch_board_payload("experiments")
    assert res["current"] == "experiments"
    # And reading the active pointer back must reflect the switch
    assert bridge._kb().get_current_board() == "experiments"


def test_switch_board_payload_rejects_unknown_slug(monkeypatch):
    """Switching to a non-existent board is a 404, not a silent set."""
    bridge = _load_bridge(monkeypatch)
    try:
        bridge._switch_board_payload("not-a-real-board")
    except LookupError as exc:
        assert "does not exist" in str(exc)
        return
    raise AssertionError("unknown slug must raise LookupError")


def test_resolve_board_query_param_normalises_and_validates(monkeypatch):
    """The ?board=<slug> query param feeds every endpoint that's board-scoped.
    Empty/missing should resolve to None (use active board); a bad slug
    should raise ValueError; a non-existent slug should raise LookupError."""
    bridge = _load_bridge(monkeypatch)
    # Empty / missing → None (caller falls through to active board)
    assert bridge._resolve_board(_parsed(query="")) is None
    assert bridge._resolve_board(_parsed(query="board=")) is None
    # default board is always allowed (even before materialisation)
    assert bridge._resolve_board(_parsed(query="board=default")) == "default"
    # Path-traversal / malformed slugs raise ValueError
    try:
        bridge._resolve_board(_parsed(query="board=../etc/passwd"))
        raise AssertionError("path-traversal slug must raise ValueError")
    except ValueError:
        pass
    # Non-existent slug raises LookupError
    try:
        bridge._resolve_board(_parsed(query="board=ghost-board"))
        raise AssertionError("non-existent slug must raise LookupError")
    except LookupError:
        pass


def test_resolve_board_from_body_mirrors_query_contract(monkeypatch):
    """POST/PATCH/DELETE handlers receive a parsed JSON body, not a URL,
    so they read the board slug from the body. The validation contract
    must match _resolve_board exactly."""
    bridge = _load_bridge(monkeypatch)
    bridge._create_board_payload({"slug": "experiments", "name": "x"})
    assert bridge._resolve_board_from_body({}) is None
    assert bridge._resolve_board_from_body({"board": ""}) is None
    assert bridge._resolve_board_from_body({"board": "default"}) == "default"
    assert bridge._resolve_board_from_body({"board": "experiments"}) == "experiments"
    try:
        bridge._resolve_board_from_body({"board": "ghost"})
        raise AssertionError("unknown slug must raise LookupError")
    except LookupError:
        pass


def test_handle_kanban_get_routes_boards_endpoint(monkeypatch):
    """The dispatcher must surface the new /boards endpoint without
    accidentally matching the singular /board endpoint (which is task-list)."""
    bridge = _load_bridge(monkeypatch)
    captured = {}

    class FakeHandler:
        pass

    def fake_j(handler, payload, **_kwargs):
        captured["payload"] = payload
        return True

    monkeypatch.setattr(bridge, "j", fake_j)
    parsed = _parsed(path="/api/kanban/boards")
    result = bridge.handle_kanban_get(FakeHandler(), parsed)
    assert result is True
    assert "boards" in captured["payload"]
    assert "current" in captured["payload"]


def test_handle_kanban_post_routes_create_board_and_switch(monkeypatch):
    """POST /boards creates, POST /boards/<slug>/switch activates."""
    bridge = _load_bridge(monkeypatch)
    captured = []

    class FakeHandler:
        pass

    def fake_j(handler, payload, **_kwargs):
        captured.append(payload)
        return True

    monkeypatch.setattr(bridge, "j", fake_j)
    # Create
    bridge.handle_kanban_post(
        FakeHandler(), _parsed(path="/api/kanban/boards"),
        {"slug": "experiments", "name": "Experiments"},
    )
    assert "board" in captured[0]
    # Switch
    bridge.handle_kanban_post(
        FakeHandler(), _parsed(path="/api/kanban/boards/experiments/switch"),
        {},
    )
    assert captured[1]["current"] == "experiments"


def test_handle_kanban_delete_routes_archive_board(monkeypatch):
    """DELETE /boards/<slug> archives by default, hard-deletes with ?delete=1."""
    bridge = _load_bridge(monkeypatch)
    captured = []

    class FakeHandler:
        pass

    def fake_j(handler, payload, **_kwargs):
        captured.append(payload)
        return True

    monkeypatch.setattr(bridge, "j", fake_j)
    bridge._create_board_payload({"slug": "experiments", "name": "x"})
    bridge.handle_kanban_delete(
        FakeHandler(), _parsed(path="/api/kanban/boards/experiments"), {}
    )
    assert len(captured) == 1
    assert "result" in captured[0]


def test_handle_kanban_patch_routes_update_board(monkeypatch):
    """PATCH /boards/<slug> updates display metadata."""
    bridge = _load_bridge(monkeypatch)
    captured = []

    class FakeHandler:
        pass

    def fake_j(handler, payload, **_kwargs):
        captured.append(payload)
        return True

    monkeypatch.setattr(bridge, "j", fake_j)
    bridge._create_board_payload({"slug": "experiments", "name": "x"})
    bridge.handle_kanban_patch(
        FakeHandler(), _parsed(path="/api/kanban/boards/experiments"),
        {"name": "Renamed"},
    )
    assert captured[0]["board"]["name"] == "Renamed"


def test_board_param_isolates_task_writes_between_boards(monkeypatch):
    """Task created with board=A must not appear in board=B's task list.
    This is the core multi-board guarantee — without it the whole feature
    is just cosmetic. The fake's per-board isolation is simulated by
    spying on the connect() call and verifying it received the right slug."""
    bridge = _load_bridge(monkeypatch)
    bridge._create_board_payload({"slug": "board-a", "name": "A"})
    bridge._create_board_payload({"slug": "board-b", "name": "B"})

    seen_boards = []
    kb = bridge._kb()
    original_connect = kb.connect

    def spying_connect(*args, **kwargs):
        seen_boards.append(kwargs.get("board"))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(kb, "connect", spying_connect)

    # Create on board-a and board-b — each call should pin connect(board=...)
    bridge._create_task_payload({"title": "task on A"}, board="board-a")
    bridge._create_task_payload({"title": "task on B"}, board="board-b")
    assert "board-a" in seen_boards
    assert "board-b" in seen_boards


# ── SSE streaming tests ──────────────────────────────────────────────────────


def test_sse_fetch_new_returns_advanced_cursor_and_events(monkeypatch):
    """The SSE inner loop reads task_events with id > cursor and returns
    the new cursor + decoded events. Best-effort — must not raise on
    empty result."""
    bridge = _load_bridge(monkeypatch)
    # Default fake fixture has 1 event with id=7
    new_cursor, events = bridge._kanban_sse_fetch_new(None, 0)
    assert new_cursor == 7
    assert len(events) == 1
    assert events[0]["id"] == 7
    # No new events past the cursor → empty list, cursor unchanged
    new_cursor2, events2 = bridge._kanban_sse_fetch_new(None, 7)
    assert new_cursor2 == 7
    assert events2 == []


def test_sse_fetch_new_self_heals_on_db_error(monkeypatch):
    """A transient DB error inside the SSE loop must NOT drop the client —
    the loop should return the input cursor + empty list and let the
    caller continue polling."""
    bridge = _load_bridge(monkeypatch)
    kb = bridge._kb()

    def raising_connect(*args, **kwargs):
        raise RuntimeError("simulated transient sqlite contention")

    monkeypatch.setattr(kb, "connect", raising_connect)
    new_cursor, events = bridge._kanban_sse_fetch_new(None, 5)
    assert new_cursor == 5  # cursor preserved
    assert events == []  # empty, not exception


def test_sse_handler_runs_in_thread_and_streams_event(monkeypatch):
    """End-to-end SSE smoke: spin up the handler in a worker thread, write
    a fake event to the fake DB, and confirm an `events` frame appears in
    the response stream within a 2-second watchdog window. This is the
    behavioural integration test the SSE-handler-pre-release rule
    requires for every long-lived handler that crosses module boundaries.
    """
    import threading
    import io

    bridge = _load_bridge(monkeypatch)
    # Speed up the SSE poll cycle and heartbeat for the test
    monkeypatch.setattr(bridge, "_KANBAN_SSE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "_KANBAN_SSE_HEARTBEAT_SECONDS", 0.1)

    class FakeWriter(io.BytesIO):
        def flush(self):
            pass

    class FakeHandler:
        def __init__(self):
            self.wfile = FakeWriter()
            self.headers_sent = []
            self.responses = []

        def send_response(self, code):
            self.responses.append(code)

        def send_header(self, k, v):
            self.headers_sent.append((k, v))

        def end_headers(self):
            pass

    handler = FakeHandler()

    # Snapshot the initial-frame check so we can assert it without
    # re-reading after the buffer is closed at the end.
    saw_hello = threading.Event()

    # Run the SSE handler in a thread; let it run for 0.4s, then close
    # the handler's writer to force the loop to exit on the next write.
    done = threading.Event()
    error_holder = []

    def runner():
        try:
            bridge._handle_events_sse_stream(handler, _parsed(query="since=0"))
        except Exception as exc:  # noqa: BLE001
            error_holder.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    # Wait briefly for the initial frame to be written
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            buf = handler.wfile.getvalue()
        except ValueError:
            buf = b""
        if b"event: hello" in buf:
            saw_hello.set()
            break
    # Close the writer to force the loop to exit on its next write attempt
    try:
        handler.wfile.close()
    except Exception:
        pass
    # Give the loop ~250ms to notice and exit
    done.wait(timeout=2.0)
    assert done.is_set(), "SSE handler did not exit within 2s after writer close"
    assert handler.responses == [200]
    assert saw_hello.is_set(), "Initial 'event: hello' frame never appeared in stream"
    assert not error_holder, f"SSE handler raised: {error_holder!r}"


def test_handle_kanban_patch_routes_boards_slug_before_board_query_param(monkeypatch):
    """Opus advisor SHOULD-FIX #1: PATCH /api/kanban/boards/<slug>?board=ghost
    must edit `<slug>`, NOT 404 on `ghost`. The board management routes
    take their slug from the URL path; a stray ?board= query param on a
    /boards/<slug> path is meaningless and must be ignored.
    """
    bridge = _load_bridge(monkeypatch)
    bridge._create_board_payload({"slug": "experiments", "name": "Exp"})
    captured = []

    class FakeHandler:
        pass

    def fake_j(handler, payload, **_):
        captured.append(payload)
        return True

    monkeypatch.setattr(bridge, "j", fake_j)
    # Ghost board does NOT exist; query param should be ignored on a /boards path.
    parsed = _parsed(path="/api/kanban/boards/experiments", query="board=ghost")
    result = bridge.handle_kanban_patch(FakeHandler(), parsed, {"name": "Renamed"})
    assert result is True
    assert captured, "PATCH /boards/<slug> must succeed even with stray ?board="
    assert captured[0]["board"]["slug"] == "experiments"
    assert captured[0]["board"]["name"] == "Renamed"


def test_handle_kanban_delete_routes_boards_slug_before_board_query_param(monkeypatch):
    """Opus advisor SHOULD-FIX #1: same routing-order guarantee for DELETE."""
    bridge = _load_bridge(monkeypatch)
    bridge._create_board_payload({"slug": "experiments", "name": "Exp"})
    captured = []

    class FakeHandler:
        pass

    def fake_j(handler, payload, **_):
        captured.append(payload)
        return True

    monkeypatch.setattr(bridge, "j", fake_j)
    parsed = _parsed(path="/api/kanban/boards/experiments", query="board=ghost")
    result = bridge.handle_kanban_delete(FakeHandler(), parsed, {})
    assert result is True
    assert captured, "DELETE /boards/<slug> must succeed even with stray ?board="


def test_sse_emits_id_lines_so_browser_can_resume_via_last_event_id(monkeypatch):
    """Opus advisor SHOULD-FIX #2: every `event: events` frame must include
    `id: <event_id>` so the browser auto-stores Last-Event-ID and the
    server can resume from there on reconnect without re-streaming the
    backlog.
    """
    import threading
    import io

    bridge = _load_bridge(monkeypatch)
    monkeypatch.setattr(bridge, "_KANBAN_SSE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "_KANBAN_SSE_HEARTBEAT_SECONDS", 0.1)

    class FakeHandler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.headers = {}
            self.responses = []

        def send_response(self, code): self.responses.append(code)
        def send_header(self, k, v): pass
        def end_headers(self): pass

    handler = FakeHandler()
    done = threading.Event()

    def runner():
        try:
            bridge._handle_events_sse_stream(handler, _parsed(query="since=0"))
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    # Wait for an events frame to land
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            buf = handler.wfile.getvalue()
        except ValueError:
            buf = b""
        if b"event: events" in buf:
            break
    handler.wfile.close()
    done.wait(timeout=2.0)
    assert done.is_set()


def test_sse_honours_last_event_id_header_when_since_absent(monkeypatch):
    """Opus advisor SHOULD-FIX #2: when the client reconnects, EventSource
    sends Last-Event-ID automatically. The handler must use it to resume
    when no explicit ?since= is given.
    """
    import threading
    import io

    bridge = _load_bridge(monkeypatch)
    monkeypatch.setattr(bridge, "_KANBAN_SSE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "_KANBAN_SSE_HEARTBEAT_SECONDS", 0.1)

    captured_cursor = []

    def spying_fetch(board, cursor):
        captured_cursor.append(cursor)
        return cursor, []

    monkeypatch.setattr(bridge, "_kanban_sse_fetch_new", spying_fetch)

    class FakeHandler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.headers = {"Last-Event-ID": "42"}
            self.responses = []

        def send_response(self, code): self.responses.append(code)
        def send_header(self, k, v): pass
        def end_headers(self): pass

    handler = FakeHandler()
    done = threading.Event()

    def runner():
        try:
            # No ?since= in query; the handler should pick up "42" from
            # the Last-Event-ID header.
            bridge._handle_events_sse_stream(handler, _parsed(query=""))
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    # Give the loop one poll cycle to run
    time.sleep(0.2)
    handler.wfile.close()
    done.wait(timeout=2.0)
    assert done.is_set()
    assert 42 in captured_cursor, (
        f"Handler must honour Last-Event-ID=42 on reconnect; saw cursors: {captured_cursor}"
    )
