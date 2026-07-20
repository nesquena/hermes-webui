"""Read-only, profile-scoped project context projections.

Project membership is confirmed by both the compact WebUI index and each
session's bounded metadata prefix. Message content is then read from the active
profile's ``state.db`` through bounded per-session tail queries. No Session
objects are loaded and no transcript sidecars are parsed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from api.helpers import _redact_text
from api.models import _read_metadata_json_prefix, is_safe_session_id
from api.profiles import _profiles_match


_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20
_MIN_SCAN_ROWS = 20
_SCAN_MULTIPLIER = 5
_ALLOWED_ROLES = frozenset({"user", "assistant"})
_SYNTHETIC_SESSION_SOURCES = frozenset({"cron", "subagent", "delegation"})
_SYNTHETIC_CONTENT_PREFIXES = (
    "[important:",
    "[async delegation",
    "[context compaction",
    "[your active task list was preserved across context compression]",
    "[session arc summary",
    "[system:",
)
_MAX_ITERATION_SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. "
    "Please provide a final response summarizing what you've found and accomplished "
    "so far, without calling any more tools."
)
_CURSOR_VERSION = 2
_CLASSIFIER_VERSION = "project_context_v1"


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _load_json_list_with_status(path: Path) -> tuple[list[dict[str, Any]], bool, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], False, 0
    if not isinstance(value, list):
        return [], False, 0
    rows = [row for row in value if isinstance(row, dict)]
    return rows, True, len(value) - len(rows)


def _normalize_roles(roles: Any) -> tuple[str, ...]:
    if roles is None:
        return ("user",)
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, (list, tuple, set, frozenset)):
        raise ValueError("roles must be a list containing user and/or assistant")
    normalized = tuple(dict.fromkeys(str(role).strip().lower() for role in roles if str(role).strip()))
    if not normalized or any(role not in _ALLOWED_ROLES for role in normalized):
        raise ValueError("roles may contain only user and assistant")
    return normalized


def _normalize_limit(limit: Any) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer between 1 and 20") from exc
    if value < 1:
        raise ValueError("limit must be an integer between 1 and 20")
    return min(value, _MAX_LIMIT)


def _cursor_scope(project_id: str, profile: str, roles: tuple[str, ...], include_archived: bool) -> str:
    payload = json.dumps(
        [project_id, profile, sorted(roles), bool(include_archived)],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _encode_cursor(timestamp: float, session_id: str, message_rowid: int, scope: str) -> str:
    payload = json.dumps(
        [_CURSOR_VERSION, scope, timestamp, session_id, message_rowid],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: Any, scope: str) -> tuple[float, str, int] | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("before must be an opaque cursor returned by this contract")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if (
            not isinstance(decoded, list)
            or len(decoded) != 5
            or decoded[0] != _CURSOR_VERSION
            or decoded[1] != scope
        ):
            raise ValueError
        timestamp = float(decoded[2])
        session_id = str(decoded[3])
        rowid = int(decoded[4])
        if not math.isfinite(timestamp) or not is_safe_session_id(session_id) or rowid < 1:
            raise ValueError
        return timestamp, session_id, rowid
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("before must be an opaque cursor returned by this contract") from exc


def _sidecar_metadata(path: Path) -> dict[str, Any] | None:
    try:
        prefix = _read_metadata_json_prefix(path)
        if not prefix:
            return None
        value = json.loads(prefix)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _project_for_profile(projects_file: Path, project_id: str, profile: str) -> dict[str, Any]:
    project = next(
        (
            row
            for row in _load_json_list(projects_file)
            if str(row.get("project_id") or "") == project_id
            and _profiles_match(row.get("profile"), profile)
        ),
        None,
    )
    if project is None:
        # Deliberately identical for absent and foreign-profile projects.
        raise LookupError("Project not found")
    return project


def _eligible_session_metadata(
    *,
    session_index_file: Path,
    session_dir: Path,
    project_id: str,
    profile: str,
    include_archived: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "index_rows_considered": 0,
        "invalid_index_rows": 0,
        "missing_sidecars": 0,
        "unreadable_sidecars": 0,
        "membership_mismatches": 0,
        "archived_sessions_excluded": 0,
    }
    eligible: dict[str, dict[str, Any]] = {}
    index_rows, index_available, malformed_rows = _load_json_list_with_status(session_index_file)
    diagnostics["session_index_unavailable"] = 0 if index_available else 1
    diagnostics["invalid_index_rows"] += malformed_rows
    for row in index_rows:
        if "project_id" in row and row.get("project_id") != project_id:
            continue
        if "profile" in row and not _profiles_match(row.get("profile"), profile):
            continue
        sid = str(row.get("session_id") or "")
        if not is_safe_session_id(sid):
            diagnostics["invalid_index_rows"] += 1
            continue
        if row.get("project_id") != project_id or not _profiles_match(row.get("profile"), profile):
            continue
        diagnostics["index_rows_considered"] += 1
        sidecar_path = session_dir / f"{sid}.json"
        if not sidecar_path.is_file():
            diagnostics["missing_sidecars"] += 1
            continue
        sidecar = _sidecar_metadata(sidecar_path)
        if sidecar is None:
            diagnostics["unreadable_sidecars"] += 1
            continue
        if (
            str(sidecar.get("session_id") or "") != sid
            or sidecar.get("project_id") != project_id
            or not _profiles_match(sidecar.get("profile"), profile)
        ):
            diagnostics["membership_mismatches"] += 1
            continue
        archived = bool(sidecar.get("archived", row.get("archived", False)))
        if archived and not include_archived:
            diagnostics["archived_sessions_excluded"] += 1
            continue
        eligible[sid] = {
            "session_id": sid,
            "title": str(sidecar.get("title") or row.get("title") or "Untitled"),
            "workspace": sidecar.get("workspace") if sidecar.get("workspace") is not None else row.get("workspace"),
            "profile": profile,
            "project_id": project_id,
        }
    return eligible, diagnostics


def _state_db_sources(
    conn: sqlite3.Connection,
    session_ids: list[str],
) -> tuple[dict[str, str], int]:
    sources: dict[str, str] = {}
    for offset in range(0, len(session_ids), 500):
        chunk = session_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        cursor = conn.execute(
            f"SELECT id, source FROM sessions WHERE id IN ({placeholders})",
            chunk,
        )
        for row in cursor.fetchall():
            sources[str(row["id"])] = str(row["source"] or "").strip().lower()
    return sources, len(session_ids) - len(sources)


def _is_synthetic_content(content: Any) -> bool:
    text = str(content or "").strip()
    if not text or text == _MAX_ITERATION_SUMMARY_REQUEST:
        return True
    lowered = text.lower()
    return any(lowered.startswith(prefix) for prefix in _SYNTHETIC_CONTENT_PREFIXES)


def _message_tail_sql(roles: tuple[str, ...], before: tuple[float, str, int] | None) -> tuple[str, list[Any]]:
    role_placeholders = ",".join("?" for _ in roles)
    clauses = [
        f"LOWER(role) IN ({role_placeholders})",
        "timestamp IS NOT NULL",
    ]
    params: list[Any] = list(roles)
    if before is not None:
        timestamp, cursor_sid, cursor_rowid = before
        clauses.append(
            "(timestamp < ? OR "
            "(timestamp = ? AND session_id < ?) OR "
            "(timestamp = ? AND session_id = ? AND rowid < ?))"
        )
        params.extend([timestamp, timestamp, cursor_sid, timestamp, cursor_sid, cursor_rowid])
    sql = (
        "SELECT rowid AS message_rowid, session_id, role, content, timestamp "
        "FROM messages WHERE session_id = ? AND "
        + " AND ".join(clauses)
        + " ORDER BY timestamp DESC, session_id DESC, rowid DESC LIMIT ?"
    )
    return sql, params


def recent_project_messages(
    *,
    project_id: str,
    profile: str,
    projects_file: Path | str,
    session_index_file: Path | str,
    session_dir: Path | str,
    state_db_path: Path | str,
    roles: Any = None,
    limit: Any = _DEFAULT_LIMIT,
    before: Any = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Return the latest genuine messages across every eligible project session.

    Results are newest-first by ``timestamp``, then ``session_id``, then SQLite
    message ``rowid`` (all descending). The row id is used only inside the opaque
    cursor and is never exposed as message content or metadata.
    """
    project_id = str(project_id or "").strip()
    profile = str(profile or "").strip() or "default"
    if not project_id:
        raise ValueError("project_id is required")
    normalized_roles = _normalize_roles(roles)
    normalized_limit = _normalize_limit(limit)
    include_archived = bool(include_archived)
    cursor_scope = _cursor_scope(project_id, profile, normalized_roles, include_archived)
    decoded_before = _decode_cursor(before, cursor_scope)
    projects_path = Path(projects_file)
    index_path = Path(session_index_file)
    sessions_path = Path(session_dir)
    db_path = Path(state_db_path)

    project = _project_for_profile(projects_path, project_id, profile)
    eligible, diagnostics = _eligible_session_metadata(
        session_index_file=index_path,
        session_dir=sessions_path,
        project_id=project_id,
        profile=profile,
        include_archived=include_archived,
    )
    diagnostics.update(
        {
            "eligible_sessions": 0,
            "missing_state_db_sessions": 0,
            "candidate_rows_read": 0,
            "classifier_scan_saturated_sessions": 0,
            "invalid_timestamp_rows": 0,
        }
    )

    rows: list[dict[str, Any]] = []
    if eligible and db_path.is_file():
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                session_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
                message_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
                if not {"id", "source"}.issubset(session_columns) or not {
                    "session_id",
                    "role",
                    "content",
                    "timestamp",
                }.issubset(message_columns):
                    diagnostics["state_db_schema_unavailable"] = 1
                else:
                    sources, missing_count = _state_db_sources(conn, list(eligible))
                    diagnostics["missing_state_db_sessions"] = missing_count
                    for sid, metadata in eligible.items():
                        source = sources.get(sid)
                        if source is None or source in _SYNTHETIC_SESSION_SOURCES:
                            continue
                        sql, tail_params = _message_tail_sql(normalized_roles, decoded_before)
                        scan_limit = max(_MIN_SCAN_ROWS, normalized_limit * _SCAN_MULTIPLIER)
                        fetched = conn.execute(
                            sql,
                            [sid, *tail_params, scan_limit + 1],
                        ).fetchall()
                        diagnostics["candidate_rows_read"] += len(fetched)
                        genuine_count = 0
                        for row in fetched[:scan_limit]:
                            if _is_synthetic_content(row["content"]):
                                continue
                            try:
                                timestamp = float(row["timestamp"])
                            except (TypeError, ValueError):
                                diagnostics["invalid_timestamp_rows"] += 1
                                continue
                            if not math.isfinite(timestamp):
                                diagnostics["invalid_timestamp_rows"] += 1
                                continue
                            rows.append(
                                {
                                    "timestamp": timestamp,
                                    "role": str(row["role"]).lower(),
                                    "content": _redact_text(str(row["content"]), _enabled=True),
                                    "session_id": sid,
                                    "session_title": _redact_text(metadata["title"], _enabled=True),
                                    "workspace": metadata["workspace"],
                                    "profile": profile,
                                    "project_id": project_id,
                                    "_message_rowid": int(row["message_rowid"]),
                                }
                            )
                            genuine_count += 1
                            if genuine_count >= normalized_limit:
                                break
                        if len(fetched) > scan_limit and genuine_count < normalized_limit:
                            diagnostics["classifier_scan_saturated_sessions"] += 1
                    diagnostics["eligible_sessions"] = sum(
                        1
                        for sid, source in sources.items()
                        if sid in eligible and source not in _SYNTHETIC_SESSION_SOURCES
                    )
        except sqlite3.Error:
            diagnostics["state_db_read_error"] = 1
    elif eligible:
        diagnostics["state_db_unavailable"] = 1
        diagnostics["missing_state_db_sessions"] = len(eligible)

    rows.sort(
        key=lambda row: (row["timestamp"], row["session_id"], row["_message_rowid"]),
        reverse=True,
    )
    selected = rows[:normalized_limit]
    next_before = None
    if selected:
        last = selected[-1]
        next_before = _encode_cursor(
            last["timestamp"],
            last["session_id"],
            last["_message_rowid"],
            cursor_scope,
        )
    for row in selected:
        row.pop("_message_rowid", None)

    partial_keys = (
        "invalid_index_rows",
        "session_index_unavailable",
        "missing_sidecars",
        "unreadable_sidecars",
        "membership_mismatches",
        "missing_state_db_sessions",
        "state_db_schema_unavailable",
        "state_db_read_error",
        "state_db_unavailable",
        "classifier_scan_saturated_sessions",
        "invalid_timestamp_rows",
    )
    partial = any(int(diagnostics.get(key, 0) or 0) > 0 for key in partial_keys)
    return {
        "project_id": project_id,
        "project_name": project.get("name"),
        "profile": profile,
        "roles": list(normalized_roles),
        "include_archived": include_archived,
        "classifier": _CLASSIFIER_VERSION,
        "order": "timestamp_desc_session_id_desc_message_id_desc",
        "messages": selected,
        "next_before": next_before,
        "partial": partial,
        "diagnostics": diagnostics,
    }
