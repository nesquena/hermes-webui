"""WebUI/Hermex cron notification API helpers.

Reads the profile-local cron notification JSONL files written by Hermes Agent's
``cron.webui_notifications`` module.  This file deliberately contains only
filesystem-level helpers and leaves HTTP concerns in ``api.routes``.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
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
_NOTIFICATION_FILE = "notifications.jsonl"
_LOCK_FILE = "notifications.jsonl.lock"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


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


def _notification_key(profile: str, notification_id: Any) -> str:
    return json.dumps(
        [str(profile), str(notification_id or "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _authoritative_record(row: dict[str, Any], profile: str) -> dict[str, Any]:
    decorated = dict(row)
    decorated["profile"] = profile
    decorated["notification_key"] = _notification_key(profile, decorated.get("id"))
    return decorated


def _require_owned_regular(fd: int, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"{label}: expected regular file")
    if metadata.st_uid != os.geteuid():
        raise PermissionError(f"{label}: owner mismatch")
    if metadata.st_nlink != 1:
        raise OSError(f"{label}: hard-linked file rejected")


def _open_directory_path(path: Path) -> int:
    """Open every absolute path component without following symlinks."""
    expanded = path.expanduser().absolute()
    parts = expanded.parts
    if not parts or parts[0] != os.path.sep:
        raise OSError(f"{expanded}: expected absolute directory path")
    fd = os.open(os.path.sep, os.O_RDONLY | _O_DIRECTORY)
    try:
        for component in parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"{expanded}: expected profile directory")
        if metadata.st_uid != os.geteuid():
            raise PermissionError(f"{expanded}: owner mismatch")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_cron_dir(home: Path, *, create: bool) -> int | None:
    expanded = home.expanduser().absolute()
    home_fd = _open_directory_path(expanded)
    try:
        try:
            cron_fd = os.open(
                "cron",
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=home_fd,
            )
        except FileNotFoundError:
            if not create:
                return None
            os.mkdir("cron", 0o700, dir_fd=home_fd)
            cron_fd = os.open(
                "cron",
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=home_fd,
            )
        metadata = os.fstat(cron_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(cron_fd)
            raise OSError(f"{expanded / 'cron'}: expected directory")
        if metadata.st_uid != os.geteuid():
            os.close(cron_fd)
            raise PermissionError(f"{expanded / 'cron'}: owner mismatch")
        return cron_fd
    finally:
        os.close(home_fd)


def _open_owned_regular_at(
    directory_fd: int,
    name: str,
    flags: int,
    *,
    mode: int = 0o600,
) -> int:
    fd = os.open(name, flags | _O_NOFOLLOW, mode, dir_fd=directory_fd)
    try:
        _require_owned_regular(fd, name)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_records(home: Path, profile: str) -> list[dict[str, Any]]:
    cron_fd = _open_cron_dir(home, create=False)
    if cron_fd is None:
        return []
    records: list[dict[str, Any]] = []
    lock_fd = -1
    data_fd = -1
    try:
        try:
            existing = os.stat(
                _NOTIFICATION_FILE,
                dir_fd=cron_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(existing.st_mode):
            raise OSError(f"{notification_path(home)}: expected regular file")
        lock_fd = _open_owned_regular_at(
            cron_fd,
            _LOCK_FILE,
            os.O_RDWR | os.O_CREAT,
        )
        os.fchmod(lock_fd, 0o600)
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        data_fd = _open_owned_regular_at(cron_fd, _NOTIFICATION_FILE, os.O_RDONLY)
        try:
            raw_records: deque[str] = deque(maxlen=_MAX_SCAN_RECORDS)
            with os.fdopen(os.dup(data_fd), "r", encoding="utf-8") as fh:
                raw_records.extend(fh)
            for raw in raw_records:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                records.append(_authoritative_record(row, profile))
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        if data_fd >= 0:
            os.close(data_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(cron_fd)
    return records


def _rewrite_mark_read(
    home: Path,
    notification_id: str | None,
    *,
    profile: str,
    read_all: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    cron_fd = _open_cron_dir(home, create=True)
    if cron_fd is None:  # pragma: no cover - create=True guarantees a descriptor.
        raise OSError(f"{home}: cron directory unavailable")
    now = utc_now_iso()
    updated: list[dict[str, Any]] = []
    changed = 0
    lock_fd = -1
    data_fd = -1
    temp_fd = -1
    temp_name = ""
    try:
        lock_fd = _open_owned_regular_at(
            cron_fd,
            _LOCK_FILE,
            os.O_RDWR | os.O_CREAT,
        )
        os.fchmod(lock_fd, 0o600)
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data_fd = _open_owned_regular_at(
            cron_fd,
            _NOTIFICATION_FILE,
            os.O_RDWR | os.O_CREAT,
        )
        os.fchmod(data_fd, 0o600)
        try:
            rows: list[tuple[str | None, dict[str, Any] | None]] = []
            with os.fdopen(os.dup(data_fd), "r", encoding="utf-8") as fh:
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
                        row["profile"] = profile
                        updated.append(_authoritative_record(row, profile))
                    rows.append((None, row))

            temp_name = f".notifications.{os.getpid()}.{secrets.token_hex(12)}.tmp"
            temp_fd = _open_owned_regular_at(
                cron_fd,
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            os.fchmod(temp_fd, 0o600)
            try:
                with os.fdopen(os.dup(temp_fd), "w", encoding="utf-8") as tmp:
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
                os.replace(
                    temp_name,
                    _NOTIFICATION_FILE,
                    src_dir_fd=cron_fd,
                    dst_dir_fd=cron_fd,
                )
                temp_name = ""
            finally:
                if temp_fd >= 0:
                    os.close(temp_fd)
                    temp_fd = -1
                if temp_name:
                    try:
                        os.unlink(temp_name, dir_fd=cron_fd)
                    except FileNotFoundError:
                        pass
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if data_fd >= 0:
            os.close(data_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(cron_fd)
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
    active_home = Path(get_active_hermes_home()).expanduser().absolute()
    if active_home.is_symlink() or not active_home.is_dir():
        raise OSError(f"{active_home}: unsafe active profile home")
    homes: list[tuple[str, Path]] = [(active_name, active_home)]
    seen = {str(active_home)}
    if not all_profiles or _is_isolated_profile_mode():
        return homes

    names = {"default"}
    for row in list_profiles_api():
        if isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            if name:
                names.add(name)
    base_home = _active_base_home(active_home).absolute()
    profiles_dir = base_home / "profiles"
    if profiles_dir.is_symlink():
        raise OSError(f"{profiles_dir}: symlinked profiles directory rejected")
    if profiles_dir.is_dir():
        for child in profiles_dir.iterdir():
            if child.is_symlink():
                continue
            if child.is_dir() and child.name:
                names.add(child.name)

    for name in sorted(names):
        try:
            home = Path(get_hermes_home_for_profile(name)).expanduser().absolute()
        except Exception:
            continue
        expected = base_home if name == "default" else profiles_dir / name
        if home != expected or home.is_symlink() or not home.is_dir():
            continue
        key = str(home)
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
        profile_records = _read_records(home, profile)
        # Newer appended rows win ties from legacy second-resolution timestamps.
        for row in reversed(profile_records):
            if not row.get("read_at"):
                unread_by_profile[profile] = unread_by_profile.get(profile, 0) + 1
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
        selected_profile = requested
        home = match
    else:
        if not visible:
            return None
        selected_profile, home = visible[0]
    updated, _changed = _rewrite_mark_read(
        home,
        notification_id,
        profile=selected_profile,
    )
    for row in updated:
        if str(row.get("id") or "") == str(notification_id):
            return row
    return None


def mark_all_read(*, all_profiles: bool = False) -> dict[str, Any]:
    changed = 0
    for profile, home in visible_profile_homes(all_profiles=all_profiles):
        _updated, count = _rewrite_mark_read(
            home,
            None,
            profile=profile,
            read_all=True,
        )
        changed += count
    return {"ok": True, "changed": changed}


def sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
