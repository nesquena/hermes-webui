"""Single-owner launcher for resumable Claude Code sessions."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from uuid import UUID

from api.claude_code_bridge import (
    ClaudeSessionDescriptor,
    build_resume_argv,
    probe_runtime_status,
    resolve_session,
)


LOCK_DIRECTORY_NAME = ".hermes-resume-locks"
READINESS_MAX_BYTES = 256


@dataclass
class ResumeLease:
    fd: int
    path: Path
    inode: int

    def close(self) -> None:
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            fcntl.lockf(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# A collision response must not open a lease gap before the one-shot runner
# process is reaped. The OS closes these descriptors at process exit.
_LEASES_RETAINED_UNTIL_EXIT: list[ResumeLease] = []


def _canonical_uuid(value: str) -> str | None:
    try:
        canonical = str(UUID(value))
    except (AttributeError, ValueError):
        return None
    return canonical if value.lower() == canonical else None


def _store_hash(store_id: str) -> str:
    return hashlib.sha256(store_id.encode("utf-8")).hexdigest()[:16]


def resume_lock_path(config_dir: Path, store_id: str, session_id: str) -> Path:
    canonical = _canonical_uuid(session_id)
    if canonical is None or not isinstance(store_id, str) or not store_id:
        raise ValueError("invalid resume lease key")
    return Path(config_dir) / LOCK_DIRECTORY_NAME / f"{_store_hash(store_id)}-{canonical}.lock"


def _private_lock_directory(config_dir: Path) -> tuple[Path, int] | None:
    lock_dir = Path(config_dir) / LOCK_DIRECTORY_NAME
    try:
        os.mkdir(lock_dir, mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(lock_dir, flags)
        info = os.fstat(directory_fd)
    except OSError:
        try:
            os.close(directory_fd)
        except (OSError, UnboundLocalError):
            pass
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        os.close(directory_fd)
        return None
    return lock_dir, directory_fd


def acquire_resume_lease(
    config_dir: Path,
    store_id: str,
    session_id: str,
) -> ResumeLease | None:
    """Acquire the stable non-blocking lease, retaining its fd across exec."""
    try:
        lock_path = resume_lock_path(config_dir, store_id, session_id)
    except ValueError:
        return None
    opened = _private_lock_directory(Path(config_dir))
    if opened is None:
        return None
    _lock_dir, directory_fd = opened
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path.name, flags, 0o600, dir_fd=directory_fd)
    except OSError:
        os.close(directory_fd)
        return None
    os.close(directory_fd)
    try:
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            os.close(lock_fd)
            return None
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(lock_fd)
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            return None
        after = os.fstat(lock_fd)
        if (
            after.st_dev != info.st_dev
            or after.st_ino != info.st_ino
            or not stat.S_ISREG(after.st_mode)
            or after.st_uid != os.getuid()
            or after.st_nlink != 1
        ):
            os.close(lock_fd)
            return None
        os.set_inheritable(lock_fd, True)
        return ResumeLease(lock_fd, lock_path, after.st_ino)
    except OSError:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        return None


def _descriptor_signature(descriptor: ClaudeSessionDescriptor) -> tuple:
    profile = descriptor.profile
    return (
        descriptor.public_id,
        descriptor.store.store_id,
        descriptor.store.config_dir,
        descriptor.store.claude_bin,
        descriptor.claude_session_id,
        descriptor.transcript_path,
        profile.model_id if profile else None,
        profile.argv if profile else None,
        descriptor.cwd,
        descriptor.can_remote_resume,
    )


def _launch_path_signature(descriptor: ClaudeSessionDescriptor) -> tuple | None:
    if descriptor.profile is None or descriptor.cwd is None:
        return None
    paths = (
        descriptor.store.config_dir,
        descriptor.store.claude_bin,
        descriptor.transcript_path,
        Path(descriptor.profile.argv[0]),
        descriptor.cwd,
    )
    identities = []
    try:
        for path in paths:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                return None
            identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
            if stat.S_ISREG(info.st_mode):
                identity += (
                    info.st_size,
                    info.st_nlink,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            identities.append(identity)
    except OSError:
        return None
    return tuple(identities)


def _write_readiness(fd: int, state: str) -> None:
    record = json.dumps({"state": state}, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(record) > READINESS_MAX_BYTES:
        record = b'{"state":"ownership_unknown"}\n'
    try:
        view = memoryview(record)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                break
            view = view[written:]
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def run_session(
    public_id: str,
    readiness_fd: int,
    *,
    exec_fn: Callable[[str, Sequence[str]], object] = os.execv,
) -> int:
    """Acquire, revalidate, probe, report readiness, then replace this process."""
    descriptor = resolve_session(public_id)
    if descriptor is None or not descriptor.can_remote_resume:
        _write_readiness(readiness_fd, "invalid_session")
        return 1
    path_signature = _launch_path_signature(descriptor)
    if path_signature is None:
        _write_readiness(readiness_fd, "invalid_session")
        return 1
    lease = acquire_resume_lease(
        descriptor.store.config_dir,
        descriptor.store.store_id,
        descriptor.claude_session_id,
    )
    if lease is None:
        _write_readiness(readiness_fd, "active_elsewhere")
        return 1
    try:
        current = resolve_session(public_id)
        if (
            current is None
            or _descriptor_signature(current) != _descriptor_signature(descriptor)
            or _launch_path_signature(current) != path_signature
        ):
            _write_readiness(readiness_fd, "invalid_session")
            return 1
        runtime = probe_runtime_status(current, fresh=True)
        if runtime.state == "active_elsewhere":
            _write_readiness(readiness_fd, "ownership_conflict")
            _LEASES_RETAINED_UNTIL_EXIT.append(lease)
            lease = None
            return 1
        if runtime.state != "inactive":
            _write_readiness(readiness_fd, "ownership_unknown")
            return 1
        argv = build_resume_argv(current)
        _write_readiness(readiness_fd, "ready")
        exec_fn(argv[0], argv)
        return 0
    except (OSError, ValueError):
        return 1
    finally:
        if lease is not None:
            lease.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("public_id")
    parser.add_argument("readiness_fd", type=int)
    args = parser.parse_args(argv)
    if args.readiness_fd < 0:
        return 2
    return run_session(args.public_id, args.readiness_fd)


if __name__ == "__main__":
    raise SystemExit(main())
