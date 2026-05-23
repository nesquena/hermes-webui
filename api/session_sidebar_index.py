"""Pure helpers for building sidebar session group indexes."""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any, Iterable


ARCHIVE_AFTER_DAY_CHOICES = (7, 14, 30, 90)
DEFAULT_ARCHIVE_AFTER_DAYS = 7
DEFAULT_ARCHIVE_LIMIT = 30
MAX_ARCHIVE_LIMIT = 100
SECONDS_PER_DAY = 86_400
WORKSPACE_GROUP_WORKSPACE = "workspace"
WORKSPACE_GROUP_CHATS = "chats"
VALID_WORKSPACE_GROUPS = {WORKSPACE_GROUP_WORKSPACE, WORKSPACE_GROUP_CHATS}


def normalize_archive_after_days(value: Any) -> int:
    """Return a supported archive age threshold, defaulting to 7 days."""
    if isinstance(value, bool):
        return DEFAULT_ARCHIVE_AFTER_DAYS
    if isinstance(value, int):
        days = value
    elif isinstance(value, str) and value.strip().isdigit():
        days = int(value.strip())
    else:
        return DEFAULT_ARCHIVE_AFTER_DAYS
    return days if days in ARCHIVE_AFTER_DAY_CHOICES else DEFAULT_ARCHIVE_AFTER_DAYS


def normalize_workspace_group(group: Any, *, workspace: Any = None) -> str:
    """Normalize the sidebar workspace grouping mode for a session row."""
    normalized = str(group).strip().lower() if group is not None else ""
    if normalized in VALID_WORKSPACE_GROUPS:
        return normalized
    return WORKSPACE_GROUP_WORKSPACE if workspace else WORKSPACE_GROUP_CHATS


def session_activity_ts(row: dict[str, Any]) -> float:
    """Return the user-visible activity timestamp for a compact session row."""
    for key in ("last_message_at", "updated_at", "created_at"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return 0


def workspace_key_for(workspace: Any = None) -> str | None:
    """Return the normalized workspace key, or None when no workspace is set."""
    return _normalize_workspace_path(workspace)


def session_is_current(
    row: dict[str, Any],
    *,
    server_time: float,
    session_archive_after_days: Any = None,
    current_session_id: str | None = None,
) -> bool:
    """Return whether a row belongs in current sidebar sessions."""
    archive_after_days = normalize_archive_after_days(session_archive_after_days)
    compact = _compact_sidebar_row(
        row,
        server_time=server_time,
        archive_after_days=archive_after_days,
        current_session_id=current_session_id,
    )
    return not compact.get("archived") and not compact["age_archived"]


def build_session_sidebar_index(
    rows: Iterable[dict[str, Any]],
    *,
    server_time: float,
    server_tz: str = "UTC",
    session_archive_after_days: Any = None,
    current_session_id: str | None = None,
    workspace_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build grouped current-session sidebar metadata without mutating rows."""
    archive_after_days = normalize_archive_after_days(session_archive_after_days)
    groups: dict[str, dict[str, Any]] = {}
    names = workspace_names or {}
    manual_archived_count = 0

    for row in rows:
        compact = _compact_sidebar_row(
            row,
            server_time=server_time,
            archive_after_days=archive_after_days,
            current_session_id=current_session_id,
        )
        group = groups.setdefault(
            compact["group_id"],
            _empty_group(compact["group_id"], compact["workspace"], names),
        )
        activity_ts = compact["activity_ts"]
        group["latest_activity_at"] = max(group["latest_activity_at"] or 0, activity_ts)

        if compact.get("archived"):
            group["manual_archived_count"] += 1
            manual_archived_count += 1
        elif compact["age_archived"]:
            group["archive_count"] += 1
            group["archive"]["count"] += 1
            group["archive"]["has_more"] = True
            group["archive"]["next_offset"] = 0
        else:
            group["sessions"].append(compact)
            group["current_count"] += 1

    for group in groups.values():
        group["sessions"].sort(key=_sidebar_sort_key, reverse=True)

    ordered_groups = sorted(
        groups.values(),
        key=lambda group: (group["latest_activity_at"] or 0, group["group_id"]),
        reverse=True,
    )
    return {
        "groups": ordered_groups,
        "manual_archived": {"count": manual_archived_count},
        "archive_after_days": archive_after_days,
        "server_time": server_time,
        "server_tz": server_tz,
        "session_archive_after_days": archive_after_days,
    }


def build_session_archive_page(
    rows: Iterable[dict[str, Any]],
    *,
    group_id: str,
    server_time: float,
    session_archive_after_days: Any = None,
    limit: Any = DEFAULT_ARCHIVE_LIMIT,
    cursor: str | None = None,
    current_session_id: str | None = None,
) -> dict[str, Any]:
    """Return one cursor-paginated page of age-archived rows for a group."""
    archive_after_days = normalize_archive_after_days(session_archive_after_days)
    archive_rows = []
    for row in rows:
        compact = _compact_sidebar_row(
            row,
            server_time=server_time,
            archive_after_days=archive_after_days,
            current_session_id=current_session_id,
        )
        if compact["group_id"] == group_id and compact["age_archived"] and not compact.get("archived"):
            archive_rows.append(compact)

    archive_rows.sort(key=_sidebar_sort_key, reverse=True)
    start = _cursor_start_index(archive_rows, cursor)
    page_limit = _normalize_archive_limit(limit)
    page = archive_rows[start:start + page_limit]
    remaining_count = max(0, len(archive_rows) - (start + len(page)))
    next_cursor = _encode_cursor(page[-1]) if remaining_count and page else None
    return {
        "group_id": group_id,
        "key": group_id,
        "sessions": page,
        "next_cursor": next_cursor,
        "remaining_count": remaining_count,
        "archive": {
            "count": len(archive_rows),
            "has_more": remaining_count > 0,
            "next_offset": start + len(page) if remaining_count > 0 else None,
            "cursor": next_cursor,
        },
    }


def build_archive_page(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Backward-compatible alias for build_session_archive_page."""
    return build_session_archive_page(*args, **kwargs)


def _normalize_workspace_path(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return text


def _group_id_for(row: dict[str, Any]) -> tuple[str, str | None]:
    workspace = _normalize_workspace_path(row.get("workspace"))
    group = normalize_workspace_group(row.get("workspace_group"), workspace=workspace)
    if group == WORKSPACE_GROUP_WORKSPACE and workspace:
        return f"workspace:{workspace}", workspace
    return WORKSPACE_GROUP_CHATS, None


def _compact_sidebar_row(
    row: dict[str, Any],
    *,
    server_time: float,
    archive_after_days: int,
    current_session_id: str | None,
) -> dict[str, Any]:
    group_id, workspace = _group_id_for(row)
    activity_ts = session_activity_ts(row)
    compact = {
        key: row[key]
        for key in (
            "session_id",
            "title",
            "model",
            "model_provider",
            "message_count",
            "created_at",
            "updated_at",
            "last_message_at",
            "pinned",
            "archived",
            "project_id",
            "profile",
            "unread",
            "is_streaming",
            "active_stream_id",
            "has_pending_user_message",
            "is_cli_session",
            "source_tag",
            "session_source",
            "source_label",
            "read_only",
        )
        if key in row
    }
    compact["workspace"] = workspace
    compact["activity_ts"] = activity_ts
    compact["group_id"] = group_id
    compact["id"] = row.get("session_id")
    compact["title"] = row.get("title") or "Untitled"
    compact["profile"] = row.get("profile")
    compact["avatar"] = row.get("avatar")
    compact["updated_at"] = row.get("updated_at")
    compact["age_seconds"] = max(0, server_time - activity_ts) if activity_ts else None
    compact["pinned"] = bool(row.get("pinned"))
    compact["unread"] = bool(row.get("unread"))
    compact["streaming"] = bool(row.get("streaming") or row.get("is_streaming") or row.get("active_stream_id"))
    compact["pending"] = bool(row.get("pending") or row.get("has_pending_user_message") or row.get("pending_user_message"))
    compact["age_archived"] = _is_age_archived(
        compact,
        server_time=server_time,
        archive_after_days=archive_after_days,
        current_session_id=current_session_id,
    )
    return compact


def _is_age_archived(
    row: dict[str, Any],
    *,
    server_time: float,
    archive_after_days: int,
    current_session_id: str | None,
) -> bool:
    if row.get("archived"):
        return False
    if (
        row.get("pinned")
        or row.get("unread")
        or row.get("is_streaming")
        or row.get("active_stream_id")
        or row.get("streaming")
        or row.get("has_pending_user_message")
        or row.get("pending_user_message")
        or row.get("pending")
    ):
        return False
    if current_session_id and row.get("session_id") == current_session_id:
        return False
    return (server_time - row["activity_ts"]) > (archive_after_days * SECONDS_PER_DAY)


def _empty_group(
    group_id: str,
    workspace: str | None,
    workspace_names: dict[str, str],
) -> dict[str, Any]:
    is_workspace = group_id.startswith("workspace:")
    return {
        "group_id": group_id,
        "key": group_id,
        "kind": "project" if is_workspace else "chats",
        "type": "workspace" if is_workspace else "chats",
        "name": _workspace_name(workspace, workspace_names) if is_workspace else "Chats",
        "label": _workspace_name(workspace, workspace_names) if is_workspace else "Chats",
        "workspace": workspace,
        "current_count": 0,
        "archive_count": 0,
        "manual_archived_count": 0,
        "archive": {
            "count": 0,
            "has_more": False,
            "next_offset": None,
            "cursor": None,
        },
        "sessions": [],
        "latest_activity_at": 0,
    }


def _workspace_name(workspace: str | None, workspace_names: dict[str, str]) -> str:
    if workspace and workspace in workspace_names:
        return workspace_names[workspace]
    if workspace:
        return Path(workspace).name or workspace
    return "Workspace"


def _sidebar_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    return (row["activity_ts"], str(row.get("session_id") or ""))


def _encode_cursor(row: dict[str, Any]) -> str:
    payload = {"activity_ts": row["activity_ts"], "session_id": str(row.get("session_id") or "")}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[float, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        activity_ts = float(payload["activity_ts"])
        if not math.isfinite(activity_ts):
            return None
        return (activity_ts, str(payload["session_id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _cursor_start_index(rows: list[dict[str, Any]], cursor: str | None) -> int:
    cursor_key = _decode_cursor(cursor)
    if cursor_key is None:
        return 0
    for index, row in enumerate(rows):
        if _sidebar_sort_key(row) < cursor_key:
            return index
    return len(rows)


def _normalize_archive_limit(limit: Any) -> int:
    if isinstance(limit, bool):
        return DEFAULT_ARCHIVE_LIMIT
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_ARCHIVE_LIMIT
    if parsed <= 0:
        return DEFAULT_ARCHIVE_LIMIT
    return min(parsed, MAX_ARCHIVE_LIMIT)
