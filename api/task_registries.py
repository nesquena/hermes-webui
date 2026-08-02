"""Task registry backend for Hermes WebUI.

The WebUI reads the same JSON registries used by Hermes chat task workflows.
Mutations use optimistic revisions, append-only task history, a verified backup,
and an atomic same-directory replace.
"""
from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALLOWED_STATUSES = {"pending", "in_progress", "completed", "cancelled", "blocked"}
ALLOWED_PRIORITIES = {"low", "normal", "high", "urgent"}
CREATE_FIELDS = {"text", "status", "priority", "due_date", "due_at", "notes"}
UPDATE_FIELDS = CREATE_FIELDS
MAX_TEXT_LENGTH = 2000
MAX_NOTES_LENGTH = 8000

_AT_FDCWD = -100
_RENAME_EXCHANGE = 2
_MAX_ROLLBACK_EXCHANGES = 8


def _rename_exchange(first: Path, second: Path) -> bool:
    """Atomically exchange two paths, or report that renameat2 is unavailable."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        return False
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(
        _AT_FDCWD, os.fsencode(first), _AT_FDCWD, os.fsencode(second), _RENAME_EXCHANGE
    ) == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        return False
    raise OSError(error, os.strerror(error), str(first), str(second))


class RegistryError(Exception):
    """Base domain error."""


class RegistryNotFound(RegistryError):
    pass


class TaskNotFound(RegistryError):
    pass


class RegistryConflict(RegistryError):
    pass


class RegistryValidationError(RegistryError):
    pass


def _registry_root() -> Path:
    process_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    try:
        from api.profiles import get_active_hermes_home

        hermes_home = Path(get_active_hermes_home()).expanduser()
    except Exception as exc:
        raise RegistryError("active profile task registry root cannot be resolved") from exc

    try:
        is_default_profile = hermes_home.resolve(strict=False) == process_home.resolve(strict=False)
    except OSError:
        is_default_profile = hermes_home == process_home

    explicit = os.environ.get("HERMES_TASK_REGISTRY_DIR")
    if explicit and is_default_profile:
        return Path(explicit).expanduser()

    canonical = hermes_home / "private"
    if canonical.exists() or not is_default_profile:
        return canonical

    legacy = Path("/opt/data/private")
    if legacy.exists():
        return legacy
    return canonical


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now(timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return datetime.now(tz).isoformat(timespec="seconds")


def _uuid4() -> str:
    return str(uuid.uuid4())


def _registry_id(path: Path) -> str:
    name = path.name.removesuffix(".json")
    return name.removesuffix("-tasks")


def _registry_label(registry_id: str, payload: dict[str, Any]) -> str:
    project = payload.get("project")
    if isinstance(project, str) and project.strip():
        return project.strip()
    if registry_id == "ivan-daily":
        return "Личные"
    return registry_id.replace("-", " ").title()


def _clean_text(value: Any, field: str, max_length: int, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise RegistryValidationError(f"{field} must be a string")
    cleaned = " ".join(value.split()) if field == "text" else value.strip()
    if required and not cleaned:
        raise RegistryValidationError(f"{field} is required")
    if len(cleaned) > max_length:
        raise RegistryValidationError(f"{field} is too long")
    return cleaned or None


def _clean_due_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise RegistryValidationError("due_date must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise RegistryValidationError("due_date must be an ISO date") from exc


def _clean_due_at(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise RegistryValidationError("due_at must be an ISO datetime with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryValidationError("due_at must be an ISO datetime with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegistryValidationError("due_at must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _validate_fields(body: Any, *, create: bool) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise RegistryValidationError("request body must be an object")
    allowed = CREATE_FIELDS if create else UPDATE_FIELDS
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise RegistryValidationError(f"unsupported fields: {', '.join(unknown)}")
    if create and "text" not in body:
        raise RegistryValidationError("text is required")
    if not create and not body:
        raise RegistryValidationError("at least one change is required")

    cleaned: dict[str, Any] = {}
    if "text" in body:
        cleaned["text"] = _clean_text(body["text"], "text", MAX_TEXT_LENGTH, required=True)
    if "notes" in body:
        cleaned["notes"] = _clean_text(body["notes"], "notes", MAX_NOTES_LENGTH, required=False)
    if "status" in body:
        status = body["status"]
        if status not in ALLOWED_STATUSES:
            raise RegistryValidationError("unsupported status")
        cleaned["status"] = status
    if "priority" in body:
        priority = body["priority"]
        if priority not in ALLOWED_PRIORITIES:
            raise RegistryValidationError("unsupported priority")
        cleaned["priority"] = priority
    if "due_date" in body:
        cleaned["due_date"] = _clean_due_date(body["due_date"])
    if "due_at" in body:
        cleaned["due_at"] = _clean_due_at(body["due_at"])
    return cleaned


def _validate_registry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise RegistryValidationError("invalid task registry")
    timezone_name = payload.get("timezone", "Europe/Moscow")
    if not isinstance(timezone_name, str):
        raise RegistryValidationError("task registry timezone is invalid")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RegistryValidationError("task registry timezone is invalid") from exc
    seen: set[str] = set()
    for item in payload["tasks"]:
        if not isinstance(item, dict):
            raise RegistryValidationError("registry contains an invalid task")
        task_id = item.get("id")
        try:
            parsed = uuid.UUID(str(task_id), version=4)
        except (ValueError, TypeError, AttributeError) as exc:
            raise RegistryValidationError("registry contains an invalid task UUID") from exc
        if str(parsed) != str(task_id).lower() or task_id in seen:
            raise RegistryValidationError("registry contains a duplicate or non-canonical task UUID")
        seen.add(task_id)
        if not isinstance(item.get("history"), list):
            raise RegistryValidationError("registry task history must be a list")
    return payload


def _public_task(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    if "notes" not in result and "note" in result:
        result["notes"] = result.get("note")
    if not result.get("due_date") and result.get("deadline"):
        result["display_deadline"] = result.get("deadline")
    return result


class TaskRegistryStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or _registry_root()).expanduser()

    def _discover(self) -> dict[str, Path]:
        if not self.root.is_dir():
            return {}
        self._validate_root()
        found: dict[str, Path] = {}
        for path in sorted(self.root.glob("*-tasks.json")):
            try:
                self._read_path(path)
            except RegistryError:
                continue
            found[_registry_id(path)] = path
        return found

    def _validate_root(self) -> None:
        try:
            info = self.root.lstat()
        except OSError as exc:
            raise RegistryError("task registry root cannot be inspected safely") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RegistryError("task registry root has unsafe ownership or permissions")

    def _path(self, registry_id: str) -> Path:
        if not isinstance(registry_id, str) or not registry_id:
            raise RegistryNotFound("registry not found")
        path = self._discover().get(registry_id)
        if path is None:
            raise RegistryNotFound("registry not found")
        return path

    def _read_path(self, path: Path) -> tuple[dict[str, Any], bytes, str]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise RegistryValidationError(
                        "task registry file has unsafe ownership, links, or permissions"
                    )
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    raw = handle.read()
            finally:
                if fd >= 0:
                    os.close(fd)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryValidationError("task registry cannot be read") from exc
        return _validate_registry(payload), raw, _revision(raw)

    def _read_revision(self, path: Path) -> str:
        _payload, _raw, revision = self._read_path(path)
        return revision

    def list_registries(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for registry_id, path in self._discover().items():
            try:
                payload, _raw, revision = self._read_path(path)
            except RegistryError:
                continue
            statuses: dict[str, int] = {}
            for item in payload["tasks"]:
                status = str(item.get("status", "pending"))
                statuses[status] = statuses.get(status, 0) + 1
            result.append(
                {
                    "id": registry_id,
                    "label": _registry_label(registry_id, payload),
                    "task_count": len(payload["tasks"]),
                    "statuses": statuses,
                    "updated_at": payload.get("updated_at"),
                    "revision": revision,
                }
            )
        result.sort(key=lambda x: (x["id"] != "ivan-daily", x["label"].casefold()))
        return result

    def get_registry(self, registry_id: str) -> dict[str, Any]:
        path = self._path(registry_id)
        payload, _raw, revision = self._read_path(path)
        return {
            "id": registry_id,
            "label": _registry_label(registry_id, payload),
            "timezone": payload.get("timezone", "Europe/Moscow"),
            "updated_at": payload.get("updated_at"),
            "revision": revision,
            "tasks": [_public_task(item) for item in payload["tasks"]],
        }

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        self._validate_root()
        lock_path = self.root / ".task-registry.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise RegistryError("task registry lock cannot be opened safely") from exc
        with os.fdopen(fd, "a+b") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RegistryError("task registry lock must be an unlinked regular file")
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    path_info = lock_path.lstat()
                except OSError as exc:
                    raise RegistryError("task registry lock changed while locking") from exc
                locked_info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(path_info.st_mode)
                    or stat.S_ISLNK(path_info.st_mode)
                    or (path_info.st_dev, path_info.st_ino)
                    != (locked_info.st_dev, locked_info.st_ino)
                ):
                    raise RegistryError("task registry lock changed while locking")
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _backup(self, path: Path, raw: bytes, revision: str) -> Path:
        backup_dir = self.root / ".task-registry-backups"
        try:
            backup_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(backup_dir, dir_flags)
        except OSError as exc:
            raise RegistryError("task registry backup directory cannot be opened safely") from exc
        directory_info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or directory_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            os.close(directory_fd)
            raise RegistryError("task registry backup directory has unsafe ownership or permissions")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup_name = f"{path.stem}.{stamp}.{revision[:12]}.json"
        try:
            os.fchmod(directory_fd, 0o700)
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(backup_name, flags, 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "w+b") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
                handle.seek(0)
                if handle.read() != raw:
                    raise RegistryError("backup verification failed")
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return backup_dir / backup_name

    def _atomic_write(
        self, path: Path, payload: dict[str, Any], expected_revision: str
    ) -> bytes:
        _validate_registry(payload)
        raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        preserve_temp = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                if _rename_exchange(temp_path, path):
                    os.fsync(directory_fd)
                    try:
                        displaced_matches = self._read_revision(temp_path) == expected_revision
                    except RegistryError:
                        displaced_matches = False
                    if not displaced_matches:
                        expected_displaced = raw
                        for _attempt in range(_MAX_ROLLBACK_EXCHANGES):
                            candidate = temp_path.read_bytes()
                            if not _rename_exchange(temp_path, path):
                                raise RegistryError("atomic registry rollback is unavailable")
                            os.fsync(directory_fd)
                            displaced = temp_path.read_bytes()
                            if displaced == expected_displaced:
                                break
                            # A newer external replacement was displaced. Put it back on
                            # the next exchange and verify the value currently canonical.
                            expected_displaced = candidate
                        else:
                            recovery_path = path.parent / (
                                f".{path.name}.conflict-recovery.{uuid.uuid4().hex}.json"
                            )
                            preserve_temp = True
                            os.rename(temp_path, recovery_path)
                            temp_path = recovery_path
                            os.fsync(directory_fd)
                            raise RegistryError(
                                "registry rollback could not stabilize; newest displaced "
                                f"version preserved as {recovery_path.name}"
                            )
                        raise RegistryConflict("registry changed; reload and try again")
                    temp_path.unlink()
                else:
                    raise RegistryError(
                        "atomic task registry compare-and-swap is unavailable on this filesystem"
                    )
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path.exists() and not preserve_temp:
                temp_path.unlink()
        if self._read_revision(path) != _revision(raw):
            raise RegistryError("registry write verification failed")
        return raw

    def _mutate(
        self,
        registry_id: str,
        expected_revision: str,
        operation,
    ) -> dict[str, Any]:
        if not isinstance(expected_revision, str) or len(expected_revision) != 64:
            raise RegistryValidationError("expected_revision is required")
        path = self._path(registry_id)
        with self._locked():
            payload, raw, current_revision = self._read_path(path)
            if current_revision != expected_revision:
                raise RegistryConflict("registry changed; reload and try again")
            task, changed = operation(payload)
            if not changed:
                return {"changed": False, "revision": current_revision, "task": _public_task(task)}
            self._backup(path, raw, current_revision)
            written = self._atomic_write(path, payload, current_revision)
            return {"changed": True, "revision": _revision(written), "task": _public_task(task)}

    def create_task(
        self,
        registry_id: str,
        body: dict[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        cleaned = _validate_fields(body, create=True)

        def operation(payload: dict[str, Any]):
            timezone_name = str(payload.get("timezone") or "Europe/Moscow")
            now = _now(timezone_name)
            status = cleaned.get("status", "pending")
            task: dict[str, Any] = {
                "id": _uuid4(),
                "text": cleaned["text"],
                "status": status,
                "priority": cleaned.get("priority", "normal"),
                "due_date": cleaned.get("due_date"),
                "due_at": cleaned.get("due_at"),
                "notes": cleaned.get("notes"),
                "created_at": now,
                "updated_at": now,
                "completed_at": now if status == "completed" else None,
                "cancelled_at": now if status == "cancelled" else None,
                "history": [
                    {
                        "id": _uuid4(),
                        "changed_at": now,
                        "action": "created",
                        "changes": {
                            key: {"old": None, "new": value}
                            for key, value in cleaned.items()
                        },
                        "note": "Created in Hermes WebUI",
                    }
                ],
            }
            payload["tasks"].append(task)
            payload["updated_at"] = now
            return task, True

        return self._mutate(registry_id, expected_revision, operation)

    def update_task(
        self,
        registry_id: str,
        task_id: str,
        body: dict[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        try:
            canonical_id = str(uuid.UUID(task_id, version=4))
        except (ValueError, TypeError, AttributeError) as exc:
            raise TaskNotFound("task not found") from exc
        cleaned = _validate_fields(body, create=False)

        def operation(payload: dict[str, Any]):
            task = next((item for item in payload["tasks"] if item.get("id") == canonical_id), None)
            if task is None:
                raise TaskNotFound("task not found")
            timezone_name = str(payload.get("timezone") or "Europe/Moscow")
            now = _now(timezone_name)
            changes: dict[str, dict[str, Any]] = {}
            old_status = task.get("status", "pending")

            for field, new_value in cleaned.items():
                target = field
                if field == "notes" and "notes" not in task and "note" in task:
                    target = "note"
                old_value = task.get(target)
                if old_value != new_value:
                    changes[field] = {"old": old_value, "new": new_value}
                    task[target] = new_value

            if not changes:
                return task, False

            new_status = task.get("status", old_status)
            if new_status == "completed" and old_status != "completed":
                task["completed_at"] = now
                task["completed_at_source"] = "hermes_webui"
                task["cancelled_at"] = None
                if "cancelled_at_source" in task:
                    task["cancelled_at_source"] = None
                action = "completed"
            elif new_status == "cancelled" and old_status != "cancelled":
                task["cancelled_at"] = now
                task["cancelled_at_source"] = "hermes_webui"
                task["completed_at"] = None
                if "completed_at_source" in task:
                    task["completed_at_source"] = None
                action = "cancelled"
            elif old_status in {"completed", "cancelled"} and new_status not in {"completed", "cancelled"}:
                task["completed_at"] = None
                task["cancelled_at"] = None
                if "completed_at_source" in task:
                    task["completed_at_source"] = None
                if "cancelled_at_source" in task:
                    task["cancelled_at_source"] = None
                action = "reopened"
            else:
                action = "updated"

            task["updated_at"] = now
            task.setdefault("history", []).append(
                {
                    "id": _uuid4(),
                    "changed_at": now,
                    "action": action,
                    "changes": changes,
                    "note": "Updated in Hermes WebUI",
                }
            )
            payload["updated_at"] = now
            return task, True

        return self._mutate(registry_id, expected_revision, operation)
