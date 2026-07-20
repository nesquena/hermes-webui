#!/usr/bin/env python3
"""Plan/apply additive Hermex/WebUI cron delivery migration.

Adds the profile-local WebUI inbox target (`webui`) in front of existing push
cron delivery targets while preserving existing Telegram/origin fallback. Local
jobs stay local by default.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - apply is intentionally POSIX-only.
    fcntl = None  # type: ignore[assignment]

WEBUI_ALIASES = {"webui", "hermex"}
LOCAL_TOKENS = {"", "local"}
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
JOBS_LOCK_FILE = ".jobs.lock"
JOBS_LOCK_TIMEOUT_SECONDS = 5.0


@dataclass
class ProfileCronFile:
    profile: str
    home: Path
    path: Path


@dataclass
class DeliveryChange:
    profile: str
    job_id: str
    name: str
    old_deliver: Any
    new_deliver: Any
    reason: str


@dataclass
class ProfileReport:
    profile: str
    path: str
    status: str
    total_jobs: int = 0
    changed_jobs: int = 0
    unchanged_jobs: int = 0
    local_jobs: int = 0


class PlannedJobs(dict[str, Any]):
    """Transformed jobs plus the exact source digest the plan was based on."""

    def __init__(self, data: dict[str, Any], source_sha256: str):
        super().__init__(data)
        self.source_sha256 = source_sha256


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assert_no_symlink_components(path: Path, *, allow_missing: bool = False) -> None:
    expanded = path.expanduser().absolute()
    parts = expanded.parts
    if not parts or parts[0] != os.path.sep:
        raise ValueError(f"{expanded}: expected absolute path")
    cursor = Path(parts[0])
    for component in parts[1:]:
        cursor /= component
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            if allow_missing:
                return
            raise ValueError(f"{cursor}: path component missing") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{cursor}: symlinked path component rejected")


def _open_directory_path(path: Path) -> int:
    """Open every absolute directory component with openat + O_NOFOLLOW."""
    expanded = path.expanduser().absolute()
    parts = expanded.parts
    if not parts or parts[0] != os.path.sep:
        raise ValueError(f"{expanded}: expected absolute path")
    fd = os.open(os.path.sep, os.O_RDONLY | O_DIRECTORY)
    try:
        for component in parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{expanded}: expected directory")
        if metadata.st_uid != os.geteuid():
            raise PermissionError(f"{expanded}: owner mismatch")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _require_owned_regular(fd: int, label: str) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label}: expected regular file")
    if metadata.st_uid != os.geteuid():
        raise PermissionError(f"{label}: owner mismatch")
    if metadata.st_nlink != 1:
        raise ValueError(f"{label}: hard-linked file rejected")
    return metadata


def _open_profile_file(
    profile_file: ProfileCronFile,
) -> tuple[int, int, int] | None:
    """Return stable home/cron/jobs descriptors, or None for a missing file."""
    home_fd = _open_directory_path(profile_file.home)
    cron_fd = -1
    jobs_fd = -1
    try:
        try:
            cron_fd = os.open(
                "cron",
                os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW,
                dir_fd=home_fd,
            )
        except FileNotFoundError:
            os.close(home_fd)
            return None
        try:
            jobs_fd = os.open(
                profile_file.path.name,
                os.O_RDONLY | O_NOFOLLOW,
                dir_fd=cron_fd,
            )
        except FileNotFoundError:
            os.close(cron_fd)
            os.close(home_fd)
            return None
        _require_owned_regular(jobs_fd, str(profile_file.path))
        return home_fd, cron_fd, jobs_fd
    except BaseException:
        if jobs_fd >= 0:
            os.close(jobs_fd)
        if cron_fd >= 0:
            os.close(cron_fd)
        if home_fd >= 0:
            os.close(home_fd)
        raise


def _close_profile_file(handles: tuple[int, int, int]) -> None:
    for fd in reversed(handles):
        os.close(fd)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _assert_profile_file_current(
    profile_file: ProfileCronFile,
    handles: tuple[int, int, int],
) -> None:
    """Fail closed if any validated home/cron/jobs anchor was swapped."""
    home_fd, cron_fd, jobs_fd = handles
    reopened_home = _open_directory_path(profile_file.home)
    try:
        if not _same_inode(os.fstat(home_fd), os.fstat(reopened_home)):
            raise ValueError(f"{profile_file.home}: profile home changed during apply")
    finally:
        os.close(reopened_home)

    cron_path_stat = os.stat("cron", dir_fd=home_fd, follow_symlinks=False)
    if not stat.S_ISDIR(cron_path_stat.st_mode) or not _same_inode(
        cron_path_stat, os.fstat(cron_fd)
    ):
        raise ValueError(f"{profile_file.home / 'cron'}: cron directory changed during apply")

    jobs_path_stat = os.stat(
        profile_file.path.name,
        dir_fd=cron_fd,
        follow_symlinks=False,
    )
    jobs_fd_stat = _require_owned_regular(jobs_fd, str(profile_file.path))
    if not stat.S_ISREG(jobs_path_stat.st_mode) or not _same_inode(
        jobs_path_stat, jobs_fd_stat
    ):
        raise ValueError(f"{profile_file.path}: jobs file changed during apply")


def _acquire_jobs_lock(profile_file: ProfileCronFile, cron_fd: int) -> int:
    """Acquire Hermes Agent's canonical per-profile cron writer lock."""
    if fcntl is None:
        raise RuntimeError("cron migration apply requires POSIX fcntl locking")
    lock_fd = os.open(
        JOBS_LOCK_FILE,
        os.O_RDWR | os.O_CREAT | O_NOFOLLOW,
        0o600,
        dir_fd=cron_fd,
    )
    try:
        metadata = _require_owned_regular(
            lock_fd, str(profile_file.home / "cron" / JOBS_LOCK_FILE)
        )
        os.fchmod(lock_fd, 0o600)
        path_metadata = os.stat(
            JOBS_LOCK_FILE,
            dir_fd=cron_fd,
            follow_symlinks=False,
        )
        if not _same_inode(metadata, path_metadata):
            raise ValueError(
                f"{profile_file.home / 'cron' / JOBS_LOCK_FILE}: lock changed"
            )
        deadline = time.monotonic() + JOBS_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_fd
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{profile_file.home / 'cron' / JOBS_LOCK_FILE}: "
                        "timed out waiting for cron writer lock"
                    ) from None
                time.sleep(0.05)
    except BaseException:
        os.close(lock_fd)
        raise


def _assert_jobs_lock_current(
    profile_file: ProfileCronFile, cron_fd: int, lock_fd: int
) -> None:
    lock_fd_stat = _require_owned_regular(
        lock_fd, str(profile_file.home / "cron" / JOBS_LOCK_FILE)
    )
    lock_path_stat = os.stat(
        JOBS_LOCK_FILE,
        dir_fd=cron_fd,
        follow_symlinks=False,
    )
    if not stat.S_ISREG(lock_path_stat.st_mode) or not _same_inode(
        lock_fd_stat, lock_path_stat
    ):
        raise ValueError(
            f"{profile_file.home / 'cron' / JOBS_LOCK_FILE}: lock changed"
        )


def _release_jobs_lock(lock_fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)


def discover_profile_files(home: Path) -> list[ProfileCronFile]:
    requested_home = home.expanduser().absolute()
    assert_no_symlink_components(requested_home)
    home = requested_home.resolve(strict=True)
    if not home.is_dir():
        raise ValueError(f"{home}: expected Hermes home directory")
    rows = [ProfileCronFile("default", home, home / "cron" / "jobs.json")]
    profiles_dir = home / "profiles"
    if profiles_dir.is_symlink():
        raise ValueError(f"{profiles_dir}: symlinked profiles directory rejected")
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name):
            if child.is_symlink():
                raise ValueError(f"{child}: symlinked profile directory rejected")
            if child.is_dir() and child.name:
                rows.append(ProfileCronFile(child.name, child, child / "cron" / "jobs.json"))
    return rows


def assert_profile_file_safe(profile_file: ProfileCronFile) -> None:
    home = profile_file.home
    path = profile_file.path
    cron_dir = home / "cron"
    assert_no_symlink_components(home)
    assert_no_symlink_components(cron_dir, allow_missing=True)
    assert_no_symlink_components(path, allow_missing=True)
    if not path.exists():
        return
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{path}: expected regular jobs file")
    try:
        path.resolve(strict=True).relative_to(home.resolve(strict=True))
    except ValueError:
        raise ValueError(f"{path}: jobs file escapes profile home") from None


def _validate_jobs_data(
    data: Any, label: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(data, dict):
        raise ValueError(f"{label}: expected top-level object")
    jobs = data.get("jobs")
    if jobs is None:
        jobs = []
        data["jobs"] = jobs
    if not isinstance(jobs, list):
        raise ValueError(f"{label}: expected jobs list")
    for idx, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"{label}: jobs[{idx}] is not an object")
    return data, jobs


def load_jobs_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _validate_jobs_data(
        json.loads(path.read_text(encoding="utf-8")), str(path)
    )


def _read_fd_bytes(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _sha256_fd(fd: int) -> str:
    return hashlib.sha256(_read_fd_bytes(fd)).hexdigest()


def _load_jobs_fd(
    fd: int, label: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    raw = _read_fd_bytes(fd)
    data = json.loads(raw.decode("utf-8"))
    validated, jobs = _validate_jobs_data(data, label)
    return validated, jobs, hashlib.sha256(raw).hexdigest()


def split_deliver(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).split(",")
    tokens: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in tokens:
            tokens.append(text)
    return tokens


def deliver_has_webui(tokens: Iterable[str]) -> bool:
    return any(str(token).strip().lower() in WEBUI_ALIASES for token in tokens)


def deliver_is_local_only(tokens: list[str]) -> bool:
    if not tokens:
        return True
    return len(tokens) == 1 and tokens[0].strip().lower() in LOCAL_TOKENS


def add_webui_delivery(value: Any, *, include_local: bool = False) -> tuple[Any, str | None]:
    tokens = split_deliver(value)
    if deliver_has_webui(tokens):
        return value, None
    if deliver_is_local_only(tokens) and not include_local:
        return value, "local"
    if not tokens:
        tokens = ["local"]
    new_tokens = ["webui"] + tokens
    if isinstance(value, list):
        return new_tokens, "additive"
    return ",".join(new_tokens), "additive"


def job_label(job: dict[str, Any]) -> str:
    return str(job.get("name") or job.get("id") or "unnamed")


def plan_profile(profile_file: ProfileCronFile, *, include_local: bool = False) -> tuple[ProfileReport, list[DeliveryChange], dict[str, Any] | None]:
    path = profile_file.path
    assert_profile_file_safe(profile_file)
    handles = _open_profile_file(profile_file)
    if handles is None:
        return ProfileReport(profile_file.profile, str(path), "missing"), [], None
    try:
        _assert_profile_file_current(profile_file, handles)
        data, jobs, source_sha256 = _load_jobs_fd(handles[2], str(path))
    finally:
        _close_profile_file(handles)
    planned = PlannedJobs(copy.deepcopy(data), source_sha256)
    planned_jobs = planned.get("jobs") or []
    changes: list[DeliveryChange] = []
    local_jobs = 0
    unchanged = 0
    for job in planned_jobs:
        old = job.get("deliver", "local")
        new, reason = add_webui_delivery(old, include_local=include_local)
        if reason == "local":
            local_jobs += 1
            unchanged += 1
            continue
        if reason is None:
            unchanged += 1
            continue
        if new != old:
            job["deliver"] = new
            changes.append(
                DeliveryChange(
                    profile=profile_file.profile,
                    job_id=str(job.get("id") or ""),
                    name=job_label(job),
                    old_deliver=old,
                    new_deliver=new,
                    reason=reason,
                )
            )
        else:
            unchanged += 1
    report = ProfileReport(
        profile=profile_file.profile,
        path=str(path),
        status="ok",
        total_jobs=len(jobs),
        changed_jobs=len(changes),
        unchanged_jobs=unchanged,
        local_jobs=local_jobs,
    )
    return report, changes, planned if changes else data


def atomic_write_json_at(
    profile_file: ProfileCronFile,
    handles: tuple[int, int, int],
    data: dict[str, Any],
    expected_source_sha256: str,
) -> None:
    _home_fd, cron_fd, jobs_fd = handles
    metadata = _require_owned_regular(jobs_fd, str(profile_file.path))
    temp_name = (
        f".{profile_file.path.name}.{os.getpid()}.{secrets.token_hex(12)}.tmp"
    )
    temp_fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW,
        metadata.st_mode & 0o777,
        dir_fd=cron_fd,
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _assert_profile_file_current(profile_file, handles)
        if _sha256_fd(jobs_fd) != expected_source_sha256:
            raise ValueError(
                f"{profile_file.path}: jobs contents changed after planning"
            )
        os.replace(
            temp_name,
            profile_file.path.name,
            src_dir_fd=cron_fd,
            dst_dir_fd=cron_fd,
        )
        os.fsync(cron_fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=cron_fd)
        except FileNotFoundError:
            pass


def backup_file_from_fd(
    source_fd: int, backup_dir: Path, profile: str
) -> Path:
    target = backup_dir / profile / "cron" / "jobs.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_no_symlink_components(target.parent)
    os.lseek(source_fd, 0, os.SEEK_SET)
    source_mode = os.fstat(source_fd).st_mode & 0o777
    with os.fdopen(os.dup(source_fd), "rb") as source, target.open("xb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    target.chmod(source_mode)
    return target


def build_plan(home: Path, *, include_local: bool = False) -> tuple[list[ProfileReport], list[DeliveryChange], dict[str, dict[str, Any]]]:
    reports: list[ProfileReport] = []
    changes: list[DeliveryChange] = []
    planned_by_profile: dict[str, dict[str, Any]] = {}
    for profile_file in discover_profile_files(home):
        report, profile_changes, planned = plan_profile(profile_file, include_local=include_local)
        reports.append(report)
        changes.extend(profile_changes)
        if profile_changes and planned is not None:
            planned_by_profile[profile_file.profile] = planned
    return reports, changes, planned_by_profile


def print_report(reports: list[ProfileReport], changes: list[DeliveryChange], *, as_json: bool) -> None:
    payload = {
        "summary": {
            "profiles": len(reports),
            "changed_jobs": len(changes),
            "local_jobs_unchanged": sum(r.local_jobs for r in reports),
        },
        "profiles": [asdict(r) for r in reports],
        "changes": [asdict(c) for c in changes],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"Hermex/WebUI cron migration plan: {len(changes)} job(s) would gain webui")
    for report in reports:
        if report.status == "missing":
            print(f"- {report.profile}: no cron/jobs.json ({report.path})")
        else:
            print(
                f"- {report.profile}: {report.changed_jobs}/{report.total_jobs} change(s), "
                f"{report.local_jobs} local-only unchanged"
            )
    if changes:
        print("\nProposed delivery changes:")
        for change in changes:
            print(
                f"- {change.profile}:{change.job_id} — {change.name}: "
                f"{change.old_deliver!r} -> {change.new_deliver!r}"
            )


def apply_plan(home: Path, backup_dir: Path, reports: list[ProfileReport], changes: list[DeliveryChange], planned_by_profile: dict[str, dict[str, Any]]) -> dict[str, Any]:
    backup_dir = backup_dir.expanduser().absolute()
    backup_dir.mkdir(parents=True, exist_ok=True)
    assert_no_symlink_components(backup_dir)
    profile_files = {row.profile: row for row in discover_profile_files(home)}
    backups: list[dict[str, str]] = []
    opened: dict[str, tuple[int, int, int]] = {}
    lock_fds: dict[str, int] = {}
    try:
        for profile in planned_by_profile:
            profile_file = profile_files[profile]
            planned = planned_by_profile[profile]
            expected_source_sha256 = getattr(planned, "source_sha256", "")
            if not expected_source_sha256:
                raise ValueError(
                    f"{profile_file.path}: plan is missing its source digest"
                )
            assert_profile_file_safe(profile_file)
            handles = _open_profile_file(profile_file)
            if handles is None:
                raise ValueError(f"{profile_file.path}: jobs file disappeared before apply")
            opened[profile] = handles

        for profile in planned_by_profile:
            profile_file = profile_files[profile]
            lock_fds[profile] = _acquire_jobs_lock(
                profile_file, opened[profile][1]
            )

        for profile in planned_by_profile:
            profile_file = profile_files[profile]
            handles = opened[profile]
            _assert_jobs_lock_current(
                profile_file, handles[1], lock_fds[profile]
            )
            _assert_profile_file_current(profile_file, handles)
            if _sha256_fd(handles[2]) != planned_by_profile[profile].source_sha256:
                raise ValueError(
                    f"{profile_file.path}: jobs contents changed after planning"
                )

        for profile in planned_by_profile:
            profile_file = profile_files[profile]
            handles = opened[profile]
            _assert_jobs_lock_current(
                profile_file, handles[1], lock_fds[profile]
            )
            _assert_profile_file_current(profile_file, handles)
            expected_source_sha256 = planned_by_profile[profile].source_sha256
            if _sha256_fd(handles[2]) != expected_source_sha256:
                raise ValueError(
                    f"{profile_file.path}: jobs contents changed after planning"
                )
            backup_path = backup_file_from_fd(
                handles[2], backup_dir, profile
            )
            backups.append(
                {
                    "profile": profile,
                    "source": str(profile_file.path),
                    "backup": str(backup_path),
                }
            )

        manifest = {
            "created_at": utc_now(),
            "home": str(home.expanduser().absolute()),
            "changes": [asdict(c) for c in changes],
            "reports": [asdict(r) for r in reports],
            "backups": backups,
        }
        manifest_path = backup_dir / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
            manifest_file.write("\n")
            manifest_file.flush()
            os.fsync(manifest_file.fileno())

        for profile, planned in planned_by_profile.items():
            profile_file = profile_files[profile]
            handles = opened[profile]
            _assert_jobs_lock_current(
                profile_file, handles[1], lock_fds[profile]
            )
            _assert_profile_file_current(profile_file, handles)
            atomic_write_json_at(
                profile_file,
                handles,
                planned,
                planned.source_sha256,
            )
        return manifest
    finally:
        for lock_fd in reversed(list(lock_fds.values())):
            _release_jobs_lock(lock_fd)
        for handles in opened.values():
            _close_profile_file(handles)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan/apply additive webui delivery migration for Hermes cron jobs.")
    parser.add_argument("--home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")), help="Base Hermes home containing cron/jobs.json and profiles/. Default: $HERMES_HOME or ~/.hermes")
    parser.add_argument("--apply", action="store_true", help="Write proposed delivery changes. Dry-run is the default.")
    parser.add_argument("--backup-dir", help="Required with --apply. A copy of every affected jobs.json is written here before mutation.")
    parser.add_argument("--include-local", action="store_true", help="Also add webui to local-only jobs. Default leaves local jobs unchanged.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON plan/report.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    home = Path(args.home).expanduser().absolute()
    reports, changes, planned_by_profile = build_plan(home, include_local=args.include_local)
    if args.apply and not args.backup_dir:
        print("ERROR: --apply requires --backup-dir", file=sys.stderr)
        return 2
    print_report(reports, changes, as_json=args.json)
    if args.apply:
        manifest = apply_plan(home, Path(args.backup_dir), reports, changes, planned_by_profile)
        if args.json:
            # Keep stdout JSON parseable for dry-run plans; apply receipt goes to stderr.
            print(json.dumps({"applied": True, "manifest": str(Path(args.backup_dir).expanduser().resolve() / "manifest.json")}), file=sys.stderr)
        else:
            print(f"\nApplied {len(changes)} change(s). Backup manifest: {Path(args.backup_dir).expanduser().resolve() / 'manifest.json'}")
            for item in manifest["backups"]:
                print(f"- backup {item['profile']}: {item['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
