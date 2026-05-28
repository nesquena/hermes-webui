"""Read-only sidebar discoverability audit for Hermes WebUI sessions.

This module does not repair or mutate session state. It cross-checks the four
places that decide whether a session can be found from the WebUI sidebar:

- JSON sidecars under the WebUI session directory
- ``_index.json`` sidebar metadata
- canonical ``state.db`` rows/messages
- the live ``api.models.all_sessions()`` sidebar response, when available
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Iterable


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _message_count_from_payload(payload: dict) -> int:
    messages = payload.get("messages")
    if isinstance(messages, list):
        return len(messages)
    return _safe_int(payload.get("message_count"), 0)


def _record_from_mapping(mapping: dict, source_name: str) -> dict:
    sid = str(mapping.get("session_id") or mapping.get("id") or "").strip()
    if not sid:
        return {}
    return {
        "session_id": sid,
        "title": mapping.get("title"),
        "message_count": _message_count_from_payload(mapping),
        "source_tag": mapping.get("source_tag"),
        "session_source": mapping.get("session_source"),
        "source": mapping.get("source"),
        "is_cli_session": mapping.get("is_cli_session"),
        "parent_session_id": mapping.get("parent_session_id"),
        "pre_compression_snapshot": bool(mapping.get("pre_compression_snapshot")),
        "_lineage_root_id": mapping.get("_lineage_root_id"),
        "archived": bool(mapping.get("archived")),
        "project_id": mapping.get("project_id"),
        "workspace": mapping.get("workspace"),
        "_source_name": source_name,
    }


def _read_sidecars(session_dir: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not session_dir.exists():
        return records
    for path in sorted(p for p in session_dir.glob("*.json") if not p.name.startswith("_")):
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        record = _record_from_mapping(payload, "sidecar")
        if record:
            records[record["session_id"]] = record
    return records


def _read_index(session_dir: Path) -> dict[str, dict]:
    payload = _read_json(session_dir / "_index.json")
    records: dict[str, dict] = {}
    if not isinstance(payload, list):
        return records
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        record = _record_from_mapping(entry, "index")
        if record:
            records[record["session_id"]] = record
    return records


def _optional_expr(name: str, columns: set[str], fallback: str = "NULL") -> str:
    return name if name in columns else f"{fallback} AS {name}"


def _read_state_db(state_db_path: Path | None) -> dict[str, dict]:
    if state_db_path is None or not state_db_path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
            if "sessions" not in tables:
                return {}
            session_cols = {row[1] for row in conn.execute("pragma table_info(sessions)")}
            if "id" not in session_cols:
                return {}
            message_cols: set[str] = set()
            if "messages" in tables:
                message_cols = {row[1] for row in conn.execute("pragma table_info(messages)")}
            title_expr = _optional_expr("title", session_cols)
            source_expr = _optional_expr("source", session_cols)
            parent_expr = _optional_expr("parent_session_id", session_cols)
            msg_expr = _optional_expr("message_count", session_cols, "0")
            workspace_expr = _optional_expr("workspace", session_cols)
            rows = conn.execute(
                f"""
                SELECT id, {title_expr}, {source_expr}, {parent_expr}, {msg_expr}, {workspace_expr}
                FROM sessions
                """
            ).fetchall()
            message_counts: dict[str, int] = {}
            if {"session_id"}.issubset(message_cols):
                for row in conn.execute("SELECT session_id, COUNT(*) AS count FROM messages GROUP BY session_id"):
                    message_counts[str(row["session_id"])] = _safe_int(row["count"], 0)
            records: dict[str, dict] = {}
            for row in rows:
                sid = str(row["id"] or "").strip()
                if not sid:
                    continue
                count = message_counts.get(sid, _safe_int(row["message_count"], 0))
                records[sid] = {
                    "session_id": sid,
                    "title": row["title"],
                    "message_count": count,
                    "source": row["source"],
                    "source_tag": row["source"],
                    "session_source": row["source"],
                    "parent_session_id": row["parent_session_id"],
                    "workspace": row["workspace"],
                    "_source_name": "state_db",
                }
            return records
    except Exception:
        return {}


def _normalize_api_sessions(api_sessions: Iterable[dict] | None) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if api_sessions is None:
        try:
            from api.models import all_sessions

            api_sessions = all_sessions()
        except Exception:
            api_sessions = []
    for entry in api_sessions or []:
        if not isinstance(entry, dict):
            continue
        record = _record_from_mapping(entry, "api")
        if record:
            records[record["session_id"]] = record
    return records


def _merged_field(sid: str, stores: list[dict[str, dict]], field: str):
    for store in stores:
        value = store.get(sid, {}).get(field)
        if value not in (None, ""):
            return value
    return None


def _max_message_count(sid: str, stores: list[dict[str, dict]]) -> int:
    return max((_safe_int(store.get(sid, {}).get("message_count"), 0) for store in stores), default=0)


def _lineage_root(sid: str, parent_by_id: dict[str, str | None]) -> str:
    seen: set[str] = set()
    current = sid
    while current and current not in seen:
        seen.add(current)
        parent = parent_by_id.get(current)
        if not parent:
            return current
        current = parent
    return sid


def _webui_origin(*records: dict) -> bool:
    values: list[str] = []
    for record in records:
        for key in ("source", "source_tag", "session_source"):
            value = record.get(key)
            if value is not None:
                values.append(str(value).strip().lower())
    return "webui" in values


def _computed_is_cli_session(row: dict) -> bool:
    sources = {
        str(row.get(key) or "").strip().lower()
        for key in ("session_source", "source_tag", "raw_source", "source", "source_label")
    }
    if "webui" in sources:
        return False
    try:
        from api.agent_sessions import is_cli_session_row

        return is_cli_session_row(row)
    except Exception:
        source = str(row.get("session_source") or row.get("source_tag") or row.get("raw_source") or row.get("source") or "").strip().lower()
        return source == "cli"


def _new_item(session_id: str, kind: str, category: str, recommendation: str, **extra) -> dict:
    item = {
        "session_id": session_id,
        "kind": kind,
        "category": category,
        "recommendation": recommendation,
    }
    item.update(extra)
    return item


def audit_session_discoverability(
    session_dir: Path,
    state_db_path: Path | None = None,
    *,
    api_sessions: Iterable[dict] | None = None,
) -> dict:
    """Return a read-only cross-store discoverability report.

    The audit is intentionally diagnostic only. It reports cases where
    messageful sessions have no visible API/sidebar representative, stale source
    flags can put WebUI sessions into the CLI tab, and index/sidecar/state-db
    drift can make a session harder to resolve.
    """
    session_dir = Path(session_dir)
    sidecars = _read_sidecars(session_dir)
    index = _read_index(session_dir)
    state = _read_state_db(state_db_path)
    api = _normalize_api_sessions(api_sessions)
    stores = [sidecars, index, state, api]
    all_ids = set().union(*(store.keys() for store in stores))

    parent_by_id: dict[str, str | None] = {}
    for sid in all_ids:
        parent = _merged_field(sid, stores, "parent_session_id")
        parent_by_id[sid] = str(parent) if parent else None
    api_lineage_ids: set[str] = set()
    for sid, row in api.items():
        explicit_root = row.get("_lineage_root_id")
        if explicit_root:
            api_lineage_ids.add(str(explicit_root))
        current = sid
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            api_lineage_ids.add(current)
            current = parent_by_id.get(current) or ""

    items: list[dict] = []
    for sid in sorted(all_ids):
        message_count = _max_message_count(sid, stores)
        present_in = {
            "sidecar": sid in sidecars,
            "index": sid in index,
            "state_db": sid in state,
            "api": sid in api,
        }
        sidecar = sidecars.get(sid, {})
        index_row = index.get(sid, {})
        state_row = state.get(sid, {})
        api_row = api.get(sid, {})
        webui_origin = _webui_origin(sidecar, index_row, state_row, api_row)

        api_is_cli = api_row.get("is_cli_session") is True
        api_computed_is_cli = _computed_is_cli_session(api_row) if api_row else False
        index_is_cli = index_row.get("is_cli_session") is True
        sidecar_is_cli = sidecar.get("is_cli_session") is True
        if webui_origin and api_is_cli and api_computed_is_cli:
            items.append(_new_item(
                sid,
                "source_misclassified",
                "warning",
                "normalize_api_source_flags",
                message_count=message_count,
                state_source=state_row.get("source"),
                api_is_cli_session=api_row.get("is_cli_session"),
                api_computed_is_cli_session=api_computed_is_cli,
                index_is_cli_session=index_row.get("is_cli_session"),
                sidecar_is_cli_session=sidecar.get("is_cli_session"),
                present_in=present_in,
            ))
        elif webui_origin and (api_is_cli or index_is_cli or sidecar_is_cli):
            items.append(_new_item(
                sid,
                "persisted_source_flag_stale",
                "warning",
                "rewrite_persisted_sidebar_source_flags_or_ignore_route_normalizes",
                message_count=message_count,
                state_source=state_row.get("source"),
                api_is_cli_session=api_row.get("is_cli_session"),
                api_computed_is_cli_session=api_computed_is_cli,
                index_is_cli_session=index_row.get("is_cli_session"),
                sidecar_is_cli_session=sidecar.get("is_cli_session"),
                present_in=present_in,
            ))

        if message_count <= 0 or sid in api:
            continue

        lineage_root = _lineage_root(sid, parent_by_id)
        is_hidden_snapshot = bool(sidecar.get("pre_compression_snapshot") or index_row.get("pre_compression_snapshot"))
        if sid in api_lineage_ids or lineage_root in api_lineage_ids:
            continue

        if is_hidden_snapshot:
            items.append(_new_item(
                sid,
                "lineage_missing_visible_representative",
                "warning",
                "repair_lineage_or_expose_tip",
                message_count=message_count,
                lineage_root=lineage_root,
                present_in=present_in,
            ))
            continue

        if not present_in["sidecar"] and not present_in["index"] and present_in["state_db"]:
            items.append(_new_item(
                sid,
                "state_db_messageful_missing_sidecar",
                "warning",
                "materialize_sidecar_or_archive_state_row",
                message_count=message_count,
                lineage_root=lineage_root,
                present_in=present_in,
            ))
            continue

        items.append(_new_item(
            sid,
            "api_missing_messageful",
            "warning",
            "investigate_sidebar_filters_or_api_merge",
            message_count=message_count,
            lineage_root=lineage_root,
            present_in=present_in,
        ))

    summary = {
        "sessions_seen": len(all_ids),
        "messageful": sum(1 for sid in all_ids if _max_message_count(sid, stores) > 0),
        "visible_api": len(api),
        "warnings": sum(1 for item in items if item.get("category") == "warning"),
    }
    status = "warn" if summary["warnings"] else "ok"
    return {
        "status": status,
        "summary": summary,
        "stores": {
            "sidecar": len(sidecars),
            "index": len(index),
            "state_db": len(state),
            "api": len(api),
        },
        "items": items,
    }


def render_discoverability_markdown(report: dict) -> str:
    lines = [
        "# WebUI Session Discoverability Audit",
        "",
        f"Status: `{report.get('status')}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in (report.get("summary") or {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Stores", ""])
    for key, value in (report.get("stores") or {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Findings", ""])
    items = report.get("items") or []
    if items:
        lines.extend(["### By kind", ""])
        for kind, count in sorted(Counter(str(item.get("kind")) for item in items).items()):
            lines.append(f"- `{kind}`: {count}")
        lines.extend(["", "### Details", ""])
    if not items:
        lines.append("No discoverability findings.")
    else:
        for item in items:
            lines.append(
                f"- `{item.get('kind')}` `{item.get('session_id')}` "
                f"messages={item.get('message_count', 'n/a')} recommendation=`{item.get('recommendation')}`"
            )
            present = item.get("present_in")
            if isinstance(present, dict):
                lines.append(
                    "  - present_in: " + ", ".join(f"{k}={v}" for k, v in sorted(present.items()))
                )
    lines.append("")
    return "\n".join(lines)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Hermes WebUI session discoverability audit")
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, default=None)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    report = audit_session_discoverability(args.session_dir, state_db_path=args.state_db)
    text = render_discoverability_markdown(report) if args.format == "markdown" else json.dumps(report, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
