"""Read-only operator Kanban payloads for the hermes-operator board.

Slice 3 is an evidence/safety surface only. This module reads the allowlisted
local Kanban SQLite DB in SQLite read-only mode, summarizes task/operator
metadata, and degrades missing or unstructured evidence to ``unknown``/``stale``.
It does not import Hermes Kanban write helpers, shell out, dispatch, claim, or
mutate board state.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BOARD = "hermes-operator"
ALLOWED_BOARDS = {BOARD}
BOARD_DB = Path("/home/malac/.hermes/kanban/boards/hermes-operator/kanban.db")
SAFE_SCRATCH_ROOT = Path("/home/malac/.hermes/kanban/workspaces/hermes-operator")
WORKSPACE_ROOT = Path("/mnt/c/Users/malac/.openclaw/workspace/main")
HARDENING_NOTE = WORKSPACE_ROOT / "obsidian-vault" / "Agent-Kimi" / "Hermes Kanban Pilot Hardening.md"
PROJECT_PATHS = [WORKSPACE_ROOT, Path("/home/malac/hermes-webui")]
STATUS_ORDER = {"live": 0, "stale": 1, "unknown": 2}
STALE_AFTER_SECONDS = 72 * 60 * 60
MAX_TEXT = 500
MAX_LIST_ITEMS = 12
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})(?:[T ]([0-2]\d:[0-5]\d(?::[0-5]\d)?)(?:Z|[+-][0-2]\d:[0-5]\d)?)?\b")

TASK_COLUMNS = [
    "id",
    "title",
    "body",
    "assignee",
    "status",
    "priority",
    "created_by",
    "created_at",
    "started_at",
    "completed_at",
    "workspace_kind",
    "workspace_path",
    "branch_name",
    "claim_lock",
    "claim_expires",
    "tenant",
    "result",
    "consecutive_failures",
    "worker_pid",
    "last_failure_error",
    "max_runtime_seconds",
    "last_heartbeat_at",
    "current_run_id",
    "workflow_template_id",
    "current_step_key",
    "skills",
    "model_override",
    "max_retries",
    "session_id",
]
RUN_COLUMNS = [
    "id",
    "task_id",
    "profile",
    "step_key",
    "status",
    "claim_lock",
    "claim_expires",
    "worker_pid",
    "max_runtime_seconds",
    "last_heartbeat_at",
    "started_at",
    "ended_at",
    "outcome",
    "summary",
    "metadata",
    "error",
]
EVENT_COLUMNS = ["id", "task_id", "run_id", "kind", "payload", "created_at"]
COMMENT_COLUMNS = ["id", "task_id", "author", "body", "created_at"]
REQUIRED_TASK_COLUMNS = {"id", "title", "status", "workspace_kind", "workspace_path"}


def build_operator_kanban_payload(
    *,
    board: str = BOARD,
    session_id: str | None = None,
    ui_board_hint: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build the versioned read-only operator Kanban payload."""

    generated_at = float(time.time() if now is None else now)
    requested_board = str(board or BOARD).strip() or BOARD
    sources: list[dict[str, Any]] = []
    issues: list[str] = []
    counts = _empty_counts()
    truth = _operator_truth_summary(session_id=session_id, ui_board_hint=ui_board_hint, now=generated_at)
    sources.append({"id": "operator_truth", "kind": "api", "api": "/api/operator/truth", "state": truth.get("status", "unknown")})
    if truth.get("issue"):
        issues.append(f"operator_truth: {truth['issue']}")
    if truth.get("status") in {"stale", "unknown"}:
        issues.append(f"operator_truth is {truth.get('status')}")

    hardening_source = _hardening_source(generated_at)
    sources.append(hardening_source)
    if hardening_source.get("issue"):
        issues.append(f"hardening_note: {hardening_source['issue']}")

    board_source = _source_stat("kanban_db", BOARD_DB, kind="sqlite", now=generated_at)
    sources.insert(0, board_source)

    if requested_board not in ALLOWED_BOARDS:
        issues.append(f"board {requested_board!r} is not allowlisted for operator Kanban")
        return _payload(
            generated_at=generated_at,
            status="unknown",
            board=requested_board,
            summary="Operator Kanban unavailable — board not allowlisted",
            counts=counts,
            board_safety=_board_safety([], state="unknown", notes=["Only hermes-operator is allowlisted"]),
            truth=truth,
            tasks=[],
            sources=sources,
            issues=issues,
        )

    if board_source.get("state") == "unknown":
        issue = board_source.get("issue") or "missing or unreadable"
        issues.append(f"kanban_db: {issue}")
        return _payload(
            generated_at=generated_at,
            status="unknown",
            board=requested_board,
            summary="Operator Kanban unavailable — kanban_db unreadable",
            counts=counts,
            board_safety=_board_safety([], state="unknown", notes=["Kanban DB missing or unreadable"]),
            truth=truth,
            tasks=[],
            sources=sources,
            issues=_dedupe(issues),
        )

    try:
        with _connect_readonly(BOARD_DB) as conn:
            columns = _table_columns(conn, "tasks")
            missing = sorted(REQUIRED_TASK_COLUMNS - columns)
            if missing:
                issues.append(f"kanban_db schema missing task columns: {', '.join(missing)}")
                return _payload(
                    generated_at=generated_at,
                    status="unknown",
                    board=requested_board,
                    summary="Operator Kanban unavailable — schema missing required columns",
                    counts=counts,
                    board_safety=_board_safety([], state="unknown", notes=["Task schema incomplete"]),
                    truth=truth,
                    tasks=[],
                    sources=sources,
                    issues=_dedupe(issues),
                )
            raw_tasks = _read_rows(conn, "tasks", TASK_COLUMNS, order_by="created_at ASC, id ASC")
            runs = _group_by_task(_read_optional_rows(conn, "task_runs", RUN_COLUMNS, order_by="COALESCE(ended_at, started_at, id) ASC"))
            events = _group_by_task(_read_optional_rows(conn, "task_events", EVENT_COLUMNS, order_by="created_at ASC, id ASC"))
            comments = _group_by_task(_read_optional_rows(conn, "task_comments", COMMENT_COLUMNS, order_by="created_at ASC, id ASC"))
    except Exception as exc:
        issues.append(f"kanban_db unreadable: {_short_error(exc)}")
        return _payload(
            generated_at=generated_at,
            status="unknown",
            board=requested_board,
            summary="Operator Kanban unavailable — kanban_db unreadable",
            counts=counts,
            board_safety=_board_safety([], state="unknown", notes=["Kanban DB read failed"]),
            truth=truth,
            tasks=[],
            sources=sources,
            issues=_dedupe(issues),
        )

    task_payloads: list[dict[str, Any]] = []
    status_inputs = ["live", str(hardening_source.get("state") or "unknown"), str(truth.get("status") or "unknown")]
    for task in raw_tasks:
        status = _clean_text(task.get("status"), "unknown")
        counts[status] = counts.get(status, 0) + 1
        task_runs = runs.get(str(task.get("id")), [])
        task_events = events.get(str(task.get("id")), [])
        task_comments = comments.get(str(task.get("id")), [])
        task_payload = _task_payload(task, task_runs, task_events, task_comments, issues)
        task_payloads.append(task_payload)
        status_inputs.append(task_payload["scratch_safety"].get("state", "unknown"))
        completion = task_payload.get("completion", {})
        if status == "done" and completion.get("metadata_state") != "structured":
            issues.append(f"task {task_payload['id']} completion metadata is {completion.get('metadata_state', 'unknown')}")
            status_inputs.append("stale")
        if status == "done" and not task_payload.get("receipt_links"):
            issues.append(f"task {task_payload['id']} receipt links missing")
            status_inputs.append("stale")
        if status == "done" and not completion.get("validation"):
            issues.append(f"task {task_payload['id']} validation metadata missing")
            status_inputs.append("stale")

    safety = _board_safety([task["scratch_safety"] for task in task_payloads])
    status_inputs.append(safety.get("state", "unknown"))
    status = _worst_status(status_inputs)
    total = len(task_payloads)
    done = counts.get("done", 0)
    scratch_text = "scratch safe" if safety.get("state") == "live" else f"scratch {safety.get('state', 'unknown')}"
    if total:
        summary = f"{total} task{'s' if total != 1 else ''}, {done} done; {scratch_text}"
        if status != "live":
            summary += "; stale/unknown evidence needs review"
    else:
        summary = "0 tasks from read-only hermes-operator board"

    return _payload(
        generated_at=generated_at,
        status=status,
        board=requested_board,
        summary=summary,
        counts=counts,
        board_safety=safety,
        truth=truth,
        tasks=task_payloads,
        sources=sources,
        issues=_dedupe(issues),
    )


def _payload(
    *,
    generated_at: float,
    status: str,
    board: str,
    summary: str,
    counts: dict[str, int],
    board_safety: dict[str, Any],
    truth: dict[str, Any],
    tasks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": generated_at,
        "status": status if status in STATUS_ORDER else "unknown",
        "mode": "read-only-kanban-operator",
        "would_execute": False,
        "board": board,
        "summary": summary,
        "counts": counts,
        "board_safety": board_safety,
        "truth": truth,
        "tasks": tasks,
        "sources": sources,
        "issues": issues,
    }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _read_rows(conn: sqlite3.Connection, table: str, wanted: list[str], *, order_by: str) -> list[dict[str, Any]]:
    columns = _table_columns(conn, table)
    selected = [column for column in wanted if column in columns]
    if not selected:
        return []
    order = order_by if order_by else selected[0]
    query = f"SELECT {', '.join(selected)} FROM {table} ORDER BY {order}"
    return [dict(row) for row in conn.execute(query).fetchall()]


def _read_optional_rows(conn: sqlite3.Connection, table: str, wanted: list[str], *, order_by: str) -> list[dict[str, Any]]:
    if not _table_columns(conn, table):
        return []
    return _read_rows(conn, table, wanted, order_by=order_by)


def _group_by_task(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            grouped.setdefault(task_id, []).append(row)
    return grouped


def _task_payload(
    task: dict[str, Any],
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    task_id = _clean_text(task.get("id"), "unknown")
    latest_run = _latest_run(runs)
    completion = _completion_metadata(task, latest_run, runs, events, issues)
    receipt_links = _receipt_links(completion, runs, issues, task_id)
    review_state = _review_state(task, completion, latest_run)
    return {
        "id": task_id,
        "title": _truncate(_clean_text(task.get("title"), task_id), 160),
        "status": _clean_text(task.get("status"), "unknown"),
        "assignee": _none_if_empty(task.get("assignee")),
        "profile": _none_if_empty(latest_run.get("profile") if latest_run else None),
        "tenant": _none_if_empty(task.get("tenant")),
        "priority": task.get("priority"),
        "workspace_kind": _clean_text(task.get("workspace_kind"), "unknown"),
        "workspace_path": _clean_text(task.get("workspace_path"), ""),
        "scratch_safety": _workspace_safety(task),
        "blocked_reason": _blocked_reason(task, runs, events),
        "review_state": review_state,
        "receipt_links": receipt_links,
        "completion": completion,
        "runs": [_run_summary(run, issues, task_id) for run in runs[-3:]],
        "comment_count": len(comments),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


def _latest_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not runs:
        return None
    return sorted(runs, key=lambda run: (run.get("ended_at") or run.get("started_at") or 0, run.get("id") or 0))[-1]


def _run_summary(run: dict[str, Any], issues: list[str], task_id: str) -> dict[str, Any]:
    metadata = _json_object(run.get("metadata"), issues, f"task {task_id} run {run.get('id')} metadata") or {}
    return {
        "id": run.get("id"),
        "profile": _none_if_empty(run.get("profile")),
        "step_key": _none_if_empty(run.get("step_key")),
        "status": _clean_text(run.get("status"), "unknown"),
        "outcome": _none_if_empty(run.get("outcome")),
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "summary": _truncate(_clean_text(run.get("summary"), ""), MAX_TEXT),
        "metadata_state": "structured" if metadata else "missing",
        "error": _truncate(_clean_text(run.get("error"), ""), 240) or None,
    }


def _workspace_safety(task: dict[str, Any]) -> dict[str, Any]:
    kind = _clean_text(task.get("workspace_kind"), "unknown")
    raw_path = _clean_text(task.get("workspace_path"), "")
    if kind != "scratch":
        points = _points_to_project(raw_path)
        return {
            "state": "stale" if points else "unknown",
            "kind": "project-bound" if points else (kind or "explicit"),
            "scratch_points_to_project": points,
            "reason": "non-scratch workspace; not labelled scratch safe",
        }
    if not raw_path:
        return {"state": "unknown", "scratch_points_to_project": False, "reason": "scratch workspace path missing"}
    if _same_or_under(raw_path, SAFE_SCRATCH_ROOT):
        return {"state": "live", "scratch_points_to_project": False, "reason": "scratch workspace under safe board root"}
    if _points_to_project(raw_path):
        return {"state": "stale", "scratch_points_to_project": True, "reason": "scratch workspace points at a project/workspace path"}
    return {"state": "stale", "scratch_points_to_project": False, "reason": "scratch workspace outside safe board root"}


def _board_safety(scratch_states: list[dict[str, Any]], *, state: str | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    scratch_points = any(bool(item.get("scratch_points_to_project")) for item in scratch_states)
    derived_state = state or _worst_status([str(item.get("state") or "unknown") for item in scratch_states] or ["live"])
    safety_notes = list(notes or [])
    if not safety_notes:
        if not scratch_states:
            safety_notes.append("No task scratch workspaces to evaluate")
        elif derived_state == "live":
            safety_notes.append("All scratch task workspaces are under the safe board root")
        else:
            safety_notes.append("One or more task workspaces need operator review")
    return {
        "state": derived_state,
        "board_db": str(BOARD_DB),
        "safe_scratch_root": str(SAFE_SCRATCH_ROOT),
        "scratch_points_to_project": scratch_points,
        "notes": safety_notes,
    }


def _blocked_reason(task: dict[str, Any], runs: list[dict[str, Any]], events: list[dict[str, Any]]) -> str | None:
    direct = _clean_text(task.get("last_failure_error"), "")
    if direct:
        return _truncate(direct, 240)
    for event in reversed(events):
        kind = _clean_text(event.get("kind"), "")
        if kind not in {"blocked", "failed", "crashed", "timed_out"}:
            continue
        payload = _json_object(event.get("payload"), [], "event payload") or {}
        reason = _clean_text(payload.get("reason") or payload.get("error") or payload.get("summary"), "")
        if reason:
            return _truncate(reason, 240)
    for run in reversed(runs):
        error = _clean_text(run.get("error"), "")
        if error:
            return _truncate(error, 240)
        if _clean_text(run.get("status"), "") == "blocked" or _clean_text(run.get("outcome"), "") == "blocked":
            summary = _clean_text(run.get("summary"), "")
            if summary:
                return _truncate(summary, 240)
    return None


def _completion_metadata(
    task: dict[str, Any],
    latest_run: dict[str, Any] | None,
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    result_raw = task.get("result")
    result_obj = _json_object(result_raw, issues, f"task {task.get('id')} result")
    result_text = _clean_text(result_raw, "") if result_obj is None else ""
    run_meta = _json_object(latest_run.get("metadata"), issues, f"task {task.get('id')} run metadata") if latest_run else None
    run_meta = run_meta or {}

    source_obj = result_obj or {}
    metadata_state = "structured" if result_obj else ("unstructured" if result_text else ("structured" if run_meta else "missing"))
    summary = _clean_text(source_obj.get("summary") or source_obj.get("result_summary"), "")
    if not summary and result_text:
        summary = result_text
    if not summary and latest_run:
        summary = _clean_text(latest_run.get("summary"), "")
    if not summary:
        for event in reversed(events):
            payload = _json_object(event.get("payload"), issues, f"task {task.get('id')} event payload") or {}
            summary = _clean_text(payload.get("summary"), "")
            if summary:
                break

    merged = dict(run_meta)
    merged.update(source_obj)
    return {
        "completed_at": task.get("completed_at") or (latest_run or {}).get("ended_at"),
        "result_summary": _truncate(summary, MAX_TEXT),
        "metadata_state": metadata_state,
        "changed_files": _string_list(merged.get("changed_files")),
        "receipts": _receipt_values(merged),
        "validation": _string_list(merged.get("validation")),
        "side_effects": _string_list(merged.get("side_effects")),
        "review_state": merged.get("review_state"),
        "review_required": merged.get("review_required"),
    }


def _receipt_values(data: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for key in ("receipt_links", "receipts", "receipt"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            values.extend(value)
        else:
            values.append(value)
    return values[:MAX_LIST_ITEMS]


def _receipt_links(completion: dict[str, Any], runs: list[dict[str, Any]], issues: list[str], task_id: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    values = list(completion.get("receipts") or [])
    for run in runs:
        metadata = _json_object(run.get("metadata"), issues, f"task {task_id} run {run.get('id')} receipt metadata") or {}
        values.extend(_receipt_values(metadata))
    for index, value in enumerate(values[:MAX_LIST_ITEMS], start=1):
        if isinstance(value, dict):
            path = _clean_text(value.get("path") or value.get("url") or value.get("href") or value.get("receipt"), "")
            label = _clean_text(value.get("label") or value.get("title"), f"receipt {index}")
        else:
            path = _clean_text(value, "")
            label = f"receipt {index}"
        if path:
            links.append({"label": _truncate(label, 80), "path": _truncate(path, 240)})
    return _dedupe_links(links)


def _review_state(task: dict[str, Any], completion: dict[str, Any], latest_run: dict[str, Any] | None) -> dict[str, str]:
    for source in (completion, latest_run or {}, task):
        raw = source.get("review_state") if isinstance(source, dict) else None
        if isinstance(raw, dict):
            state = _clean_text(raw.get("state"), "unknown")
            reason = _clean_text(raw.get("reason"), "")
            return {"state": state if state in {"approved", "required", "unknown", "blocked"} else "unknown", "reason": reason or "structured review metadata found"}
        if isinstance(raw, str) and raw.strip():
            state = raw.strip().lower()
            return {"state": state if state in {"approved", "required", "unknown", "blocked"} else "unknown", "reason": "review state from metadata"}
        if isinstance(source, dict) and source.get("review_required") is True:
            return {"state": "required", "reason": "review_required metadata is true"}
    if _clean_text(task.get("status"), "") == "blocked":
        return {"state": "required", "reason": "task is blocked"}
    return {"state": "unknown", "reason": "no structured review metadata found"}


def _hardening_source(now: float) -> dict[str, Any]:
    source = _source_stat("hardening_note", HARDENING_NOTE, kind="markdown", now=now)
    if source.get("state") == "unknown":
        return source
    try:
        text = _read_bounded_text(HARDENING_NOTE)
    except Exception as exc:
        source["state"] = "unknown"
        source["issue"] = f"unreadable: {_short_error(exc)}"
        return source
    embedded = _extract_timestamp(text)
    if embedded:
        source["embedded_at"] = embedded.isoformat().replace("+00:00", "Z")
        if now >= embedded.timestamp() and now - embedded.timestamp() > STALE_AFTER_SECONDS:
            source["state"] = "stale"
            source["issue"] = "embedded timestamp is stale"
    return source


def _operator_truth_summary(*, session_id: str | None, ui_board_hint: str | None, now: float) -> dict[str, Any]:
    try:
        from api.operator_truth import build_operator_truth_payload

        payload = build_operator_truth_payload(session_id=session_id, ui_board_hint=ui_board_hint, now=now)
    except Exception as exc:
        return {"status": "unknown", "verified_at": now, "summary": "Truth unavailable", "api": "/api/operator/truth", "issue": _short_error(exc)}
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in STATUS_ORDER else "unknown"
    truth: dict[str, Any] = {
        "status": status,
        "verified_at": payload.get("verified_at", now),
        "summary": payload.get("summary") or f"Truth {status}",
        "api": "/api/operator/truth",
    }
    if payload.get("issues"):
        truth["issues"] = [str(issue) for issue in payload.get("issues", [])[:5]]
    return truth


def _source_stat(source_id: str, path: Any, *, kind: str, now: float | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": source_id,
        "kind": kind,
        "path": str(path) if path else "",
        "exists": False,
        "state": "unknown",
    }
    if not path:
        item["issue"] = "path unavailable"
        return item
    try:
        p = Path(path)
        item["exists"] = p.exists()
        if not item["exists"]:
            item["issue"] = "missing"
            return item
        if not p.is_file():
            item["issue"] = "not a regular file"
            return item
        item["mtime"] = p.stat().st_mtime
        item["state"] = "live"
        return item
    except Exception as exc:  # pragma: no cover - platform edge case
        item["issue"] = f"unreadable: {_short_error(exc)}"
        return item


def _read_bounded_text(path: Path, *, max_bytes: int = 200_000) -> str:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"source exceeds {max_bytes} byte limit")
    return path.read_text(encoding="utf-8-sig")


def _json_object(value: Any, issues: list[str], label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not (text.startswith("{") or text.startswith("[")):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        if issues is not None:
            issues.append(f"{label} malformed JSON: {exc.msg}")
        return None
    if isinstance(parsed, dict):
        return parsed
    if issues is not None:
        issues.append(f"{label} JSON is not an object")
    return None


def _extract_timestamp(text: str) -> datetime | None:
    match = _DATE_RE.search(text[:5000])
    if not match:
        return None
    date = match.group(1)
    clock = match.group(2) or "00:00:00"
    if len(clock) == 5:
        clock += ":00"
    try:
        return datetime.fromisoformat(f"{date}T{clock}+00:00").astimezone(timezone.utc)
    except ValueError:
        return None


def _same_or_under(path: str, root: Path) -> bool:
    if not path:
        return False
    try:
        p = Path(path).expanduser().resolve(strict=False)
        r = root.expanduser().resolve(strict=False)
        return p == r or p.is_relative_to(r)
    except Exception:
        path_text = str(path).rstrip("/")
        root_text = str(root).rstrip("/")
        return path_text == root_text or path_text.startswith(root_text + "/")


def _points_to_project(path: str) -> bool:
    return any(_same_or_under(path, root) for root in PROJECT_PATHS)


def _empty_counts() -> dict[str, int]:
    return {"triage": 0, "todo": 0, "ready": 0, "running": 0, "blocked": 0, "done": 0}


def _worst_status(statuses: Iterable[str]) -> str:
    worst = "live"
    for status in statuses:
        normalized = status if status in STATUS_ORDER else "unknown"
        if STATUS_ORDER[normalized] > STATUS_ORDER[worst]:
            worst = normalized
    return worst


def _clean_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _none_if_empty(value: Any) -> Any:
    text = _clean_text(value, "")
    return text if text else None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    items = []
    for item in raw[:MAX_LIST_ITEMS]:
        text = _clean_text(item, "")
        if text:
            items.append(_truncate(text, 240))
    return items


def _truncate(text: str, limit: int) -> str:
    text = _clean_text(text, "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _short_error(exc: BaseException) -> str:
    return _truncate(str(exc) or exc.__class__.__name__, 180)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = _clean_text(item, "")
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _dedupe_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for link in links:
        key = (link.get("label", ""), link.get("path", ""))
        if key[1] and key not in seen:
            seen.add(key)
            result.append(link)
    return result
