#!/usr/bin/env python3
"""Migrate Hermes WebUI sessions from JSON sidecars to SQLite.

Usage:
    python3 scripts/migrate_sessions_to_sqlite.py /path/to/webui-mvp/sessions [--commit]

With --commit, original .json files are moved to a ``json-backup/``
subdirectory after a successful round-trip verification. Without it, the
migration runs in dry-run mode: it builds the SQLite DB and verifies every
session round-trips correctly, but leaves the JSON files untouched.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# Allow running from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.webui_session_sqlite import WebUISqliteSessionDB, _is_safe_session_id


def _load_json_session(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"SKIP {path.name}: cannot read/parse: {e}")
        return None
    if not isinstance(data, dict):
        print(f"SKIP {path.name}: not a dict")
        return None
    sid = data.get("session_id") or path.stem
    if not _is_safe_session_id(sid):
        print(f"SKIP {path.name}: unsafe session_id {sid!r}")
        return None
    if "session_id" not in data:
        data["session_id"] = sid
    return data


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sessions_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for key in ("messages", "tool_calls", "context_messages"):
        if _canon(a.get(key)) != _canon(b.get(key)):
            return False
    ignored = {"messages", "tool_calls", "context_messages", "last_message_at", "message_count"}
    for key, value in a.items():
        if key in ignored or value is None:
            continue
        if key not in b or _canon(value) != _canon(b[key]):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate WebUI sessions from JSON to SQLite")
    parser.add_argument("session_dir", type=Path, help="Path to webui-mvp/sessions directory")
    parser.add_argument("--commit", action="store_true", help="Move JSON files to json-backup/ after verification")
    parser.add_argument("--db-name", default="sessions.db", help="SQLite database filename")
    args = parser.parse_args()

    session_dir = args.session_dir.expanduser().resolve()
    if not session_dir.is_dir():
        print(f"ERROR: {session_dir} is not a directory")
        return 1

    json_files = sorted(p for p in session_dir.glob("*.json") if not p.name.startswith("_"))
    if not json_files:
        print("No session JSON files found.")
        return 0

    print(f"Found {len(json_files)} JSON session files in {session_dir}")
    db = WebUISqliteSessionDB(session_dir=session_dir, db_name=args.db_name)

    t0 = time.time()
    migrated = 0
    failed = 0
    for path in json_files:
        data = _load_json_session(path)
        if data is None:
            failed += 1
            continue
        sid = data["session_id"]
        db.write_session(data)
        loaded = db.read_session(sid)
        if loaded is None or not _sessions_equal(data, loaded):
            print(f"FAIL {sid}: round-trip verification failed")
            failed += 1
            continue
        migrated += 1
        print(f"OK   {sid}")

    elapsed = time.time() - t0
    print(f"\nMigrated {migrated}/{len(json_files)} sessions in {elapsed:.2f}s")
    if failed:
        print(f"FAILURES: {failed}")
        return 1

    if args.commit:
        backup_dir = session_dir / "json-backup"
        backup_dir.mkdir(exist_ok=True)
        for path in json_files:
            dest = backup_dir / path.name
            shutil.move(str(path), str(dest))
        print(f"Moved {len(json_files)} JSON files to {backup_dir}")
    else:
        print("Dry-run complete. Pass --commit to move JSON files to json-backup/.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
