import importlib
import io
import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch


def _default_task(**overrides):
    task = {
        "id": "t_safe",
        "title": "Safe operator task",
        "body": "Do a bounded operator task.",
        "assignee": "default",
        "status": "done",
        "priority": 0,
        "created_by": "max",
        "created_at": 100,
        "started_at": 110,
        "completed_at": 200,
        "workspace_kind": "scratch",
        "workspace_path": "/tmp/hermes-operator-workspaces/t_safe",
        "branch_name": None,
        "claim_lock": None,
        "claim_expires": None,
        "tenant": "local",
        "result": "Completed with a concise receipt.",
        "consecutive_failures": 0,
        "worker_pid": None,
        "last_failure_error": None,
        "max_runtime_seconds": None,
        "last_heartbeat_at": None,
        "current_run_id": 1,
        "workflow_template_id": None,
        "current_step_key": None,
        "skills": None,
        "model_override": None,
        "max_retries": None,
        "session_id": "sess_1",
    }
    task.update(overrides)
    return task


def _default_run(**overrides):
    run = {
        "id": 1,
        "task_id": "t_safe",
        "profile": "default",
        "step_key": None,
        "status": "done",
        "claim_lock": None,
        "claim_expires": None,
        "worker_pid": None,
        "max_runtime_seconds": None,
        "last_heartbeat_at": None,
        "started_at": 111,
        "ended_at": 199,
        "outcome": "completed",
        "summary": "Run completed.",
        "metadata": "{}",
        "error": None,
    }
    run.update(overrides)
    return run


def _create_kanban_db(path: Path, *, tasks=None, runs=None, events=None, comments=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                body TEXT,
                assignee TEXT,
                status TEXT,
                priority INTEGER,
                created_by TEXT,
                created_at INTEGER,
                started_at INTEGER,
                completed_at INTEGER,
                workspace_kind TEXT,
                workspace_path TEXT,
                branch_name TEXT,
                claim_lock TEXT,
                claim_expires INTEGER,
                tenant TEXT,
                result TEXT,
                consecutive_failures INTEGER,
                worker_pid INTEGER,
                last_failure_error TEXT,
                max_runtime_seconds INTEGER,
                last_heartbeat_at INTEGER,
                current_run_id INTEGER,
                workflow_template_id TEXT,
                current_step_key TEXT,
                skills TEXT,
                model_override TEXT,
                max_retries INTEGER,
                session_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                profile TEXT,
                step_key TEXT,
                status TEXT,
                claim_lock TEXT,
                claim_expires INTEGER,
                worker_pid INTEGER,
                max_runtime_seconds INTEGER,
                last_heartbeat_at INTEGER,
                started_at INTEGER,
                ended_at INTEGER,
                outcome TEXT,
                summary TEXT,
                metadata TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                run_id INTEGER,
                kind TEXT,
                payload TEXT,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                author TEXT,
                body TEXT,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_links (
                parent_id TEXT,
                child_id TEXT,
                kind TEXT,
                created_at INTEGER,
                PRIMARY KEY (parent_id, child_id)
            )
            """
        )
        for task in tasks or [_default_task()]:
            keys = list(task.keys())
            conn.execute(
                f"INSERT INTO tasks ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                [task[key] for key in keys],
            )
        for run in runs or [_default_run()]:
            keys = list(run.keys())
            conn.execute(
                f"INSERT INTO task_runs ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                [run[key] for key in keys],
            )
        for event in events or [
            {"id": 1, "task_id": "t_safe", "run_id": 1, "kind": "completed", "payload": json.dumps({"summary": "Completed."}), "created_at": 200}
        ]:
            keys = list(event.keys())
            conn.execute(
                f"INSERT INTO task_events ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                [event[key] for key in keys],
            )
        for comment in comments or []:
            keys = list(comment.keys())
            conn.execute(
                f"INSERT INTO task_comments ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                [comment[key] for key in keys],
            )
        conn.commit()
    finally:
        conn.close()


def _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path):
    safe_root = tmp_path / "hermes-operator-workspaces"
    safe_root.mkdir(parents=True, exist_ok=True)
    hardening = tmp_path / "Hermes Kanban Pilot Hardening.md"
    hardening.write_text(
        "# Hermes Kanban Pilot Hardening\n"
        "Updated: `2099-01-01T00:00:00Z`\n"
        "Board: `hermes-operator`\n"
        f"Safe board default workdir: `{safe_root}`\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(operator_kanban, "BOARD_DB", db_path, raising=False)
    monkeypatch.setattr(operator_kanban, "SAFE_SCRATCH_ROOT", safe_root, raising=False)
    monkeypatch.setattr(operator_kanban, "HARDENING_NOTE", hardening, raising=False)
    monkeypatch.setattr(operator_kanban, "PROJECT_PATHS", [Path("/mnt/c/Users/malac/.openclaw/workspace/main"), Path("/home/malac/hermes-webui")], raising=False)
    return safe_root


def _patch_truth(monkeypatch, *, status="live"):
    import api.operator_truth as operator_truth

    def fake_truth_payload(*, session_id=None, ui_board_hint=None, now=None):
        return {
            "version": 1,
            "verified_at": now,
            "status": status,
            "summary": f"Truth {status}",
            "chips": [],
            "sources": [],
            "issues": [] if status == "live" else [f"truth {status}"],
        }

    monkeypatch.setattr(operator_truth, "build_operator_truth_payload", fake_truth_payload, raising=False)


def test_operator_kanban_payload_has_read_only_contract(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    _create_kanban_db(db_path, tasks=[_default_task(workspace_path=str(safe_root / "t_safe"))])
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=123.0)

    assert payload["version"] == 1
    assert payload["generated_at"] == 123.0
    assert payload["mode"] == "read-only-kanban-operator"
    assert payload["would_execute"] is False
    assert payload["board"] == "hermes-operator"
    assert payload["status"] in {"live", "stale", "unknown"}
    assert payload["counts"]["done"] == 1
    assert payload["board_safety"]["board_db"] == str(db_path)
    assert payload["truth"]["status"] == "live"
    assert payload["tasks"]
    assert {"kanban_db", "hardening_note", "operator_truth"} <= {source["id"] for source in payload["sources"]}
    for task in payload["tasks"]:
        assert task["status"]
        assert "assignee" in task
        assert "profile" in task
        assert "workspace_path" in task
        assert "scratch_safety" in task
        assert "blocked_reason" in task
        assert "receipt_links" in task
        assert "review_state" in task
        assert "completion" in task


def test_operator_kanban_opens_sqlite_read_only_and_does_not_init_or_dispatch(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    _create_kanban_db(db_path, tasks=[_default_task(workspace_path=str(safe_root / "t_safe"))])
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")
    seen_connects = []
    original_connect = operator_kanban.sqlite3.connect
    calls = []

    def checked_connect(database, *args, **kwargs):
        seen_connects.append((str(database), kwargs))
        return original_connect(database, *args, **kwargs)

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("operator kanban builder must be read-only and shell-free")

    fake_kanban_db = types.ModuleType("hermes_cli.kanban_db")
    for name in ("init_db", "dispatch", "dispatch_ready_tasks", "claim", "complete", "create_task", "update_task"):
        setattr(fake_kanban_db, name, forbidden)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kanban_db)
    monkeypatch.setattr(operator_kanban.sqlite3, "connect", checked_connect)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    assert payload["would_execute"] is False
    assert calls == []
    assert seen_connects
    assert all("mode=ro" in database for database, _kwargs in seen_connects)
    assert all(kwargs.get("uri") is True for _database, kwargs in seen_connects)


def test_operator_kanban_marks_scratch_under_safe_root_live(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    _create_kanban_db(db_path, tasks=[_default_task(workspace_path=str(safe_root / "t_safe"))])
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    task = payload["tasks"][0]
    assert task["scratch_safety"]["state"] == "live"
    assert task["scratch_safety"]["scratch_points_to_project"] is False
    assert "safe board root" in task["scratch_safety"]["reason"]


def test_operator_kanban_marks_scratch_pointing_at_project_stale_or_unknown(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    _create_kanban_db(
        db_path,
        tasks=[_default_task(id="t_bad", workspace_path="/mnt/c/Users/malac/.openclaw/workspace/main")],
        runs=[_default_run(task_id="t_bad")],
        events=[{"id": 1, "task_id": "t_bad", "run_id": 1, "kind": "completed", "payload": "{}", "created_at": 200}],
    )
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    assert payload["status"] != "live"
    task = payload["tasks"][0]
    assert task["scratch_safety"]["scratch_points_to_project"] is True
    assert task["scratch_safety"]["state"] in {"stale", "unknown"}


def test_operator_kanban_missing_db_returns_unknown_without_fake_tasks(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    missing = tmp_path / "missing" / "kanban.db"
    _patch_sources(monkeypatch, operator_kanban, tmp_path, missing)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=123.0)

    assert payload["status"] == "unknown"
    assert payload["tasks"] == []
    assert not missing.exists()
    assert any("kanban_db" in issue for issue in payload["issues"])


def test_operator_kanban_parses_completion_metadata_and_receipts_when_structured(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    structured_result = json.dumps(
        {
            "summary": "Structured completion summary",
            "changed_files": ["api/operator_kanban.py"],
            "receipts": [{"label": "receipt", "path": "receipts/operator-kanban.md"}],
            "validation": ["pytest tests/test_operator_kanban.py"],
            "side_effects": ["none"],
        }
    )
    _create_kanban_db(db_path, tasks=[_default_task(result=structured_result, workspace_path=str(safe_root / "t_safe"))])
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    task = payload["tasks"][0]
    assert task["completion"]["metadata_state"] == "structured"
    assert task["completion"]["result_summary"] == "Structured completion summary"
    assert task["completion"]["changed_files"] == ["api/operator_kanban.py"]
    assert task["completion"]["validation"] == ["pytest tests/test_operator_kanban.py"]
    assert task["completion"]["side_effects"] == ["none"]
    assert task["receipt_links"]
    assert task["receipt_links"][0]["path"] == "receipts/operator-kanban.md"


def test_operator_kanban_surfaces_structured_review_state_from_task_result(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    structured_result = json.dumps(
        {
            "summary": "Structured completion summary",
            "review_state": {"state": "required", "reason": "independent review pending"},
            "receipts": ["receipts/operator-kanban.md"],
            "validation": ["pytest tests/test_operator_kanban.py"],
        }
    )
    _create_kanban_db(db_path, tasks=[_default_task(result=structured_result, workspace_path=str(safe_root / "t_safe"))])
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    assert payload["tasks"][0]["review_state"] == {"state": "required", "reason": "independent review pending"}


def test_operator_kanban_surfaces_review_required_from_run_metadata(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    _create_kanban_db(
        db_path,
        tasks=[_default_task(result="plain text completion", workspace_path=str(safe_root / "t_safe"))],
        runs=[_default_run(metadata=json.dumps({"review_required": True}))],
    )
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    assert payload["tasks"][0]["review_state"] == {"state": "required", "reason": "review_required metadata is true"}


def test_operator_kanban_plain_text_result_is_unstructured_not_fake_green(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    _create_kanban_db(db_path, tasks=[_default_task(result="plain text completion", workspace_path=str(safe_root / "t_safe"))])
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    task = payload["tasks"][0]
    assert payload["status"] == "stale"
    assert task["completion"]["metadata_state"] == "unstructured"
    assert task["completion"]["result_summary"] == "plain text completion"
    assert task["review_state"]["state"] in {"unknown", "required"}
    assert task["review_state"]["state"] != "approved"
    assert task["completion"]["changed_files"] == []
    assert task["completion"]["validation"] == []


def test_operator_kanban_blocked_reason_comes_from_real_failure_fields(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    safe_root = tmp_path / "hermes-operator-workspaces"
    _create_kanban_db(
        db_path,
        tasks=[_default_task(id="t_blocked", status="blocked", last_failure_error="missing receipt", workspace_path=str(safe_root / "t_blocked"))],
        runs=[_default_run(task_id="t_blocked", status="blocked", outcome="blocked", error="worker blocked on review")],
        events=[
            {
                "id": 1,
                "task_id": "t_blocked",
                "run_id": 1,
                "kind": "blocked",
                "payload": json.dumps({"reason": "operator requested review"}),
                "created_at": 201,
            }
        ],
    )
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(now=1.0)

    task = payload["tasks"][0]
    assert task["blocked_reason"] == "missing receipt"
    assert payload["counts"]["blocked"] == 1


def test_operator_kanban_disallows_non_hermes_operator_board(monkeypatch, tmp_path):
    operator_kanban = importlib.import_module("api.operator_kanban")
    db_path = tmp_path / "kanban.db"
    _create_kanban_db(db_path)
    _patch_sources(monkeypatch, operator_kanban, tmp_path, db_path)
    _patch_truth(monkeypatch, status="live")

    payload = operator_kanban.build_operator_kanban_payload(board="aim-operator-control", now=123.0)

    assert payload["status"] == "unknown"
    assert payload["board"] == "aim-operator-control"
    assert payload["tasks"] == []
    assert any("not allowlisted" in issue.lower() for issue in payload["issues"])


def test_operator_kanban_route_returns_json(monkeypatch):
    import api.routes as routes

    expected = {"version": 1, "status": "unknown", "tasks": [], "sources": []}
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        return True

    with patch("api.operator_kanban.build_operator_kanban_payload", return_value=expected) as build_payload, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_get(
            types.SimpleNamespace(wfile=io.BytesIO()),
            urlparse("/api/operator/kanban?board=hermes-operator&session_id=abc123&ui_board=hermes-operator"),
        )

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"] == expected
    build_payload.assert_called_once_with(
        board="hermes-operator",
        session_id="abc123",
        ui_board_hint="hermes-operator",
    )
