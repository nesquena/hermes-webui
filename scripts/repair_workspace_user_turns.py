#!/usr/bin/env python3
"""Repair workspace-prefixed and duplicated user turns in WebUI transcripts.

WebUI may store model-facing user messages prefixed with
``[Workspace: /path]``. That prefix is useful for the model, but it should not
remain in display transcripts. Older data can also contain adjacent duplicate
user bubbles when a display turn and a workspace-prefixed model turn were merged
as separate messages.

This script cleans those historical artifacts in WebUI sidecar JSON files and,
when requested, the SQLite session database.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

_WORKSPACE_PREFIX_RE = re.compile(r"^\s*\[Workspace:[^\]]+\]\s*")


def strip_workspace_prefix(text: str | None) -> str:
    """Return user text without WebUI's model-facing workspace prefix."""
    return _WORKSPACE_PREFIX_RE.sub("", str(text or "")).strip()


def normalized_text(text: str | None) -> str:
    return " ".join(strip_workspace_prefix(text).split())


def clean_message_list(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Strip workspace prefixes and remove adjacent duplicate user turns."""
    cleaned: list[dict[str, Any]] = []
    stats = {"stripped_workspace_prefixes": 0, "removed_adjacent_user_duplicates": 0}

    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue

        next_message = dict(message)
        if next_message.get("role") == "user":
            original = str(next_message.get("content") or "")
            stripped = strip_workspace_prefix(original)
            if stripped and stripped != original:
                next_message["content"] = stripped
                stats["stripped_workspace_prefixes"] += 1

            if cleaned and isinstance(cleaned[-1], dict) and cleaned[-1].get("role") == "user":
                previous_text = normalized_text(str(cleaned[-1].get("content") or ""))
                current_text = normalized_text(str(next_message.get("content") or ""))
                if previous_text and previous_text == current_text:
                    stats["removed_adjacent_user_duplicates"] += 1
                    continue

        cleaned.append(next_message)

    return cleaned, stats


def _backup_file(path: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)


def repair_sidecars(sessions_dir: Path, backup_dir: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    changed: list[dict[str, Any]] = []
    for path in sorted(sessions_dir.glob("*.json")):
        if path.name == "_index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        messages = data.get("messages")
        if not isinstance(messages, list):
            continue
        cleaned, stats = clean_message_list(messages)
        if stats["stripped_workspace_prefixes"] or stats["removed_adjacent_user_duplicates"]:
            changed.append({"file": path.name, **stats, "messages_after": len(cleaned)})
            if not dry_run:
                if backup_dir is not None:
                    _backup_file(path, backup_dir)
                data["messages"] = cleaned
                data["message_count"] = len(cleaned)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"changed_sidecars": changed}


def repair_state_db(state_db: Path, backup_dir: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    if not state_db.exists():
        return {"updated_workspace_prefix_user_messages": 0, "removed_adjacent_user_duplicates": 0}
    if not dry_run and backup_dir is not None:
        _backup_file(state_db, backup_dir)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(state_db) + suffix)
            if extra.exists():
                _backup_file(extra, backup_dir)

    con = sqlite3.connect(state_db)
    con.row_factory = sqlite3.Row
    updated = 0
    deleted = 0
    affected_sessions: set[str] = set()
    try:
        rows = con.execute(
            "select id, session_id, content from messages "
            "where role = 'user' and content like '[Workspace:%' order by session_id, id"
        ).fetchall()
        duplicate_ids: list[int] = []
        for row in rows:
            stripped = strip_workspace_prefix(row["content"])
            if stripped and stripped != row["content"]:
                updated += 1
                affected_sessions.add(row["session_id"])
                if not dry_run:
                    con.execute("update messages set content = ? where id = ?", (stripped, row["id"]))

        for sid_row in con.execute("select distinct session_id from messages order by session_id").fetchall():
            sid = sid_row["session_id"]
            previous = None
            for row in con.execute("select id, role, content from messages where session_id = ? order by id", (sid,)).fetchall():
                if previous and previous["role"] == "user" and row["role"] == "user":
                    if normalized_text(previous["content"]) and normalized_text(previous["content"]) == normalized_text(row["content"]):
                        duplicate_ids.append(row["id"])
                        affected_sessions.add(sid)
                        continue
                previous = row

        deleted = len(duplicate_ids)
        if not dry_run:
            for message_id in duplicate_ids:
                con.execute("delete from messages where id = ?", (message_id,))
            for sid in sorted(affected_sessions):
                message_count = con.execute("select count(*) from messages where session_id = ?", (sid,)).fetchone()[0]
                tool_count = con.execute(
                    "select count(*) from messages where session_id = ? and role = 'tool'", (sid,)
                ).fetchone()[0]
                con.execute(
                    "update sessions set message_count = ?, tool_call_count = ? where id = ?",
                    (message_count, tool_count, sid),
                )
            con.commit()
    finally:
        con.close()

    return {
        "updated_workspace_prefix_user_messages": updated,
        "removed_adjacent_user_duplicates": deleted,
        "affected_sessions": sorted(affected_sessions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-dir", type=Path, help="WebUI sidecar session directory")
    parser.add_argument("--state-db", type=Path, help="Hermes SQLite state.db path")
    parser.add_argument("--backup-dir", type=Path, help="Directory for backups before mutation")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = parser.parse_args()

    if not args.sessions_dir and not args.state_db:
        parser.error("provide --sessions-dir, --state-db, or both")

    backup_dir = args.backup_dir
    if backup_dir is None and not args.dry_run:
        backup_dir = Path("backups") / f"workspace-user-turn-repair-{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    report: dict[str, Any] = {"dry_run": args.dry_run}
    if backup_dir is not None:
        report["backup_dir"] = str(backup_dir)
    if args.sessions_dir:
        report.update(repair_sidecars(args.sessions_dir, backup_dir, args.dry_run))
    if args.state_db:
        report["state_db"] = repair_state_db(args.state_db, backup_dir, args.dry_run)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
