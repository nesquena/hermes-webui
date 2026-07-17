"""WebUI/Hermex cron notification API helpers.

Reads the profile-local cron notification JSONL files written by Hermes Agent's
``cron.webui_notifications`` module.  This file deliberately contains only
filesystem-level helpers and leaves HTTP concerns in ``api.routes``.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # pragma: no cover - platform fallback.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_MAX_LIMIT = 200
_MAX_SCAN_RECORDS = 2000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def notification_path(home: str | Path) -> Path:
    return Path(home).expanduser() / "cron" / "notifications.jsonl"


def profile_name_for_home(home: str | Path) -> str:
    p = Path(home).expanduser()
    if p.parent.name == "profiles" and p.name:
        return p.name
    return "default"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _read_records(home: Path) -> list[dict[str, Any]]:
    path = notification_path(home)
    if not path.exists():
        return []
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    with open(lock, "a+", encoding="utf-8") as lock_fh:
        try:
            os.chmod(lock, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
        try:
            raw_records: deque[str] = deque(maxlen=_MAX_SCAN_RECORDS)
            with open(path, "r", encoding="utf-8") as fh:
                raw_records.extend(fh)
            for raw in raw_records:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                row = dict(row)
                row.setdefault("profile", profile_name_for_home(home))
                records.append(row)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    return records


def _rewrite_mark_read(home: Path, notification_id: str | None, *, read_all: bool = False) -> tuple[list[dict[str, Any]], int]:
    path = notification_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8"):
        pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    now = utc_now_iso()
    updated: list[dict[str, Any]] = []
    changed = 0
    with open(lock, "a+", encoding="utf-8") as lock_fh:
        try:
            os.chmod(lock, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            rows: list[tuple[str | None, dict[str, Any] | None]] = []
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        rows.append((raw, None))
                        continue
                    if not isinstance(row, dict):
                        rows.append((raw, None))
                        continue
                    row = dict(row)
                    row_id = str(row.get("id") or "")
                    should_mark = read_all or (notification_id is not None and row_id == str(notification_id))
                    if should_mark and not row.get("read_at"):
                        row["read_at"] = now
                        changed += 1
                    if should_mark:
                        row.setdefault("profile", profile_name_for_home(home))
                        updated.append(dict(row))
                    rows.append((None, row))

            fd, tmp_name = tempfile.mkstemp(prefix="notifications.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    for raw, row in rows:
                        if row is not None:
                            tmp.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                        elif raw:
                            tmp.write(raw if raw.endswith("\n") else raw + "\n")
                    tmp.flush()
                    try:
                        os.fsync(tmp.fileno())
                    except OSError:
                        pass
                os.replace(tmp_name, path)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    return updated, changed


def _active_base_home(active_home: Path) -> Path:
    if active_home.parent.name == "profiles":
        return active_home.parent.parent
    return active_home


def visible_profile_homes(*, all_profiles: bool = False) -> list[tuple[str, Path]]:
    """Return visible profile homes for notification reads.

    Default is the active request profile only.  ``all_profiles`` mirrors the
    existing WebUI cross-profile opt-in and is ignored in isolated profile mode.
    """
    from api.profiles import (
        _is_isolated_profile_mode,
        get_active_hermes_home,
        get_active_profile_name,
        get_hermes_home_for_profile,
        list_profiles_api,
    )

    active_name = get_active_profile_name() or "default"
    active_home = Path(get_active_hermes_home()).expanduser()
    homes: list[tuple[str, Path]] = [(active_name, active_home)]
    seen = {str(active_home.resolve()) if active_home.exists() else str(active_home)}
    if not all_profiles or _is_isolated_profile_mode():
        return homes

    names = {"default"}
    for row in list_profiles_api():
        if isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            if name:
                names.add(name)
    profiles_dir = _active_base_home(active_home) / "profiles"
    if profiles_dir.is_dir():
        for child in profiles_dir.iterdir():
            if child.is_dir() and child.name:
                names.add(child.name)

    for name in sorted(names):
        try:
            home = Path(get_hermes_home_for_profile(name)).expanduser()
        except Exception:
            continue
        key = str(home.resolve()) if home.exists() else str(home)
        if key in seen:
            continue
        seen.add(key)
        homes.append((name, home))
    return homes


def notification_summary(*, limit: int = 50, unread_only: bool = False, all_profiles: bool = False) -> dict[str, Any]:
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 50
    cap = max(1, min(cap, _MAX_LIMIT))
    records: list[dict[str, Any]] = []
    unread_by_profile: dict[str, int] = {}
    for profile, home in visible_profile_homes(all_profiles=all_profiles):
        profile_records = _read_records(home)
        # Newer appended rows win ties from legacy second-resolution timestamps.
        for row in reversed(profile_records):
            row.setdefault("profile", profile)
            if not row.get("read_at"):
                unread_by_profile[row["profile"]] = unread_by_profile.get(row["profile"], 0) + 1
            if unread_only and row.get("read_at"):
                continue
            records.append(row)
    records.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return {
        "notifications": records[:cap],
        "unread_count": sum(unread_by_profile.values()),
        "unread_by_profile": unread_by_profile,
    }


def mark_read(notification_id: str, *, profile: str | None = None) -> dict[str, Any] | None:
    if not notification_id:
        return None
    visible = visible_profile_homes(all_profiles=bool(profile))
    if profile:
        requested = str(profile).strip()
        match = next((home for name, home in visible if name == requested), None)
        if match is None:
            return None
        home = match
    else:
        if not visible:
            return None
        home = visible[0][1]
    updated, _changed = _rewrite_mark_read(home, notification_id)
    for row in updated:
        if str(row.get("id") or "") == str(notification_id):
            return row
    return None


def mark_all_read(*, all_profiles: bool = False) -> dict[str, Any]:
    changed = 0
    for _profile, home in visible_profile_homes(all_profiles=all_profiles):
        _updated, count = _rewrite_mark_read(home, None, read_all=True)
        changed += count
    return {"ok": True, "changed": changed}


def sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
